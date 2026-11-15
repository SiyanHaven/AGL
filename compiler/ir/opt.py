# -*- coding: utf-8 -*-
"""M4 优化通道：常量折叠 + 死代码消除 + 简单化简 + 无用变量消除 + 公共子表达式消除。

**默认关闭**。当前版本 的承诺是「IR 与源码 1:1」，行号人审才可信；
优化会改变转译产物，所以只能由使用者显式打开（`--opt`）。

这里只做**能证明等价**的变换，宁可不做：

  · 折叠只发生在字面量之间（整数 / 浮点 / 真假值）；
  · **不折除法**：折 1/0 会在编译期炸掉，折 4/2 又会把整数变成浮点，
    两件事都会悄悄改变语义；
  · 不折文本比较：`"a" < "b"` 的结果依赖宿主语言的排序规则，不是语言承诺；
  · 死代码只删「终止语句之后同层的语句」，不跨分支推理。

新增三个优化 Pass，每一个都带"前提才动手"的闸门：

  · **简单化简**：`x + 0`、`x * 1`、`!(!x)`。前提是**能证明变量的类型**——
    没显式标注类型的变量一律不碰；类型已知的还得保证这个变量没被重新赋值过
    （否则声明的类型和实际值可能对不上）。
    `x + 0` 只对整数做：整数加 0 恒等于自身，而浮点数有 `-0.0` 这个坑
    （`-0.0 + 0` 是 `0.0`，不是 `-0.0`）。`x * 1` 对整数浮点都恒等。
  · **无用变量消除**：从头到尾没人读、初始化又不可能出错的变量。
    初始化只允许字面量、变量引用、字面量列表 / 映射表 —— 出现调用、
    取成员、取下标都不行，因为它们可能抛错，删掉就等于把报错也删了。
  · **公共子表达式消除**（同块内）：复用本块里**已经算过并绑定到变量**的纯表达式。
    **不插入临时变量** —— 插入新语句会让后面的行号整体顺延，
    破坏"行号可人审"这条承诺。所以只有"本来就有个变量装着这个值"时才复用。

折叠后的节点**保留原来的 span**，否则行号对齐就断了。
"""
from __future__ import annotations

import copy

from compiler.lexer import KeywordTable

_ARITH = {"+", "-", "*"}
_CMP = {"==", "!=", ">", "<", ">=", "<="}
_LOGIC = {"&&", "||"}

_TERMINATORS = {"return", "break", "continue"}

# 比较与逻辑运算的结果一定是真假值
_BOOL_OPS = _CMP | _LOGIC


class _Counter:
    def __init__(self) -> None:
        self.folded = 0
        self.removed = 0
        self.simplified = 0     # 简单化简
        self.dead_vars = 0      # 无用变量消除
        self.cse = 0            # 公共子表达式消除

    def notes(self) -> list[str]:
        parts: list[str] = []
        if self.folded:
            parts.append(f"折叠 {self.folded} 处常量")
        if self.removed:
            parts.append(f"删除 {self.removed} 条不可达语句")
        if self.simplified:
            parts.append(f"化简 {self.simplified} 处")
        if self.dead_vars:
            parts.append(f"删掉 {self.dead_vars} 个无用变量")
        if self.cse:
            parts.append(f"复用 {self.cse} 处公共子表达式")
        return [] if not parts else ["，".join(parts)]


