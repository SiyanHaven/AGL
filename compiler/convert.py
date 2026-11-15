# -*- coding: utf-8 -*-
"""双分支转换与结构比对。

转换的边界（这是本文件唯一的规则，不许越界）：
  · **只替换关键字与运算符的表层写法**；
  · 标识符、字符串、注释、数字 **一个字都不动**；
  · 因此 `中文 → 英文 → 中文` 必然逐字还原（可逆性由本文件的实现保证）。

结构比对（`aglc compare`）解决的是另一个问题：
  两个人分别写的中英两份代码，**逻辑是否等价**？
  做法是 alpha 等价：把用户标识符按首次出现顺序编号后比较骨架，
  这样 `相加(甲, 乙)` 和 `add(a, b)` 会被判定为同一个形状。
"""
from __future__ import annotations

from compiler.diagnostics import Code, FixHint, error
from compiler.lexer import (
    COMMENT, EOF, IDENT, KEYWORD, LANG_ZH, NEWLINE, OP, KeywordTable,
    _is_id_part, detect_lang, tokenize,
)

DECL_ZH = "#语言=中文"
DECL_EN = "#lang=English"


def _user_names(tokens) -> set[str]:
    """收集"作者自己起的名字"：函数名、变量名、常量名、参数名。

    这些名字转换时一个字都不许动。收集它们还有个更重要的用途：
    用户完全可以定义一个叫 `split` 的函数（内建函数名不是关键字），
    那时它就不再是内建函数，转换器不能把它翻成 `切分`。
    """
    names: set[str] = set()
    for i, t in enumerate(tokens):
        if t.kind == KEYWORD and t.canonical in ("fn", "let", "const"):
            j = i + 1
            while j < len(tokens) and tokens[j].kind in (NEWLINE, COMMENT):
                j += 1
            if j < len(tokens) and tokens[j].kind == IDENT:
                names.add(tokens[j].text)
    # 参数名：`函数 加(甲: 整数, 乙: 整数)` 里的「甲」「乙」
    for i, t in enumerate(tokens):
        if t.kind == IDENT and i + 1 < len(tokens) and tokens[i + 1].kind == OP \
                and tokens[i + 1].canonical == ":":
            names.add(t.text)
    return names


