# -*- coding: utf-8 -*-
"""AGB 字节码（v0）：IR → 指令流 + 反汇编。

这是「自有运行时」的第一层：`aglc build` 只产 `.agb`，`aglc run` 默认走 VM。
支持范围（M2/M3 已落地，超出即报 AgbUnsupported，绝不静默生成半成品）：
  · 字面量 / 名字 / 算术 / 比较 / 逻辑 / 一元
  · let / const / assign / aug_assign / return / expr 语句
  · if / while / for / break / continue / match / try / with
  · 列表 / 映射表 / 集合 / 下标 / `.长度`
  · 用户函数调用、内建调用、模块调用、FFI、函数引用
  · 结构体、`任务` / `等待` / `当任务完成`
"""
from __future__ import annotations

import json

FORMAT = "AGB"
VERSION = 0


class AgbUnsupported(Exception):
    """IR 里出现当前 AGB 子集还没覆盖的节点。"""


def _span_line(node: dict) -> int:
    return int((node.get("span") or {}).get("line") or 1)


class _Pool:
    def __init__(self):
        self.consts: list = []
        self.names: list[str] = []
        # `当任务完成 / 当任务失败 / 当收到进度` 编译出的回调函数
        self.extra_fns: list[dict] = []

    def const(self, value) -> int:
        # 布尔与整数在 Python 里相等，但 AGL 语义不同，必须分开存放。
        for i, old in enumerate(self.consts):
            if type(old) is type(value) and old == value:
                return i
        self.consts.append(value)
        return len(self.consts) - 1

    def name(self, text: str) -> int:
        if text in self.names:
            return self.names.index(text)
        self.names.append(text)
        return len(self.names) - 1