class _Ctx:
    """一个函数里的优化上下文。

    记的都是"能不能证明"用的证据：
      · `types`   —— 显式标注了类型的变量名 → 类型名（没标注的查不到，不碰）；
      · `assigned`—— 被重新赋值过的名字（它的值会变，不能当常量用）；
      · `for_items` —— 循环变量（每轮都在变）。
    """

    def __init__(self, fn: dict, fn_effects: dict[str, list[str]]):
        self.fn = fn
        self.fn_effects = fn_effects
        self.types: dict[str, str] = {}
        self.assigned: set[str] = set()
        self.for_items: set[str] = set()
        self.bound: set[str] = set()
        for p in fn.get("params") or []:
            t = p.get("type") or {}
            self.types[p["name"]] = t.get("type", "")
            self.bound.add(p["name"])
        self._scan(fn.get("body") or [])

    def _scan(self, stmts: list[dict]) -> None:
        for s in stmts:
            k = s.get("stmt")
            if k == "let":
                self.bound.add(s["name"])
                t = s.get("type") or {}
                if t.get("type"):
                    self.types[s["name"]] = t["type"]
            elif k == "let_multi":
                self.bound.update(s["names"])
                t = s.get("type") or {}
                if t.get("type"):
                    for n in s["names"]:
                        self.types[n] = t["type"]
            elif k == "assign":
                tgt = s.get("target") or {}
                if tgt.get("expr") == "name":
                    self.assigned.add(tgt["name"])
            elif k == "assign_multi":
                for tgt in s.get("targets", []):
                    if tgt.get("expr") == "name":
                        self.assigned.add(tgt["name"])
            elif k == "aug_assign":
                tgt = s.get("target") or {}
                if tgt.get("expr") == "name":
                    self.assigned.add(tgt["name"])
            elif k == "for":
                self.for_items.add(s["item"])
            elif k == "with" and s.get("name"):
                self.bound.add(s["name"])
            for key in ("body", "then", "else", "catch", "finally"):
                sub = s.get(key)
                if isinstance(sub, list):
                    self._scan(sub)
            if k == "match":
                for arm in s.get("arms", []):
                    self._scan(arm.get("body") or [])

    # ── 判定 ──────────────────────────────────────────────────────────
    def known_type(self, name: str) -> str:
        return self.types.get(name, "")

    def stable(self, name: str) -> bool:
        """这个变量的值在函数执行期间不会变。"""
        return (name in self.bound and name not in self.assigned
                and name not in self.for_items)

    def pure_call(self, e: dict) -> bool:
        """这个调用是纯的（不碰外部），才可以被复用。"""
        if "ffi" in e:
            return False          # FFI 一律带最坏效应
        if "builtin" in e:
            bi = KeywordTable.load().builtin_by_canon(e["builtin"])
            return bi is not None and not bi.effect
        # 用户函数：没声明效应才算纯（效应传播是编译期保证的）
        return not (self.fn_effects.get(e["name"]) or [])


def _num(e: dict) -> float | int | None:
    if e.get("expr") == "int":
        return e["value"]
    if e.get("expr") == "float":
        return e["value"]
    return None


def _lit(e: dict, kind: str, value) -> dict:
    return {"expr": kind, "value": value, "span": e["span"]}


def _is_zero(e: dict) -> bool:
    return e.get("expr") == "int" and e["value"] == 0


def _is_one(e: dict) -> bool:
    return e.get("expr") == "int" and e["value"] == 1


def _known_int(e: dict, ctx: _Ctx) -> bool:
    """能证明这个表达式是整数。"""
    if e.get("expr") == "int":
        return True
    if e.get("expr") == "name":
        n = e["name"]
        return ctx.known_type(n) == "Int" and n not in ctx.assigned
    return False


def _known_numeric(e: dict, ctx: _Ctx) -> bool:
    """能证明这个表达式是数（整数或浮点）。"""
    if e.get("expr") in ("int", "float"):
        return True
    if e.get("expr") == "name":
        n = e["name"]
        return ctx.known_type(n) in ("Int", "Float") and n not in ctx.assigned
    return False


def _known_bool(e: dict, ctx: _Ctx) -> bool:
    """能证明这个表达式是真假值。"""
    k = e.get("expr")
    if k == "bool":
        return True
    if k == "binop" and e.get("op") in _BOOL_OPS:
        return True
    if k == "name":
        n = e["name"]
        return ctx.known_type(n) == "Bool" and n not in ctx.assigned
    return False


def _simplify(e: dict, c: _Counter, ctx: _Ctx) -> dict:
    """简单化简：只做恒等变换，且只在类型可证时做。"""
    op = e.get("op")
    left, right = e.get("left"), e.get("right")
    if op == "+":
        # 整数加 0 恒等于自身。浮点不做：`-0.0 + 0` 是 `0.0`。
        if _is_zero(right) and _known_int(left, ctx):
            c.simplified += 1
            return left
        if _is_zero(left) and _known_int(right, ctx):
            c.simplified += 1
            return right
    elif op == "*":
        # 乘 1 对任何数值都恒等（`-0.0 * 1` 还是 `-0.0`）。
        if _is_one(right) and _known_numeric(left, ctx):
            c.simplified += 1
            return left
        if _is_one(left) and _known_numeric(right, ctx):
            c.simplified += 1
            return right
    return e


