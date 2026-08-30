from __future__ import annotations

import json
from pathlib import Path

import pytest

from dn42ctl.db import BgpPeerRecord, Database, IbgpPeerRecord
from dn42ctl.db_managed import ManagedNodeStore, RevisionStore
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.desired_state import (
    build_desired_state,
    compute_content_digest,
    compute_desired_fingerprint,
    digest_of_revision,
    require_managed_node_exists,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0cy0zMmJ5dGU="


def _seed_node_with_peers(db_path: Path, node_id: str = NODE_A) -> None:
    db = Database.open(db_path)
    try:
        db.ensure_node(node_id)
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=node_id,
                peer_asn=4242421234,
                ifname="dn42_1234",
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
        db.insert_ibgp_peer(
            IbgpPeerRecord(
                node_id=node_id,
                name="alpha",
                ifname="wg_alpha",
                wg_private_key=FAKE_PRIV,
                wg_public_key=FAKE_PUB,
                peer_public_key=FAKE_PUB,
                endpoint="alpha.example:51820",
                local_lla="fe80::10",
                peer_lla="fe80::20",
                listen_port=31234,
                allowed_ips=["::/0"],
                net_backend="networkd",
                babel_rxcost=96,
                peer_ip="fd42:1::1",
                has_wg=True,
                babel_type="tunnel",
            )
        )
    finally:
        db.close()


class TestBuildDesiredState:
    def test_empty_node(self, db_path: Path) -> None:
        db = Database.open(db_path)
        db.ensure_node(NODE_A)
        db.close()
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert ds.bgp_peers == []
        assert ds.ibgp_peers == []
        assert ds.node_id == NODE_A
        assert ds.revision
        # 文件位置属于节点本地信息,中心不下发。节点自行按 node.toml [apply]、本机
        # config.toml、内置默认值三级解析,见 services/node_apply._resolve_paths。
        assert "paths" not in ds.to_dict()

    def test_with_peers(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert len(ds.bgp_peers) == 1
        assert ds.bgp_peers[0]["peer_asn"] == 4242421234
        assert ds.bgp_peers[0]["allowed_ips"] == ["fe80::/64", "fd00::/8"]
        assert ds.bgp_peers[0]["wg_private_key"] == FAKE_PRIV
        assert len(ds.ibgp_peers) == 1
        assert ds.ibgp_peers[0]["name"] == "alpha"
        assert ds.ibgp_peers[0]["babel_rxcost"] == 96
        assert ds.ibgp_peers[0]["has_wg"] is True

    def test_revision_deterministic(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        ds1 = build_desired_state(db_path=db_path, node_id=NODE_A)
        ds2 = build_desired_state(db_path=db_path, node_id=NODE_A)
        # Hash suffix is content-based so it must match even when generated_at differs.
        assert ds1.revision.split("-")[-1] == ds2.revision.split("-")[-1]

    def test_isolation_between_nodes(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path, NODE_A)
        db = Database.open(db_path)
        db.ensure_node(NODE_B)
        db.close()
        ds_a = build_desired_state(db_path=db_path, node_id=NODE_A)
        ds_b = build_desired_state(db_path=db_path, node_id=NODE_B)
        assert len(ds_a.bgp_peers) == 1
        assert ds_b.bgp_peers == []

    def test_to_dict_serializable(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        # Must round-trip through JSON.
        payload = json.dumps(ds.to_dict())
        parsed = json.loads(payload)
        assert parsed["node_id"] == NODE_A
        assert parsed["bgp_peers"][0]["ifname"] == "dn42_1234"


def _revision_count(db_path: Path, node_id: str = NODE_A) -> int:
    db = Database.open(db_path)
    try:
        return len(RevisionStore(db.connection).list_for_node(node_id, limit=1000))
    finally:
        db.close()


class TestRevisionChurn:
    """The revision string embeds `generated_at`, so a naive record-every-build
    writes a row every time. With the agent's periodic reconcile that would evict
    the rollback history within hours.
    """

    def test_unchanged_content_records_once(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        for _ in range(5):
            build_desired_state(db_path=db_path, node_id=NODE_A)
        assert _revision_count(db_path) == 1

    def test_unchanged_content_returns_stable_revision(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        first = build_desired_state(db_path=db_path, node_id=NODE_A)
        second = build_desired_state(db_path=db_path, node_id=NODE_A)
        # Not just the digest half — the whole string, including generated_at.
        assert first.revision == second.revision
        assert first.generated_at == second.generated_at

    def test_changed_content_records_again(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        build_desired_state(db_path=db_path, node_id=NODE_A)
        db = Database.open(db_path)
        try:
            db.delete_bgp_peer(NODE_A, 4242421234)
        finally:
            db.close()
        build_desired_state(db_path=db_path, node_id=NODE_A)
        assert _revision_count(db_path) == 2

    def test_recorded_payload_matches_returned_state(self, db_path: Path) -> None:
        """The snapshot we keep must be the one we hand out, or rollback lies."""
        _seed_node_with_peers(db_path)
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        db = Database.open(db_path)
        try:
            stored = RevisionStore(db.connection).get_by_revision(NODE_A, ds.revision)
        finally:
            db.close()
        assert stored is not None
        assert stored.payload == ds.to_dict()


class TestDigestOfRevision:
    def test_splits_from_the_right(self) -> None:
        # ISO timestamps contain '-', so a left split would grab the wrong half.
        assert digest_of_revision("2026-05-18T10:00:00+00:00-abcd1234") == "abcd1234"

    def test_rejects_garbage(self) -> None:
        assert digest_of_revision("nodashes") is None


class TestComputeDesiredFingerprint:
    def test_stable_across_calls(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        a = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        b = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        assert a.content_hash == b.content_hash

    def test_writes_nothing(self, db_path: Path) -> None:
        """This runs on every watcher tick per connected node — it must not write."""
        _seed_node_with_peers(db_path)
        for _ in range(5):
            compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        assert _revision_count(db_path) == 0

    def test_changes_with_content(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        before = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        db = Database.open(db_path)
        try:
            db.delete_bgp_peer(NODE_A, 4242421234)
        finally:
            db.close()
        after = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        assert before.content_hash != after.content_hash

    def test_matches_build_desired_state_digest(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        fp = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert fp.content_hash == digest_of_revision(ds.revision)

    def test_follows_the_pin(self, db_path: Path) -> None:
        """A rolled-back node must fingerprint the PINNED payload, not the live one.

        This is why the hub cannot reuse build_desired_state(record_revision=False):
        that path skips the pin lookup and would report the live content.
        """
        _seed_node_with_peers(db_path)
        pinned = build_desired_state(db_path=db_path, node_id=NODE_A)
        db = Database.open(db_path)
        try:
            RevisionStore(db.connection).pin(NODE_A, pinned.revision)
            db.delete_bgp_peer(NODE_A, 4242421234)
        finally:
            db.close()

        fp = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        assert fp.pinned_revision == pinned.revision
        # Live content lost a peer, but the fingerprint must still reflect the pin.
        assert fp.content_hash == digest_of_revision(pinned.revision)

    def test_unpinned_reports_none(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        assert compute_desired_fingerprint(db_path=db_path, node_id=NODE_A).pinned_revision is None

    def test_isolation_between_nodes(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path, NODE_A)
        db = Database.open(db_path)
        db.ensure_node(NODE_B)
        db.close()
        a = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        b = compute_desired_fingerprint(db_path=db_path, node_id=NODE_B)
        assert a.content_hash != b.content_hash


class TestComputeContentDigest:
    def test_ignores_generated_at(self) -> None:
        """Two builds of identical content must agree regardless of when they ran."""
        args = {"node_id": NODE_A, "bgp_peers": [], "ibgp_peers": []}
        assert compute_content_digest(**args) == compute_content_digest(**args)

    def test_key_order_insensitive(self) -> None:
        a = compute_content_digest(node_id=NODE_A, bgp_peers=[{"x": 1, "y": 2}], ibgp_peers=[])
        b = compute_content_digest(node_id=NODE_A, bgp_peers=[{"y": 2, "x": 1}], ibgp_peers=[])
        assert a == b

    def test_distinguishes_nodes(self) -> None:
        a = compute_content_digest(node_id=NODE_A, bgp_peers=[], ibgp_peers=[])
        b = compute_content_digest(node_id=NODE_B, bgp_peers=[], ibgp_peers=[])
        assert a != b


class TestNodeBlockDigest:
    """零抖动规则:空 block 必须与"没有 block"哈希成同一个值。

    否则升级瞬间全网每个节点的内容哈希都会变,于是每个节点各收一次无意义的推送、
    各写一行 config_revisions。
    """

    def test_empty_block_digest_unchanged(self) -> None:
        args = {"node_id": NODE_A, "bgp_peers": [], "ibgp_peers": []}
        baseline = compute_content_digest(**args)
        assert compute_content_digest(**args, node=None) == baseline
        assert compute_content_digest(**args, node={}) == baseline

    def test_non_empty_block_changes_digest(self) -> None:
        args = {"node_id": NODE_A, "bgp_peers": [], "ibgp_peers": []}
        assert compute_content_digest(**args, node={"own_ipv6": "fd42::1"}) != compute_content_digest(**args)

    def test_block_contents_distinguished(self) -> None:
        args = {"node_id": NODE_A, "bgp_peers": [], "ibgp_peers": []}
        a = compute_content_digest(**args, node={"own_ipv6": "fd42::1"})
        b = compute_content_digest(**args, node={"own_ipv6": "fd42::2"})
        assert a != b


class TestNodeBlockInDesiredState:
    @staticmethod
    def _set_addresses(db_path: Path, **kwargs: object) -> None:
        db = Database.open(db_path)
        try:
            store = ManagedNodeStore(db.connection)
            if store.get(NODE_A) is None:
                store.add(NODE_A, "alpha")
            store.set_addresses(NODE_A, **kwargs)  # type: ignore[arg-type]
        finally:
            db.close()

    def test_absent_when_columns_null(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        state = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert state.node == {}
        assert "node" not in state.to_dict()

    def test_present_when_columns_set(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        self._set_addresses(db_path, own_ipv6="fd42:4242:1::1", router_id="172.20.1.1")
        state = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert state.node == {"own_ipv6": "fd42:4242:1::1", "router_id": "172.20.1.1"}
        assert state.to_dict()["node"] == state.node

    def test_only_non_null_keys_appear(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        self._set_addresses(db_path, router_id="172.20.1.1")
        assert build_desired_state(db_path=db_path, node_id=NODE_A).node == {"router_id": "172.20.1.1"}

    def test_endpoint_host_is_never_pushed(self, db_path: Path) -> None:
        """节点不会拨自己,apply 对 endpoint_host 无事可做。"""
        _seed_node_with_peers(db_path)
        self._set_addresses(db_path, endpoint_host="a.example.com")
        assert build_desired_state(db_path=db_path, node_id=NODE_A).node == {}

    def test_address_change_moves_fingerprint(self, db_path: Path) -> None:
        _seed_node_with_peers(db_path)
        before = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A).content_hash
        self._set_addresses(db_path, own_ipv6="fd42:4242:1::1")
        after = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A).content_hash
        assert before != after

    def test_old_pinned_payload_without_node_key(self, db_path: Path) -> None:
        """老快照里没有 node 键、却带着已经废弃的 paths,回放不能 KeyError 也不能把它带出来。"""
        _seed_node_with_peers(db_path)
        db = Database.open(db_path)
        try:
            store = RevisionStore(db.connection)
            store.record(
                node_id=NODE_A,
                revision="2026-01-01T00:00:00+00:00-deadbeef",
                generated_at="2026-01-01T00:00:00+00:00",
                payload={
                    "node_id": NODE_A,
                    "revision": "2026-01-01T00:00:00+00:00-deadbeef",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "bgp_peers": [],
                    "ibgp_peers": [],
                    "paths": {"bird_conf_path": "/etc/bird/bird.conf"},
                },
            )
            store.pin(NODE_A, "2026-01-01T00:00:00+00:00-deadbeef")
        finally:
            db.close()

        state = build_desired_state(db_path=db_path, node_id=NODE_A)
        assert state.node == {}
        assert "paths" not in state.to_dict()
        fp = compute_desired_fingerprint(db_path=db_path, node_id=NODE_A)
        assert fp.pinned_revision == "2026-01-01T00:00:00+00:00-deadbeef"


class TestRequireManagedNodeExists:
    def test_missing(self, db_path: Path) -> None:
        Database.open(db_path).close()
        with pytest.raises(Dn42CtlError, match="不存在"):
            require_managed_node_exists(db_path=db_path, node_id=NODE_A)

    def test_present(self, db_path: Path) -> None:
        from dn42ctl.db_managed import ManagedNodeStore

        db = Database.open(db_path)
        try:
            ManagedNodeStore(db.connection).add(NODE_A, "alpha")
        finally:
            db.close()
        require_managed_node_exists(db_path=db_path, node_id=NODE_A)