def convert(source: str, filename: str, target: str) -> str:
    """把源码转换到目标分支。返回新源码字符串。"""
    src_lang = detect_lang(source)
    if src_lang is None:
        raise error(Code.NO_LANG_DECL,
                    "文件第一行缺少语言声明，无法判断当前属于哪个分支",
                    file=filename, line=1, col=0,
                    fix_hints=[FixHint("add_lang_decl", f"{filename}:1",
                                       "补 `#语言=中文` 或 `#lang=English`")])
    if src_lang == target:
        return source

    table = KeywordTable.load()
    tokens = tokenize(source, filename, table)
    user_names = _user_names(tokens)

    edits: list[tuple[int, int, str]] = []

    # 1) 首行语言声明
    nl = source.find("\n")
    first_end = len(source) if nl < 0 else nl
    edits.append((0, first_end, DECL_ZH if target == LANG_ZH else DECL_EN))

    # 类型上下文跟踪：`<` `>` 在泛型里是括号、在表达式里是比较运算符，符号相同。
    # 不做这个判断，`List<Int>` 会被翻成 `列表小于整数大于`。
    in_type = False
    type_depth = 0
    prev_dot = False          # 上一个记号是不是 `.`，用来识别内置属性名

    for t in tokens:
        if t.kind in (EOF, NEWLINE, COMMENT):
            if t.kind == NEWLINE:
                # 类型注解不可能跨行 —— 行末无条件收状态。
                # 留着它跨行，等于让前面任何一处误判一路污染到文件结尾。
                in_type = False
                type_depth = 0
            continue

        # ── 进入 / 离开类型上下文 ────────────────────────────────────
        # 为什么要这么小心：`<` `>` 在泛型里是括号（`列表<整数>`），在表达式里是
        # 比较运算符（`1 大于 2`）。符号是同一个，只能靠"现在站没站在类型位置上"
        # 来区分。以前这个状态机是"宽进严出"：类型上下文一出 `<` 就再也出不来，
        # 于是 `变量 m = {"键": 1 大于 2}` 里那个 `:` 会把人带进类型上下文，
        # 后面整份文件的 `大于` `小于` 全被当成泛型括号漏掉转换 ——
        # 翻出来的英文 `1 大于 2` 根本编译不了，往返校验也跟着挂。
        #
        # 现在的规矩反过来：**宽出**。类型位置上不可能出现的记号一冒头就立刻收，
        # 绝不留恋 —— 宁可漏判一个泛型，也不能吞掉后面的表达式。
        if in_type:
            stray = t.kind not in (IDENT, KEYWORD, OP)
            closer = t.kind == OP and type_depth == 0 and t.canonical in (
                "{", "=", "(", ")", "]", "}", ";", ",")
            if stray or closer:
                in_type = False
                type_depth = 0

        if t.kind == OP and t.canonical in (":", "->"):
            in_type = True
            type_depth = 0
        elif in_type and t.kind == OP:
            if t.canonical == "<":
                type_depth += 1
                continue                      # 泛型左括号，绝不转换
            if t.canonical == ">":
                if type_depth > 0:
                    type_depth -= 1
                if type_depth == 0:
                    in_type = False           # 泛型到此为止，回到表达式
                continue                      # 泛型右括号，绝不转换

        if t.kind == KEYWORD:
            want = table.text_for(t.canonical, target)
            if want != t.text:
                edits.append((t.offset, t.end_offset, want))
            prev_dot = False
            continue
        if t.kind == OP:
            # 运算符有两个方向，必须都处理，否则往返校验挂：
            #   中文分支：比较/逻辑用词（大于、并且），算术用符号（+ - * /）
            #   英文分支：一律用符号（>、&&）
            prefix = table.op_arity(t.canonical) == "prefix"
            if target == LANG_ZH:
                zh = table.op_text_for(t.canonical, LANG_ZH)
                if zh != t.text:
                    # 前缀运算符后面必须与操作数分开：`!x` → `非 x`。
                    # 不补这个空格，`非x` 会被词法器当成一个标识符。
                    # 中缀运算符不动空格 —— `a > 1` 两边本来就有空格。
                    if prefix:
                        nxt = source[t.end_offset:t.end_offset + 1]
                        if nxt and _is_id_part(nxt):
                            zh += " "
                    edits.append((t.offset, t.end_offset, zh))
            else:
                if not t.sym and t.text != t.canonical:
                    end = t.end_offset
                    # 反向：前缀运算符的写法占了空格，回英文符号时要吞回来，
                    # 否则 `非 x` → `! x`，往返就不再逐字还原。
                    # 但只有空格后面跟着标识符字符才吞：`非 (x)` 里的空格是写法
                    # 的一部分，吞掉会变成 `非(x)`，与正向补空格的规则对称。
                    if prefix and source[end:end + 1] == " " \
                            and _is_id_part(source[end + 1:end + 2]):
                        end += 1
                    edits.append((t.offset, end, t.canonical))
            prev_dot = t.canonical == "."
            continue

        # 内置只读属性名也是语言的一部分，跟着分支走（`.长度` ↔ `.length`）。
        # 只认紧跟在 `.` 后面的那个名字，用户自定义的成员名一律不动。
        entry = table.attr_entry(t.text) if prev_dot else None
        if entry is not None:
            want = table.attr_text_for(entry.canonical, target)
            if want != t.text:
                edits.append((t.offset, t.end_offset, want))
            prev_dot = False
            continue

        # 内建函数名也是语言词汇，跟着分支走（`切分` ↔ `split`）。
        # 但用户自己定义的函数名绝不动：内建名不是关键字，用户有权用同名函数，
        # 这时它已经不是内建函数了，翻了就是把作者的代码改坏。
        if t.kind == IDENT and t.text not in user_names:
            bi = table.builtin_entry(t.text)
            if bi is not None:
                want = bi.zh if target == LANG_ZH else bi.en
                if want != t.text:
                    edits.append((t.offset, t.end_offset, want))
        prev_dot = False

    # 2) 从后往前替换，避免偏移错乱
    out = source
    for start, end, new in sorted(edits, key=lambda x: -x[0]):
        out = out[:start] + new + out[end:]
    return out


# ══════════════════════════════════════════════════════════════════════
# 结构比对（alpha 等价）
# ══════════════════════════════════════════════════════════════════════
# 这些字段装的是作者起的名字，比对时一律打码（换成 #0 #1 …）。
# `module` 也在内：中文版导入「工具」，英文版导入 helper，属于同一逻辑的两种命名。
_IDENT_FIELDS = {"name", "field", "item", "catch_name", "module", "param", "bind"}


def _builtin_map() -> dict[str, str]:
    """内建名的中英写法 → 统一规范名（主入口 / main 都归到 main）。

    两类都要收：关键字型内建（打印 / print）与函数型内建（切分 / split）。
    少收一类，中英两份的骨架就会因为「名字不同」被误判为不等价。
    """
    t = KeywordTable.load()
    out: dict[str, str] = {}
    for k in t.keywords:
        if k.kind == "builtin":
            out[k.zh] = k.en
            out[k.en] = k.en
    for b in t.builtins:
        out[b.zh] = b.canonical
        out[b.en] = b.canonical
    return out


