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
        patch("dn42ctl.services.bgp.generate_random_lla", return_value="fe80::abcd:1234"),
        patch("dn42ctl.services.ibgp.generate_random_lla", return_value="fe80::abcd:5678"),
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


REMOTE_NODE = "99999999-9999-4999-8999-999999999999"


class TestNodeScoping:
    @pytest.mark.usefixtures("_mock_wg")
    def test_peers_are_partitioned_by_node(self, sample_config, db_path: Path) -> None:
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242421234,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
        )
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242425678,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            node_id=REMOTE_NODE,
            render_files=False,
        )

        own = show_bgp_peers(config=sample_config, db_path=db_path, include_live=False)
        assert [p.peer_asn for p in own] == [4242421234]

        remote = show_bgp_peers(config=sample_config, db_path=db_path, include_live=False, node_id=REMOTE_NODE)
        assert [p.peer_asn for p in remote] == [4242425678]

    @pytest.mark.usefixtures("_mock_wg")
    def test_remote_node_reports_no_local_files(self, sample_config, db_path: Path) -> None:
        """peer_files_for_backend 用的是 hub 的目录,对远端节点显示成'缺失'会主动误导。"""
        create_bgp_peer(
            config=sample_config,
            db_path=db_path,
            peer_asn=4242425678,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            node_id=REMOTE_NODE,
            render_files=False,
        )
        remote = show_bgp_peers(config=sample_config, db_path=db_path, include_live=False, node_id=REMOTE_NODE)
        assert remote[0].files == []

        own = show_bgp_peers(config=sample_config, db_path=db_path, include_live=False)
        assert own == []

    @pytest.mark.usefixtures("_mock_wg")
    def test_remote_node_never_probes_live(self, sample_config, db_path: Path) -> None:
        """live 探的是本机接口,对远端节点毫无意义,必须完全跳过。"""
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="remote-site",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            node_id=REMOTE_NODE,
            render_files=False,
        )
        with patch("dn42ctl.services.show._run_live_probes") as probes:
            peers = show_ibgp_peers(config=sample_config, db_path=db_path, include_live=True, node_id=REMOTE_NODE)
        probes.assert_not_called()
        assert peers[0].live_wg is None
        assert peers[0].live_bird is None

    @pytest.mark.usefixtures("_mock_wg")
    def test_wg_tunnels_honour_node_id(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="remote-site",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            node_id=REMOTE_NODE,
            render_files=False,
        )
        assert show_wg_tunnels(config=sample_config, db_path=db_path, include_live=False) == []
        remote = show_wg_tunnels(config=sample_config, db_path=db_path, include_live=False, node_id=REMOTE_NODE)
        # sanitize_name 会把 '-' 归一成 '_'
        assert [t.name for t in remote] == ["remote_site"]

    @pytest.mark.usefixtures("_mock_wg")
    def test_remote_node_id_surfaced_in_view(self, sample_config, db_path: Path) -> None:
        create_ibgp_peer(
            config=sample_config,
            db_path=db_path,
            name="linked",
            peer_ip=VALID_PEER_IP,
            peer_public_key=VALID_PUBKEY,
            endpoint=VALID_ENDPOINT,
            peer_lla=VALID_PEER_LLA,
            net_backend="networkd",
            remote_node_id=REMOTE_NODE,
        )
        peers = show_ibgp_peers(config=sample_config, db_path=db_path, include_live=False)
        assert peers[0].remote_node_id == REMOTE_NODE
