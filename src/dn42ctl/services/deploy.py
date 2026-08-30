"""dn42ctl deploy 的实现：构建并复制 Web UI，安装 daemon 到系统目录。

命令参考见 docs/commands/deploy.md。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dn42ctl.services.core import Dn42CtlError, ensure_dir

_WEB_ASSET_DIRS = ("admin", "peer", "assets")
_COMMIT_STAMP = Path("src/dn42ctl/_build_commit.txt")

__all__ = ["DeployResult", "deploy_daemon", "deploy_web", "pkg_root"]


@dataclass(frozen=True)
class DeployResult:
    action: str
    changed_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def pkg_root() -> Path:
    """仓库根目录，由本文件的位置反推。

    deploy 只能在源码检出中运行：uv tool install 要拿到仓库本身，deploy web 还要读
    web/package.json。装进 wheel 之后这个位置没有意义，也没有调用它的场景。
    """
    return Path(__file__).resolve().parents[3]


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    """执行子进程，输出继承到终端。

    与 services/system.py 的同名函数不同，这里刻意不捕获 stdout：pnpm build 与
    uv tool install 都是分钟级的命令，进度必须实时可见。
    """
    try:
        subprocess.run(cmd, cwd=None if cwd is None else str(cwd), env=env, check=True)
    except FileNotFoundError as exc:
        raise Dn42CtlError(f"命令不存在: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise Dn42CtlError(f"命令失败: {' '.join(cmd)} (exit={exc.returncode})") from exc


def _require_tool(name: str, *, hint: str = "") -> str:
    found = shutil.which(name)
    if found is None:
        raise Dn42CtlError(f"未找到 {name}{hint}")
    return found


def _restorecon(path: Path) -> list[str]:
    """重置 SELinux 标签，尽力而为。

    非 SELinux 系统上找不到 restorecon 属于常态，静默跳过。命令存在却失败则必须报出来：
    标签不正确的静态文件会让 nginx 读不到，现象只是 403，从那里反查很费时。
    """
    tool = shutil.which("restorecon")
    if tool is None:
        return []
    try:
        _run([tool, "-Rv", str(path)])
    except Dn42CtlError as exc:
        return [f"restorecon 失败: {exc}"]
    return []


def _replace_tree(src: Path, target: Path) -> None:
    try:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
    except OSError as exc:
        raise Dn42CtlError(f"复制失败: {src} -> {target} ({exc})") from exc


def _web_dir() -> Path:
    web = pkg_root() / "web"
    if not (web / "package.json").exists():
        raise Dn42CtlError(f"找不到 web 目录 ({web})，deploy web 需要在源码检出中运行")
    return web


def deploy_web(*, dest: Path, skip_build: bool = False, api_base: str = "") -> DeployResult:
    web_dir = _web_dir()
    dist_dir = web_dir / "dist"
    notes: list[str] = []

    if not skip_build:
        pnpm = _require_tool("pnpm", hint="，请先安装 (npm install -g pnpm)")
        build_env = {**os.environ}
        if api_base:
            build_env["VITE_API_BASE"] = api_base
            notes.append(f"VITE_API_BASE={api_base}")
        _run([pnpm, "install", "--frozen-lockfile"], cwd=web_dir)
        _run([pnpm, "build"], cwd=web_dir, env=build_env)

    if not dist_dir.exists():
        raise Dn42CtlError(f"dist/ 不存在 ({dist_dir})，请先运行构建")

    ensure_dir(dest)

    changed: list[str] = []
    for name in _WEB_ASSET_DIRS:
        src = dist_dir / name
        if not src.exists():
            continue
        _replace_tree(src, dest / name)
        changed.append(f"{src} -> {dest / name}")

    warnings: list[str] = []
    if not changed:
        warnings.append(f"{dist_dir} 里没有 {' / '.join(_WEB_ASSET_DIRS)} 中的任何一项，未复制文件")
    warnings.extend(_restorecon(dest))

    return DeployResult(action="web", changed_files=changed, notes=notes, warnings=warnings)


def _stamp_build_commit(root: Path, commit_file: Path) -> str | None:
    """把当前 commit 写进包内，让 uv tool install 一并打进 venv。

    取不到 commit 不算故障：从 tarball 部署时没有 .git。
    """
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        commit_file.write_text(commit, encoding="utf-8")
    except (subprocess.CalledProcessError, OSError):
        return None
    return commit


def deploy_daemon(*, dest: Path, tool_dir: Path) -> DeployResult:
    root = pkg_root()
    uv = _require_tool("uv")

    ensure_dir(dest)
    ensure_dir(tool_dir)

    notes: list[str] = []
    warnings: list[str] = []
    commit_file = root / _COMMIT_STAMP
    commit = _stamp_build_commit(root, commit_file)
    if commit is None:
        warnings.append("无法获取 git commit，安装出来的 dn42ctl 不带构建标记")
    else:
        notes.append(f"注入 commit: {commit[:12]}")

    env = {**os.environ, "UV_TOOL_BIN_DIR": str(dest), "UV_TOOL_DIR": str(tool_dir)}
    try:
        _run([uv, "tool", "install", "--force", "--reinstall", str(root)], env=env)
    finally:
        # 标记文件只为这次安装存在，留在检出里会污染下一次 git status。
        commit_file.unlink(missing_ok=True)

    installed = dest / "dn42ctl"
    warnings.extend(_restorecon(installed))
    return DeployResult(action="daemon", changed_files=[str(installed)], notes=notes, warnings=warnings)
