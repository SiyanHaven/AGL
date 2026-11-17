# -*- coding: utf-8 -*-
"""从 spec/keywords.toml 生成 自举/内建表.agl（AGL 侧的内建名字表 + 效应表）。

单一真值源是 keywords.toml；本文件是生成的产物，手改无效。
doctor 的关键字表检查负责校验本文件与关键字表同步。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from compiler.lexer import KeywordTable  # noqa: E402

table = KeywordTable.load()
lines = [
    "#语言=中文",
    "// 本文件由 spec/keywords.toml 自动生成（scripts/gen_builtin_table.py）。",
    "// 供 AGL 侧校验器（自举/校验器.agl）判断「这是不是内建、它带什么效应」。",
    "// 中英双名都登记（IR 调用名可能以任一分支形态出现）。",
    "// 手改无效——改关键字表后重新生成；doctor 校验同步。",
    "",
    "模块 内建表",
    "",
    "导出 函数 是内建(名: 文本) -> 布尔 {",
]
pairs = []
for b in table.builtins:
    zh = b.zh.replace('"', '\\"')
    en = (b.en or "").replace('"', '\\"')
    eff = (b.effect or "").replace('"', '\\"')
    pairs.append((zh, en, eff))
    lines.append(f'  如果 名 等于 "{zh}" {{ 返回 真 }}')
    if en and en != zh:
        lines.append(f'  如果 名 等于 "{en}" {{ 返回 真 }}')
lines += ["  返回 假", "}", "", "导出 函数 内建效应(名: 文本) -> 文本 {"]
for zh, en, eff in pairs:
    lines.append(f'  如果 名 等于 "{zh}" {{ 返回 "{eff}" }}')
    if en and en != zh:
        lines.append(f'  如果 名 等于 "{en}" {{ 返回 "{eff}" }}')
lines += ['  返回 ""', "}", ""]

out = REPO / "自举" / "内建表.agl"
out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
print(f"生成 {out}: {len(table.builtins)} 个内建，{len(lines) + 2} 行")
