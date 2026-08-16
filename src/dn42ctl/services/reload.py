"""apply 之后让守护进程重读配置。

在此之前 `node apply` 只落盘、从不 reload —— 改了地址会写进文件然后静静躺着不生效。

两条命令都**只是让守护进程重读配置文件**，不添加也不删除任何路由，因此不违反
`docs/spec.md` 的"禁止自动修改路由表"约束；`RouteTable=off` 仍由 netdev 模板保证。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

RELOAD_TIMEOUT = 15

NETWORKCTL_RELOAD = ["networkctl", "reload"]
# 用 configure 而非 configure soft —— soft 不能正确加载新增的 protocol。
BIRDC_CONFIGURE = ["birdc", "configure"]

RELOAD_POLICY_AUTO = "auto"
RELOAD_POLICY_NEVER = "never"
VALID_RELOAD_POLICIES = (RELOAD_POLICY_AUTO, RELOAD_POLICY_NEVER)


@dataclass(frozen=True)
class ReloadAction:
    cmd: list[str]
    ok: bool
    output: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReloadResult:
    actions: list[ReloadAction] = field(default_factory=list)
    skipped: str | None = None  # 未执行的原因(dry-run / policy / 无变更)

    @property
    def warnings(self) -> list[str]:
        return [f"{' '.join(a.cmd)} 失败: {a.error}" for a in self.actions if not a.ok]


Runner = Callable[[list[str]], ReloadAction]


def default_runner(cmd: list[str]) -> ReloadAction:
    try:
        out = subprocess.check_output(  # noqa: S603 — cmd 是本模块的字面量常量
            cmd,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=RELOAD_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # best-effort:失败绝不抛出。agent 是常驻进程,缺 birdc 不能让它崩溃重启;
        # 文件已经正确落盘,应当报 success-with-warnings。
        return ReloadAction(cmd=cmd, ok=False, error=str(exc))
    return ReloadAction(cmd=cmd, ok=True, output=out.strip())


def _under(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().parent == directory.resolve()
    except OSError:  # pragma: no cover — resolve 在异常文件系统上可能失败
        return path.parent == directory


def plan_reloads(
    *,
    touched: Sequence[Path],
    networkd_dir: Path,
    bird_peers_dir: Path,
    bird_files: Sequence[Path] = (),
) -> list[list[str]]:
    """按实际写入/删除的路径决定要跑哪些命令。

    什么都没变就一条都不跑 —— 否则 agent 的 900 秒 reconcile 会让每个节点每天无谓地
    `birdc configure` 96 次。

    顺序固定:先 networkctl 起接口,再 birdc 读到引用这些接口的 protocol。
    """
    paths = list(touched)
    cmds: list[list[str]] = []
    if any(_under(p, networkd_dir) for p in paths):
        cmds.append(list(NETWORKCTL_RELOAD))
    bird_targets = {f.resolve() if f.is_absolute() else f for f in bird_files}
    if any(_under(p, bird_peers_dir) or p in bird_targets for p in paths):
        cmds.append(list(BIRDC_CONFIGURE))
    return cmds


def run_reloads(cmds: Sequence[list[str]], *, runner: Runner | None = None) -> ReloadResult:
    if not cmds:
        return ReloadResult(skipped="无变更")
    run = runner or default_runner
    return ReloadResult(actions=[run(list(cmd)) for cmd in cmds])


__all__ = [
    "BIRDC_CONFIGURE",
    "NETWORKCTL_RELOAD",
    "RELOAD_POLICY_AUTO",
    "RELOAD_POLICY_NEVER",
    "VALID_RELOAD_POLICIES",
    "ReloadAction",
    "ReloadResult",
    "Runner",
    "default_runner",
    "plan_reloads",
    "run_reloads",
]
