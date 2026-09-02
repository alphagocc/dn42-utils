from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import FAKE_WG_PUBKEY, VALID_ENDPOINT, VALID_PEER_IP, VALID_PEER_LLA, VALID_PUBKEY

from dn42ctl.db import Database
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.ibgp import create_ibgp_peer, delete_ibgp_peer, modify_ibgp_peer


@pytest.fixture
def _mock_wg(mock_wg_keypair):
    with patch("dn42ctl.services.ibgp.generate_random_lla", return_value="fe80::abcd:5678"):
        yield


class TestCreateIbgpPeer:
    @pytest.mark.usefixtures("_mock_wg")
    def test_create_with_wg(self, sample_config, db_path: Path) -> None:
        result = create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            babel_rxcost=120,
            babel_type="tunnel",
        )
        assert result.ifname == "wg_mynode"
        assert result.wg_public_key == FAKE_WG_PUBKEY
        assert len(result.generated_files) >= 3

        babel_files = [f for f in result.generated_files if "babel" in str(f)]
        assert len(babel_files) == 1

    @pytest.mark.usefixtures("_mock_wg")
    def test_create_no_wg(self, sample_config, db_path: Path) -> None:
        result = create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="no_wg_node",
            peer_ip=VALID_PEER_IP,
            has_wg=False,
        )
        assert result.ifname == "wg_no_wg_node"
        assert result.wg_public_key == ""
        assert result.listen_port == 0
        assert len(result.generated_files) == 1

    @pytest.mark.usefixtures("_mock_wg")
    def test_long_name_raises(self, sample_config, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="接口名过长"):
            create_ibgp_peer(
                config=sample_config,
                db_path=db_path,
                name="this_is_a_very_long_name",
                peer_ip=VALID_PEER_IP,
            )

    @pytest.mark.usefixtures("_mock_wg")
    def test_duplicate_raises(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            babel_rxcost=120,
        )
        with pytest.raises(Dn42CtlError):
            create_ibgp_peer(
                config=sample_config,
                db_path=db_path,
                name="mynode",
                peer_ip=VALID_PEER_IP,
            )

    @pytest.mark.usefixtures("_mock_wg")
    def test_files_written(self, sample_config, db_path: Path) -> None:
        result = create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            babel_rxcost=120,
        )
        for f in result.generated_files:
            assert f.exists(), f"Expected file to exist: {f}"


class TestModifyIbgpPeer:
    @pytest.mark.usefixtures("_mock_wg")
    def test_modify(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            babel_rxcost=120,
            babel_type="tunnel",
        )
        result = modify_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_public_key=VALID_PUBKEY,
            endpoint="new.example.com:51821",
            peer_lla="fe80::99",
            net_backend="networkd",
            babel_rxcost=256,
            babel_type="wired",
            peer_ip="fd42:4242:9999::1",
        )
        assert result.ifname == "wg_mynode"

    @pytest.mark.usefixtures("_mock_wg")
    def test_modify_nonexistent_raises(self, sample_config, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            modify_ibgp_peer(
                config=sample_config,
                db_path=db_path,
                name="nonexistent",
                peer_public_key=VALID_PUBKEY,
                endpoint=VALID_ENDPOINT,
                peer_lla=VALID_PEER_LLA,
                net_backend="networkd",
                babel_rxcost=120,
                babel_type="tunnel",
                peer_ip=VALID_PEER_IP,
            )

    @pytest.mark.usefixtures("_mock_wg")
    def test_modify_no_wg_raises(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="nowg",
            peer_ip=VALID_PEER_IP,
            has_wg=False,
        )
        with pytest.raises(Dn42CtlError, match="未启用 WireGuard"):
            modify_ibgp_peer(
                config=sample_config,
                db_path=db_path,
                name="nowg",
                peer_public_key=VALID_PUBKEY,
                endpoint=VALID_ENDPOINT,
                peer_lla=VALID_PEER_LLA,
                net_backend="networkd",
                babel_rxcost=120,
                babel_type="tunnel",
                peer_ip=VALID_PEER_IP,
            )


class TestDeleteIbgpPeer:
    @pytest.mark.usefixtures("_mock_wg")
    def test_delete(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            babel_rxcost=120,
        )
        result = delete_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
        )
        assert result.kind == "ibgp"
        assert len(result.deleted_files) > 0
        assert len(result.regenerated_files) > 0

    @pytest.mark.usefixtures("_mock_wg")
    def test_nonexistent_raises(self, sample_config, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            delete_ibgp_peer(
                config=sample_config,
                db_path=db_path,
                name="nonexistent",
            )


REMOTE_NODE = "99999999-9999-4999-8999-999999999999"
OTHER_NODE = "88888888-8888-4888-8888-888888888888"


class TestRemoteNodeLink:
    @pytest.mark.usefixtures("_mock_wg")
    def test_create_stores_link(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            remote_node_id=REMOTE_NODE,
        )
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(sample_config.node_id, "mynode")["remote_node_id"] == REMOTE_NODE
        finally:
            db.close()

    @pytest.mark.usefixtures("_mock_wg")
    def test_create_defaults_to_unlinked(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(sample_config.node_id, "mynode")["remote_node_id"] is None
        finally:
            db.close()

    @pytest.mark.usefixtures("_mock_wg")
    def test_modify_without_link_preserves_it(self, sample_config, db_path: Path) -> None:
        """proposal 接受与上报导入都经由 modify,它们不知道这个关联的存在,
        不能让它们静默把链接抹掉。"""
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            remote_node_id=REMOTE_NODE,
        )
        modify_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_public_key=VALID_PUBKEY,
            endpoint="new.example.com:51821",
            peer_lla="fe80::99",
            net_backend="networkd",
            babel_rxcost=256,
            babel_type="wired",
            peer_ip="fd42:4242:9999::1",
        )
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(sample_config.node_id, "mynode")["remote_node_id"] == REMOTE_NODE
        finally:
            db.close()

    @pytest.mark.usefixtures("_mock_wg")
    def test_modify_can_set_and_clear_link(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="mynode",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            remote_node_id=REMOTE_NODE,
        )
        common = {
            "config": sample_config,
            "db_path": db_path,
            "name": "mynode",
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
            "net_backend": "networkd",
            "babel_rxcost": 120,
            "babel_type": "tunnel",
            "peer_ip": VALID_PEER_IP,
        }
        modify_ibgp_peer(**common, remote_node_id=OTHER_NODE)
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(sample_config.node_id, "mynode")["remote_node_id"] == OTHER_NODE
        finally:
            db.close()

        modify_ibgp_peer(**common, remote_node_id=None)
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(sample_config.node_id, "mynode")["remote_node_id"] is None
        finally:
            db.close()
