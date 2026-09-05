from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dn42ctl.services.reload import (
    BIRDC_CONFIGURE,
    NETWORKCTL_RELOAD,
    ReloadAction,
    default_runner,
    plan_reloads,
    run_reloads,
)


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    networkd = tmp_path / "network"
    peers = tmp_path / "bird" / "peers"
    networkd.mkdir(parents=True)
    peers.mkdir(parents=True)
    return {
        "networkd": networkd,
        "peers": peers,
        "babel": tmp_path / "bird" / "babel.conf",
        "bird": tmp_path / "bird" / "bird.conf",
    }


class TestPlanReloads:
    def test_nothing_changed_runs_nothing(self, dirs: dict[str, Path]) -> None:
        """agent 每 900 秒 reconcile 一次;无脑 reload 会让每个节点每天无谓
        birdc configure 96 次。"""
        assert plan_reloads(touched=[], networkd_dir=dirs["networkd"], bird_peers_dir=dirs["peers"]) == []

    def test_bird_peer_file_triggers_birdc_only(self, dirs: dict[str, Path]) -> None:
        cmds = plan_reloads(
            touched=[dirs["peers"] / "dn42_0001.conf"],
            networkd_dir=dirs["networkd"],
            bird_peers_dir=dirs["peers"],
        )
        assert cmds == [BIRDC_CONFIGURE]

    @pytest.mark.parametrize("key", ["babel", "bird"])
    def test_named_bird_files_trigger_birdc(self, dirs: dict[str, Path], key: str) -> None:
        cmds = plan_reloads(
            touched=[dirs[key]],
            networkd_dir=dirs["networkd"],
            bird_peers_dir=dirs["peers"],
            bird_files=[dirs["babel"], dirs["bird"]],
        )
        assert cmds == [BIRDC_CONFIGURE]

    def test_order_is_networkctl_then_birdc(self, dirs: dict[str, Path]) -> None:
        """先起接口,再让 bird 读到引用这些接口的 protocol。"""
        cmds = plan_reloads(
            touched=[dirs["peers"] / "x.conf", dirs["networkd"] / "x.netdev"],
            networkd_dir=dirs["networkd"],
            bird_peers_dir=dirs["peers"],
        )
        assert cmds == [NETWORKCTL_RELOAD, BIRDC_CONFIGURE]

    def test_unrelated_path_triggers_nothing(self, dirs: dict[str, Path], tmp_path: Path) -> None:
        cmds = plan_reloads(
            touched=[tmp_path / "elsewhere" / "file.conf"],
            networkd_dir=dirs["networkd"],
            bird_peers_dir=dirs["peers"],
        )
        assert cmds == []

    def test_deleted_files_also_count(self, dirs: dict[str, Path]) -> None:
        """删除 stale 文件同样需要 reload —— 接口要被拆掉。"""
        cmds = plan_reloads(
            touched=[dirs["networkd"] / "gone.netdev"],
            networkd_dir=dirs["networkd"],
            bird_peers_dir=dirs["peers"],
        )
        assert cmds == [NETWORKCTL_RELOAD]


class TestRunReloads:
    def test_empty_plan_skips(self) -> None:
        result = run_reloads([])
        assert result.actions == []
        assert result.skipped == "无变更"

    def test_runner_is_injected(self) -> None:
        seen: list[list[str]] = []

        def fake(cmd: list[str]) -> ReloadAction:
            seen.append(cmd)
            return ReloadAction(cmd=cmd, ok=True, output="ok")

        result = run_reloads([NETWORKCTL_RELOAD, BIRDC_CONFIGURE], runner=fake)
        assert seen == [NETWORKCTL_RELOAD, BIRDC_CONFIGURE]
        assert all(a.ok for a in result.actions)
        assert result.warnings == []

    def test_failure_becomes_warning_not_exception(self) -> None:
        """常驻 agent 不能因为缺 birdc 就崩溃重启;文件已正确写入,应报 success-with-warnings。"""

        def failing(cmd: list[str]) -> ReloadAction:
            return ReloadAction(cmd=cmd, ok=False, error="command not found")

        result = run_reloads([BIRDC_CONFIGURE], runner=failing)
        assert result.actions[0].ok is False
        assert result.warnings == ["birdc configure 失败: command not found"]

    def test_partial_failure_still_runs_rest(self) -> None:
        def flaky(cmd: list[str]) -> ReloadAction:
            ok = cmd != NETWORKCTL_RELOAD
            return ReloadAction(cmd=cmd, ok=ok, error=None if ok else "boom")

        result = run_reloads([NETWORKCTL_RELOAD, BIRDC_CONFIGURE], runner=flaky)
        assert [a.ok for a in result.actions] == [False, True]


class TestDefaultRunner:
    def test_success(self) -> None:
        action = default_runner(["true"])
        assert action.ok is True

    @pytest.mark.parametrize("binary", ["dn42ctl-no-such-binary-xyz", "false"], ids=["missing-binary", "nonzero-exit"])
    def test_command_failure_is_captured(self, binary: str) -> None:
        action = default_runner([binary])
        assert action.ok is False
        assert action.error

    def test_timeout_is_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> str:
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr(subprocess, "check_output", boom)
        action = default_runner(["whatever"])
        assert action.ok is False
