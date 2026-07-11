from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.init_sys import genconf, init_node


def _init_node_helper(tmp_path: Path, bird_conf_path: Path | None = None):
    config_path = tmp_path / "config.toml"
    db_path = tmp_path / "test.sqlite3"
    bird_dir = tmp_path / "bird"
    bird_dir.mkdir(exist_ok=True)
    peers_dir = bird_dir / "peers"
    peers_dir.mkdir(exist_ok=True)
    (tmp_path / "networkd").mkdir(exist_ok=True)
    (tmp_path / "nm").mkdir(exist_ok=True)

    with (
        patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
        patch("sys.platform", "linux"),
    ):
        result = init_node(
            config_path=config_path,
            db_path=db_path,
            node_id="test-node",
            own_asn=4242421234,
            router_id="172.23.0.1",
            own_ipv6="fd42:4242:1234::1",
            ownnet_v6="fd42:4242:1234::/48",
            ownnetset_v6="[fd42:4242:1234::/48+]",
            bird_conf_path=bird_conf_path or (bird_dir / "bird.conf"),
            bird_peers_dir=peers_dir,
            bird_babel_conf_path=bird_dir / "babel.conf",
            bird_roa_v6_conf_path=bird_dir / "roa_dn42_v6.conf",
            networkd_dir=tmp_path / "networkd",
            nm_system_connections_dir=tmp_path / "nm",
        )
    return result, db_path


class TestInitNode:
    def test_creates_config_and_db(self, tmp_path: Path) -> None:
        result, db_path = _init_node_helper(tmp_path)
        config_path = tmp_path / "config.toml"
        assert config_path.exists()
        assert db_path.exists()
        assert result.config.node_id == "test-node"
        assert result.config.own_asn == 4242421234


class TestGenconf:
    def test_generates_configs(self, tmp_path: Path) -> None:
        init_result, db_path = _init_node_helper(tmp_path)

        mock_resp = io.BytesIO(b"# ROA content\n")
        mock_resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
        mock_resp.__exit__ = lambda s, *a: None  # type: ignore[attr-defined]

        with (
            patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
            patch("sys.platform", "linux"),
            patch("shutil.which", return_value=None),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = genconf(
                config=init_result.config,
                db_path=db_path,
                overwrite_bird_conf=True,
                overwrite_babel_conf=True,
            )

        assert result.bird_conf_path.exists()
        assert result.bird_babel_conf_path.exists()
        assert result.bird_roa_v6_conf_path.exists()

    def test_no_overwrite_raises(self, tmp_path: Path) -> None:
        bird_dir = tmp_path / "bird"
        bird_dir.mkdir(exist_ok=True)
        bird_conf = bird_dir / "bird.conf"
        bird_conf.write_text("existing")

        init_result, db_path = _init_node_helper(tmp_path, bird_conf_path=bird_conf)

        with (
            patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
            patch("sys.platform", "linux"),
            pytest.raises(Dn42CtlError, match="未允许覆盖"),
        ):
            genconf(
                config=init_result.config,
                db_path=db_path,
                overwrite_bird_conf=False,
                overwrite_babel_conf=True,
            )


class TestGenconfIbgpPeerIp:
    def test_genconf_skips_empty_peer_ip_with_warning(self, sample_config, mem_db_with_node) -> None:
        from conftest import FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY, VALID_PEER_LLA, VALID_PUBKEY

        from dn42ctl.db import IbgpPeerRecord

        db = mem_db_with_node

        db.insert_ibgp_peer(
            IbgpPeerRecord(
                node_id="test-node",
                name="scanned",
                ifname="wg_scanned",
                wg_private_key=FAKE_WG_PRIVKEY,
                wg_public_key=FAKE_WG_PUBKEY,
                peer_public_key=VALID_PUBKEY,
                endpoint="example.com:51820",
                local_lla="fe80::2",
                peer_lla=VALID_PEER_LLA,
                listen_port=51822,
                allowed_ips=["fe80::/64", "fd00::/8"],
                net_backend="networkd",
                babel_rxcost=20,
                babel_type="tunnel",
                peer_ip=None,
                has_wg=True,
            )
        )

        roa_path = Path(sample_config.bird_roa_v6_conf_path)
        roa_path.write_text("# ROA placeholder\n")

        with (
            patch("dn42ctl.services.init_sys.open_db_and_ensure_node", return_value=db),
            patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
        ):
            result = genconf(
                config=sample_config,
                db_path=Path(":memory:"),
                overwrite_bird_conf=True,
                overwrite_babel_conf=True,
                regenerate_peers=True,
            )

        assert any("peer_ip 为空" in w for w in result.warnings)
        assert not (Path(sample_config.bird_peers_dir) / "ibgp_scanned.conf").exists()

    def test_genconf_generates_bird_conf_when_peer_ip_present(self, sample_config, mem_db_with_node) -> None:
        from conftest import FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY, VALID_PEER_LLA, VALID_PUBKEY

        from dn42ctl.db import IbgpPeerRecord

        db = mem_db_with_node

        db.insert_ibgp_peer(
            IbgpPeerRecord(
                node_id="test-node",
                name="normal",
                ifname="wg_normal",
                wg_private_key=FAKE_WG_PRIVKEY,
                wg_public_key=FAKE_WG_PUBKEY,
                peer_public_key=VALID_PUBKEY,
                endpoint="example.com:51820",
                local_lla="fe80::3",
                peer_lla=VALID_PEER_LLA,
                listen_port=51823,
                allowed_ips=["fe80::/64", "fd00::/8"],
                net_backend="networkd",
                babel_rxcost=20,
                babel_type="tunnel",
                peer_ip="fd42::99",
                has_wg=True,
            )
        )

        roa_path = Path(sample_config.bird_roa_v6_conf_path)
        roa_path.write_text("# ROA placeholder\n")

        with (
            patch("dn42ctl.services.init_sys.open_db_and_ensure_node", return_value=db),
            patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
        ):
            genconf(
                config=sample_config,
                db_path=Path(":memory:"),
                overwrite_bird_conf=True,
                overwrite_babel_conf=True,
                regenerate_peers=True,
            )

        bird_conf = (Path(sample_config.bird_peers_dir) / "ibgp_normal.conf").read_text()
        assert "fd42::99" in bird_conf
        assert "OWNAS" in bird_conf
