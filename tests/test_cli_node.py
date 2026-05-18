from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from typer.testing import CliRunner

from dn42ctl.cli import app

NODE_A = "11111111-1111-4111-8111-111111111111"


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
    cfg_path = tmp_path / "config.toml"
    # Empty config file is enough for `node` subcommands (they don't require AppConfig).
    cfg_path.write_text("")
    return ["--db-path", str(db_path), "--config-path", str(cfg_path)]


class TestNodeAdd:
    def test_basic(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output

    def test_invalid_uuid(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "add", "bad-id", "--name", "x"])
        assert result.exit_code != 0


class TestNodeList:
    def test_empty(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "list"])
        assert result.exit_code == 0
        assert "(没有" in result.output

    def test_after_add(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "list"])
        assert result.exit_code == 0
        assert NODE_A in result.output


class TestNodeShow:
    def test_existing(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "show", NODE_A])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "write_policy" in result.output


class TestNodeRemove:
    def test_remove(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "remove", NODE_A])
        assert result.exit_code == 0
        assert "已删除" in result.output


class TestNodeTokenRotate:
    def test_prints_plaintext_once(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "token", "rotate", NODE_A])
        assert result.exit_code == 0, result.output
        assert "token (明文" in result.output


class TestNodePolicySet:
    def test_update(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(
            app,
            [*base_args, "node", "policy", "set", NODE_A, "--peer-add", "auto_accept"],
        )
        assert result.exit_code == 0, result.output
        assert "auto_accept" in result.output

    def test_no_options(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "policy", "set", NODE_A])
        assert result.exit_code != 0

    def test_invalid_peer_modify(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(
            app,
            [*base_args, "node", "policy", "set", NODE_A, "--peer-modify", "auto_accept"],
        )
        assert result.exit_code != 0
