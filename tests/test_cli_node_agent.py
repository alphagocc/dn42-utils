from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dn42ctl.cli import app
from dn42ctl.node_config import AgentOptions, NodeConfig, save_node_config
from dn42ctl.services.core import Dn42CtlError

NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_args(db_path: Path, tmp_path: Path) -> list[str]:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    return ["--db-path", str(db_path), "--config-path", str(cfg)]


@pytest.fixture
def node_toml(tmp_path: Path) -> Path:
    path = tmp_path / "node.toml"
    save_node_config(
        path,
        NodeConfig(
            server="https://hub.example",
            node_id=NODE_A,
            token="tok",
            cache_db_path=tmp_path / "cache.sqlite3",
        ),
    )
    return path


def _invoke(runner: CliRunner, base_args: list[str], node_toml: Path):
    return runner.invoke(app, [*base_args, "node", "agent", "--node-config-path", str(node_toml)])


class TestConfigValidation:
    def test_missing_node_toml_exits_2(self, runner: CliRunner, base_args: list[str], tmp_path: Path) -> None:
        """Not initialized is bad input, not a runtime failure."""
        result = _invoke(runner, base_args, tmp_path / "nope.toml")
        assert result.exit_code == 2
        assert "错误" in result.output

    def test_malformed_node_toml_exits_2(self, runner: CliRunner, base_args: list[str], tmp_path: Path) -> None:
        bad = tmp_path / "node.toml"
        bad.write_text('server = "s"\n', encoding="utf-8")  # missing node_id / token
        result = _invoke(runner, base_args, bad)
        assert result.exit_code == 2

    def test_bad_agent_block_exits_2(self, runner: CliRunner, base_args: list[str], tmp_path: Path) -> None:
        bad = tmp_path / "node.toml"
        bad.write_text(
            'server = "s"\nnode_id = "x"\ntoken = "y"\n[agent]\nreconnect_max_seconds = "soon"\n',
            encoding="utf-8",
        )
        result = _invoke(runner, base_args, bad)
        assert result.exit_code == 2
        assert "必须是数字" in result.output


class TestRunAgentWiring:
    def test_passes_the_config_path(
        self, runner: CliRunner, base_args: list[str], node_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path, not a loaded config — the agent re-reads it on every reconnect."""
        seen: dict = {}

        async def fake_run_agent(**kwargs):
            seen.update(kwargs)

        monkeypatch.setattr("dn42ctl.node_ws_agent.run_agent", fake_run_agent)
        result = _invoke(runner, base_args, node_toml)
        assert result.exit_code == 0, result.output
        assert seen["node_config_path"] == node_toml

    def test_agent_options_are_read_from_node_toml(self, node_toml: Path) -> None:
        """The CLI does not pass settings; the agent picks them up per reconnect."""
        from dn42ctl.node_config import load_node_config

        save_node_config(
            node_toml,
            NodeConfig(
                server="https://hub.example",
                node_id=NODE_A,
                token="tok",
                agent=AgentOptions(reconcile_interval_seconds=42.0),
            ),
        )
        assert load_node_config(node_toml).agent.reconcile_interval_seconds == 42.0


class TestExitCodes:
    @pytest.mark.parametrize(
        ("error", "exit_code", "message"),
        [(KeyboardInterrupt(), 0, "已停止"), (Dn42CtlError("写文件失败"), 1, "写文件失败")],
        ids=["keyboard-interrupt", "runtime-error"],
    )
    def test_agent_exit_status_and_message(
        self,
        runner: CliRunner,
        base_args: list[str],
        node_toml: Path,
        monkeypatch: pytest.MonkeyPatch,
        error: BaseException,
        exit_code: int,
        message: str,
    ) -> None:
        """`systemctl stop` is a clean stop, not a failure."""

        async def fake_run_agent(**_kwargs):
            raise error

        monkeypatch.setattr("dn42ctl.node_ws_agent.run_agent", fake_run_agent)
        result = _invoke(runner, base_args, node_toml)
        assert result.exit_code == exit_code
        assert message in result.output

    def test_config_error_during_reconnect_exits_2(
        self, runner: CliRunner, base_args: list[str], node_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """node.toml edited into an invalid state while the agent is running."""
        from dn42ctl.node_config import NodeConfigError

        async def fake_run_agent(**_kwargs):
            raise NodeConfigError("node.toml 不存在")

        monkeypatch.setattr("dn42ctl.node_ws_agent.run_agent", fake_run_agent)
        result = _invoke(runner, base_args, node_toml)
        assert result.exit_code == 2


class TestHelp:
    def test_agent_and_manual_sync_commands_are_listed(self, runner: CliRunner) -> None:
        """One-shot commands stay for manual troubleshooting."""
        result = runner.invoke(app, ["node", "--help"])
        assert result.exit_code == 0
        assert "agent" in result.output
        assert "once" in result.output
