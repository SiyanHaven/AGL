# -*- coding: utf-8 -*-
"""旧示范源码迁移：只改已经确定改名的关键字/内建，不碰语义。

v0.31 前后冻结过一次名字。旧示范里常见的旧写法：
  `若`      → `如果`
  `输入(`   → `读入(`
  `转JSON(` → `生成JSON(`

`读文件(...)` 从 v0.30 起返回 `结果`，不能再直接当文本用；这个迁移器
**不自动加 `取到(...)`**（括号配对、嵌套调用都要看上下文），只在报告里
点名，交给人或 Agent 确认。这样迁移器永远不会静默改变程序语义。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Change:
    line: int
    old: str
    new: str
    reason: str


# 只匹配「行首的若 + 空白」和「否则 若」，避免把变量名 `若干` / `若水` 改掉。
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^([ \t]*)若(?=[ \t])", re.MULTILINE), r"\1如果", "旧条件关键字 `若` → `如果`"),
    (re.compile(r"(否则[ \t]+)若(?=[ \t])"), r"\1如果", "`否则 若` → `否则 如果`"),
    (re.compile(r"(?<![\w])输入(?=[ \t]*\()"), "读入", "旧内建 `输入` → `读入`"),
    (re.compile(r"(?<![\w])转JSON(?=[ \t]*\()"), "生成JSON", "旧内建 `转JSON` → `生成JSON`"),
]

# 只报告、不自动改：这些需要看返回值怎么用。
_REPORT_ONLY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<!取到\()(?<![\w])读文件(?=[ \t]*\()"),
     "`读文件` 现在返回 `结果`；直接当文本用要改成 `取到(读文件(...))`"),
    (re.compile(r"(?<!取到\()(?<![\w])写文件(?=[ \t]*\()"),
     "`写文件` 现在返回 `结果`；忽略返回值通常没问题，当值用时要用 `取到(...)`"),
    (re.compile(r"(?<!取到\()解析JSON(?=[ \t]*\()"),
     "`解析JSON` 现在返回 `结果`；直接当值用要改成 `取到(解析JSON(...))`"),
    (re.compile(r"(?<!取到\()生成JSON(?=[ \t]*\()"),
     "`生成JSON` 现在返回 `结果`；当文本用要改成 `取到(生成JSON(...))`"),
    (re.compile(r"\.(结尾是|开头是|替换|拆分|追加|排序|插入|大写|是数字|居中)\("),
     "旧方法调用；改用对应的内建函数（见 docs/旧示范兼容与迁移.md）"),
]


def migrate_text(text: str) -> tuple[str, list[Change], list[str]]:
    """返回 (新文本, 改动列表, 需要人工确认的事项)。"""
    changes: list[Change] = []
    warnings: list[str] = []

    for pattern, repl, reason in _RULES:
        def _sub(m: re.Match[str], reason: str = reason, pattern: re.Pattern[str] = pattern) -> str:
            line = text.count("\n", 0, m.start()) + 1
            old = m.group(0)
            new = m.expand(repl)
            changes.append(Change(line, old, new, reason))
            return new

        text = pattern.sub(_sub, text)

    for pattern, message in _REPORT_ONLY:
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            warnings.append(f"第 {line} 行：{message}")
    return text, changes, warnings


def migrate_file(path: Path, write: bool) -> tuple[list[Change], list[str]]:
    raw_bytes = path.read_bytes()
    bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    try:
        raw = raw_bytes.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        # 老 Windows 机器上可能存在 GBK/GB18030 源码；能读就按原编码写回，
        # 不因为迁移工具把注释里的中文弄成乱码。
        raw = raw_bytes.decode("gb18030")
        encoding = "gb18030"
        bom = False
    new, changes, warnings = migrate_text(raw)
    if write and new != raw:
        # 保留原文件换行风格和 BOM：CRLF 文件写回 CRLF，LF 文件写回 LF。
        newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
        out = new.replace("\r\n", "\n").replace("\n", newline)
        encoded = out.encode(encoding)
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + encoded)
    return changes, warnings