_EXC_CANON = {
    "值错误": "ValueError", "ValueError": "ValueError",
    "类型错误": "TypeError", "TypeError": "TypeError",
    "键错误": "KeyError", "KeyError": "KeyError",
    "下标越界": "IndexError", "IndexError": "IndexError",
    "零除": "ZeroDivisionError", "ZeroDivisionError": "ZeroDivisionError",
    "文件错误": "OSError", "OSError": "OSError",
    "运行时错误": "RuntimeError", "RuntimeError": "RuntimeError",
    "断言错误": "AssertionError", "AssertionError": "AssertionError",
    "全部": "Exception", "Exception": "Exception",
}


def skeleton(ir: dict) -> str:
    """把 IR 压成与标识符无关的形状签名。两份逻辑等价 ⇒ 签名相同。"""
    env: dict[str, str] = {}
    counter = [0]
    builtins = _builtin_map()
    table = KeywordTable.load()

    def slot(kind: str, name: str, scope: str = "") -> str:
        """给作者起的名字编号。

        `scope` 必须是「第几个函数」（序号，不是函数名）：同一个名字出现在
        两个不同函数里，本来就是两个不相干的变量。少了这一层，
        中文版把 `重复` 的参数也叫「次数」、英文版叫 `times`，
        就会因为"中文的第二次出现被当成复用"而误判成不等价。
        """
        key = f"{kind}:{name}@{scope}"
        if key not in env:
            env[key] = f"#{counter[0]}"
            counter[0] += 1
        return env[key]

    def walk(node, path: str = "", scope: str = "") -> str:
        if isinstance(node, list):
            idx_scope = scope
            out = []
            for i, x in enumerate(node):
                # 函数体内部按函数序号隔离；其它列表沿用当前作用域
                out.append(walk(x, path, f"fn{i}" if path.endswith(".functions") else idx_scope))
            return "[" + ",".join(out) + "]"
        if not isinstance(node, dict):
            # 裸值一律保留：结构标签（binop / int / name）、枚举（Int / Net）、
            # 运算符（+ / -）、数字、布尔，全都参与比较。
            # 只打码用户内容（字符串字面量的 value 在父节点已单独处理）。
            return repr(node)

        parts: list[str] = []
        for k in sorted(node):
            v = node[k]
            if k == "span":
                continue
            if k == "entry" and isinstance(v, str):
                # 入口名按内建归一（主入口 ↔ main），否则中英两份必然不等价
                parts.append(f"{k}={builtins.get(v, v)}")
                continue
            if k == "exports" and isinstance(v, list):
                # 导出表里装的是作者起的函数名，必须和 `name` 用同一套编号：
                # 中文版导出「求和」、英文版导出 `sum`，逻辑上是同一件事。
                parts.append("exports=[" + ",".join(
                    slot("name", x) if isinstance(x, str) else walk(x, path, scope)
                    for x in v) + "]")
                continue
            if k in _IDENT_FIELDS and isinstance(v, str):
                canon = builtins.get(v)
                if canon is not None:
                    parts.append(f"{k}=builtin:{canon}")
                elif k == "field" and table.attr_entry(v) is not None:
                    # 内置只读属性不是作者起的名字，必须按规范名比较：
                    # `.长度` 和 `.length` 等价，但 `.长度` 和 `.大小` 不等价。
                    parts.append(f"{k}=attr:{table.attr_entry(v).canonical}")
                else:
                    parts.append(f"{k}={slot(k, v, scope)}")
                continue
            if k == "catch_type" and isinstance(v, str):
                # 类型化捕获的异常类型名：中英写法指向同一宿主异常类
                # （下标越界 ↔ IndexError），必须归一后再比较。
                parts.append(f"{k}=exc:{_EXC_CANON.get(v, v)}")
                continue
            if k == "value" and isinstance(v, str) and node.get("expr") == "string":
                parts.append("value=str")
                continue
            if k == "value" and isinstance(v, str) and node.get("expr") == "scope":
                # 生效范围里的 `模块 财务报告` 是用户起的模块名，中英必须按同一套编号比。
                parts.append(f"{k}={slot('scope_value', v, scope)}")
                continue
            if k == "declared":
                parts.append("declared=#")
                continue
            parts.append(f"{k}={walk(v, path + '.' + k, scope)}")
        return "(" + ",".join(parts) + ")"

    return walk(ir["program"])


def compare(ir_a: dict, ir_b: dict) -> tuple[bool, str]:
    """返回 (是否等价, 差异说明)。"""
    sa, sb = skeleton(ir_a), skeleton(ir_b)
    if sa == sb:
        return True, "结构完全等价"
    # 找出第一个不同的位置，给人看
    i = 0
    while i < min(len(sa), len(sb)) and sa[i] == sb[i]:
        i += 1
    lo = max(0, i - 60)
    return False, (
        "结构不一致。\n"
        f"  第一处差异在签名第 {i} 个字符附近：\n"
        f"  甲: …{sa[lo:i + 80]}…\n"
        f"  乙: …{sb[lo:i + 80]}…"
    )
