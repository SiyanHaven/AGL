# -*- coding: utf-8 -*-
"""AGL 诊断中心 —— 全流水线唯一的报错出口。

纪律（不可违反）：
  1. 错误码只能取自下面 `Code` 里的常量，禁止在调用处临时拼字符串；
  2. 每条诊断必须能指向 AGL 源码的 file:line:col；
  3. 人看的消息（message_zh）里禁止出现宿主语言术语（AttributeError / NoneType / traceback）；
  4. 结构化输出（to_json）是 AI 自循环修复的唯一依据，字段一个都不能少。

这个模块不依赖任何其他 AGL 模块，任何模块都可以 import 它。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 错误码常量：新增一种错误，先在这里加常量，再去调用处使用
# ══════════════════════════════════════════════════════════════════════
class Code:
    # 词法 01xx
    NO_LANG_DECL = "AGL-E-0101"
    MIXED_BRANCH = "AGL-E-0102"
    UNCLOSED_STRING = "AGL-E-0103"
    BAD_CHAR = "AGL-E-0104"
    BAD_NUMBER = "AGL-E-0105"
    KW_STAGE = "AGL-E-0106"
    UNKNOWN_ESCAPE = "AGL-E-0107"
    RESERVED_UNDERSCORE = "AGL-E-0108"
    SEMI_INLINE = "AGL-E-0109"
    UNCLOSED_COMMENT = "AGL-E-0110"
    # 语法 02xx
    EXPECT = "AGL-E-0201"
    BRACE = "AGL-E-0202"
    NO_RETURN = "AGL-E-0203"
    DUP_PARAM = "AGL-E-0204"
    NEED_BLOCK = "AGL-E-0205"
    RECURSION_LIMIT = "AGL-E-0206"
    # 语义 03xx
    TYPE_MISMATCH = "AGL-E-0301"
    UNDEFINED = "AGL-E-0302"
    CONST_REASSIGN = "AGL-E-0303"
    UNINIT = "AGL-E-0304"
    CMP_TEXT_NUM = "AGL-E-0305"
    OUT_OF_SCOPE = "AGL-E-0306"
    DUP_DECL = "AGL-E-0307"
    NO_ENTRY = "AGL-E-0308"
    BUILTIN_ARITY = "AGL-E-0309"
    BUILTIN_ARG_TYPE = "AGL-E-0310"
    AWAIT_OUTSIDE_ASYNC = "AGL-E-0311"
    ASYNC_NOT_AWAITED = "AGL-E-0312"
    ENTRY_PARAMS = "AGL-E-0313"
    UNKNOWN_TYPE = "AGL-E-0314"
    TASK_ARG_NOT_ASYNC = "AGL-E-0315"
    EQ_MIXED_KIND = "AGL-E-0316"
    MATCH_DUP_ARM = "AGL-E-0317"
    MATCH_NOT_EXHAUSTIVE = "AGL-E-0318"
    IFACE_MEMBER = "AGL-E-0319"
    IFACE_MISSING = "AGL-E-0320"
    IFACE_SIGNATURE = "AGL-E-0321"
    # 效应 04xx
    PURE_CALLS_EFFECT = "AGL-E-0401"
    MISSING_EFFECT = "AGL-E-0402"
    CHECK_NOT_STATIC = "AGL-E-0403"
    DUP_EFFECT = "AGL-E-0404"
    # IR / 转译 05xx
    IR_NO_SPAN = "AGL-E-0501"
    IR_SCHEMA = "AGL-E-0502"
    UNSUPPORTED = "AGL-E-0503"
    # 模块 06xx
    MODULE_NOT_FOUND = "AGL-E-0601"
    CIRCULAR_IMPORT = "AGL-E-0602"
    NOT_EXPORTED = "AGL-E-0603"
    NO_SUCH_MEMBER = "AGL-E-0604"
    CONFIG_INVALID = "AGL-E-0605"
    # 运行时 RT
    RT_GENERIC = "AGL-RT-0001"
    RT_NULL_MEMBER = "AGL-RT-0007"
    RT_NO_MEMBER = "AGL-RT-0008"
    RT_DIV_ZERO = "AGL-RT-0010"
    RT_INDEX = "AGL-RT-0020"
    RT_KEY = "AGL-RT-0021"
    RT_NET = "AGL-RT-0030"
    RT_HTTP = "AGL-RT-0031"
    RT_NOFILE = "AGL-RT-0040"
    RT_PERM = "AGL-RT-0041"
    RT_SUBPROCESS = "AGL-RT-0050"
    RT_MODEL = "AGL-RT-0060"
    # FFI 黑盒调用（外部 Python 库）
    RT_FFI_MISSING = "AGL-RT-0070"
    RT_FFI_NO_MEMBER = "AGL-RT-0071"
    RT_FFI_ERROR = "AGL-RT-0072"
    RT_UNMAPPED = "AGL-RT-0099"


@dataclass
class FixHint:
    """一条可执行的修复建议。kind 供 AI 程序化识别，suggest 给人看。"""

    kind: str
    at: str
    suggest: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "at": self.at, "suggest": self.suggest}


@dataclass
class Diagnostic:
    code: str
    message_zh: str
    severity: str = "error"
    file: str = "<未知>"
    line: int = 1
    col: int = 0
    end_line: int = 0
    end_col: int = 0
    effects: list[str] = field(default_factory=list)
    stack: list[dict[str, Any]] = field(default_factory=list)
    cause: str | None = None
    fix_hints: list[FixHint] = field(default_factory=list)
    # 编译期附带的源码行，仅用于渲染，不进 JSON
    source_line: str | None = None

    # ── 结构化输出（给 AI）────────────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message_zh": self.message_zh,
            "agl": {
                "file": self.file,
                "line": self.line,
                "col": self.col,
                "end_col": self.end_col or self.col + 1,
            },
            "stack": self.stack,
            "effects": self.effects,
            "cause": self.cause,
            "fix_hints": [h.to_json() for h in self.fix_hints],
        }

    def to_json_text(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, indent=2)

    # ── 人看的渲染 ────────────────────────────────────────────────────
    def render(self) -> str:
        where = f"{self.file} 第 {self.line} 行"
        if self.end_col > self.col + 1:
            where += f"，第 {self.col + 1}–{self.end_col} 列"
        elif self.col >= 0:
            where += f"，第 {self.col + 1} 列"
        out = [f"{where}", ""]
        if self.source_line is not None:
            num = str(self.line)
            out.append(f"  {num} | {self.source_line}")
            caret_len = max(1, (self.end_col or self.col + 1) - self.col)
            out.append(f"  {' ' * len(num)} | {' ' * self.col}{'^' * caret_len}")
            out.append("")
        out.append(f"  问题    {self.message_zh}")
        if self.cause:
            out.append(f"  原因    {self.cause}")
        for i, h in enumerate(self.fix_hints):
            lead = "建议" if i == 0 else "    "
            out.append(f"  {lead}    {h.suggest}")
        return "\n".join(out)


class AglError(Exception):
    """携带诊断信息的异常。任何阶段都可以抛它，由 CLI 统一渲染。"""

    def __init__(self, diag: Diagnostic):
        super().__init__(diag.message_zh)
        self.diag = diag


def error(
    code: str,
    message_zh: str,
    *,
    file: str = "<未知>",
    line: int = 1,
    col: int = 0,
    end_col: int = 0,
    end_line: int = 0,
    effects: list[str] | None = None,
    cause: str | None = None,
    fix_hints: list[FixHint] | None = None,
    source_line: str | None = None,
) -> AglError:
    """构造并抛出 AglError。用法：raise error(Code.EXPECT, "…")"""
    return AglError(
        Diagnostic(
            code=code,
            message_zh=message_zh,
            file=file,
            line=line,
            col=col,
            end_col=end_col,
            end_line=end_line,
            effects=effects or [],
            cause=cause,
            fix_hints=fix_hints or [],
            source_line=source_line,
        )
    )
