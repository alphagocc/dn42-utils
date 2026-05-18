from __future__ import annotations

import json
from pathlib import Path

import pytest

from dn42ctl.db import BgpPeerRecord, Database, IbgpPeerRecord
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.desired_state import build_desired_state, require_managed_node_exists

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="


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
                local_lla="fe80::1/64",
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
                local_lla="fe80::10/64",
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
        assert "bird_conf_path" in ds.paths

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


class TestRequireManagedNodeExists:
    def test_missing(self, db_path: Path) -> None:
        # DB exists (created by build_desired_state? No, that returns empty); create.
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
