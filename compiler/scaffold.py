# -*- coding: utf-8 -*-
"""工程骨架：`aglc init` 生成的那几个文件。

骨架的目的不是"给个模板"，而是让拿到仓库的人/AI 在第一次跑之前就看过：
  · 入口长什么样（main.agl 真能跑）；
  · 工程配置有哪些固定字段（agl.toml）。
所以 doctor 会把骨架生成到临时目录并真的编译运行一遍 —— 骨架写坏了体检先红。
"""
from __future__ import annotations

from pathlib import Path

from compiler import card

ENTRY = "main.agl"

TOML = '''# AGL 工程配置（字段固定，不要自己加 key）

[project]
name = "{name}"
version = "0.1.0"
entry = "{entry}"
language = "{lang}"

[build]
backend = "python"
out_dir = "build"
ir_dir = "build/ir"
diff_dir = "build/diff"

[dependencies]

[tool]

[sandbox]
max_duration_sec = 60
max_memory_mb = 512
max_loop_iterations = 10000
'''

MAIN_ZH = '''#语言=中文
// 入口函数必须叫「主入口」，程序从这里开始

函数 主入口() {
  变量 名字 = "世界"
  打印("你好，", 名字)
}
'''

MAIN_EN = '''#lang=English
// the entry function must be main; execution starts here

fn main() {
  let name = "world"
  print("hello,", name)
}
'''

GUIDE_ZH = '''# 写码须知

1. 先读 `关键字速查.md`，再用 AGL 写代码。
2. 写完跑 `python -m compiler.cli check main.agl --json`，按 `fix_hints` 修到通过。
3. 名字（函数名、变量名、字符串、注释）用你自己的话写，机器不翻译、不改写。
4. 不要发明语法：速查卡里没有的写法就是没有。
'''

GUIDE_EN = '''# Before you write code

1. Read `关键字速查.md` (the keyword card) first, then write AGL.
2. Run `python -m compiler.cli check main.agl --json` and fix until `ok` is true,
   following each `fix_hints` entry.
3. Names (functions, variables, strings, comments) are yours; the machine never
   translates or rewrites them.
4. Do not invent syntax: if the card does not list it, it does not exist.
'''


def files(root: Path, lang: str) -> dict[str, str]:
    """骨架的全部文件：{文件名: 内容}。"""
    zh = lang == "zh"
    return {
        "agl.toml": TOML.format(name=root.name, entry=ENTRY, lang=lang),
        ENTRY: MAIN_ZH if zh else MAIN_EN,
        "关键字速查.md": card.build(lang),
        "写码须知.md": GUIDE_ZH if zh else GUIDE_EN,
    }


def write(root: Path, lang: str) -> tuple[list[str], list[str]]:
    """写骨架，已存在的文件不覆盖。返回 (新写的, 跳过的)。"""
    root.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    skipped: list[str] = []
    for name, content in files(root, lang).items():
        p = root / name
        if p.exists():
            skipped.append(name)
            continue
        p.write_text(content, encoding="utf-8")
        made.append(name)
    return made, skipped
