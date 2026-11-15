# -*- coding: utf-8 -*-
"""旧写法提示。

只做“读源码、按行报告”，不改文件；真正的改写交给 `compiler.migrate`。
放在这里而不是 lexer/parser 里，是为了不改变既有语法和诊断优先级：
老代码仍然按当前语法报错，只是旁边多一条“这是旧写法，怎么改”的提醒。
"""
from __future__ import annotations

import re

# 每条规则只报告，不负责改写。正则尽量避开变量名（如 `若干`）和注释。
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^[ \t]*若[ \t]"), "`若` 已改名 `如果`；可用 `python -m compiler.cli migrate <文件> --write` 自动改"),
    (re.compile(r"否则[ \t]+若[ \t]"), "`否则 若` 已改名 `否则 如果`；可用 `migrate --write` 自动改"),
    (re.compile(r"(?<![\w])输入[ \t]*\("), "`输入(` 已改名 `读入(`；可用 `migrate --write` 自动改"),
    (re.compile(r"(?<![\w])转JSON[ \t]*\("), "`转JSON(` 已改名 `生成JSON(`；它现在返回 `结果`，当文本用还要改成 `取到(生成JSON(...))`"),
    (re.compile(r"(?<!取到\()读文件[ \t]*\("), "`读文件` 现在返回 `结果`；当文本用要改成 `取到(读文件(...))`"),
    (re.compile(r"(?<!取到\()解析JSON[ \t]*\("), "`解析JSON` 现在返回 `结果`；当值用要改成 `取到(解析JSON(...))`"),
    (re.compile(r"(?<!取到\()生成JSON[ \t]*\("), "`生成JSON` 现在返回 `结果`；当文本用要改成 `取到(生成JSON(...))`"),
    (re.compile(r"\.(结尾是|开头是)[ \t]*\("), "旧字符串方法；改用 `以结尾(文本, 后缀)` / `以开头(文本, 前缀)`"),
    (re.compile(r"\.(替换|拆分|大写|是数字|居中)[ \t]*\("), "旧字符串方法；改用对应的内建函数（`替换` / `分割字符串` / `转大写` / `是数字` / `居中`）"),
    (re.compile(r"\.(追加|插入|排序)[ \t]*\("), "旧列表方法；`追加` / `插入` / `排序` 都返回新列表，要写成 `列表 = 追加(列表, 值)` 这类形式"),
]


def scan(source: str) -> list[tuple[int, str]]:
    """返回 [(行号, 提示)]；同一行同一条提示只保留一次。"""
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for pattern, message in _RULES:
        for m in pattern.finditer(source):
            line = source.count("\n", 0, m.start()) + 1
            key = (line, message)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return sorted(out)
