from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from dn42ctl.db import BgpPeerRecord, Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import (
    Dn42CtlError,
    build_desired_state,
    clear_rollback,
    get_pinned,
    list_revisions,
    rollback_to,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


def _register(db_path: Path) -> None:
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).add(NODE_A, "alpha")
    finally:
        db.close()


def _insert_peer(db_path: Path, asn: int) -> None:
    db = Database.open(db_path)
    try:
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=NODE_A,
                peer_asn=asn,
                ifname=f"dn42_{asn % 10000:04d}",
                wg_private_key=FAKE_PRIV,
                wg_public_key=FAKE_PUB,
                peer_public_key=FAKE_PUB,
                endpoint="ep:51820",
                local_lla="fe80::1/64",
                peer_lla="fe80::2",
                listen_port=20000 + (asn % 10000),
                allowed_ips=["fe80::/64", "fd00::/8"],
                net_backend="networkd",
            )
        )
    finally:
        db.close()


def _delete_peer(db_path: Path, asn: int) -> None:
    db = Database.open(db_path)
    try:
        db.delete_bgp_peer(NODE_A, asn)
    finally:
        db.close()


class TestRevisionsRecorded:
    def test_each_build_records(self, db_path: Path) -> None:
        _register(db_path)
        _insert_peer(db_path, 4242421111)
        build_desired_state(db_path=db_path, node_id=NODE_A)
        _insert_peer(db_path, 4242422222)
        build_desired_state(db_path=db_path, node_id=NODE_A)
        rows = list_revisions(db_path=db_path, node_id=NODE_A)
        assert len(rows) == 2

    def test_repeat_with_no_change_is_idempotent(self, db_path: Path) -> None:
        _register(db_path)
        _insert_peer(db_path, 4242421111)
        ds1 = build_desired_state(db_path=db_path, node_id=NODE_A)
        ds2 = build_desired_state(db_path=db_path, node_id=NODE_A)
        # Same content hash -> same revision suffix -> same row.
        assert ds1.revision.split("-")[-1] == ds2.revision.split("-")[-1]
        rows = list_revisions(db_path=db_path, node_id=NODE_A)
        # Both call recorded same revision; UNIQUE means one row.
        assert len(rows) == 1


class TestRollback:
    def test_pinned_pull_returns_pinned(self, db_path: Path) -> None:
        _register(db_path)
        _insert_peer(db_path, 4242421111)
        ds1 = build_desired_state(db_path=db_path, node_id=NODE_A)
        _insert_peer(db_path, 4242422222)
        ds2 = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert ds1.revision != ds2.revision

        rollback_to(db_path=db_path, node_id=NODE_A, revision=ds1.revision)
        # Subsequent build sees the new peer in DB, but should return ds1's content.
        ds3 = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert ds3.revision == ds1.revision
        assert len(ds3.bgp_peers) == 1  # only the first peer
        # Confirm pinned
        pin = get_pinned(db_path=db_path, node_id=NODE_A)
        assert pin is not None
        assert pin.revision == ds1.revision

    def test_clear_rollback(self, db_path: Path) -> None:
        _register(db_path)
        _insert_peer(db_path, 4242421111)
        ds1 = build_desired_state(db_path=db_path, node_id=NODE_A)
        _insert_peer(db_path, 4242422222)
        rollback_to(db_path=db_path, node_id=NODE_A, revision=ds1.revision)
        clear_rollback(db_path=db_path, node_id=NODE_A)
        ds_after = build_desired_state(db_path=db_path, node_id=NODE_A)
        # Now back to latest content (2 peers).
        assert len(ds_after.bgp_peers) == 2

    def test_rollback_unknown_revision(self, db_path: Path) -> None:
        _register(db_path)
        with pytest.raises(Dn42CtlError, match="不存在"):
            rollback_to(db_path=db_path, node_id=NODE_A, revision="no-such")


class TestTrim:
    def test_keep_latest_default(self, db_path: Path) -> None:
        _register(db_path)
        for asn in range(4242420001, 4242420061):
            _insert_peer(db_path, asn)
            build_desired_state(db_path=db_path, node_id=NODE_A)
            # Delete to keep table small, but each build should still trim revisions table.
            _delete_peer(db_path, asn)
        rows = list_revisions(db_path=db_path, node_id=NODE_A, limit=100)
        # Default keep_latest=50, so at most 50 retained.
        assert len(rows) <= 50

    def test_explicit_keep_latest(self, db_path: Path) -> None:
        _register(db_path)
        for asn in range(4242420001, 4242420011):
            _insert_peer(db_path, asn)
            build_desired_state(db_path=db_path, node_id=NODE_A, keep_latest=3)
            _delete_peer(db_path, asn)
        rows = list_revisions(db_path=db_path, node_id=NODE_A, limit=100)
        assert len(rows) == 3
