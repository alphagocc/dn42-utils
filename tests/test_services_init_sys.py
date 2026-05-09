from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.init_sys import genconf, init_node


class TestInitNode:
    def test_creates_config_and_db(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        db_path = tmp_path / "test.sqlite3"
        bird_dir = tmp_path / "bird"
        bird_dir.mkdir()
        peers_dir = bird_dir / "peers"
        peers_dir.mkdir()

        with patch("dn42ctl.services.init_sys.ensure_dummy_interface") as mock_dummy:
            mock_dummy.return_value = None
            with patch("sys.platform", "linux"):
                result = init_node(
                    config_path=config_path,
                    db_path=db_path,
                    node_id="test-node",
                    own_asn=4242421234,
                    router_id="172.23.0.1",
                    own_ipv6="fd42:4242:1234::1",
                    ownnet_v6="fd42:4242:1234::/48",
                    ownnetset_v6="[fd42:4242:1234::/48+]",
                    bird_conf_path=bird_dir / "bird.conf",
                    bird_peers_dir=peers_dir,
                    bird_babel_conf_path=bird_dir / "babel.conf",
                    bird_roa_v6_conf_path=bird_dir / "roa_dn42_v6.conf",
                    networkd_dir=tmp_path / "networkd",
                    nm_system_connections_dir=tmp_path / "nm",
                )

        assert config_path.exists()
        assert db_path.exists()
        assert result.config.node_id == "test-node"
        assert result.config.own_asn == 4242421234


class TestGenconf:
    def test_generates_configs(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        db_path = tmp_path / "test.sqlite3"
        bird_dir = tmp_path / "bird"
        bird_dir.mkdir()
        peers_dir = bird_dir / "peers"
        peers_dir.mkdir()
        networkd_dir = tmp_path / "networkd"
        networkd_dir.mkdir()
        nm_dir = tmp_path / "nm"
        nm_dir.mkdir()

        with patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None):
            with patch("sys.platform", "linux"):
                init_result = init_node(
                    config_path=config_path,
                    db_path=db_path,
                    node_id="test-node",
                    own_asn=4242421234,
                    router_id="172.23.0.1",
                    own_ipv6="fd42:4242:1234::1",
                    ownnet_v6="fd42:4242:1234::/48",
                    ownnetset_v6="[fd42:4242:1234::/48+]",
                    bird_conf_path=bird_dir / "bird.conf",
                    bird_peers_dir=peers_dir,
                    bird_babel_conf_path=bird_dir / "babel.conf",
                    bird_roa_v6_conf_path=bird_dir / "roa_dn42_v6.conf",
                    networkd_dir=networkd_dir,
                    nm_system_connections_dir=nm_dir,
                )

        with (
            patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
            patch("sys.platform", "linux"),
            patch("shutil.which", return_value=None),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            import io
            mock_resp = io.BytesIO(b"# ROA content\n")
            mock_resp.read = mock_resp.read
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = lambda s, *a: None
            mock_urlopen.return_value = mock_resp

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
        config_path = tmp_path / "config.toml"
        db_path = tmp_path / "test.sqlite3"
        bird_dir = tmp_path / "bird"
        bird_dir.mkdir()
        peers_dir = bird_dir / "peers"
        peers_dir.mkdir()
        bird_conf = bird_dir / "bird.conf"
        bird_conf.write_text("existing")

        with patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None):
            with patch("sys.platform", "linux"):
                init_result = init_node(
                    config_path=config_path,
                    db_path=db_path,
                    node_id="test-node",
                    own_asn=4242421234,
                    router_id="172.23.0.1",
                    own_ipv6="fd42:4242:1234::1",
                    ownnet_v6="fd42:4242:1234::/48",
                    ownnetset_v6="[fd42:4242:1234::/48+]",
                    bird_conf_path=bird_conf,
                    bird_peers_dir=peers_dir,
                    bird_babel_conf_path=bird_dir / "babel.conf",
                    bird_roa_v6_conf_path=bird_dir / "roa.conf",
                    networkd_dir=tmp_path / "networkd",
                    nm_system_connections_dir=tmp_path / "nm",
                )

        with (
            patch("dn42ctl.services.init_sys.ensure_dummy_interface", return_value=None),
            patch("sys.platform", "linux"),
        ):
            with pytest.raises(Dn42CtlError, match="未允许覆盖"):
                genconf(
                    config=init_result.config,
                    db_path=db_path,
                    overwrite_bird_conf=False,
                    overwrite_babel_conf=True,
                )
