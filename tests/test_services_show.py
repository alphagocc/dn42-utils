from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import VALID_ENDPOINT, VALID_PEER_IP, VALID_PEER_LLA, VALID_PUBKEY

from dn42ctl.services.bgp import create_bgp_peer
from dn42ctl.services.ibgp import create_ibgp_peer
from dn42ctl.services.show import (
    _run_cmd_best_effort,
    show_bgp_peers,
    show_ibgp_peers,
    show_wg_tunnels,
)


@pytest.fixture
def _mock_wg(mock_wg_keypair):
    with (
        patch("dn42ctl.services.bgp.generate_random_lla_cidr", return_value="fe80::abcd:1234/64"),
        patch("dn42ctl.services.ibgp.generate_random_lla_cidr", return_value="fe80::abcd:5678/64"),
    ):
        yield


class TestRunCmdBestEffort:
    def test_success(self) -> None:
        with patch("subprocess.check_output", return_value="output\n"):
            result = _run_cmd_best_effort(["echo", "hi"])
            assert result.ok is True
            assert result.output == "output"

    def test_timeout(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.TimeoutExpired(["cmd"], 2),
        ):
            result = _run_cmd_best_effort(["cmd"])
            assert result.ok is False
            assert result.error == "timeout"

    def test_file_not_found(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=FileNotFoundError("not found"),
        ):
            result = _run_cmd_best_effort(["cmd"])
            assert result.ok is False
            assert "not found" in (result.error or "")

    def test_called_process_error(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "cmd", output="err"),
        ):
            result = _run_cmd_best_effort(["cmd"])
            assert result.ok is False
            assert "exit=1" in (result.error or "")


class TestShowBgpPeers:
    @pytest.mark.usefixtures("_mock_wg")
    def test_no_live(self, sample_config, db_path: Path) -> None:
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        peers = show_bgp_peers(config=sample_config, db_path=db_path, include_live=False)
        assert len(peers) == 1
        assert peers[0].peer_asn == 4242421234
        assert peers[0].live_wg is None
        assert peers[0].live_bird is None

    @pytest.mark.usefixtures("_mock_wg")
    def test_empty(self, sample_config, db_path: Path) -> None:
        peers = show_bgp_peers(config=sample_config, db_path=db_path, include_live=False)
        assert peers == []


class TestShowIbgpPeers:
    @pytest.mark.usefixtures("_mock_wg")
    def test_no_live(self, sample_config, db_path: Path) -> None:
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
        peers = show_ibgp_peers(config=sample_config, db_path=db_path, include_live=False)
        assert len(peers) == 1
        assert peers[0].name == "mynode"
        assert peers[0].babel_rxcost == 120


class TestShowWgTunnels:
    @pytest.mark.usefixtures("_mock_wg")
    def test_combines(self, sample_config, db_path: Path) -> None:
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
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
        tunnels = show_wg_tunnels(config=sample_config, db_path=db_path, include_live=False)
        assert len(tunnels) == 2
        kinds = {t.kind for t in tunnels}
        assert kinds == {"bgp", "ibgp"}

    @pytest.mark.usefixtures("_mock_wg")
    def test_excludes_no_wg(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="nowg",
            peer_ip=VALID_PEER_IP,
            has_wg=False,
        )
        tunnels = show_wg_tunnels(config=sample_config, db_path=db_path, include_live=False)
        assert len(tunnels) == 0
