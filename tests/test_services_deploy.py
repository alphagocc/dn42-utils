from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.deploy import deploy_daemon, deploy_web


def _checkout(tmp_path: Path, *, dist: tuple[str, ...] = ("admin", "peer", "assets")) -> Path:
    """A source checkout skeleton: web/package.json plus the built dist/ subdirs."""
    root = tmp_path / "repo"
    (root / "src" / "dn42ctl").mkdir(parents=True)
    web = root / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text("{}", encoding="utf-8")
    for name in dist:
        d = web / "dist" / name
        d.mkdir(parents=True)
        (d / "index.html").write_text(name, encoding="utf-8")
    return root


class TestDeployWeb:
    def test_builds_then_copies(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path)
        dest = tmp_path / "www"
        run = MagicMock()
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value="/usr/bin/pnpm"),
            patch("dn42ctl.services.deploy._run", run),
        ):
            result = deploy_web(dest=dest, api_base="https://api.example.com")

        assert result.action == "web"
        assert (dest / "admin" / "index.html").read_text() == "admin"
        assert len(result.changed_files) == 3
        assert "VITE_API_BASE=https://api.example.com" in result.notes

        install, build = run.call_args_list[0], run.call_args_list[1]
        assert install.args[0] == ["/usr/bin/pnpm", "install", "--frozen-lockfile"]
        assert build.args[0] == ["/usr/bin/pnpm", "build"]
        assert build.kwargs["env"]["VITE_API_BASE"] == "https://api.example.com"

    def test_skip_build_runs_no_subprocess(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path)
        run = MagicMock()
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value=None),
            patch("dn42ctl.services.deploy._run", run),
        ):
            result = deploy_web(dest=tmp_path / "www", skip_build=True)

        run.assert_not_called()
        assert len(result.changed_files) == 3

    def test_existing_target_is_replaced(self, tmp_path: Path) -> None:
        """A stale file from a previous deploy must not survive the copy."""
        root = _checkout(tmp_path)
        dest = tmp_path / "www"
        (dest / "admin").mkdir(parents=True)
        (dest / "admin" / "stale.js").write_text("old", encoding="utf-8")

        with patch("dn42ctl.services.deploy.pkg_root", return_value=root):
            deploy_web(dest=dest, skip_build=True)

        assert not (dest / "admin" / "stale.js").exists()

    def test_empty_dist_warns(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path, dist=("unrelated",))
        with patch("dn42ctl.services.deploy.pkg_root", return_value=root):
            result = deploy_web(dest=tmp_path / "www", skip_build=True)

        assert result.changed_files == []
        assert any("未复制文件" in w for w in result.warnings)

    def test_missing_dist_raises(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path, dist=())
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            pytest.raises(Dn42CtlError, match="dist/ 不存在"),
        ):
            deploy_web(dest=tmp_path / "www", skip_build=True)

    def test_missing_web_dir_raises(self, tmp_path: Path) -> None:
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=tmp_path / "empty"),
            pytest.raises(Dn42CtlError, match="找不到 web 目录"),
        ):
            deploy_web(dest=tmp_path / "www", skip_build=True)

    def test_missing_pnpm_raises(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path)
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value=None),
            pytest.raises(Dn42CtlError, match="未找到 pnpm"),
        ):
            deploy_web(dest=tmp_path / "www")


class TestDeployDaemon:
    def test_installs_with_uv_tool_env(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path)
        dest, tool_dir = tmp_path / "bin", tmp_path / "opt"
        run = MagicMock()
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value="/usr/bin/uv"),
            patch("dn42ctl.services.deploy._run", run),
            patch("dn42ctl.services.deploy.subprocess.check_output", return_value="abc123def456789\n"),
        ):
            result = deploy_daemon(dest=dest, tool_dir=tool_dir)

        assert result.action == "daemon"
        assert result.changed_files == [str(dest / "dn42ctl")]
        assert "注入 commit: abc123def456" in result.notes

        call = run.call_args_list[0]
        assert call.args[0] == ["/usr/bin/uv", "tool", "install", "--force", "--reinstall", str(root)]
        assert call.kwargs["env"]["UV_TOOL_BIN_DIR"] == str(dest)
        assert call.kwargs["env"]["UV_TOOL_DIR"] == str(tool_dir)

    def test_commit_stamp_is_removed_even_when_install_fails(self, tmp_path: Path) -> None:
        """Leaving the stamp behind would make the next `git status` dirty."""
        root = _checkout(tmp_path)
        stamp = root / "src" / "dn42ctl" / "_build_commit.txt"

        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value="/usr/bin/uv"),
            patch("dn42ctl.services.deploy._run", side_effect=Dn42CtlError("命令失败")),
            patch("dn42ctl.services.deploy.subprocess.check_output", return_value="abc123\n"),
            pytest.raises(Dn42CtlError),
        ):
            deploy_daemon(dest=tmp_path / "bin", tool_dir=tmp_path / "opt")

        assert not stamp.exists()

    def test_missing_git_warns_but_continues(self, tmp_path: Path) -> None:
        """Deploying from a tarball has no .git; that is not a failure."""
        root = _checkout(tmp_path)
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value="/usr/bin/uv"),
            patch("dn42ctl.services.deploy._run", MagicMock()),
            patch("dn42ctl.services.deploy.subprocess.check_output", side_effect=OSError("no git")),
        ):
            result = deploy_daemon(dest=tmp_path / "bin", tool_dir=tmp_path / "opt")

        assert any("无法获取 git commit" in w for w in result.warnings)
        assert result.notes == []

    def test_missing_uv_raises(self, tmp_path: Path) -> None:
        with (
            patch("dn42ctl.services.deploy.shutil.which", return_value=None),
            pytest.raises(Dn42CtlError, match="未找到 uv"),
        ):
            deploy_daemon(dest=tmp_path / "bin", tool_dir=tmp_path / "opt")


class TestRestorecon:
    def test_failure_becomes_a_warning(self, tmp_path: Path) -> None:
        """A wrong SELinux label shows up only as an nginx 403; do not drop it."""
        root = _checkout(tmp_path)

        def which(name: str) -> str | None:
            return "/usr/sbin/restorecon" if name == "restorecon" else None

        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", side_effect=which),
            patch("dn42ctl.services.deploy._run", side_effect=Dn42CtlError("exit=1")),
        ):
            result = deploy_web(dest=tmp_path / "www", skip_build=True)

        assert any("restorecon 失败" in w for w in result.warnings)

    def test_absent_restorecon_is_silent(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path)
        with (
            patch("dn42ctl.services.deploy.pkg_root", return_value=root),
            patch("dn42ctl.services.deploy.shutil.which", return_value=None),
        ):
            result = deploy_web(dest=tmp_path / "www", skip_build=True)

        assert result.warnings == []
