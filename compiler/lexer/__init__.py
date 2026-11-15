# -*- coding: utf-8 -*-
"""M1 词法分析器。

设计要点（违反即破坏双分支同构）：
  1. **关键字只从 spec/keywords.toml 读**，代码里一个关键字都不许硬编码；
  2. 每个 Token 有 text（原文）和 canonical（规范形式）：中英两分支的 canonical 必须完全相同；
  3. 标识符 / 字符串 / 注释的 canonical == text，**绝不规范化、绝不翻译**；
  4. 首行必须声明语言，缺失即 AGL-E-0101 并终止（否则无法决定用哪套关键字表）；
  5. 同一文件出现另一分支的关键字即 AGL-E-0102（禁止中英混用）。

中文分词策略（本文件的核心难点）：
  汉字连写没有空格。做法是「位置敏感 + 最长前缀」：
    · 在语句起始位置（行首、`{`、`}` 之后）：把连续汉字串按最长关键字前缀切开
      → `变量和 = 1` 正确切成 `变量` + `和`
    · 但如果整词后面紧跟 `=` / `(` / `[` / `.` / `+=` 等赋值或调用后缀，
      整词优先当标识符（`当前行 = 1`、`函数名(…)`、`结果类型 = …` 不再被
      切成 `当` / `函数` / `结果` + 半个名字）；`变量` / `常量` 例外，仍按声明切分。
    · 在表达式位置（已有 token 之后）：整串要么是关键字，要么整体是标识符
      → `变量 函数名 = 1` 里的 `函数名` 不会被误切
  这条规则是所有模块的共同假设。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from compiler.diagnostics import Code, FixHint, error

SPEC_DIR = Path(__file__).resolve().parents[2] / "spec"
KEYWORDS_TOML = SPEC_DIR / "keywords.toml"

# Token 种类
KEYWORD = "KEYWORD"
IDENT = "IDENT"
INT = "INT"
FLOAT = "FLOAT"
STRING = "STRING"
OP = "OP"
NEWLINE = "NEWLINE"
COMMENT = "COMMENT"
EOF = "EOF"

LANG_ZH = "zh"
LANG_EN = "en"

# 语言声明的可接受写法
_LANG_DECL = {
    "中文": LANG_ZH, "zh": LANG_ZH, "chinese": LANG_ZH,
    "english": LANG_EN, "en": LANG_EN, "英文": LANG_EN,
}

# 全角 / 中文标点归一 —— 只在字符串外生效
_FULLWIDTH = {
    "（": "(", "）": ")", "｛": "{", "｝": "}", "［": "[", "］": "]",
    "，": ",", "：": ":", "；": ";", "＝": "=", "＋": "+", "－": "-",
    "＊": "*", "／": "/", "％": "%", "！": "!", "＜": "<", "＞": ">",
    "。": ".", "、": ",", "“": '"', "”": '"', "‘": "'", "’": "'",
    "　": " ", "\t": " ",
}

# 多字符符号，必须按长度降序匹配
_SYMBOLS = ["=>", "->", "+=", "-=", "*=", "/=", "%=", "==", "!=", ">=", "<=", "&&", "||",
            "+", "-", "*", "/", "%", "!", "=", "<", ">",
            "(", ")", "{", "}", "[", "]", ",", ":", ".", "?"]


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    canonical: str
    line: int
    col: int
    end_line: int
    end_col: int
    file: str
    offset: int = 0       # 源码字节偏移（内部用，供 convert 精确替换）
    end_offset: int = 0
    sym: bool = False     # 该 OP 是否由符号直接写成（convert 用）

    def to_json(self) -> dict:
        """对外的 8 个字段。offset/sym 只在内部使用，不导出。"""
        return {
            "kind": self.kind,
            "text": self.text,
            "canonical": self.canonical,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "file": self.file,
        }


@dataclass(frozen=True)
class KwEntry:
    zh: str
    en: str
    kind: str
    stage: int


@dataclass(frozen=True)
class AttrEntry:
    """语言内置的只读属性（`.长度` ↔ `.length`）。

    canonical 是跨分支统一的名字；lower_to 是转译成宿主语言时用的等价操作。
    """
    zh: str
    en: str
    canonical: str
    lower_to: str
    readonly: bool


@dataclass(frozen=True)
class BuiltinEntry:
    """语言自带的内建函数（`切分` ↔ `split`）。

    它们是**函数**，不是关键字：词法器按普通标识符扫描，语义层才认出来。
    这样用户仍然可以拿这些名字当变量名；而当他真的定义同名函数时，
    用户自己的定义优先（见 checker 的调用解析顺序）。
    """
    zh: str
    en: str
    canonical: str
    params: tuple[str, ...]
    rest: bool
    opt_tail: int
    ret: str
    effect: str
    lower: str
    may_fail: bool
    needs_await: bool
    summary_zh: str
    summary_en: str
    example: str

    def min_args(self) -> int:
        # 末尾可选的参数（`opt = N`）不参与最少个数
        return max(0, len(self.params) - self.opt_tail)

    def arity_text(self) -> str:
        n = len(self.params)
        return f"至少 {n}" if self.rest else f"{n}"


class KeywordTable:
    """spec/keywords.toml 的内存形态。全流水线共用，禁止各模块自己解析。"""

    _cache: "KeywordTable | None" = None

    def __init__(self, data: dict):
        self.version: str = data["meta"]["version"]
        self.keywords: list[KwEntry] = [
            KwEntry(k["zh"], k["en"], k["kind"], k["stage"]) for k in data["keyword"]
        ]
        self.ops: list[dict] = data.get("operator", [])
        self.attrs: list[AttrEntry] = [
            AttrEntry(a["zh"], a["en"], a.get("canonical", a["en"]),
                      a.get("lower_to", ""), bool(a.get("readonly", True)))
            for a in data.get("attribute", [])
        ]
        self._zh: dict[str, KwEntry] = {k.zh: k for k in self.keywords}
        self._en: dict[str, KwEntry] = {k.en: k for k in self.keywords}
        self._op_zh: dict[str, str] = {o["zh"]: o["en"] for o in self.ops}
        self._op_en: dict[str, str] = {o["en"]: o["zh"] for o in self.ops}
        self._attr_zh: dict[str, AttrEntry] = {a.zh: a for a in self.attrs}
        self._attr_en: dict[str, AttrEntry] = {a.en: a for a in self.attrs}
        self._attr_canon: dict[str, AttrEntry] = {a.canonical: a for a in self.attrs}
        self.builtins: list[BuiltinEntry] = [
            BuiltinEntry(
                zh=b["zh"], en=b["en"], canonical=b.get("canonical", b["en"]),
                params=tuple(b.get("params") or ()),
                rest=bool(b.get("rest", False)),
                opt_tail=int(b.get("opt", 0)),
                ret=b.get("ret", "空"),
                effect=b.get("effect", "") or "",
                lower=b.get("lower", b["en"]),
                may_fail=bool(b.get("may_fail", False)),
                needs_await=bool(b.get("needs_await", False)),
                summary_zh=b.get("summary_zh", ""),
                summary_en=b.get("summary_en", ""),
                example=b.get("example", ""),
            )
            for b in data.get("builtin", [])
        ]
        self._bi_zh: dict[str, BuiltinEntry] = {b.zh: b for b in self.builtins}
        self._bi_en: dict[str, BuiltinEntry] = {b.en: b for b in self.builtins}
        self._bi_canon: dict[str, BuiltinEntry] = {b.canonical: b for b in self.builtins}
        # 中文关键字按长度降序，供最长前缀匹配。
        # **只收当前阶段启用的词**：未启用的关键字不该占用用户的词汇空间。
        self._zh_by_len = sorted(
            [k.zh for k in self.keywords if k.stage <= 1], key=len, reverse=True
        )

    @classmethod
    def load(cls, path: Path = KEYWORDS_TOML) -> "KeywordTable":
        if cls._cache is None:
            with path.open("rb") as f:
                cls._cache = cls(tomllib.load(f))
        return cls._cache

    # ── 查询 ─────────────────────────────────────────────────────────
    def is_keyword(self, text: str, lang: str) -> bool:
        return text in (self._zh if lang == LANG_ZH else self._en)

    def other_branch_keyword(self, text: str, lang: str) -> bool:
        """text 是否是「另一分支」**当前阶段已启用**的关键字（用于禁混用检测）。

        必须过滤 stage：`配置` 是后续版本 的词，当前版本 里两分支都可以拿它当标识符，
        若不过滤，英文分支写 `let 配置 = …` 会被误报成混用。
        """
        tbl = self._en if lang == LANG_ZH else self._zh
        e = tbl.get(text)
        return e is not None and e.stage <= 1

    def canonical_of(self, text: str, lang: str) -> str | None:
        tbl = self._zh if lang == LANG_ZH else self._en
        e = tbl.get(text)
        return e.en if e else None

    def stage_of(self, text: str, lang: str) -> int:
        tbl = self._zh if lang == LANG_ZH else self._en
        return tbl[text].stage

    def op_canonical(self, text: str, lang: str) -> str | None:
        """运算符词 → 符号形式。中文「等于」→ `==`。"""
        return (self._op_zh if lang == LANG_ZH else self._op_en).get(text)

    def op_arity(self, canonical: str) -> str:
        """运算符是前缀（`!`）还是中缀（`>`）。转换时空格规则不同。"""
        if not hasattr(self, "_op_arity"):
            self._op_arity = {o["en"]: o.get("arity", "infix") for o in self.ops}
        return self._op_arity.get(canonical, "infix")

    def kind_of(self, canon: str) -> str | None:
        """按 canonical 反查关键字类别（decl/stmt/type/effect/literal/builtin/modifier）。"""
        if not hasattr(self, "_by_canon"):
            self._by_canon = {k.en: k for k in self.keywords}
        e = self._by_canon.get(canon)
        return e.kind if e else None

    def text_for(self, canon: str, lang: str) -> str:
        """按 canonical 反查当前分支的写法（报错与 convert 共用）。"""
        if not hasattr(self, "_by_canon"):
            self._by_canon = {k.en: k for k in self.keywords}
        e = self._by_canon.get(canon)
        if e is None:
            return canon
        return e.zh if lang == LANG_ZH else e.en

    def op_text_for(self, canon: str, lang: str) -> str:
        """按符号形式的 canonical 反查当前分支写法：「==」→ 中文「等于」/ 英文「==」。"""
        if lang == LANG_ZH:
            return self._op_en.get(canon, canon)
        return canon

    def all_texts(self) -> set[str]:
        return set(self._zh) | set(self._en)

    # ── 内置只读属性 ─────────────────────────────────────────────────
    def attr_entry(self, text: str) -> AttrEntry | None:
        """任意分支写法 → 属性定义。不是内置属性就返回 None。"""
        return self._attr_zh.get(text) or self._attr_en.get(text)

    def attr_text_for(self, canonical: str, lang: str) -> str:
        """属性的规范名 → 该分支写法（`length` → `长度` / `length`）。"""
        a = self._attr_canon.get(canonical)
        if a is None:
            return canonical
        return a.zh if lang == LANG_ZH else a.en

    def attr_lower_to(self, text: str) -> str | None:
        """属性在宿主语言里的等价操作名（`长度` → `len`）；没有映射返回 None。"""
        a = self.attr_entry(text)
        return a.lower_to if a and a.lower_to else None

    # ── 内建函数 ─────────────────────────────────────────────────────
    def builtin_entry(self, text: str) -> BuiltinEntry | None:
        """任意分支写法 → 内建函数定义。不是内建就返回 None。"""
        return self._bi_zh.get(text) or self._bi_en.get(text)

    def builtin_by_canon(self, canonical: str) -> BuiltinEntry | None:
        return self._bi_canon.get(canonical)

    def builtin_text_for(self, canonical: str, lang: str) -> str:
        """内建函数的规范名 → 该分支写法（`split` → `切分` / `split`）。"""
        b = self._bi_canon.get(canonical)
        if b is None:
            return canonical
        return b.zh if lang == LANG_ZH else b.en


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def _is_id_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha() or _is_cjk(ch)


def _is_id_part(ch: str) -> bool:
    return ch == "_" or ch.isalnum() or _is_cjk(ch)


class Lexer:
    def __init__(self, source: str, filename: str, table: KeywordTable | None = None):
        self.src = source
        self.file = filename
        self.table = table or KeywordTable.load()
        self.pos = 0
        self.line = 1
        self.col = 0
        self.tokens: list[Token] = []
        self.lang: str = ""
        self.at_stmt_start = True
        self.lines = source.splitlines()

    # ── 基础工具 ──────────────────────────────────────────────────────
    def _peek(self, k: int = 0) -> str:
        i = self.pos + k
        return self.src[i] if i < len(self.src) else ""

    def _advance(self, n: int = 1) -> None:
        for _ in range(n):
            if self.pos < len(self.src):
                if self.src[self.pos] == "\n":
                    self.line += 1
                    self.col = 0
                else:
                    self.col += 1
                self.pos += 1

    def _emit(self, kind: str, start: int, start_line: int, start_col: int,
              canonical: str | None = None, sym: bool = False) -> Token:
        text = self.src[start:self.pos]
        tok = Token(
            kind=kind,
            text=text,
            canonical=text if canonical is None else canonical,
            line=start_line,
            col=start_col,
            end_line=self.line,
            end_col=self.col,
            file=self.file,
            offset=start,
            end_offset=self.pos,
            sym=sym,
        )
        self.tokens.append(tok)
        if kind not in (NEWLINE, COMMENT):
            self.at_stmt_start = text in ("{", "}")
        return tok

    def _src_line(self, line: int) -> str | None:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1]
        return None

    def _fail(self, code: str, msg: str, line: int, col: int, end_col: int = 0,
              hints: list[FixHint] | None = None) -> None:
        raise error(code, msg, file=self.file, line=line, col=col,
                    end_col=end_col or col + 1, fix_hints=hints,
                    source_line=self._src_line(line))

    # ── 主流程 ────────────────────────────────────────────────────────
    def tokenize(self) -> list[Token]:
        self._read_lang_decl()
        self.at_stmt_start = True
        while self.pos < len(self.src):
            ch = self._peek()

            # 归一化（字符串内不归一，见 _read_string）
            norm = _FULLWIDTH.get(ch)
            if norm is not None:
                # 归一后按普通字符重新处理：直接替换 src 对应位置代价大，
                # 这里用「合成 token 文本」的方式处理符号类全角。
                if norm in _SYMBOLS or norm in "(){}[],:":
                    start, sl, sc = self.pos, self.line, self.col
                    self._advance(1)
                    self._emit(OP, start, sl, sc, canonical=norm, sym=True)
                    # 修正 text 为归一后的符号
                    self.tokens[-1] = Token(
                        kind=OP, text=norm, canonical=norm, line=sl, col=sc,
                        end_line=self.line, end_col=self.col, file=self.file,
                        offset=start, end_offset=self.pos, sym=True,
                    )
                    continue
                ch = norm  # 空白类

            if ch in " \t\r":
                self._advance(1)
                continue
            if ch == "\n":
                start, sl, sc = self.pos, self.line, self.col
                self._advance(1)
                self._emit(NEWLINE, start, sl, sc)
                self.at_stmt_start = True
                continue

            # 注释 // 到行尾
            if ch == "/" and self._peek(1) == "/":
                start, sl, sc = self.pos, self.line, self.col
                while self.pos < len(self.src) and self._peek() != "\n":
                    self._advance(1)
                self._emit(COMMENT, start, sl, sc)
                continue

            # 块注释 /* … */（可跨行，不嵌套；未闭合报错）
            if ch == "/" and self._peek(1) == "*":
                start, sl, sc = self.pos, self.line, self.col
                end = self.src.find("*/", self.pos + 2)
                if end == -1:
                    self._fail(Code.UNCLOSED_COMMENT,
                               "块注释 `/* … */` 没有闭合", sl, sc,
                               hints=[FixHint("close_block_comment", f"{self.file}:{sl}",
                                              "补上结尾的 `*/`")])
                while self.pos < end + 2:
                    self._advance(1)
                self._emit(COMMENT, start, sl, sc)
                # 块注释内部有换行时，它结束的位置就是新的语句起点
                if "\n" in self.src[start:self.pos]:
                    self.at_stmt_start = True
                continue

            # 分号：C 系习惯的「语句结束符」。AGL 用换行分隔语句，`;` 可省略；
            # 只允许写在行尾（后面只剩空白 / 注释 / `}` / 文件尾），
            # 行中再写 `;` 会打乱"一行一语句"的行号契约，直接报错。
            if ch == ";":
                start, sl, sc = self.pos, self.line, self.col
                i = self.pos + 1
                while i < len(self.src) and self.src[i] in " \t\r":
                    i += 1
                tail_ok = (i >= len(self.src) or self.src[i] == "\n"
                           or self.src[i] == "}")
                if not tail_ok and self.src[i] == "/" and i + 1 < len(self.src) \
                        and self.src[i + 1] in "/*":
                    tail_ok = True
                if tail_ok:
                    self._advance(1)
                    continue
                self._fail(Code.SEMI_INLINE,
                           "AGL 用换行分隔语句，`;` 只能写在语句末尾（可省略），"
                           "这里一行中间又写了 `;`",
                           sl, sc, sc + 1,
                           hints=[FixHint("drop_semi", f"{self.file}:{sl}",
                                          "删掉这个 `;`，用换行拆成两行")])

            if ch == '"':
                self._read_string()
                continue

            if ch.isdigit():
                self._read_number()
                continue

            if _is_id_start(ch) or (ch == "_" and not _is_id_part(self._peek(1))):
                self._read_word()
                continue

            # 符号
            matched = False
            for sym in _SYMBOLS:
                if self.src.startswith(sym, self.pos):
                    start, sl, sc = self.pos, self.line, self.col
                    self._advance(len(sym))
                    self._emit(OP, start, sl, sc, canonical=sym, sym=True)
                    matched = True
                    break
            if matched:
                continue

            self._fail(Code.BAD_CHAR, f"无法识别的字符 `{ch}`", self.line, self.col,
                       hints=[FixHint("remove_char", f"{self.file}:{self.line}",
                                      f"删掉这个字符，或把它改成半角符号")])

        self._emit(EOF, self.pos, self.line, self.col)
        return self.tokens

    # ── 语言声明 ──────────────────────────────────────────────────────
    def _read_lang_decl(self) -> None:
        first_line = self.lines[0] if self.lines else ""
        s = first_line.strip()
        lang = None
        if s.startswith("#语言=") or s.startswith("#語言="):
            lang = _LANG_DECL.get(s[4:].strip())
        elif s.startswith("#lang="):
            lang = _LANG_DECL.get(s[6:].strip().lower())
        if lang is None:
            self._fail(
                Code.NO_LANG_DECL,
                "文件第一行缺少语言声明。请写 `#语言=中文` 或 `#lang=English`",
                1, 0,
                hints=[FixHint("add_lang_decl", f"{self.file}:1",
                               "在第一行补 `#语言=中文`（中文分支）或 `#lang=English`（英文分支）")],
            )
        self.lang = lang
        # 消费首行
        while self.pos < len(self.src) and self._peek() != "\n":
            self._advance(1)
        if self.pos < len(self.src):
            start, sl, sc = self.pos, self.line, self.col
            self._advance(1)
            self._emit(NEWLINE, start, sl, sc)

    # ── 字符串 ────────────────────────────────────────────────────────
    def _read_string(self) -> None:
        start, sl, sc = self.pos, self.line, self.col
        triple = self._peek() == '"' and self._peek(1) == '"' and self._peek(2) == '"'
        if triple:
            self._advance(3)
            close_len = 3
        else:
            self._advance(1)
            close_len = 1
        while True:
            if self.pos >= len(self.src):
                self._fail(Code.UNCLOSED_STRING,
                           f"字符串没有闭合，从第 {sl} 行第 {sc + 1} 列开始", sl, sc,
                           hints=[FixHint("close_quote", f"{self.file}:{sl}", "补上结尾的双引号")])
            ch = self._peek()
            if ch == "\\":
                nxt = self._peek(1)
                if nxt not in ('"', "\\", "n", "t", "r"):
                    self._fail(
                        Code.UNKNOWN_ESCAPE,
                        f"字符串里有无法识别的转义 `\\{nxt}`：AGL 只认 `\\\\` `\\\"` "
                        "`\\n` `\\t` `\\r`，其他一律报错（反斜杠不会帮你偷偷去掉）",
                        sl, sc,
                        hints=[FixHint("escape", f"{self.file}:{sl}",
                                       "想写反斜杠就写 `\\\\`；换行用 `\\n`，制表用 `\\t`")],
                    )
                self._advance(2)
                continue
            if ch == "\n" and not triple:
                self._fail(Code.UNCLOSED_STRING,
                           f"字符串没有闭合，从第 {sl} 行第 {sc + 1} 列开始", sl, sc,
                           hints=[FixHint("close_quote", f"{self.file}:{sl}",
                                          "字符串不能跨行；补引号或改用拼接")])
            if ch == '"':
                if close_len == 3 and not (self._peek(1) == '"' and self._peek(2) == '"'):
                    self._advance(1)
                    continue
                self._advance(close_len)
                break
            self._advance(1)
        # 字符串文本原样保留（canonical == text）
        self._emit(STRING, start, sl, sc)

    # ── 数字 ──────────────────────────────────────────────────────────
    def _read_number(self) -> None:
        start, sl, sc = self.pos, self.line, self.col
        while self._peek().isdigit():
            self._advance(1)
        is_float = False
        if self._peek() == "." and self._peek(1).isdigit():
            is_float = True
            self._advance(1)
            while self._peek().isdigit():
                self._advance(1)
        # 科学计数法：1e5 / 1.5e-3 / 2E+10（v0.31 起支持，与 Python 字面量一致）
        if self._peek() in "eE" and (
                self._peek(1).isdigit()
                or (self._peek(1) in "+-" and self._peek(2).isdigit())):
            is_float = True
            self._advance(1)
            if self._peek() in "+-":
                self._advance(1)
            while self._peek().isdigit():
                self._advance(1)
        # 1.2.3 这类
        if self._peek() == "." and self._peek(1).isdigit():
            while self._peek() in "0123456789.":
                self._advance(1)
            self._fail(Code.BAD_NUMBER, f"数字格式不合法 `{self.src[start:self.pos]}`",
                       sl, sc, hints=[FixHint("fix_number", f"{self.file}:{sl}", "一个数字里只能有一个小数点")])
        # 12abc 这类
        if _is_id_part(self._peek()):
            while _is_id_part(self._peek()):
                self._advance(1)
            self._fail(Code.BAD_NUMBER, f"数字格式不合法 `{self.src[start:self.pos]}`",
                       sl, sc,
                       hints=[FixHint("fix_number", f"{self.file}:{sl}",
                                      "数字后面不能紧贴标识符；加空格，或把名字改成合法写法")])
        self._emit(FLOAT if is_float else INT, start, sl, sc)

    # ── 单词（标识符 / 关键字 / 运算符词）──────────────────────────────
    def _read_word(self) -> None:
        start, sl, sc = self.pos, self.line, self.col
        while _is_id_part(self._peek()):
            self._advance(1)
        word = self.src[start:self.pos]

        # 1) 运算符词优先（等于 / 不等于 / 大于等于 …）
        op = self.table.op_canonical(word, self.lang)
        if op is not None:
            self._emit(OP, start, sl, sc, canonical=op)
            return

        # 2) 整串命中关键字
        if self.table.is_keyword(word, self.lang):
            if self.table.stage_of(word, self.lang) <= 1:
                self._emit_keyword(word, start, sl, sc)
                return
            # stage > 1 的关键字在当前阶段**不占用词汇**，按普通标识符处理。
            # 理由：`配置`、`入参`、`依赖` 都是日常中文词，当前版本 却因为
            # 一个尚未实现的功能不许用户拿它当变量名，这是不可接受的体验。
            # 代价：将来启用这些词时，曾用作标识符的代码需要改名。
            self._emit(IDENT, start, sl, sc)
            return

        # 3) 禁混用：整串是另一分支的关键字
        if self.table.other_branch_keyword(word, self.lang):
            want = self.table.canonical_of(word, LANG_EN if self.lang == LANG_ZH else LANG_ZH)
            # 反查另一分支的写法
            other = self._other_text(word)
            self._fail(
                Code.MIXED_BRANCH,
                f"{'中文' if self.lang == LANG_ZH else '英文'}分支里出现了"
                f"{'英文' if self.lang == LANG_ZH else '中文'}关键字 `{word}`，"
                f"本文件已声明为{'中文' if self.lang == LANG_ZH else '英文'}",
                sl, sc, sc + len(word),
                hints=[FixHint("replace_keyword", f"{self.file}:{sl}",
                               f"改成 `{other}`；或把首行语言声明改成另一分支")],
            )

        # 4) 中文语句起始位置：最长前缀切分（`变量和` → `变量` + `和`）
        #    但整词若本身就是内建名（`失败原因`、`当前时间戳`），
        #    会被下面的前缀切分拦腰截成 `失败` + `原因`——先认整串。
        if self.lang == LANG_ZH and self.at_stmt_start:
            if word in self.table._bi_zh:
                self._emit(IDENT, start, sl, sc)
                return
            # 3.5) 先看整词后面紧跟什么：`当前行 = 1`、`函数名(…)`、
            #      `结果类型 = …` 这类「关键字前缀 + 赋值/调用/下标/成员」，
            #      整词是用户标识符，不是语句关键字。只有真正的语句关键字
            #      （`当 条件 {`、`如果 条件 {`、`函数 名(…)` 带空格）才切分。
            #      `变量X = 1` / `常量X = 1` 也应整词当标识符：声明必须写成
            #      `变量 X = 1`（关键字后带空格），不能靠“关键字前缀”猜声明。
            tail = self.src[self.pos:].lstrip(" \t")
            if (tail.startswith("(") or tail.startswith("[") or tail.startswith(".")
                    or tail.startswith("+=") or tail.startswith("-=")
                    or tail.startswith("*=") or tail.startswith("/=")
                    or tail.startswith("%=") or tail.startswith("=")):
                for kw0 in self.table._zh_by_len:
                    if word.startswith(kw0) and len(word) > len(kw0):
                        self._emit(IDENT, start, sl, sc)
                        return
            for kw in self.table._zh_by_len:
                if word.startswith(kw) and len(word) > len(kw):
                    # 内建名 / 字面量前缀（打印…、真…、空…）常常只是用户标识符的
                    # 前两个字：`打印纸 = 1` 里 `打印纸` 是一个变量名，不该被切成
                    # `打印` + `纸`。走到这里就说明整词不是关键字、每个字符又都是
                    # 合法标识符字符 —— 先把整词当标识符收下。
                    # （整串内建 `失败原因`、`当前时间戳` 在上面已整体识别，不受影响；
                    #  语句/声明类关键字前缀如 `如果x`、`变量和` 不在豁免范围，切分照旧。）
                    ke = self.table._zh[kw]
                    if ke.kind in ("literal", "builtin") or ke.en == "Null":
                        break
                    rest_start = start + len(kw)
                    # 先把游标拨回切分点再发 token：否则 token 的 text/offset 会盖过
                    # 整个原词，与后面 token 的区间重叠，convert 按区间替换时会把下一个
                    # token 吞掉（`真空` 转出 `trueull`、`打印纸` 转出 `print = 1`）。
                    self.pos = rest_start
                    self.col = sc + len(kw)
                    self._emit_keyword(kw, start, sl, sc)
                    # 剩下部分重新扫描成标识符 / 关键字
                    self.at_stmt_start = False
                    self._read_word()
                    return

        # `_` 作为标识符放行：§3.3 匹配通配符 / 丢弃绑定的语义由
        # parser/checker 在上下文里执行（匹配模式允许，声明位置拒绝）。
        self._emit(IDENT, start, sl, sc)

    def _other_text(self, word: str) -> str:
        """给混用报错用：word 是另一分支的写法，返回**当前分支**对应的写法。"""
        e = self.table._en.get(word) if self.lang == LANG_ZH else self.table._zh.get(word)
        if e is not None:
            return e.zh if self.lang == LANG_ZH else e.en
        for k in self.table.keywords:
            if word in (k.zh, k.en):
                return k.zh if self.lang == LANG_ZH else k.en
        return word

    def _emit_keyword(self, word: str, start: int, sl: int, sc: int) -> None:
        stage = self.table.stage_of(word, self.lang)
        if stage > 1:
            self._fail(
                Code.KW_STAGE,
                f"关键字 `{word}` 在当前阶段未启用（需阶段 {stage}）",
                sl, sc, sc + len(word),
                hints=[FixHint("use_basic_syntax", f"{self.file}:{sl}",
                               "改用当前版本 的基础写法，或等阶段升级后再用")],
            )
        canon = self.table.canonical_of(word, self.lang) or word
        self._emit(KEYWORD, start, sl, sc, canonical=canon)


def tokenize(source: str, filename: str, table: KeywordTable | None = None) -> list[Token]:
    """对外唯一入口。返回含 EOF 的完整 Token 流。"""
    return Lexer(source, filename, table).tokenize()


def detect_lang(source: str) -> str | None:
    """只看首行判断语言，不做完整词法。供 CLI 决定展示口径。"""
    if not source:
        return None
    s = source.splitlines()[0].strip()
    if s.startswith("#语言=") or s.startswith("#語言="):
        return _LANG_DECL.get(s[4:].strip())
    if s.startswith("#lang="):
        return _LANG_DECL.get(s[6:].strip().lower())
    return None
