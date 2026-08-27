"""Hub WebSocket endpoint: handshake, auth, and message dispatch.

Nodes are seeded by a fixture that runs BEFORE `TestClient.__enter__`, because
startup anchors the watcher's `sync_events` cursor. Issuing a node token emits an
`access_revoked` event; if that landed after the anchor, the watcher would close
the very connection the test just opened. Production has the same ordering —
nodes are registered ahead of time, then the agent connects.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import dn42ctl.ws_hub as ws_hub
from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import BgpPeerRecord, Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.ws_protocol import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    CLOSE_VERSION_MISMATCH,
    ERR_BAD_ENVELOPE,
    ERR_PAYLOAD_INVALID,
    ERR_SERVICE_ERROR,
    ERR_UNKNOWN_TYPE,
    MSG_ACK,
    MSG_DESIRED_PUSH,
    MSG_ERROR,
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_PONG,
    Envelope,
    encode,
)

ADMIN_TOKEN = "admin-secret"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="


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
    """Both nodes registered before the server starts — see module docstring."""
    return {
        NODE_A: _seed_node(db_path, NODE_A, "a"),
        NODE_B: _seed_node(db_path, NODE_B, "b"),
    }


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path, tokens: dict[str, str]) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN, sync_poll_interval=0.02)
    # `with` runs lifespan, which is what starts the sync_events watcher.
    with TestClient(app) as c:
        yield c


def _ws_path(node_id: str) -> str:
    return f"/api/v1/nodes/{node_id}/ws"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_peer(db_path: Path, node_id: str, peer_asn: int = 4242421234) -> None:
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
                listen_port=21234,
                allowed_ips=["fe80::/64", "fd00::/8"],
                net_backend="networkd",
            )
        )
    finally:
        db.close()


def _handshake(ws, cached_revision: str | None = None) -> dict:
    """Send hello, return the hello_ack payload."""
    env = Envelope(
        type=MSG_HELLO,
        payload={"agent_version": "test", "cached_revision": cached_revision},
    )
    ws.send_text(encode(env))
    ack = ws.receive_json()
    assert ack["type"] == MSG_HELLO_ACK, ack
    assert ack["re"] == env.id
    return ack["payload"]


def _connected(client: TestClient, node_id: str, token: str):
    return client.websocket_connect(_ws_path(node_id), headers=_auth(token))


class TestHandshakeAuth:
    def test_success(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            payload = _handshake(ws)
            assert payload["node_id"] == NODE_A
            assert payload["server_version"]

    def test_missing_header(self, client: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(_ws_path(NODE_A)) as ws:
            # An error frame precedes the close so the reason is visible to the peer.
            assert ws.receive_json()["payload"]["code"] == "unauthorized"
            ws.receive_json()
        assert exc.value.code == CLOSE_UNAUTHORIZED

    def test_invalid_token(self, client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            _connected(client, NODE_A, "bogus") as ws,
        ):
            ws.receive_json()
            ws.receive_json()
        assert exc.value.code == CLOSE_UNAUTHORIZED

    def test_non_bearer_scheme(self, client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(_ws_path(NODE_A), headers={"Authorization": "Basic abc"}) as ws,
        ):
            ws.receive_json()
            ws.receive_json()
        assert exc.value.code == CLOSE_UNAUTHORIZED

    def test_token_for_other_node(self, client: TestClient, tokens: dict[str, str]) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            _connected(client, NODE_B, tokens[NODE_A]) as ws,
        ):
            assert ws.receive_json()["payload"]["code"] == "forbidden"
            ws.receive_json()
        assert exc.value.code == CLOSE_FORBIDDEN

    def test_removed_node_is_rejected(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        """Deleting the node takes its token hash with it, so auth fails closed.

        (The separate 4404 path guards the race where the row disappears between
        `authenticate` and the existence check — not reachable deterministically.)
        """
        db = Database.open(db_path)
        try:
            db.connection.execute("DELETE FROM managed_nodes WHERE node_id=?", (NODE_A,))
            db.connection.commit()
        finally:
            db.close()
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            _connected(client, NODE_A, tokens[NODE_A]) as ws,
        ):
            ws.receive_json()
            ws.receive_json()
        assert exc.value.code == CLOSE_UNAUTHORIZED

    def test_token_verified_once_per_connection(
        self, client: TestClient, tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of a long-lived connection: never re-authenticate per frame."""
        calls = {"n": 0}
        real = ws_hub._authenticate_sync

        def counting(**kwargs):
            calls["n"] += 1
            return real(**kwargs)

        monkeypatch.setattr(ws_hub, "_authenticate_sync", counting)
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()  # initial push
            for _ in range(10):
                env = Envelope(type="ping")
                ws.send_text(encode(env))
                assert ws.receive_json()["type"] == MSG_PONG
        assert calls["n"] == 1


