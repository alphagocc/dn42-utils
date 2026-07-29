"""End-to-end test of the sync_events watcher.

`with TestClient(app)` runs lifespan in a background portal, so the REAL watcher
polls the REAL sqlite file while the test thread mutates it from the outside —
which is exactly the cross-process shape production has.

Negative assertions ("this change must NOT be pushed to that node") cannot be
written as a timeout: `WebSocketTestSession.receive` blocks forever. Instead they
are written as ORDERING assertions — mutate node B first, then node A, then assert
the first frame node A's connection sees is A's own push. The watcher processes
events in `sync_events.id` order, so a misrouted push would necessarily arrive
first. No sleeps, no flakes.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import dn42ctl.ws_hub as ws_hub
from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import BgpPeerRecord, Database
from dn42ctl.db_managed import ManagedNodeStore, RevisionStore
from dn42ctl.ws_protocol import (
    CLOSE_REVOKED,
    MSG_DESIRED_PUSH,
    MSG_HELLO,
    MSG_HELLO_ACK,
    Envelope,
    encode,
)

ADMIN_TOKEN = "admin-secret"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="

POLL = 0.02


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


def _seed_node(db_path: Path, node_id: str, name: str) -> str:
    token = secrets.token_urlsafe(16)
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        store.add(node_id, name)
        store.rotate_token(node_id, token)
    finally:
        db.close()
    return token


@pytest.fixture
def tokens(db_path: Path) -> dict[str, str]:
    """Nodes must exist before startup anchors the watcher cursor — see module docstring."""
    return {
        NODE_A: _seed_node(db_path, NODE_A, "a"),
        NODE_B: _seed_node(db_path, NODE_B, "b"),
    }


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path, tokens: dict[str, str]) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN, sync_poll_interval=POLL)
    with TestClient(app) as c:
        yield c


def _connect(client: TestClient, node_id: str, token: str):
    return client.websocket_connect(f"/api/v1/nodes/{node_id}/ws", headers={"Authorization": f"Bearer {token}"})


def _handshake(ws) -> None:
    """hello -> hello_ack -> drain the initial desired_push."""
    ws.send_text(encode(Envelope(type=MSG_HELLO, payload={"agent_version": "test", "cached_revision": None})))
    assert ws.receive_json()["type"] == MSG_HELLO_ACK
    assert ws.receive_json()["type"] == MSG_DESIRED_PUSH


def _add_peer(db_path: Path, node_id: str, peer_asn: int) -> None:
    """Mutate from the test thread — the same cross-process path the CLI takes."""
    db = Database.open(db_path)
    try:
        db.ensure_node(node_id)
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=node_id,
                peer_asn=peer_asn,
                ifname=f"dn42_{peer_asn}",
                wg_private_key=FAKE_PRIV,
                wg_public_key=FAKE_PUB,
                peer_public_key=FAKE_PUB,
                endpoint="peer.example:51820",
                local_lla="fe80::1",
                peer_lla="fe80::2",
                listen_port=21000 + (peer_asn % 1000),
                allowed_ips=["fe80::/64"],
                net_backend="networkd",
            )
        )
    finally:
        db.close()


class _StopWatcher(Exception):
    """Sentinel to break the watcher's infinite loop deterministically."""


class _FakeRegistry:
    def __init__(self) -> None:
        self.notified: list[str] = []
        self.closed: list[tuple[str, int]] = []

    async def notify(self, node_id: str) -> None:
        self.notified.append(node_id)

    async def close_node(self, node_id: str, code: int, *, err_code: str, message: str) -> None:
        self.closed.append((node_id, code))


def _run_watcher(db_path: Path, registry: _FakeRegistry, *, ticks: int) -> None:
    """Drive `run_sync_watcher` for exactly `ticks` iterations, with no real sleeps."""
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] > ticks:
            raise _StopWatcher
        await anyio.sleep(0)

    async def main() -> None:
        with contextlib.suppress(_StopWatcher):
            await ws_hub.run_sync_watcher(registry, lambda: db_path, lambda: 0.0, sleep=fake_sleep)

    anyio.run(main)