class _FnCompiler:
    def __init__(self, pool: _Pool, fn_names: set[str], types: dict[str, list[str]]):
        self.pool = pool
        self.fn_names = fn_names
        self.types = types
        self.temp_id = 0
        self.code: list[dict] = []
        self.lines: list[int] = []
        # break/continue 待回填的跳转位置
        self.breaks: list[int] = []
        self.continues: list[int] = []
        self.loops: list[str] = []
        # 循环入口处的 try 处理器深度：break/continue 要先弹掉循环内打开的处理器
        self.loop_handler_depth: list[int] = []
        # 当前处于哪些 `最后` 块里（外层 → 内层），return/break/continue 要补跑
        self.finally_blocks: list[list[dict]] = []
        self.handler_depth = 0

    def emit(self, op: str, arg=None, line: int = 1) -> int:
        self.code.append({"op": op, "arg": arg} if arg is not None else {"op": op})
        self.lines.append(line)
        return len(self.code) - 1

    def patch(self, at: int, target: int) -> None:
        cur = self.code[at].get("arg")
        if isinstance(cur, dict):
            cur["target"] = target
        else:
            self.code[at]["arg"] = target

    def _emit_finalizers(self) -> None:
        """在离开当前 finally 作用域前，从内到外补跑所有 `最后` 块。"""
        active = self.finally_blocks
        for i in range(len(active) - 1, -1, -1):
            saved = self.finally_blocks
            self.finally_blocks = active[:i]
            for item in active[i]:
                self.stmt(item)
            self.finally_blocks = saved

    def _compile_finally_block(self, block: list[dict]) -> None:
        """正常路径/异常清理路径里执行一段 `最后`：编译时它自己不再算活跃。"""
        saved = self.finally_blocks
        self.finally_blocks = self.finally_blocks[:-1]
        for item in block:
            self.stmt(item)
        self.finally_blocks = saved

    def _pop_loop_try_handlers(self) -> None:
        """break/continue 要离开循环内的 try，先把这些 TRY_BEGIN 弹掉。"""
        base = self.loop_handler_depth[-1] if self.loop_handler_depth else 0
        while self.handler_depth > base:
            self.emit("TRY_END")
            self.handler_depth -= 1

    def _make_callback(self, body: list[dict], param: str, line: int) -> str:
        """把 `当任务完成 { … }` 的块编译成一个 AGB 函数，返回函数名。"""
        name = f"__回调{len(self.pool.extra_fns)}"
        fc = _FnCompiler(self.pool, self.fn_names, self.types)
        for item in body:
            fc.stmt(item)
        fn = fc.finish(name, [param], line)
        self.pool.extra_fns.append(fn)
        self.fn_names.add(name)
        return name

    # ── 表达式 ──────────────────────────────────────────────────────
    def expr(self, e: dict) -> None:
        kind = e.get("expr")
        line = _span_line(e)
        if kind in ("int", "float", "bool", "null", "string"):
            self.emit("LOAD_CONST", self.pool.const(e.get("value")), line)
            return
        if kind == "name":
            # 函数名当值使用（目前只有 `按键排序(xs, 键函数)` 这类位置）：
            # 发 LOAD_FUNC，VM 生成函数引用；普通变量仍走 LOAD_NAME。
            if e["name"] in self.fn_names:
                self.emit("LOAD_FUNC", self.pool.name(e["name"]), line)
            else:
                self.emit("LOAD_NAME", self.pool.name(e["name"]), line)
            return
        if kind == "named":
            self.expr(e["value"])
            return
        if kind == "binop":
            op = e["op"]
            # `&&` / `||` 必须短路：右操作数可能越界、可能有副作用。
            # 以前是「先把两边都算出来再 AND/OR」，VM 与 Python 后端语义不一致；
            # 这里改成显式跳转，左边已经决定结果时右侧一条指令都不执行。
            if op == "&&":
                self.expr(e["left"])
                jfalse = self.emit("JUMP_IF_FALSE", None, line)
                self.expr(e["right"])
                self.emit("NOT", line=line)
                self.emit("NOT", line=line)
                jend = self.emit("JUMP", None, line)
                self.patch(jfalse, len(self.code))
                self.emit("LOAD_CONST", self.pool.const(False), line)
                self.patch(jend, len(self.code))
                return
            if op == "||":
                self.expr(e["left"])
                self.emit("NOT", line=line)
                jright = self.emit("JUMP_IF_FALSE", None, line)
                self.expr(e["right"])
                self.emit("NOT", line=line)
                self.emit("NOT", line=line)
                jend = self.emit("JUMP", None, line)
                self.patch(jright, len(self.code))
                self.emit("LOAD_CONST", self.pool.const(True), line)
                self.patch(jend, len(self.code))
                return
            self.expr(e["left"])
            self.expr(e["right"])
            if op not in ("==", "!=", ">", "<", ">=", "<=", "&&", "||",
                          "+", "-", "*", "/", "%"):
                raise AgbUnsupported(f"未知二元运算符 `{op}`")
            self.emit({
                "==": "EQ", "!=": "NE", ">": "GT", "<": "LT",
                ">=": "GE", "<=": "LE", "&&": "AND", "||": "OR",
                "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
            }[op], line=line)
            return
        if kind == "unop":
            self.expr(e["operand"])
            self.emit("NEG" if e["op"] == "-" else "NOT", line=line)
            return
        if kind == "await":
            self.expr(e["value"])
            self.emit("AWAIT", line=line)
            return
        if kind == "call":
            target = e.get("target")
            # `任务(慢())`：不要现在执行内层调用，生成一个待跑的任务体。
            if e.get("builtin") == "task" and len(e.get("args") or []) == 1:
                inner = e["args"][0]
                if inner.get("expr") != "call":
                    raise AgbUnsupported("`任务` 的参数要是一个函数调用")
                for a in inner.get("args") or []:
                    self.expr(a)
                if inner.get("builtin"):
                    self.emit("TASK", {"kind": "builtin",
                                       "name": self.pool.name(inner["builtin"]),
                                       "argc": len(inner.get("args") or [])}, line)
                elif inner.get("ffi"):
                    raise AgbUnsupported("`任务` 里暂不支持 FFI")
                elif inner.get("target") is not None:
                    t = inner["target"]
                    if t.get("expr") != "name":
                        raise AgbUnsupported("`任务` 里不支持方法调用")
                    self.emit("TASK", {"kind": "module",
                                       "module": self.pool.name(t["name"]),
                                       "name": self.pool.name(inner["name"]),
                                       "argc": len(inner.get("args") or [])}, line)
                elif inner["name"] in self.fn_names:
                    self.emit("TASK", {"kind": "user",
                                       "name": self.pool.name(inner["name"]),
                                       "argc": len(inner.get("args") or [])}, line)
                else:
                    raise AgbUnsupported(f"`任务` 的目标 `{inner['name']}` 不在 AGB 范围")
                return
            argc = 0
            for a in e.get("args") or []:
                self.expr(a)
                argc += 1
            if e.get("ffi"):
                self.emit("CALL_FFI", {
                    "lib": self.pool.name(e["ffi"]),
                    "name": self.pool.name(e["name"]),
                    "argc": argc,
                }, line)
                return
            if target is not None:
                if target.get("expr") != "name":
                    raise AgbUnsupported("方法调用不在 M1 范围")
                self.emit("CALL_MODULE", {
                    "module": self.pool.name(target["name"]),
                    "name": self.pool.name(e["name"]),
                    "argc": argc,
                }, line)
                return
            name = e["name"]
            if e.get("builtin"):
                self.emit("CALL_BUILTIN",
                          {"name": self.pool.name(e["builtin"]), "argc": argc}, line)
            elif name in self.types:
                if e.get("args") and any(a.get("expr") != "named" for a in e["args"]):
                    raise AgbUnsupported("结构体构造必须使用命名实参")
                self.emit("STRUCT_NEW", {
                    "type": self.pool.name(name),
                    "fields": [self.pool.name(a["name"]) for a in e["args"]],
                    "argc": argc,
                }, line)
            elif name in self.fn_names:
                self.emit("CALL_USER",
                          {"name": self.pool.name(name), "argc": argc}, line)
            else:
                # 名字可能是值（函数引用）或模块，当前 AGB 子集不处理
                raise AgbUnsupported(f"调用目标 `{name}` 不在当前 AGB 子集")
            return
        if kind == "index":
            self.expr(e["target"])
            self.expr(e["index"])
            self.emit("INDEX", line=line)
            return
        if kind == "member":
            field = e["field"]
            if field in ("长度", "length"):
                self.expr(e["target"])
                self.emit("LENGTH", line=line)
                return
            self.expr(e["target"])
            self.emit("GET_FIELD", self.pool.name(field), line)
            return
        if kind == "list":
            for item in e.get("items") or []:
                self.expr(item)
            self.emit("BUILD_LIST", len(e.get("items") or []), line)
            return
        if kind == "map":
            items = e.get("entries") or []
            for pair in items:
                self.expr(pair["key"])
                self.expr(pair["value"])
            self.emit("BUILD_MAP", len(items), line)
            return
        if kind == "set":
            items = e.get("items") or []
            for item in items:
                self.expr(item)
            self.emit("BUILD_SET", len(items), line)
            return
        if kind == "match":
            temp = f"__match{self.temp_id}"
            self.temp_id += 1
            self.expr(e["subject"])
            self.emit("STORE_NAME", self.pool.name(temp), line)
            ends: list[int] = []
            for arm in e["arms"]:
                pat = arm["pattern"]
                wildcard = pat.get("expr") == "name" and pat.get("name") == "_"
                list_pat = pat.get("expr") == "list_pattern"
                map_pat = pat.get("expr") == "map_pattern"
                checks: list[int] = []
                if map_pat:
                    self._emit_map_pattern(temp, pat.get("entries") or [], line, checks)
                elif list_pat:
                    self._emit_list_pattern(temp, pat.get("items") or [], line, checks)
                elif not wildcard:
                    self.emit("LOAD_NAME", self.pool.name(temp), line)
                    self.expr(pat)
                    self.emit("EQ", line=line)
                    checks.append(self.emit("JUMP_IF_FALSE", -1, line))
                bind = arm.get("bind")
                if bind:
                    self.emit("LOAD_NAME", self.pool.name(temp), line)
                    self.emit("STORE_NAME", self.pool.name(str(bind)), line)
                guard_skip = None
                if arm.get("guard") is not None:
                    self.expr(arm["guard"])
                    guard_skip = self.emit("JUMP_IF_FALSE", -1, line)
                self.expr(arm["body"])
                ends.append(self.emit("JUMP", -1, line))
                for skip in checks:
                    self.patch(skip, len(self.code))
                if guard_skip is not None:
                    self.patch(guard_skip, len(self.code))
            self.emit("LOAD_CONST", self.pool.const(None), line)
            for j in ends:
                self.patch(j, len(self.code))
            return
        if kind == "struct_call":
            args = e.get("args") or []
            if args and any(a.get("expr") != "named" for a in args):
                raise AgbUnsupported("结构体构造必须使用命名实参")
            argc = 0
            for a in args:
                self.expr(a)
                argc += 1
            self.emit("STRUCT_NEW", {
                "module": self.pool.name(e["module"]),
                "type": self.pool.name(e["name"]),
                "fields": [self.pool.name(a["name"]) for a in args],
                "argc": argc,
            }, line)
            return
        raise AgbUnsupported(f"当前 AGB 子集不支持表达式 `{kind}`")

    def _emit_list_pattern(self, container: str, items: list[dict],
                           line: int, checks: list[int]) -> None:
        """递归发列表模式检查；container 是已存好列表的临时名。"""
        self.emit("LOAD_NAME", self.pool.name(container), line)
        self.emit("MATCH_LIST", len(items), line)
        checks.append(self.emit("JUMP_IF_FALSE", -1, line))
        for i, it in enumerate(items):
            kind = it.get("kind")
            if kind == "literal":
                self.emit("LOAD_NAME", self.pool.name(container), line)
                self.emit("LOAD_CONST", self.pool.const(i), line)
                self.emit("INDEX", line=line)
                self.expr(it["value"])
                self.emit("EQ", line=line)
                checks.append(self.emit("JUMP_IF_FALSE", -1, line))
            elif kind == "bind":
                self.emit("LOAD_NAME", self.pool.name(container), line)
                self.emit("LOAD_CONST", self.pool.const(i), line)
                self.emit("INDEX", line=line)
                self.emit("STORE_NAME", self.pool.name(str(it["name"])), line)
            elif kind in ("list", "map"):
                nested_temp = f"__match{self.temp_id}"
                self.temp_id += 1
                self.emit("LOAD_NAME", self.pool.name(container), line)
                self.emit("LOAD_CONST", self.pool.const(i), line)
                self.emit("INDEX", line=line)
                self.emit("STORE_NAME", self.pool.name(nested_temp), line)
                nested = it.get("pattern") or {}
                if kind == "list":
                    self._emit_list_pattern(nested_temp, nested.get("items") or [],
                                            line, checks)
                else:
                    self._emit_map_pattern(nested_temp, nested.get("entries") or [],
                                           line, checks)

    def _emit_map_pattern(self, container: str, entries: list[dict],
                          line: int, checks: list[int]) -> None:
        """递归发映射模式检查：键存在 + 值匹配；允许多余键。"""
        for ent in entries:
            key = ent["key"]
            self.emit("LOAD_NAME", self.pool.name(container), line)
            self.expr(key)
            self.emit("CALL_BUILTIN",
                      {"name": self.pool.name("contains"), "argc": 2}, line)
            checks.append(self.emit("JUMP_IF_FALSE", -1, line))
            val = ent["value"]
            kind = val.get("kind")
            if kind == "literal":
                self.emit("LOAD_NAME", self.pool.name(container), line)
                self.expr(key)
                self.emit("INDEX", line=line)
                self.expr(val["value"])
                self.emit("EQ", line=line)
                checks.append(self.emit("JUMP_IF_FALSE", -1, line))
            elif kind == "bind":
                self.emit("LOAD_NAME", self.pool.name(container), line)
                self.expr(key)
                self.emit("INDEX", line=line)
                self.emit("STORE_NAME", self.pool.name(str(val["name"]),), line)
            elif kind in ("list", "map"):
                nested_temp = f"__match{self.temp_id}"
                self.temp_id += 1
                self.emit("LOAD_NAME", self.pool.name(container), line)
                self.expr(key)
                self.emit("INDEX", line=line)
                self.emit("STORE_NAME", self.pool.name(nested_temp), line)
                nested = val.get("pattern") or {}
                if kind == "list":
                    self._emit_list_pattern(nested_temp, nested.get("items") or [],
                                            line, checks)
                else:
                    self._emit_map_pattern(nested_temp, nested.get("entries") or [],
                                           line, checks)

    # ── 语句 ────────────────────────────────────────────────────────
    def stmt(self, s: dict) -> None:
        kind = s.get("stmt")
        line = _span_line(s)
        if kind == "let_multi":
            names = s["names"]
            self.expr(s["value"])
            self.emit("UNPACK", len(names), line)
            for name in reversed(names):
                self.emit("STORE_NAME", self.pool.name(name), line)
            return
        if kind == "let":
            self.expr(s["value"])
            self.emit("STORE_NAME", self.pool.name(s["name"]), line)
            return
        if kind == "assign":
            target = s["target"]
            if target.get("expr") == "index":
                self.expr(target["target"])
                self.expr(target["index"])
                self.expr(s["value"])
                self.emit("STORE_INDEX", line=line)
                return
            if target.get("expr") != "name":
                raise AgbUnsupported("当前 AGB 子集只支持给变量或下标赋值")
            self.expr(s["value"])
            self.emit("STORE_NAME", self.pool.name(target["name"]), line)
            return
        if kind == "assign_multi":
            targets = s["targets"]
            for t in targets:
                if t.get("expr") != "name":
                    raise AgbUnsupported("多目标赋值的目标必须是变量名")
            self.expr(s["value"])
            self.emit("UNPACK", len(targets), line)
            for t in reversed(targets):
                self.emit("STORE_NAME", self.pool.name(t["name"]), line)
            return
        if kind == "aug_assign":
            target = s["target"]
            if target.get("expr") != "name":
                raise AgbUnsupported("当前 AGB 子集只支持对变量做增广赋值")
            self.emit("LOAD_NAME", self.pool.name(target["name"]), line)
            self.expr(s["value"])
            self.emit({
                "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
            }[s["op"]], line=line)
            self.emit("STORE_NAME", self.pool.name(target["name"]), line)
            return
        if kind == "return":
            if s.get("value") is not None:
                self.expr(s["value"])
            else:
                self.emit("LOAD_CONST", self.pool.const(None), line)
            self._emit_finalizers()
            self.emit("RETURN", line=line)
            return
        if kind == "expr":
            self.expr(s["value"])
            self.emit("POP", line=line)
            return
        if kind == "if":
            self.expr(s["cond"])
            jf = self.emit("JUMP_IF_FALSE", -1, line)
            for item in s.get("then") or []:
                self.stmt(item)
            jend = self.emit("JUMP", -1, line) if s.get("else") else None
            self.patch(jf, len(self.code))
            for item in s.get("else") or []:
                self.stmt(item)
            if jend is not None:
                self.patch(jend, len(self.code))
            return
        if kind == "while":
            start = len(self.code)
            self.expr(s["cond"])
            jf = self.emit("JUMP_IF_FALSE", -1, line)
            old_breaks, old_continues = self.breaks, self.continues
            self.breaks, self.continues = [], []
            self.loops.append("while")
            self.loop_handler_depth.append(self.handler_depth)
            for item in s["body"]:
                self.stmt(item)
            self.loop_handler_depth.pop()
            self.loops.pop()
            self.emit("JUMP", start, line)
            end = len(self.code)
            self.patch(jf, end)
            for at in self.breaks:
                self.patch(at, end)
            for at in self.continues:
                self.patch(at, start)
            self.breaks, self.continues = old_breaks, old_continues
            if s.get("else"):
                raise AgbUnsupported("循环的 `否则` 块不在当前 AGB 子集")
            return
        if kind == "for":
            if s.get("else"):
                raise AgbUnsupported("循环的 `否则` 块不在当前 AGB 子集")
            self.expr(s["iter"])
            self.emit("GET_ITER", line=line)
            start = len(self.code)
            jend = self.emit("ITER_NEXT", -1, line)
            self.emit("STORE_NAME", self.pool.name(s["item"]), line)
            old_breaks, old_continues = self.breaks, self.continues
            self.breaks, self.continues = [], []
            self.loops.append("for")
            self.loop_handler_depth.append(self.handler_depth)
            for item in s["body"]:
                self.stmt(item)
            self.loop_handler_depth.pop()
            self.loops.pop()
            self.emit("JUMP", start, line)
            end = len(self.code)
            self.patch(jend, end)
            for at in self.breaks:
                self.patch(at, end)
            for at in self.continues:
                self.patch(at, start)
            self.breaks, self.continues = old_breaks, old_continues
            return
        if kind == "break":
            self._pop_loop_try_handlers()
            self._emit_finalizers()
            if self.loops and self.loops[-1] == "for":
                self.emit("POP_ITER", line=line)
            self.breaks.append(self.emit("JUMP", -1, line))
            return
        if kind == "continue":
            self._pop_loop_try_handlers()
            self._emit_finalizers()
            self.continues.append(self.emit("JUMP", -1, line))
            return
        if kind == "with":
            self.expr(s["expr"])
            self.emit("WITH_ENTER", line=line)
            self.emit("STORE_NAME", self.pool.name(s["name"]), line)
            for item in s.get("body") or []:
                self.stmt(item)
            return
        if kind == "try":
            if not s.get("catch") and not s.get("finally"):
                raise AgbUnsupported("`尝试` 既没有 `捕获` 也没有 `最后`")
            fin = s.get("finally") or []
            has_catch = bool(s.get("catch"))
            outer = None
            if fin:
                # 兜底处理器：任何没被内层捕获的异常都先进清理块，再原样抛回。
                outer = self.emit("TRY_BEGIN", {"target": -1, "type": None}, line)
                self.handler_depth += 1
            inner = None
            if has_catch:
                inner = self.emit("TRY_BEGIN",
                                  {"target": -1, "type": s.get("catch_type")}, line)
                self.handler_depth += 1
            if fin:
                self.finally_blocks.append(fin)
            for item in s["body"]:
                self.stmt(item)
            if fin:
                self.finally_blocks.pop()
            if has_catch:
                self.emit("TRY_END", line=line)
                self.handler_depth -= 1
            for item in s.get("else") or []:
                self.stmt(item)
            jend = self.emit("JUMP", -1, line)
            if has_catch:
                self.patch(inner, len(self.code))
                # 运行期内层处理器已被异常弹出；编译期同步减一。
                self.handler_depth -= 1
                if s.get("catch_name"):
                    self.emit("STORE_NAME", self.pool.name(s["catch_name"]), line)
                else:
                    self.emit("POP", line=line)
                if fin:
                    self.finally_blocks.append(fin)
                for item in s["catch"]:
                    self.stmt(item)
                if fin:
                    self.finally_blocks.pop()
                self.patch(jend, len(self.code))
            if not fin:
                return
            if not has_catch:
                # 没有捕获块时，`jend` 直接落到正常清理路径。
                self.patch(jend, len(self.code))
            # 正常 / 已捕获路径：先弹掉兜底处理器，再执行 `最后`，最后跳过清理块。
            self.emit("TRY_END", line=line)
            self.handler_depth -= 1
            self.finally_blocks.append(fin)
            self._compile_finally_block(fin)
            self.finally_blocks.pop()
            jend2 = self.emit("JUMP", -1, line)
            # 异常清理路径：执行同一段 `最后`，再把原异常抛回外层。
            self.patch(outer, len(self.code))
            self.finally_blocks.append(fin)
            self._compile_finally_block(fin)
            self.finally_blocks.pop()
            self.emit("RETHROW", line=line)
            self.patch(jend2, len(self.code))
            return
        if kind == "match":
            temp = f"__match{self.temp_id}"
            self.temp_id += 1
            self.expr(s["subject"])
            self.emit("STORE_NAME", self.pool.name(temp), line)
            ends = []
            for arm in s["arms"]:
                pat = arm["pattern"]
                wildcard = pat.get("expr") == "name" and pat.get("name") == "_"
                list_pat = pat.get("expr") == "list_pattern"
                map_pat = pat.get("expr") == "map_pattern"
                checks: list[int] = []
                def emit_list_pattern(container: str, items: list[dict]) -> None:
                    """递归发列表模式检查；container 是已存好列表的临时名。"""
                    self.emit("LOAD_NAME", self.pool.name(container), line)
                    self.emit("MATCH_LIST", len(items), line)
                    checks.append(self.emit("JUMP_IF_FALSE", -1, line))
                    for i, it in enumerate(items):
                        kind = it.get("kind")
                        if kind == "literal":
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.emit("LOAD_CONST", self.pool.const(i), line)
                            self.emit("INDEX", line=line)
                            self.expr(it["value"])
                            self.emit("EQ", line=line)
                            checks.append(self.emit("JUMP_IF_FALSE", -1, line))
                        elif kind == "bind":
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.emit("LOAD_CONST", self.pool.const(i), line)
                            self.emit("INDEX", line=line)
                            self.emit("STORE_NAME", self.pool.name(str(it["name"])), line)
                        elif kind == "list":
                            nested_temp = f"__match{self.temp_id}"
                            self.temp_id += 1
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.emit("LOAD_CONST", self.pool.const(i), line)
                            self.emit("INDEX", line=line)
                            self.emit("STORE_NAME", self.pool.name(nested_temp), line)
                            emit_list_pattern(nested_temp, it["pattern"].get("items") or [])
                        elif kind == "map":
                            nested_temp = f"__match{self.temp_id}"
                            self.temp_id += 1
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.emit("LOAD_CONST", self.pool.const(i), line)
                            self.emit("INDEX", line=line)
                            self.emit("STORE_NAME", self.pool.name(nested_temp), line)
                            emit_map_pattern(nested_temp, it["pattern"].get("entries") or [])

                def emit_map_pattern(container: str, entries: list[dict]) -> None:
                    """递归发映射模式检查：键存在 + 值匹配；允许多余键。"""
                    for e in entries:
                        key = e["key"]
                        self.emit("LOAD_NAME", self.pool.name(container), line)
                        self.expr(key)
                        self.emit("CALL_BUILTIN",
                                  {"name": self.pool.name("contains"), "argc": 2}, line)
                        checks.append(self.emit("JUMP_IF_FALSE", -1, line))
                        val = e["value"]
                        kind = val.get("kind")
                        if kind == "literal":
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.expr(key)
                            self.emit("INDEX", line=line)
                            self.expr(val["value"])
                            self.emit("EQ", line=line)
                            checks.append(self.emit("JUMP_IF_FALSE", -1, line))
                        elif kind == "bind":
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.expr(key)
                            self.emit("INDEX", line=line)
                            self.emit("STORE_NAME", self.pool.name(str(val["name"]),), line)
                        elif kind in ("list", "map"):
                            nested_temp = f"__match{self.temp_id}"
                            self.temp_id += 1
                            self.emit("LOAD_NAME", self.pool.name(container), line)
                            self.expr(key)
                            self.emit("INDEX", line=line)
                            self.emit("STORE_NAME", self.pool.name(nested_temp), line)
                            nested = val.get("pattern") or {}
                            if kind == "list":
                                emit_list_pattern(nested_temp, nested.get("items") or [])
                            else:
                                emit_map_pattern(nested_temp, nested.get("entries") or [])

                if map_pat:
                    self._emit_map_pattern(temp, pat.get("entries") or [], line, checks)
                elif list_pat:
                    self._emit_list_pattern(temp, pat.get("items") or [], line, checks)
                elif not wildcard:
                    self.emit("LOAD_NAME", self.pool.name(temp), line)
                    self.expr(pat)
                    self.emit("EQ", line=line)
                    checks.append(self.emit("JUMP_IF_FALSE", -1, line))
                # 绑定模式：主体已经存在 temp 里，进守卫/体之前先落到绑定名。
                bind = arm.get("bind")
                if bind:
                    self.emit("LOAD_NAME", self.pool.name(temp), line)
                    self.emit("STORE_NAME", self.pool.name(str(bind)), line)
                guard_skip = None
                if arm.get("guard") is not None:
                    self.expr(arm["guard"])
                    guard_skip = self.emit("JUMP_IF_FALSE", -1, line)
                for item in arm["body"]:
                    self.stmt(item)
                ends.append(self.emit("JUMP", -1, line))
                for skip in checks:
                    self.patch(skip, len(self.code))
                if guard_skip is not None:
                    self.patch(guard_skip, len(self.code))
            for j in ends:
                self.patch(j, len(self.code))
            return
        if kind == "on_event":
            self.expr(s["task"])
            cb = self._make_callback(s["body"], s["param"], line)
            self.emit("REGISTER_EVENT", {
                "event": s["event"], "name": self.pool.name(cb),
            }, line)
            return
        raise AgbUnsupported(f"当前 AGB 子集不支持语句 `{kind}`")

    def finish(self, name: str, params: list[str], line: int,
               effects: list[str] | None = None) -> dict:
        if not self.code or self.code[-1]["op"] != "RETURN":
            self.emit("LOAD_CONST", self.pool.const(None), line)
            self.emit("RETURN", line=line)
        return {
            "name": name,
            "params": params,
            "effects": list(effects or []),
            "code": self.code,
            "lines": self.lines,
        }