def _fold(e: dict, c: _Counter, ctx: _Ctx) -> dict:
    k = e.get("expr")
    if k == "binop":
        e["left"] = _fold(e["left"], c, ctx)
        e["right"] = _fold(e["right"], c, ctx)
        op, left, right = e["op"], e["left"], e["right"]

        if op in _ARITH:
            a, b = _num(left), _num(right)
            if a is not None and b is not None:
                val = {"+": a + b, "-": a - b, "*": a * b}[op]
                c.folded += 1
                return _lit(e, "float" if isinstance(val, float) else "int", val)

        if op in _CMP:
            a, b = _num(left), _num(right)
            if a is not None and b is not None:
                c.folded += 1
                return _lit(e, "bool", {"==": a == b, "!=": a != b, ">": a > b,
                                        "<": a < b, ">=": a >= b, "<=": a <= b}[op])

        if op in _LOGIC and left.get("expr") == right.get("expr") == "bool":
            lv, rv = bool(left["value"]), bool(right["value"])
            c.folded += 1
            return _lit(e, "bool", (lv and rv) if op == "&&" else (lv or rv))

        return _simplify(e, c, ctx)

    if k == "unop":
        e["operand"] = _fold(e["operand"], c, ctx)
        operand = e["operand"]
        if e["op"] == "!" and operand.get("expr") == "bool":
            c.folded += 1
            return _lit(e, "bool", not operand["value"])
        if e["op"] == "-" and _num(operand) is not None:
            c.folded += 1
            val = -_num(operand)
            return _lit(e, "float" if isinstance(val, float) else "int", val)
        # 双重否定：`!(!x)` → `x`（前提是能证明 x 是真假值）
        if e["op"] == "!" and operand.get("expr") == "unop" and operand.get("op") == "!":
            inner = operand["operand"]
            if _known_bool(inner, ctx):
                c.simplified += 1
                return inner
        return e

    # 其余表达式：只递归子节点，不改变结构
    for child in _children(e):
        _fold(child, c, ctx)
    return e


# ── 公共子表达式消除 ──────────────────────────────────────────────────
def _sig(e: dict, ctx: _Ctx) -> str | None:
    """表达式签名（忽略 span）。返回 None 表示这个表达式不可复用。

    不可复用的三种情况：
      · 不是纯的（调用带效应、FFI）；
      · 引用了值会变的变量（被重新赋值过、是循环变量）；
      · 结构上无法用签名表达。
    """
    k = e.get("expr")
    if k == "string":
        return "s:" + e["value"]
    if k in ("int", "float", "bool"):
        return f"{k}:{e['value']}"
    if k == "null":
        return "null"
    if k == "name":
        return ("n:" + e["name"]) if ctx.stable(e["name"]) else None
    if k == "binop":
        a, b = _sig(e["left"], ctx), _sig(e["right"], ctx)
        return None if a is None or b is None else f"b:{e['op']}({a},{b})"
    if k == "unop":
        a = _sig(e["operand"], ctx)
        return None if a is None else f"u:{e['op']}({a})"
    if k == "call":
        if not ctx.pure_call(e):
            return None
        # 跨模块的调用一律不参与复用：`fn_effects` 只有本模块的声明，别的文件里
        # 那个同名（甚至不同名）函数干了什么，到这一层已经看不见了。
        # 看不见就是不安全 —— `甲.取(0)` 和 `乙.取(0)` 的签名若只按方法名算会撞车，
        # 复用以后第二个会拿到第一个的结果，属于把答案换成别人的答案。
        if isinstance(e.get("target"), dict) and not e.get("builtin"):
            return None
        args: list[str] = []
        for a in e["args"]:
            s = _sig(a, ctx)
            if s is None:
                return None
            args.append(s)
        tag = e.get("builtin") or ("fn:" + e["name"])
        return "c:" + tag + "(" + ",".join(args) + ")"
    if k == "list":
        items: list[str] = []
        for i in e["items"]:
            s = _sig(i, ctx)
            if s is None:
                return None
            items.append(s)
        return "l:[" + ",".join(items) + "]"
    if k == "map":
        ents: list[str] = []
        for x in e["entries"]:
            ks, vs = _sig(x["key"], ctx), _sig(x["value"], ctx)
            if ks is None or vs is None:
                return None
            ents.append(ks + ":" + vs)
        return "m:{" + ",".join(ents) + "}"
    if k == "set":
        items: list[str] = []
        for i in e["items"]:
            s = _sig(i, ctx)
            if s is None:
                return None
            items.append(s)
        return "s:{" + ",".join(items) + "}"
    return None


