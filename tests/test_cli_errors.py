from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from dn42ctl.cli_errors import EXIT_FAILURE, EXIT_USAGE, cli_errors, db_open_hint
from dn42ctl.config import ConfigError
from dn42ctl.context import AppContext
from dn42ctl.db import DatabaseError
from dn42ctl.node_config import NodeConfigError
from dn42ctl.services import Dn42CtlError
from dn42ctl.wg import WireGuardError

DB_PATH = Path("/var/lib/dn42ctl/dn42.sqlite3")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _app(exc: BaseException | None, *, with_context: bool = True) -> typer.Typer:
    """A one-command app whose body raises `exc`, wired like the real CLI."""
    app = typer.Typer(add_completion=False)

    @app.callback()
    def main(ctx: typer.Context) -> None:
        if with_context:
            ctx.obj = AppContext(config_path=Path("/etc/dn42ctl/config.toml"), db_path=DB_PATH)

    @app.command("boom")
    @cli_errors
    def cmd_boom(ctx: typer.Context, name: str = typer.Option("默认", "--name", help="名称")) -> None:
        typer.echo(f"name={name}")
        if exc is not None:
            raise exc

    return app


class TestExitCodes:
    """The mapping documented in docs/architecture/cli_errors.md."""

    @pytest.mark.parametrize(
        ("exc", "code", "prefix"),
        [
            (typer.BadParameter("不是数字"), EXIT_USAGE, "输入错误: "),
            (ConfigError("配置文件读取失败"), EXIT_USAGE, "错误: "),
            (NodeConfigError("node.toml 不存在"), EXIT_USAGE, "错误: "),
            (Dn42CtlError("渲染失败"), EXIT_FAILURE, "错误: "),
            (DatabaseError("表不存在"), EXIT_FAILURE, "错误: "),
            (WireGuardError("wg 不可用"), EXIT_FAILURE, "错误: "),
        ],
    )
    def test_maps_exception_to_code(self, runner: CliRunner, exc: Exception, code: int, prefix: str) -> None:
        result = runner.invoke(_app(exc), ["boom"])
        assert result.exit_code == code, result.output
        assert f"{prefix}{exc}" in result.output

    def test_subclass_of_service_error_is_handled(self, runner: CliRunner) -> None:
        """AutoPeerError and KioubitAuthError derive from Dn42CtlError."""
        from dn42ctl.services.auto_peer import AutoPeerExpiredError

        result = runner.invoke(_app(AutoPeerExpiredError("会话已过期")), ["boom"])
        assert result.exit_code == EXIT_FAILURE
        assert "错误: 会话已过期" in result.output

    def test_success_is_untouched(self, runner: CliRunner) -> None:
        result = runner.invoke(_app(None), ["boom", "--name", "abc"])
        assert result.exit_code == 0, result.output
        assert "name=abc" in result.output


class TestErrorsGoToStderr:
    def test_stdout_stays_clean(self, runner: CliRunner) -> None:
        """A piped `dn42ctl show ... | jq` must not receive the error text."""
        result = runner.invoke(_app(Dn42CtlError("渲染失败")), ["boom"])
        assert "错误: 渲染失败" in result.stderr
        assert "错误" not in result.stdout


class TestDatabaseHint:
    def test_hint_follows_the_error(self, runner: CliRunner) -> None:
        result = runner.invoke(_app(DatabaseError("打不开")), ["boom"])
        assert db_open_hint(DB_PATH) in result.output

    def test_no_context_means_no_hint(self, runner: CliRunner) -> None:
        """`deploy` commands declare no ctx parameter; the hint is then unavailable."""
        result = runner.invoke(_app(DatabaseError("打不开"), with_context=False), ["boom"])
        assert result.exit_code == EXIT_FAILURE
        assert "无法打开数据库" not in result.output


class TestPassThrough:
    def test_typer_exit_is_not_swallowed(self, runner: CliRunner) -> None:
        """cmd_system_install raises Exit(2) from inside a decorated body."""
        result = runner.invoke(_app(typer.Exit(EXIT_USAGE)), ["boom"])
        assert result.exit_code == EXIT_USAGE
        assert "错误" not in result.output

    def test_keyboard_interrupt_is_not_swallowed(self, runner: CliRunner) -> None:
        """`node agent` catches it itself to exit 0 on `systemctl stop`.

        Undecorated it reaches click, which turns it into the shell's 128+SIGINT.
        """
        result = runner.invoke(_app(KeyboardInterrupt()), ["boom"])
        assert result.exit_code == 130
        assert "错误" not in result.output

    def test_unknown_exception_still_surfaces(self, runner: CliRunner) -> None:
        """Anything unmapped stays a traceback rather than becoming a tidy exit 1."""
        result = runner.invoke(_app(ZeroDivisionError("boom")), ["boom"])
        assert isinstance(result.exception, ZeroDivisionError)


class TestSignatureIsPreserved:
    """functools.wraps keeps __wrapped__, which Typer's inspect.signature follows."""

    def test_help_still_lists_the_option(self, runner: CliRunner) -> None:
        result = runner.invoke(_app(None), ["boom", "--help"])
        assert result.exit_code == 0, result.output
        assert "--name" in result.output
        assert "名称" in result.output

    def test_default_still_applies(self, runner: CliRunner) -> None:
        result = runner.invoke(_app(None), ["boom"])
        assert "name=默认" in result.output
