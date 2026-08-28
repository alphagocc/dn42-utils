"""`scripts/check_language.py` 的行为约束。

校验器不属于 `dn42ctl` 包,经由 importlib 按文件位置载入。

这里最要紧的是子串排除:短词表在真实文档上跑,误判很容易压过真实命中——既有文档中
「定性」的全部命中都来自「确定性、幂等」。下面的用例把这条边界固定下来。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_language.py"
_spec = importlib.util.spec_from_file_location("check_language", _CHECKER)
assert _spec is not None and _spec.loader is not None
check_language = importlib.util.module_from_spec(_spec)
sys.modules["check_language"] = check_language
_spec.loader.exec_module(check_language)

BLOCK = check_language.LEVEL_BLOCK
WARN = check_language.LEVEL_WARN


def terms(text: str, *, level: str | None = None) -> set[str]:
    found = check_language.check_text(text, "样例.md")
    return {f.term for f in found if level is None or f.level == level}


class TestSubstringExclusion:
    """排除表必须让正当的长词吞掉其中的短词。"""

    @pytest.mark.parametrize(
        ("text", "absent"),
        [
            ("确定性、幂等地生成配置。", "定性"),
            ("不确定性来自并发写入。", "定性"),
            ("写入本地配置文件以保持稳定。", "稳"),
            ("稳态同步由常驻进程负责。", "稳"),
            ("代码走查安排在评审之后。", "走"),
            ("迭代器在此处一次性求值。", "迭代"),
            ("结构体按内存对齐排布。", "对齐"),
        ],
    )
    def test_longer_form_is_not_flagged(self, text: str, absent: str) -> None:
        assert absent not in terms(text)


class TestBlockingTerms:
    @pytest.mark.parametrize("text", ["需要一个抓手", "两边先拉齐口径", "让新地址落地", "配置只落盘不重载"])
    def test_jargon_blocks(self, text: str) -> None:
        assert terms(text, level=BLOCK)

    def test_misused_verb_blocks(self) -> None:
        """「走 gpg」的规范写法是「使用 gpg」。"""
        assert "走" in terms("签名校验走 gpg。", level=BLOCK)

    def test_longest_phrase_wins(self) -> None:
        """长短语先匹配,短词不得把它拆开。"""
        assert "下面把你" in terms("下面把你现在的配置导出。", level=BLOCK)


class TestWarningTerms:
    @pytest.mark.parametrize("text", ["默认路径指向 /etc", "高丢包链路建议 wireless", "按 peer 粒度存储"])
    def test_technical_terms_only_warn(self, text: str) -> None:
        assert terms(text, level=WARN)
        assert not terms(text, level=BLOCK)

    def test_reproduce_is_accepted_as_warning(self) -> None:
        """「复现」与「重现」等义,不拦截。"""
        assert "复现" in terms("可复现环境由 uv 锁定。", level=WARN)


class TestExemption:
    def test_lang_ok_skips_line(self) -> None:
        assert not terms("这里必须保留抓手二字 lang-ok")

    def test_ascii_only_line_skipped(self) -> None:
        assert not terms("AllowedIPs = fd42::/48")


class TestCommitMessage:
    def _check(self, text: str):
        return check_language.check_text(text, "提交信息", is_commit_msg=True)

    def test_trailing_question_blocks(self) -> None:
        found = self._check("fix: 修正心跳限流\n\n还需要我继续处理吗?")
        assert any(f.level == BLOCK and f.term == "以提问结尾" for f in found)

    def test_leading_dash_warns(self) -> None:
        found = self._check("fix: 修正心跳限流\n\n- 调整哨兵取值")
        assert any(f.level == WARN and f.term == "行首 -" for f in found)

    def test_git_comments_ignored(self) -> None:
        """git 自动附加的注释不该被当成正文。"""
        assert not self._check("fix: 修正心跳限流\n\n# 请输入提交信息,以抓手开头的行会被忽略")

    def test_clean_message_passes(self) -> None:
        assert not self._check("fix: 修正心跳限流的哨兵取值\n\n开机不足一分钟时首次心跳会被静默丢弃。")


class TestStrictMode:
    def test_strict_promotes_warnings(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = check_language.check_text("默认路径指向 /etc", "样例.md")
        assert check_language.report(findings, strict=False) == 0
        assert check_language.report(findings, strict=True) == 1

    def test_clean_text_reports_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert check_language.report([], strict=False) == 0
        assert capsys.readouterr().out == ""


class TestFileSelection:
    def test_self_referential_files_skipped(self) -> None:
        """词表文件必然列举禁用词,校验它们等于校验词表自身。"""
        assert not check_language.is_selectable("AGENTS.md")
        assert not check_language.is_selectable("scripts/check_language.py")

    def test_binary_and_unknown_suffixes_skipped(self) -> None:
        assert not check_language.is_selectable("assets/logo.png")
        assert check_language.is_selectable("docs/spec.md")