def _children(e: dict) -> list[dict]:
    """表达式节点的子节点。

    **必须列全**：漏掉一种结构（比如 binop 的 left/right），
    遍历就会"看不见"里面的东西 —— 名字没被收集、调用没被复用，
    最后表现成"变量莫名其妙被删了"这种查起来很费劲的错。
    """
    k = e.get("expr")
    if k == "binop":
        return [e["left"], e["right"]]
    if k == "unop":
        return [e["operand"]]
    if k == "member":
        return [e["target"]]
    if k == "index":
        return [e["target"], e["index"]]
    if k == "call":
        kids = list(e.get("args") or [])
        if isinstance(e.get("target"), dict):
            kids.append(e["target"])
        return kids
    if k == "list":
        return list(e.get("items") or [])
    if k == "map":
        return [x for ent in (e.get("entries") or []) for x in (ent["key"], ent["value"])]
    if k == "set":
        return list(e.get("items") or [])
    if k == "await":
        return [e["value"]]
    return []


def _reuse_expr(e: dict, seen: dict[str, str], c: _Counter, ctx: _Ctx) -> dict:
    """把 e 里能复用的子表达式换成对应变量的引用。"""
    if not isinstance(e, dict):
        return e
    if e.get("expr") == "call":
        sig = _sig(e, ctx)
        if sig is not None and sig in seen:
            name = seen[sig]
            c.cse += 1
            return {"expr": "name", "name": name, "span": e["span"]}
    for child in _children(e):
        _reuse_expr(child, seen, c, ctx)
    return e


# ── 无用变量消除 ──────────────────────────────────────────────────────
def _trivial(e: dict | None) -> bool:
    """这个表达式既没有副作用，也不可能出错。"""
    if not isinstance(e, dict):
        return False
    k = e.get("expr")
    if k in ("int", "float", "bool", "string", "null"):
        return True
    if k == "name":
        return True
    if k == "list":
        return all(_trivial(i) for i in e["items"])
    if k == "map":
        return all(_trivial(x["key"]) and _trivial(x["value"]) for x in e["entries"])
    if k == "set":
        return all(_trivial(i) for i in e["items"])
    return False


def _expr_refs(e: dict, out: set[str]) -> None:
    if not isinstance(e, dict):
        return
    if e.get("expr") == "name":
        out.add(e["name"])
    for child in _children(e):
        _expr_refs(child, out)


def _collect_refs(stmts: list[dict], out: set[str]) -> None:
    """收集函数里所有被读到的名字（外加被赋值的名字与循环变量）。"""
    for s in stmts:
        k = s.get("stmt")
        if k == "assign":
            tgt = s.get("target") or {}
            if tgt.get("expr") == "name":
                out.add(tgt["name"])
        elif k == "assign_multi":
            for tgt in s.get("targets", []):
                if tgt.get("expr") == "name":
                    out.add(tgt["name"])
        elif k == "aug_assign":
            tgt = s.get("target") or {}
            if tgt.get("expr") == "name":
                out.add(tgt["name"])
        elif k == "for":
            out.add(s["item"])
        # `task` 是 `on_event` 的任务句柄引用。漏掉它，`变量 句柄 = 甲` 会被当成
        # "没人读"的无用变量删掉 —— 运行期就是 NameError。
        for key in ("value", "target", "cond", "iter", "task", "subject", "expr"):
            if isinstance(s.get(key), dict):
                _expr_refs(s[key], out)
        if k == "on_event":
            out.add(s.get("param", ""))
        for key in ("body", "then", "else", "catch", "finally"):
            sub = s.get(key)
            if isinstance(sub, list):
                _collect_refs(sub, out)
        if k == "match":
            for arm in s.get("arms", []):
                if isinstance(arm.get("pattern"), dict):
                    _expr_refs(arm["pattern"], out)
                if isinstance(arm.get("guard"), dict):
                    _expr_refs(arm["guard"], out)
                _collect_refs(arm.get("body") or [], out)


