# -*- coding: utf-8 -*-
"""编辑器语法高亮（TextMate / VSCode）生成器。

为什么生成而不是手写：手写的高亮文件一定会和关键字表漂移 ——
表里加了词，高亮漏了，编辑器里就是"这个词不着色但编译器认它"，
人第一反应是"那我是不是写错了"。所以这张语法文件由 keywords.toml 生成，
`doctor` 会反过来验一遍：表里每个启用的词，语法文件里必须都能匹配上。

只做高亮，不做语义 —— 语义归 LSP（compiler/lsp.py），两边都不重复实现语言规则。
"""
from __future__ import annotations

import json

from compiler.lexer import KeywordTable

SCOPE_NAME = "source.agl"

# 关键字类别 → TextMate scope。scope 决定颜色，照常见主题的语义选：
#   storage.* 主题一定会着色；keyword.control 也是；support.* 一般偏亮色。
_SCOPE_BY_KIND = {
    "decl": "storage.type.agl",
    "modifier": "storage.modifier.agl",
    "stmt": "keyword.control.agl",
    "type": "support.type.primitive.agl",
    "effect": "storage.modifier.effect.agl",
    "literal": "constant.language.agl",
    "builtin": "support.function.builtin.agl",
}

# 中文词两侧的"词边界"：汉字连写没有空格，所以不能用 \b。
# 这里把汉字、字母、数字、下划线都算作词内字符，两侧都不许粘着它们。
_ZH_WORD = r"\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9_"

_COMMENT = {"match": r"//.*$", "name": "comment.line.double-slash.agl"}
_LANG_DECL = {
    "match": r"^\s*#(?:语言|語言|lang)=.*$",
    "name": "comment.line.directive.agl",
}
_STRING = {
    "begin": r'"',
    "end": r'"',
    "name": "string.quoted.double.agl",
    "patterns": [{"match": r"\\.", "name": "constant.character.escape.agl"}],
}
_NUMBER = {
    "match": r"\b\d+(?:\.\d+)?\b",
    "name": "constant.numeric.agl",
}
# 3.3 共用语法符号集合：两分支完全相同，不属于任何一侧的关键字。
_PUNCT = {
    "match": r"->|==|!=|>=|<=|&&|\|\||[+\-*/%!=<>(){}\[\],:.]",
    "name": "punctuation.separator.agl",
}


def _alt(words: list[str]) -> str:
    """把一批词拼成可读的正则分支：长的排前面，避免短词先吃掉长词前缀。

    同长度再按字典序兜底 —— 顺序必须**完全确定**，否则同一份表生成出来的
    语法文件每次都不一样，产物比对永远红（这里踩过：集合迭代顺序随哈希
    种子变化，同长度词的先后就飘了）。
    """
    return "|".join(sorted({w for w in words if w}, key=lambda w: (-len(w), w)))


def _zh_pattern(words: list[str]) -> str:
    alt = _alt(words)
    return rf"(?<![{_ZH_WORD}])(?:{alt})(?![{_ZH_WORD}])"


def _en_pattern(words: list[str]) -> str:
    alt = _alt(words)
    return rf"\b(?:{alt})\b"


def build(table: KeywordTable | None = None) -> dict:
    """生成完整的 TextMate 语法（JSON 版），中英两分支的启用的词都在里面。"""
    t = table or KeywordTable.load()
    live = [k for k in t.keywords if k.stage <= 1]

    patterns: list[dict] = [_LANG_DECL, _COMMENT, _STRING, _NUMBER]

    # ── 定义处：把名字摘出来单独着色（`函数 相加(` 里的「相加」）──
    # 必须在关键字规则之前，否则「函数」先被当普通关键字匹配掉，名字就漏了。
    decl_words = [k.zh for k in live if k.kind == "decl"]
    decl_words_en = [k.en for k in live if k.kind == "decl"]
    if decl_words or decl_words_en:
        both = _alt(decl_words + decl_words_en)
        patterns.append({
            "match": rf"(?:{both})\s+([^\s({{]{{1,64}})",
            "captures": {"1": {"name": "entity.name.function.agl"}},
        })

    # ── 关键字：按类别分条，各自一个 scope ─────────────────────────
    for kind, scope in _SCOPE_BY_KIND.items():
        zh = [k.zh for k in live if k.kind == kind]
        en = [k.en for k in live if k.kind == kind]
        if zh:
            patterns.append({"match": _zh_pattern(zh), "name": scope})
        if en:
            patterns.append({"match": _en_pattern(en), "name": scope})

    # ── 中文语句起始位置：汉字连写时关键字会粘住后面的名字 ───────────
    # 例：`变量名字 = 1`（合法，词法器按最长前缀切开）。上面那条带边界的
    # 规则匹配不到它，所以再补一条「行首 + 关键字前缀 + 紧接汉字」的规则。
    stmt_like = [k.zh for k in live if k.kind in ("decl", "stmt", "modifier")]
    if stmt_like:
        patterns.append({
            "match": rf"^(\s*)({_alt(stmt_like)})(?=[{_ZH_WORD}])",
            "captures": {"2": {"name": "keyword.control.agl"}},
        })

    # ── 运算符词（中文「大于」）与符号（英文 `>`）──────────────────
    op_words = [o["zh"] for o in t.ops if o.get("zh")]
    if op_words:
        patterns.append({
            "match": rf"(?<![{_ZH_WORD}])(?:{_alt(op_words)})(?![{_ZH_WORD}])",
            "name": "keyword.operator.word.agl",
        })

    # ── 内置只读属性：只认紧跟 `.` 的位置，用户的同名变量不着色 ──────
    attrs = [a.zh for a in t.attrs] + [a.en for a in t.attrs]
    if attrs:
        patterns.append({
            "match": rf"(?<=\.)(?:{_alt(attrs)})\b",
            "name": "variable.other.property.agl",
        })

    # ── 内建函数：不用定义、不用导入就能调，单独着色（否则与用户函数混成一色）──
    # 它们不是关键字，所以不能进上面那批规则；但用户写 `切分(…)` 时应该一眼看出
    # 这是语言给的，不是自己定义忘了写。
    bi_zh = [b.zh for b in t.builtins]
    bi_en = [b.en for b in t.builtins]
    if bi_zh:
        patterns.append({"match": _zh_pattern(bi_zh),
                         "name": "support.function.builtin.agl"})
    if bi_en:
        patterns.append({"match": _en_pattern(bi_en),
                         "name": "support.function.builtin.agl"})

    # ── 函数调用 ────────────────────────────────────────────────────
    patterns.append({
        "match": rf"[{_ZH_WORD}]+(?=\s*\()",
        "name": "entity.name.function.call.agl",
    })

    patterns.append(_PUNCT)

    return {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json",
        "name": "AGL",
        "scopeName": SCOPE_NAME,
        "fileTypes": ["agl"],
        "comment": f"由 spec/keywords.toml v{t.version} 生成，不要手改。改表后跑 `aglc grammar`。",
        "patterns": patterns,
    }


