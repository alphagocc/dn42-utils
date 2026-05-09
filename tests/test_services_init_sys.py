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