def _drop_dead(stmts: list[dict], refs: set[str], c: _Counter) -> list[dict]:
    """删掉没人读、且初始化不可能出错的变量。"""
    drop: set[int] = set()
    for i, s in enumerate(stmts):
        if s.get("stmt") == "let" and s["name"] not in refs and _trivial(s.get("value")):
            drop.add(i)
    if drop and len(drop) == len(stmts):
        # 全删光会让这个块变成空块，生成的宿主代码会语法错误。留一条。
        drop = set()
    out: list[dict] = []
    for i, s in enumerate(stmts):
        if i in drop:
            c.dead_vars += 1
            continue
        for key in ("body", "then", "else", "catch", "finally"):
            sub = s.get(key)
            if isinstance(sub, list):
                s[key] = _drop_dead(sub, refs, c)
        out.append(s)
    return out


# ── 语句与块 ──────────────────────────────────────────────────────────
def _block(stmts: list[dict], c: _Counter, ctx: _Ctx) -> list[dict]:
    """同层语句：先递归处理每条语句内部，再砍掉终止语句后面的死代码，
    最后做同块内的公共子表达式消除。"""
    out: list[dict] = []
    dead = False
    seen: dict[str, str] = {}
    for s in stmts:
        _stmt(s, c, ctx)
        if dead:
            c.removed += 1
            continue
        out.append(s)
        if s.get("stmt") in _TERMINATORS:
            dead = True
    # 死代码砍完之后再做 CSE：被砍掉的语句不该再贡献可复用的表达式
        for s in out:
            k = s.get("stmt")
            if k in ("assign", "aug_assign"):
                tgt = s.get("target") or {}
                if tgt.get("expr") == "name":
                    for sig, nm in list(seen.items()):
                        if nm == tgt["name"]:
                            del seen[sig]
        for key in ("value", "target", "cond", "iter"):
            if isinstance(s.get(key), dict):
                s[key] = _reuse_expr(s[key], seen, c, ctx)
        if k == "let":
            sig = _sig(s.get("value") or {}, ctx)
            if sig is not None and ctx.stable(s["name"]):
                seen[sig] = s["name"]
    return out


def _stmt(s: dict, c: _Counter, ctx: _Ctx) -> None:
    k = s.get("stmt")
    if k in ("let", "assign", "aug_assign", "return", "expr"):
        if isinstance(s.get("value"), dict):
            s["value"] = _fold(s["value"], c, ctx)
        if isinstance(s.get("target"), dict):
            s["target"] = _fold(s["target"], c, ctx)
        return
    if k == "if":
        s["cond"] = _fold(s["cond"], c, ctx)
        s["then"] = _block(s["then"], c, ctx)
        if "else" in s:
            s["else"] = _block(s["else"], c, ctx)
        return
    if k in ("while", "for"):
        if "cond" in s:
            s["cond"] = _fold(s["cond"], c, ctx)
        if "iter" in s:
            s["iter"] = _fold(s["iter"], c, ctx)
        s["body"] = _block(s["body"], c, ctx)
        if s.get("else") is not None:
            s["else"] = _block(s["else"], c, ctx)
        return
    if k == "try":
        s["body"] = _block(s["body"], c, ctx)
        if s.get("catch"):
            s["catch"] = _block(s["catch"], c, ctx)
        if s.get("else") is not None:
            s["else"] = _block(s["else"], c, ctx)
        if s.get("finally"):
            s["finally"] = _block(s["finally"], c, ctx)
        return
    if k == "on_event":
        s["task"] = _fold(s["task"], c, ctx)
        s["body"] = _block(s["body"], c, ctx)
        return


def optimize(ir: dict) -> tuple[dict, list[str]]:
    """返回 (优化后的 IR, 说明)。不修改传入的 IR。"""
    out = copy.deepcopy(ir)
    c = _Counter()
    for mod in out["program"]["modules"]:
        fn_effects = {fn["name"]: (fn.get("effects") or []) for fn in mod["functions"]}
        for fn in mod["functions"]:
            ctx = _Ctx(fn, fn_effects)
            for chk in fn.get("checks") or []:
                _fold(chk, c, ctx)
            fn["body"] = _block(fn["body"], c, ctx)
            # 无用变量消除放在最后：它要知道整个函数里哪些名字真的被读过
            refs: set[str] = set()
            _collect_refs(fn["body"], refs)
            fn["body"] = _drop_dead(fn["body"], refs, c)
    return out, c.notes()