class TestWatcherUnit:
    """The watcher on its own — no WebSockets, no TestClient, no real sleeps."""

    def test_first_tick_only_sets_cursor(self, db_path: Path) -> None:
        _add_peer(db_path, NODE_A, 4242420001)
        reg = _FakeRegistry()
        _run_watcher(db_path, reg, ticks=1)
        assert reg.notified == [], "pre-existing events must not replay: initial sync covers them"

    def test_notifies_on_new_event(self, db_path: Path) -> None:
        _add_peer(db_path, NODE_A, 4242420001)
        reg = _FakeRegistry()

        # Tick 1 sets the cursor; the peer added afterwards must be seen on tick 2.
        calls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                _add_peer(db_path, NODE_A, 4242420002)
            if calls["n"] > 3:
                raise _StopWatcher
            await anyio.sleep(0)

        async def main() -> None:
            with contextlib.suppress(_StopWatcher):
                await ws_hub.run_sync_watcher(reg, lambda: db_path, lambda: 0.0, sleep=fake_sleep)

        anyio.run(main)
        assert NODE_A in reg.notified

    def test_revoked_closes_and_is_not_also_notified(self, db_path: Path) -> None:
        reg = _FakeRegistry()
        calls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                _add_peer(db_path, NODE_A, 4242420001)
                db = Database.open(db_path)
                try:
                    ManagedNodeStore(db.connection).add(NODE_A, "a")
                    ManagedNodeStore(db.connection).delete(NODE_A)
                finally:
                    db.close()
            if calls["n"] > 3:
                raise _StopWatcher
            await anyio.sleep(0)

        async def main() -> None:
            with contextlib.suppress(_StopWatcher):
                await ws_hub.run_sync_watcher(reg, lambda: db_path, lambda: 0.0, sleep=fake_sleep)

        anyio.run(main)
        assert (NODE_A, CLOSE_REVOKED) in reg.closed
        assert NODE_A not in reg.notified, "a revoked node must be disconnected, not pushed to"

    def test_unconfigured_db_path_idles(self, db_path: Path) -> None:
        reg = _FakeRegistry()
        calls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 3:
                raise _StopWatcher
            await anyio.sleep(0)

        async def main() -> None:
            with contextlib.suppress(_StopWatcher):
                await ws_hub.run_sync_watcher(reg, lambda: None, lambda: 0.0, sleep=fake_sleep)

        anyio.run(main)
        assert reg.notified == []

    def test_survives_a_transient_db_error(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """One bad tick must not kill sync for the whole fleet."""
        reg = _FakeRegistry()
        calls = {"n": 0}
        real_fetch = ws_hub._fetch_events

        def flaky(**kwargs):
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return real_fetch(**kwargs)

        monkeypatch.setattr(ws_hub, "_fetch_events", flaky)

        async def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] == 4:
                _add_peer(db_path, NODE_A, 4242420001)
            if calls["n"] > 5:
                raise _StopWatcher
            await anyio.sleep(0)

        async def main() -> None:
            with contextlib.suppress(_StopWatcher):
                await ws_hub.run_sync_watcher(reg, lambda: db_path, lambda: 0.0, sleep=fake_sleep)

        anyio.run(main)
        assert NODE_A in reg.notified