def dumps(table: KeywordTable | None = None) -> str:
    return json.dumps(build(table), ensure_ascii=False, indent=2) + "\n"


def build_language_config() -> dict:
    """VSCode 的编辑器行为配置：注释符、括号配对、自动缩进。"""
    return {
        "comments": {"lineComment": "//"},
        "brackets": [["{", "}"], ["[", "]"], ["(", ")"]],
        "autoClosingPairs": [
            {"open": "{", "close": "}"},
            {"open": "[", "close": "]"},
            {"open": "(", "close": ")"},
            {"open": '"', "close": '"', "notIn": ["string", "comment"]},
        ],
        "surroundingPairs": [["{", "}"], ["[", "]"], ["(", ")"], ['"', '"']],
        "indentationRules": {
            "increaseIndentPattern": r"\{\s*$",
            "decreaseIndentPattern": r"^\s*\}",
        },
        "wordPattern": rf"[{_ZH_WORD}]+",
    }


def write_into(root) -> list[str]:
    """把语法文件与编辑器配置写进一个 VSCode 扩展目录（返回写出的文件名）。"""
    from pathlib import Path

    root = Path(root)
    syn = root / "syntaxes"
    syn.mkdir(parents=True, exist_ok=True)
    (syn / "agl.tmLanguage.json").write_text(dumps(), encoding="utf-8")
    (root / "language-configuration.json").write_text(
        json.dumps(build_language_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return ["syntaxes/agl.tmLanguage.json", "language-configuration.json"]


# ══════════════════════════════════════════════════════════════════════
# 自证：语法文件能不能真的匹配上语言里的每一个词
# ══════════════════════════════════════════════════════════════════════
def _oniguruma_possible(word: str) -> bool:
    """该词能否被某个 pattern 匹配到（用 Python re 近似验证 TextMate 的正则）。"""
    import re

    t = KeywordTable.load()
    g = build(t)
    # 中文词需要上下文才能触发"词边界"，所以两侧各垫一个空格；
    # 英文词同理，用空格当边界最接近真实源码。
    probes = [f" {word} ", f"x.{word}", f" {word}(", f" {word}同"]
    for pat in g["patterns"]:
        rx = pat.get("match")
        if not rx:
            continue
        try:
            cre = re.compile(rx)
        except re.error:
            # Python 的 re 不认识 Oniguruma 的某些写法：这里不算失败，
            # 交给 doctor 的"至少能编译出语法文件 + 抽样匹配"来判断。
            continue
        if any(cre.search(p) for p in probes):
            return True
    return False


def audit(table: KeywordTable | None = None) -> list[str]:
    """返回问题清单（空 = 语法文件覆盖了表里所有启用的词）。"""
    t = table or KeywordTable.load()
    g = build(t)
    problems: list[str] = []

    if g["scopeName"] != SCOPE_NAME:
        problems.append(f"scopeName 必须是 {SCOPE_NAME}")
    if not g["patterns"]:
        problems.append("patterns 为空")

    live = [k for k in t.keywords if k.stage <= 1]
    for k in live:
        for word in (k.zh, k.en):
            if not _oniguruma_possible(word):
                problems.append(f"关键字 `{word}`（{k.kind}）没有被任何规则覆盖")
    for o in t.ops:
        if not _oniguruma_possible(o["zh"]):
            problems.append(f"运算符词 `{o['zh']}` 没有被覆盖")
    for a in t.attrs:
        for word in (a.zh, a.en):
            if not _oniguruma_possible(word):
                problems.append(f"内置属性 `{word}` 没有被覆盖")
    for b in t.builtins:
        for word in (b.zh, b.en):
            if not _oniguruma_possible(word):
                problems.append(f"内建函数 `{word}` 没有被覆盖")
    return problems
