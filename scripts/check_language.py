#!/usr/bin/env python3
"""按 AGENTS.md 的 Chinese Language Policy 校验中文文本。

词表硬编码在本文件,不去解析 AGENTS.md:规范在那里以自然语言列举,解析 Markdown 会
随排版变动而失效,而 hook 的行为必须可预期。每组下方标注对应的规范章节号,规范修订
时按章节号同步本文件。

命中分两级。拦截级是管理黑话,以及经逐条查证确属滥用的词。提示级是在正式出版物与
学术论文中通行的计算机领域术语——规范 1.5 自述的判断标准是"只在互联网社区和企业内部
流通"才算黑话,这些词不满足该标准,因此只提示、不阻断。分级依据详见
docs/architecture/language_check.md。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CHINESE = re.compile(r"[一-鿿]")
EXEMPT_MARK = "lang-ok"

TEXT_SUFFIXES = frozenset({".md", ".py", ".toml", ".yml", ".yaml", ".txt", ".j2", ".sql"})

# 这几个文件把禁用词当数据列举,校验它们等于校验词表自身。
SELF_REFERENTIAL = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "scripts/check_language.py",
        "tests/test_check_language.py",
        "docs/architecture/language_check.md",
    }
)

# 规范 1.1 禁用表达 + 1.6 禁用措辞。原文中的 "下面 [你/我/按]" 一类写法是模式,
# 这里展开成逐条字面量,避免正则把无关文本卷进来。
BLOCK_PHRASES: tuple[str, ...] = (
    "一句话回答就行",
    "一句话",
    "先说要点",
    "简明结论",
    "明确结论",
    "可落地",
    "可操作",
    "便于你",
    "直接可用",
    "下面你",
    "下面我",
    "下面按",
    "下面把你",
    "你现在",
    "你可以挑",
    "你可以选",
    "我接住",
    "如果让你觉得我",
    "你想要哪种",
    "我直接把",
    "你只需要",
    "二选一",
    "我不跟你",
    "你要我",
    "要是你",
    "如果你坚持",
    "但你得",
    "不需要你决定",
    "不需要立刻决定",
    "不需要你认同",
    "稳稳接住你",
    "你的问题是",
    "你的担忧是",
    "说明如下",
    "答复如下",
    "不涉及",
    "不说教",
    "不鸡汤",
    "而不是",
)

# 规范 1.5 中的管理黑话,以及查证后确属滥用的词。
# 「走」在既有文档里出现于「走 gpg」「稳态同步走」,规范用词是「使用」「经由」;
# 「落盘」「落库」应写「写入文件」「写入数据库」;「坑」应写「问题」。
BLOCK_TERMS: tuple[str, ...] = (
    "抓手",
    "赋能",
    "拉齐",
    "打通",
    "闭环",
    "沉淀",
    "透出",
    "心智",
    "触达",
    "倒逼",
    "口径",
    "落地",
    "落成",
    "收束",
    "工作流",
    "更稳",
    "聚焦",
    "结论",
    "走",
    "坑",
    "落盘",
    "落库",
)

# 规范 1.5 中在正式文献里通行的术语。「复现」与「重现」等义、「有感知」与「可察觉」
# 等义,两种写法都可接受,因此归入提示级。
WARN_TERMS: tuple[str, ...] = (
    "粒度",
    "链路",
    "对齐",
    "收敛",
    "迭代",
    "路径",
    "直接",
    "风险",
    "收紧",
    "定性",
    "复现",
    "感知",
    "稳",
)

# 含上述短词但本身正当的更长表达。匹配前先把它们抹去,否则误判会压过真实命中——
# 既有文档里「定性」的全部命中都来自「确定性、幂等」。
EXCLUSIONS: tuple[str, ...] = (
    "确定性",
    "不确定性",
    "稳定",
    "稳态",
    "稳健",
    "平稳",
    "走查",
    "迭代器",
    "内存对齐",
    "字节对齐",
)

LEVEL_BLOCK = "拦截"
LEVEL_WARN = "提示"

_MASK = "\x00"


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    level: str
    term: str
    excerpt: str

    def render(self) -> str:
        return f"{self.path}:{self.line_no}: [{self.level}] {self.term} — {self.excerpt}"


def _excerpt(line: str, index: int, term: str, width: int = 24) -> str:
    start = max(0, index - width)
    end = min(len(line), index + len(term) + width)
    text = line[start:end].strip()
    return f"…{text}…" if (start > 0 or end < len(line)) else text


def _consume(buffer: list[str], term: str) -> list[int]:
    """找出 term 在 buffer 中尚未被抹去的位置,命中后就地抹去。

    抹去是为了让同一段文字只归属一次:「更稳」命中拦截级后,提示级的「稳」不应再报。
    """
    positions: list[int] = []
    while True:
        joined = "".join(buffer)
        index = joined.find(term)
        if index < 0:
            return positions
        positions.append(index)
        buffer[index : index + len(term)] = [_MASK] * len(term)


def scan_line(line: str) -> list[tuple[str, str, int]]:
    """返回该行的命中,元素为 (级别, 命中词, 起始位置)。"""
    if EXEMPT_MARK in line or not CHINESE.search(line):
        return []

    buffer = list(line)
    for phrase in sorted(EXCLUSIONS, key=len, reverse=True):
        _consume(buffer, phrase)

    hits: list[tuple[str, str, int]] = []
    # 长短语先于短词,否则「下面把你」会被「你现在」一类的短词先拆开。
    for level, group in ((LEVEL_BLOCK, BLOCK_PHRASES), (LEVEL_BLOCK, BLOCK_TERMS), (LEVEL_WARN, WARN_TERMS)):
        for term in sorted(group, key=len, reverse=True):
            for index in _consume(buffer, term):
                hits.append((level, term, index))
    return hits


def _strip_commit_comments(text: str) -> str:
    """去掉 git 自动附加的注释与 --verbose 的 diff 段落。"""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("# ------------------------ >8"):
            break
        if line.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def check_text(text: str, path: str, *, is_commit_msg: bool = False) -> list[Finding]:
    if is_commit_msg:
        text = _strip_commit_comments(text)

    findings: list[Finding] = []
    lines = text.splitlines()

    for line_no, line in enumerate(lines, 1):
        for level, term, index in scan_line(line):
            findings.append(Finding(path, line_no, level, term, _excerpt(line, index, term)))

        if is_commit_msg and line.lstrip().startswith("- "):
            # 规范 1.8。Markdown 与 YAML 里这个写法必要,只有提交信息才算违规。
            findings.append(Finding(path, line_no, LEVEL_WARN, "行首 -", line.strip()))

        if not is_commit_msg and path.endswith(".md") and re.match(r"^#{1,6} .*[\da-z]\)", line):
            # 规范 1.8:标题中不得出现 1) a) 形式的括号编号。
            findings.append(Finding(path, line_no, LEVEL_WARN, "标题括号编号", line.strip()))

    if is_commit_msg:
        body = [line for line in lines if line.strip()]
        if body and body[-1].rstrip().endswith(("?", "？")):
            # 规范 1.9:必须以陈述结尾。
            findings.append(Finding(path, len(lines), LEVEL_BLOCK, "以提问结尾", body[-1].strip()))

    return findings


def _git(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def is_selectable(name: str) -> bool:
    return Path(name).suffix in TEXT_SUFFIXES and name not in SELF_REFERENTIAL


def collect_files(*, everything: bool) -> list[str]:
    names = _git("ls-files") if everything else _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [n for n in names if is_selectable(n)]


def report(findings: list[Finding], *, strict: bool) -> int:
    if strict:
        findings = [
            Finding(f.path, f.line_no, LEVEL_BLOCK, f.term, f.excerpt) if f.level == LEVEL_WARN else f for f in findings
        ]

    blocking = [f for f in findings if f.level == LEVEL_BLOCK]
    warning = [f for f in findings if f.level == LEVEL_WARN]

    for finding in findings:
        print(finding.render())

    if not findings:
        return 0

    print()
    print(f"中文语言规范校验:拦截 {len(blocking)} 处,提示 {len(warning)} 处。")

    if blocking:
        print()
        print("建议交由 sub agent 复查:把上述命中行连同 AGENTS.md 的 Chinese Language Policy")
        print("一并交给子代理,请它逐条给出符合规范的替换措辞,人工确认后再修改。")
        print("确需保留某行的写法时,在该行添加 lang-ok 标记;临时跳过校验用 git commit --no-verify。")
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 AGENTS.md 的中文语言规范校验文本")
    parser.add_argument("paths", nargs="*", help="待校验文件,省略时取 staged 内容")
    parser.add_argument("--all", action="store_true", dest="everything", help="校验仓库全部文本文件")
    parser.add_argument("--strict", action="store_true", help="把提示级一并升为拦截")
    parser.add_argument("--commit-msg", dest="commit_msg", help="校验提交信息文件")
    args = parser.parse_args(argv)

    findings: list[Finding] = []

    if args.commit_msg:
        content = Path(args.commit_msg).read_text(encoding="utf-8")
        findings += check_text(content, "提交信息", is_commit_msg=True)
    else:
        targets = args.paths or collect_files(everything=args.everything)
        for name in targets:
            path = Path(name)
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            findings += check_text(content, name)

    return report(findings, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