class TestPushOnChange:
    def test_peer_added_is_pushed(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        with _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            _add_peer(db_path, NODE_A, 4242420001)
            push = ws.receive_json()
            assert push["type"] == MSG_DESIRED_PUSH
            asns = [p["peer_asn"] for p in push["payload"]["desired"]["bgp_peers"]]
            assert asns == [4242420001]

    def test_successive_changes_each_push(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        with _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            _add_peer(db_path, NODE_A, 4242420001)
            assert ws.receive_json()["type"] == MSG_DESIRED_PUSH
            _add_peer(db_path, NODE_A, 4242420002)
            push = ws.receive_json()
            asns = sorted(p["peer_asn"] for p in push["payload"]["desired"]["bgp_peers"])
            assert asns == [4242420001, 4242420002]

    def test_delete_is_pushed(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        _add_peer(db_path, NODE_A, 4242420001)
        with _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            db = Database.open(db_path)
            try:
                db.delete_bgp_peer(NODE_A, 4242420001)
            finally:
                db.close()
            push = ws.receive_json()
            assert push["payload"]["desired"]["bgp_peers"] == []

    def test_rollback_pin_is_pushed(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        """Rollback changes node_desired_pin, not the peer tables — it still pushes."""
        _add_peer(db_path, NODE_A, 4242420001)
        with _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            db = Database.open(db_path)
            try:
                store = RevisionStore(db.connection)
                first = store.latest_revision(NODE_A)
                assert first is not None
            finally:
                db.close()
            _add_peer(db_path, NODE_A, 4242420002)
            assert len(ws.receive_json()["payload"]["desired"]["bgp_peers"]) == 2

            db = Database.open(db_path)
            try:
                RevisionStore(db.connection).pin(NODE_A, first)
            finally:
                db.close()
            push = ws.receive_json()
            assert push["payload"]["revision"] == first
            assert len(push["payload"]["desired"]["bgp_peers"]) == 1


class TestIsolationBetweenNodes:
    def test_other_nodes_change_is_not_pushed(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        """Ordering, not timeout: B is mutated first, so a misrouted push would
        arrive before A's own.
        """
        with _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            _add_peer(db_path, NODE_B, 4242429999)
            _add_peer(db_path, NODE_A, 4242420001)
            push = ws.receive_json()
            asns = [p["peer_asn"] for p in push["payload"]["desired"]["bgp_peers"]]
            assert asns == [4242420001], "node A received a push for node B's change"

    def test_both_nodes_get_their_own(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        with _connect(client, NODE_A, tokens[NODE_A]) as ws_a, _connect(client, NODE_B, tokens[NODE_B]) as ws_b:
            _handshake(ws_a)
            _handshake(ws_b)
            _add_peer(db_path, NODE_A, 4242420001)
            _add_peer(db_path, NODE_B, 4242420002)
            a = ws_a.receive_json()["payload"]["desired"]["bgp_peers"]
            b = ws_b.receive_json()["payload"]["desired"]["bgp_peers"]
            assert [p["peer_asn"] for p in a] == [4242420001]
            assert [p["peer_asn"] for p in b] == [4242420002]


class TestNoRedundantPush:
    def test_no_op_update_is_swallowed(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        """update_*_peer emits an event even when it changed nothing. The content
        fingerprint must absorb it — otherwise every no-op write wakes the fleet.

        Asserted by ordering: the no-op runs first, then a real change; the first
        frame received must reflect the real change.
        """
        _add_peer(db_path, NODE_A, 4242420001)
        with _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            db = Database.open(db_path)
            try:
                # Same values as the seeded row -> zero content change.
                db.update_bgp_peer(
                    node_id=NODE_A,
                    peer_asn=4242420001,
                    peer_public_key=FAKE_PUB,
                    endpoint="peer.example:51820",
                    peer_lla="fe80::2",
                    listen_port=21000 + (4242420001 % 1000),
                    allowed_ips=["fe80::/64"],
                    net_backend="networkd",
                )
            finally:
                db.close()
            _add_peer(db_path, NODE_A, 4242420002)
            push = ws.receive_json()
            asns = sorted(p["peer_asn"] for p in push["payload"]["desired"]["bgp_peers"])
            assert asns == [4242420001, 4242420002]


class TestAccessRevoked:
    def test_token_rotation_disconnects(self, client: TestClient, tokens: dict[str, str]) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            client.post(f"/api/admin/nodes/{NODE_A}/token", headers=ADMIN_H)
            frame = ws.receive_json()
            assert frame["payload"]["code"] == "revoked"
            ws.receive_json()
        assert exc.value.code == CLOSE_REVOKED

    def test_node_removal_disconnects(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            db = Database.open(db_path)
            try:
                ManagedNodeStore(db.connection).delete(NODE_A)
            finally:
                db.close()
            ws.receive_json()
            ws.receive_json()
        assert exc.value.code == CLOSE_REVOKED

    def test_revoke_wins_over_desired(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        """A node revoked in the same batch must be disconnected, not pushed to."""
        with pytest.raises(WebSocketDisconnect), _connect(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            db = Database.open(db_path)
            try:
                db.ensure_node(NODE_A)
                ManagedNodeStore(db.connection).delete(NODE_A)
            finally:
                db.close()
            _add_peer(db_path, NODE_A, 4242420001)
            frame = ws.receive_json()
            assert frame["payload"]["code"] == "revoked"
            ws.receive_json()
