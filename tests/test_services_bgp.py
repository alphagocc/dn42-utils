from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import VALID_ENDPOINT, VALID_PEER_LLA, VALID_PUBKEY

from dn42ctl.services.bgp import create_bgp_peer, delete_bgp_peer, modify_bgp_peer
from dn42ctl.services.core import Dn42CtlError


@pytest.fixture
def _mock_wg(mock_wg_keypair):
    with patch("dn42ctl.services.bgp.generate_random_lla", return_value="fe80::abcd:1234"):
        yield


class TestCreateBgpPeer:
    @pytest.mark.usefixtures("_mock_wg")
    def test_create(self, sample_config, db_path: Path) -> None:
        result = create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        assert result.ifname == "dn42_1234"
        assert result.listen_port == 21234
        assert result.wg_public_key
        assert len(result.generated_files) >= 2

    @pytest.mark.usefixtures("_mock_wg")
    def test_explicit_listen_port(self, sample_config, db_path: Path) -> None:
        result = create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            listen_port=51820,
        )
        assert result.listen_port == 51820

    @pytest.mark.usefixtures("_mock_wg")
    def test_duplicate_raises(self, sample_config, db_path: Path) -> None:
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        with pytest.raises(Dn42CtlError, match="已存在"):
            create_bgp_peer(
                config=sample_config,
                db_path=db_path,
                peer_asn=4242421234,
                peer_public_key=VALID_PUBKEY,
                endpoint=VALID_ENDPOINT,
                peer_lla=VALID_PEER_LLA,
                net_backend="networkd",
            )

    @pytest.mark.usefixtures("_mock_wg")
    def test_nm_backend(self, sample_config, db_path: Path) -> None:
        result = create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="nm",
        )
        nm_files = [f for f in result.generated_files if str(f).endswith(".nmconnection")]
        assert len(nm_files) == 1

    @pytest.mark.usefixtures("_mock_wg")
    def test_files_written(self, sample_config, db_path: Path) -> None:
        result = create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        for f in result.generated_files:
            assert f.exists(), f"Expected file to exist: {f}"


class TestModifyBgpPeer:
    @pytest.mark.usefixtures("_mock_wg")
    def test_modify(self, sample_config, db_path: Path) -> None:
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        result = modify_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint="new.example.com:51820",
            peer_lla="fe80::99",
            net_backend="networkd",
        )
        assert result.ifname == "dn42_1234"

    @pytest.mark.usefixtures("_mock_wg")
    def test_nonexistent_raises(self, sample_config, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            modify_bgp_peer(
                config=sample_config,
                db_path=db_path,
                peer_asn=99999,
                peer_public_key=VALID_PUBKEY,
                endpoint=VALID_ENDPOINT,
                peer_lla=VALID_PEER_LLA,
                net_backend="networkd",
            )


class TestDeleteBgpPeer:
    @pytest.mark.usefixtures("_mock_wg")
    def test_delete(self, sample_config, db_path: Path) -> None:
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        result = delete_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
        )
        assert result.kind == "bgp"
        assert len(result.deleted_files) > 0

    @pytest.mark.usefixtures("_mock_wg")
    def test_nonexistent_raises(self, sample_config, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            delete_bgp_peer(
                config=sample_config,
                db_path=db_path,
                peer_asn=99999,
            )
