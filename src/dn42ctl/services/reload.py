"""Make the daemons re-read their configuration after apply has written it out.

`node apply` only lands the desired state on disk; it does not itself make anything
take effect. Without this step a change is written correctly and then simply never
applies, until someone reloads by hand or the machine reboots.

This module runs exactly two commands, `networkctl reload` and `birdc configure`.
Both only make the corresponding daemon re-read its own configuration files; neither
adds or removes a route, so this does not violate the "never modify routing tables
automatically" constraint in `docs/spec.md`. `RouteTable=off` is still guaranteed by
the netdev template.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dn42ctl.constants import RELOAD_POLICY_AUTO, RELOAD_POLICY_NEVER, VALID_RELOAD_POLICIES

RELOAD_TIMEOUT = 15

NETWORKCTL_RELOAD = ["networkctl", "reload"]
# configure, not configure soft: soft does not correctly load newly added protocols.
BIRDC_CONFIGURE = ["birdc", "configure"]


@dataclass(frozen=True)
class ReloadAction:
    cmd: list[str]
    ok: bool
    output: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReloadResult:
    actions: list[ReloadAction] = field(default_factory=list)
    skipped: str | None = None  # why nothing ran (dry-run / policy / no changes)

    @property
    def warnings(self) -> list[str]:
        return [f"{' '.join(a.cmd)} 失败: {a.error}" for a in self.actions if not a.ok]


Runner = Callable[[list[str]], ReloadAction]


def default_runner(cmd: list[str]) -> ReloadAction:
    try:
        out = subprocess.check_output(  # noqa: S603 — cmd is a literal constant from this module
            cmd,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=RELOAD_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Best-effort: never raise. The agent is a long-running process and a missing
        # birdc must not crash-restart it. The files are already correctly on disk, so
        # this should surface as success-with-warnings.
        return ReloadAction(cmd=cmd, ok=False, error=str(exc))
    return ReloadAction(cmd=cmd, ok=True, output=out.strip())


def _under(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().parent == directory.resolve()
    except OSError:  # pragma: no cover — resolve can fail on an unusual filesystem
        return path.parent == directory


def plan_reloads(
    *,
    touched: Sequence[Path],
    networkd_dir: Path,
    bird_peers_dir: Path,
    bird_files: Sequence[Path] = (),
) -> list[list[str]]:
    """Decide which commands to run from the paths that were actually written or deleted.

    If nothing changed, nothing runs. Otherwise the agent's 900-second reconcile would
    mean a pointless `birdc configure` 96 times per node per day.

    The order is fixed: networkctl first so the interfaces come up, then birdc so it
    reads the protocols that reference those interfaces.
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
