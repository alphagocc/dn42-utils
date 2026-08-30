from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dn42ctl.cli import app

NODE_A = "11111111-1111-4111-8111-111111111111"
SERVER = "http://[::1]:4242"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_args(db_path: Path, tmp_path: Path) -> list[str]:
    """`--config-path` 指向一份把写入位置全部落在 tmp_path 的 config.toml。

    apply 的写入位置就取自这份文件;给一份空文件的话它会退回内置默认值,写到真实的 /etc。
    """
    from dn42ctl.config import AppConfig, save_config

    cfg = tmp_path / "config.toml"
    save_config(
        cfg,
        AppConfig(
            node_id=NODE_A,
            own_asn=4242421234,
            router_id="172.23.0.1",
            own_ipv6="fd42:4242:1234::1",
            ownnet_v6="fd42:4242:1234::/48",
            ownnetset_v6="[fd42:4242:1234::/48+]",
            bird_conf_path=str(tmp_path / "bird/bird.conf"),
            bird_peers_dir=str(tmp_path / "peers"),
            bird_babel_conf_path=str(tmp_path / "babel.conf"),
            bird_roa_v6_conf_path=str(tmp_path / "bird/roa_dn42_v6.conf"),
            bird_extra_conf_path=str(tmp_path / "bird/extra.conf"),
            networkd_dir=str(tmp_path / "networkd"),
            nm_system_connections_dir=str(tmp_path / "nm"),
            dummy_backend="networkd",
        ),
    )
    return ["--db-path", str(db_path), "--config-path", str(cfg)]


@pytest.fixture
def node_toml_path(tmp_path: Path) -> Path:
    return tmp_path / "node.toml"


def _desired(rev: str = "rev-1") -> dict:
    return {
        "node_id": NODE_A,
        "revision": rev,
        "generated_at": "2026-05-19T00:00:00+00:00",
        "bgp_peers": [],
        "ibgp_peers": [],
    }


class TestNodeInit:
    def test_writes_file(self, runner: CliRunner, base_args: list[str], node_toml_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                *base_args,
                "node",
                "init",
                "--server",
                SERVER,
                "--node-id",
                NODE_A,
                "--token",
                "tok",
                "--node-config-path",
                str(node_toml_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert node_toml_path.exists()
        text = node_toml_path.read_text()
        assert "server" in text
        assert NODE_A in text


class TestNodePull:
    def test_pull_writes_cache(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        cache_db = tmp_path / "node-cache.sqlite3"
        node_toml_path.write_text(
            f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n[cache]\ndb_path = "{cache_db}"\n',
            encoding="utf-8",
        )
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(return_value=httpx.Response(200, json=_desired()))
            result = runner.invoke(
                app,
                [*base_args, "node", "pull", "--node-config-path", str(node_toml_path)],
            )
        assert result.exit_code == 0, result.output
        assert cache_db.exists()
        assert "rev-1" in result.output


class TestNodeApply:
    def test_apply_dry_run(self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path) -> None:
        cache_db = tmp_path / "node-cache.sqlite3"
        babel = tmp_path / "babel.conf"
        node_toml_path.write_text(
            f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n[cache]\ndb_path = "{cache_db}"\n',
            encoding="utf-8",
        )
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(return_value=httpx.Response(200, json=_desired()))
            runner.invoke(app, [*base_args, "node", "pull", "--node-config-path", str(node_toml_path)])
        result = runner.invoke(
            app,
            [*base_args, "node", "apply", "--dry-run", "--node-config-path", str(node_toml_path)],
        )
        assert result.exit_code == 0, result.output
        assert not babel.exists()

    def test_no_reload_flag(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        cache_db = tmp_path / "node-cache.sqlite3"
        babel = tmp_path / "babel.conf"
        node_toml_path.write_text(
            f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n[cache]\ndb_path = "{cache_db}"\n',
            encoding="utf-8",
        )
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(return_value=httpx.Response(200, json=_desired()))
            runner.invoke(app, [*base_args, "node", "pull", "--node-config-path", str(node_toml_path)])
        result = runner.invoke(
            app,
            [*base_args, "node", "apply", "--no-reload", "--node-config-path", str(node_toml_path)],
        )
        assert result.exit_code == 0, result.output
        assert babel.exists()


class TestNodeOnce:
    def test_pull_then_apply(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        cache_db = tmp_path / "node-cache.sqlite3"
        babel = tmp_path / "babel.conf"
        node_toml_path.write_text(
            f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n[cache]\ndb_path = "{cache_db}"\n',
            encoding="utf-8",
        )
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(return_value=httpx.Response(200, json=_desired()))
            result = runner.invoke(app, [*base_args, "node", "once", "--node-config-path", str(node_toml_path)])
        assert result.exit_code == 0, result.output
        assert babel.exists()


class TestNodeMissingConfig:
    def test_pull_no_config(self, runner: CliRunner, base_args: list[str], tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*base_args, "node", "pull", "--node-config-path", str(tmp_path / "nope.toml")],
        )
        assert result.exit_code != 0