class TestHelloPhase:
    def test_first_message_must_be_hello(self, client: TestClient, tokens: dict[str, str]) -> None:
        with pytest.raises(WebSocketDisconnect), _connected(client, NODE_A, tokens[NODE_A]) as ws:
            ws.send_text(encode(Envelope(type="ping")))
            assert ws.receive_json()["payload"]["code"] == ERR_UNKNOWN_TYPE
            ws.receive_json()

    def test_version_mismatch_closes_4008(self, client: TestClient, tokens: dict[str, str]) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            _connected(client, NODE_A, tokens[NODE_A]) as ws,
        ):
            ws.send_text(encode(Envelope(type=MSG_HELLO, v=99)))
            assert ws.receive_json()["payload"]["code"] == "version_mismatch"
            ws.receive_json()
        assert exc.value.code == CLOSE_VERSION_MISMATCH

    def test_malformed_hello(self, client: TestClient, tokens: dict[str, str]) -> None:
        with pytest.raises(WebSocketDisconnect), _connected(client, NODE_A, tokens[NODE_A]) as ws:
            ws.send_text("{not json")
            assert ws.receive_json()["payload"]["code"] == ERR_BAD_ENVELOPE
            ws.receive_json()


class TestInitialSync:
    def test_pushes_when_cache_is_stale(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        _seed_peer(db_path, NODE_A)
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            payload = _handshake(ws, cached_revision="something-old")
            assert payload["in_sync"] is False
            push = ws.receive_json()
            assert push["type"] == MSG_DESIRED_PUSH
            assert push["payload"]["desired"]["bgp_peers"][0]["peer_asn"] == 4242421234

    def test_no_push_when_cache_current(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        _seed_peer(db_path, NODE_A)
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws, cached_revision=None)
            revision = ws.receive_json()["payload"]["revision"]

        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            payload = _handshake(ws, cached_revision=revision)
            assert payload["in_sync"] is True
            # Nothing was pushed. Proven by ordering rather than a timeout: the
            # next frame must be the answer to OUR request, with no stray push first.
            env = Envelope(type="desired_request", payload={"reason": "manual"})
            ws.send_text(encode(env))
            frame = ws.receive_json()
            assert frame["type"] == MSG_DESIRED_PUSH
            assert frame["re"] == env.id

    def test_empty_node_still_handshakes(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            assert ws.receive_json()["payload"]["desired"]["bgp_peers"] == []


class TestDesiredRequest:
    def test_answers_unconditionally(self, client: TestClient, db_path: Path, tokens: dict[str, str]) -> None:
        """Even when content is unchanged, an explicit request must be answered."""
        _seed_peer(db_path, NODE_A)
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()  # initial push
            for _ in range(3):
                env = Envelope(type="desired_request", payload={"reason": "reconcile"})
                ws.send_text(encode(env))
                frame = ws.receive_json()
                assert frame["type"] == MSG_DESIRED_PUSH
                assert frame["re"] == env.id


class TestProposalSubmit:
    def test_creates_proposal(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            env = Envelope(
                type="proposal_submit",
                payload={"source": "push", "kind": "peer_add", "payload": {"peer_kind": "bgp", "peer": {}}},
            )
            ws.send_text(encode(env))
            ack = ws.receive_json()
            assert ack["type"] == MSG_ACK
            assert ack["re"] == env.id
            assert ack["payload"]["status"] == "pending"

        listed = client.get(f"/api/admin/nodes/{NODE_A}/proposals", headers=ADMIN_H).json()
        assert len(listed) == 1

    def test_missing_fields(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            ws.send_text(encode(Envelope(type="proposal_submit", payload={"kind": "peer_add"})))
            frame = ws.receive_json()
            assert frame["type"] == MSG_ERROR
            assert frame["payload"]["code"] == ERR_PAYLOAD_INVALID

    def test_invalid_kind_is_service_error(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            env = Envelope(type="proposal_submit", payload={"source": "push", "kind": "nonsense", "payload": {}})
            ws.send_text(encode(env))
            assert ws.receive_json()["payload"]["code"] == ERR_SERVICE_ERROR


class TestReportSubmit:
    def test_creates_report(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            env = Envelope(
                type="report_submit",
                payload={"kind": "apply_result", "payload": {"ok": True, "revision": "r1"}},
            )
            ws.send_text(encode(env))
            ack = ws.receive_json()
            assert ack["type"] == MSG_ACK
            assert ack["payload"]["report_id"] >= 1

        listed = client.get(f"/api/admin/nodes/{NODE_A}/reports", headers=ADMIN_H).json()
        assert listed[0]["kind"] == "apply_result"

    def test_invalid_kind(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            ws.send_text(encode(Envelope(type="report_submit", payload={"kind": "nope", "payload": {}})))
            assert ws.receive_json()["payload"]["code"] == ERR_SERVICE_ERROR


class TestPingAndResilience:
    def test_pong_and_last_seen(self, client: TestClient, tokens: dict[str, str]) -> None:
        assert client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H).json()["last_seen_at"] is None
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            env = Envelope(type="ping")
            ws.send_text(encode(env))
            pong = ws.receive_json()
            assert pong["type"] == MSG_PONG
            assert pong["re"] == env.id
        assert client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H).json()["last_seen_at"] is not None

    def test_unknown_type_keeps_connection_open(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            ws.send_text(encode(Envelope(type="teapot")))
            assert ws.receive_json()["payload"]["code"] == ERR_UNKNOWN_TYPE
            ws.send_text(encode(Envelope(type="ping")))
            assert ws.receive_json()["type"] == MSG_PONG

    def test_bad_envelope_keeps_connection_open(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            ws.send_text("}}} not json")
            assert ws.receive_json()["payload"]["code"] == ERR_BAD_ENVELOPE
            ws.send_text(encode(Envelope(type="ping")))
            assert ws.receive_json()["type"] == MSG_PONG

    def test_version_mismatch_mid_stream_keeps_connection(self, client: TestClient, tokens: dict[str, str]) -> None:
        with _connected(client, NODE_A, tokens[NODE_A]) as ws:
            _handshake(ws)
            ws.receive_json()
            ws.send_text(encode(Envelope(type="ping", v=99)))
            assert ws.receive_json()["payload"]["code"] == "version_mismatch"
            ws.send_text(encode(Envelope(type="ping")))
            assert ws.receive_json()["type"] == MSG_PONG


class TestAdminToken:
    def test_admin_cannot_use_node_ws(self, client: TestClient) -> None:
        """The WS channel is for node agents; the admin token is not a node token."""
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(_ws_path(NODE_A), headers=ADMIN_H) as ws,
        ):
            ws.receive_json()
            ws.receive_json()
        assert exc.value.code == CLOSE_UNAUTHORIZED
