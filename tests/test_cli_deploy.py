from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from dn42ctl.cli import app
from dn42ctl.cli_errors import EXIT_FAILURE
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.deploy import DeployResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestDeployWebCli:
    def test_passes_options_through(self, runner: CliRunner, tmp_path: Path) -> None:
        service = MagicMock(return_value=DeployResult(action="web"))
        with patch("dn42ctl.services.deploy.deploy_web", service):
            result = runner.invoke(
                app,
                ["deploy", "web", str(tmp_path / "www"), "--skip-build", "--api-base", "https://api.example.com"],
            )

        assert result.exit_code == 0, result.output
        service.assert_called_once_with(dest=tmp_path / "www", skip_build=True, api_base="https://api.example.com")

    def test_renders_the_result(self, runner: CliRunner, tmp_path: Path) -> None:
        service = MagicMock(
            return_value=DeployResult(
                action="web",
                changed_files=["dist/admin -> /var/www/admin"],
                notes=["VITE_API_BASE=https://api.example.com"],
                warnings=["restorecon 失败: exit=1"],
            )
        )
        with patch("dn42ctl.services.deploy.deploy_web", service):
            result = runner.invoke(app, ["deploy", "web", str(tmp_path / "www"), "--skip-build"])

        assert "部署完成" in result.output
        assert "dist/admin -> /var/www/admin" in result.output
        assert "VITE_API_BASE=https://api.example.com" in result.output
        assert "警告: restorecon 失败: exit=1" in result.output

    def test_service_error_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        """deploy declares no ctx parameter, so this also covers @cli_errors without one."""
        with patch("dn42ctl.services.deploy.deploy_web", side_effect=Dn42CtlError("未找到 pnpm")):
            result = runner.invoke(app, ["deploy", "web", str(tmp_path / "www")])

        assert result.exit_code == EXIT_FAILURE
        assert "错误: 未找到 pnpm" in result.output


class TestDeployDaemonCli:
    def test_defaults(self, runner: CliRunner) -> None:
        service = MagicMock(return_value=DeployResult(action="daemon"))
        with patch("dn42ctl.services.deploy.deploy_daemon", service):
            result = runner.invoke(app, ["deploy", "daemon"])

        assert result.exit_code == 0, result.output
        service.assert_called_once_with(dest=Path("/usr/local/bin"), tool_dir=Path("/opt/dn42ctl"))
        assert "已安装: /usr/local/bin/dn42ctl" in result.output

    def test_overrides(self, runner: CliRunner, tmp_path: Path) -> None:
        service = MagicMock(return_value=DeployResult(action="daemon"))
        with patch("dn42ctl.services.deploy.deploy_daemon", service):
            result = runner.invoke(
                app,
                ["deploy", "daemon", "--dest", str(tmp_path / "bin"), "--tool-dir", str(tmp_path / "opt")],
            )

        assert result.exit_code == 0, result.output
        service.assert_called_once_with(dest=tmp_path / "bin", tool_dir=tmp_path / "opt")
