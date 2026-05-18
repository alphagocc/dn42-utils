from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from typer.testing import CliRunner

from dn42ctl.cli import app

NODE_A = "11111111-1111-4111-8111-111111111111"
SERVER = "http://[::1]:4242"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_args(db_path: Path, tmp_path: Path) -> list[str]:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
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
        "paths": {},
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
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(
                return_value=httpx.Response(200, json=_desired())
            )
            result = runner.invoke(
                app,
                [*base_args, "node", "pull", "--node-config-path", str(node_toml_path)],
            )
        assert result.exit_code == 0, result.output
        assert cache_db.exists()
        assert "rev-1" in result.output


class TestNodeApply:
    def test_apply_dry_run(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        cache_db = tmp_path / "node-cache.sqlite3"
        # Render target dirs in tmp_path.
        peers_dir = tmp_path / "peers"
        babel = tmp_path / "babel.conf"
        networkd_dir = tmp_path / "networkd"
        nm_dir = tmp_path / "nm"
        apply_overrides = (
            f'[apply]\npeers_dir = "{peers_dir}"\n'
            f'babel_conf_path = "{babel}"\nnetworkd_dir = "{networkd_dir}"\nnm_dir = "{nm_dir}"\n'
        )
        node_toml_path.write_text(
            f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n'
            f"{apply_overrides}[cache]\ndb_path = \"{cache_db}\"\n",
            encoding="utf-8",
        )
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(
                return_value=httpx.Response(200, json=_desired())
            )
            runner.invoke(app, [*base_args, "node", "pull", "--node-config-path", str(node_toml_path)])
        result = runner.invoke(
            app,
            [*base_args, "node", "apply", "--dry-run", "--node-config-path", str(node_toml_path)],
        )
        assert result.exit_code == 0, result.output
        # In dry-run nothing should be written
        assert not babel.exists()


class TestNodeOnce:
    def test_pull_then_apply(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        cache_db = tmp_path / "node-cache.sqlite3"
        babel = tmp_path / "babel.conf"
        node_toml_path.write_text(
            f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n'
            f'[apply]\nbabel_conf_path = "{babel}"\n'
            f'peers_dir = "{tmp_path / "peers"}"\n'
            f'networkd_dir = "{tmp_path / "networkd"}"\n'
            f'nm_dir = "{tmp_path / "nm"}"\n'
            f"[cache]\ndb_path = \"{cache_db}\"\n",
            encoding="utf-8",
        )
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/desired").mock(
                return_value=httpx.Response(200, json=_desired())
            )
            result = runner.invoke(
                app, [*base_args, "node", "once", "--node-config-path", str(node_toml_path)]
            )
        assert result.exit_code == 0, result.output
        assert babel.exists()


class TestNodeMissingConfig:
    def test_pull_no_config(self, runner: CliRunner, base_args: list[str], tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*base_args, "node", "pull", "--node-config-path", str(tmp_path / "nope.toml")],
        )
        assert result.exit_code != 0