def compile_program(ir: dict) -> dict:
    """IR → AGB。只编译当前 AGB 子集，遇到没覆盖的节点直接报错。"""
    program = ir["program"]
    mod = program["modules"][0]
    pool = _Pool()
    fn_names = {f["name"] for f in mod["functions"]}
    types = {t["name"]: [f["name"] for f in t.get("fields") or []]
             for t in mod.get("types") or []}

    init = _FnCompiler(pool, fn_names, types)
    for c in mod.get("consts") or []:
        init.expr(c["value"])
        init.emit("STORE_NAME", pool.name(c["name"]), _span_line(c))
    init_fn = init.finish("<init>", [], 1)

    funcs = []
    for fn in mod["functions"]:
        fc = _FnCompiler(pool, fn_names, types)
        for item in fn["body"]:
            fc.stmt(item)
        funcs.append(fc.finish(fn["name"],
                               [p["name"] for p in fn.get("params") or []],
                               _span_line(fn),
                               fn.get("effects") or []))
    funcs.extend(pool.extra_fns)

    return {
        "format": FORMAT,
        "version": VERSION,
        "lang": ir.get("lang") or "zh",
        "module": mod["name"],
        "entry": program["entry"],
        "consts": pool.consts,
        "names": pool.names,
        "types": [{"name": n, "fields": fields} for n, fields in types.items()],
        "init": init_fn,
        "functions": funcs,
    }


def encode(agb: dict) -> str:
    return json.dumps(agb, ensure_ascii=False, indent=2)


def decode(text: str) -> dict:
    data = json.loads(text)
    if data.get("format") != FORMAT:
        raise ValueError(f"不是 AGB 文件（format={data.get('format')!r}）")
    if data.get("version") != VERSION:
        raise ValueError(f"AGB 版本不支持：{data.get('version')}（本机支持 {VERSION}）")
    return data


def _arg_text(arg) -> str:
    if arg is None:
        return ""
    if isinstance(arg, dict):
        if "argc" in arg:
            return f"name={arg.get('name')} argc={arg.get('argc')}"
        return json.dumps(arg, ensure_ascii=False)
    return str(arg)


def disassemble(agb: dict) -> str:
    out = [f"; AGB v{agb['version']}  lang={agb.get('lang', 'zh')}  module={agb['module']}  entry={agb['entry']}"]
    out.append("; consts: " + ", ".join(
        f"[{i}] {v!r}" for i, v in enumerate(agb.get("consts") or [])
    ))
    out.append("; names:  " + ", ".join(
        f"[{i}] {v}" for i, v in enumerate(agb.get("names") or [])
    ))
    for fn in [agb.get("init")] + list(agb.get("functions") or []):
        if not fn:
            continue
        out.append("")
        out.append(f"fn {fn['name']}({', '.join(fn.get('params') or [])})")
        for i, (ins, ln) in enumerate(zip(fn["code"], fn["lines"])):
            out.append(f"  {i:04d}  L{ln:<4}  {ins['op']:<14} {_arg_text(ins.get('arg'))}".rstrip())
    return "\n".join(out) + "\n"
