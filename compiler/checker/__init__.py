# -*- coding: utf-8 -*-
"""M3 语义检查器：名字解析 / 轻量类型检查 / 效应传播。

设计取向：
  · **一次收集全部诊断**，不是遇到第一个就停 —— AI 自循环修复时一轮能改完；
  · 类型检查是**轻量**的（够抓住 `"a" > 1` 这类明显错误），不做完整推导；
  · 效应检查是硬规则：**调用带效应的函数，自己必须声明该效应**。

阶段 1 的类型推断能力（明确写清，避免误期待）：
    支持  字面量 / 变量 / 算术 / 比较 / 逻辑 / 调用返回 / 列表 / 映射表 / 成员 / 下标
    不支持 泛型推演、结构体字段推断、函数作为值
"""
from __future__ import annotations

from dataclasses import dataclass

import json
import re

from compiler.diagnostics import Code, Diagnostic, FixHint
from compiler.lexer import LANG_ZH, KeywordTable

# 类型的规范名 → 中文写法（报错只说中文，不甩英文类型名）
TYPE_ZH = {
    "Int": "整数", "Float": "浮数", "Bool": "布尔", "String": "文本",
    "Null": "空", "List": "列表", "Map": "映射表", "Set": "集合",
    "Result": "结果", "Error": "错误", "Task": "任务", "Custom": "自定义类型",
    "Any": "任意",
}
# 反过来：内建表里写的是中文类型名（表是给人看的），这里转回规范名
ZH_TYPE = {v: k for k, v in TYPE_ZH.items()} | {"任意": "Any"}

NUMERIC = {"Int", "Float"}

# 捕获 (错: 类型) 支持的异常类型名（中英通用；映射到宿主异常类在 backend）
_EXC_TYPES = {
    "值错误", "ValueError",
    "类型错误", "TypeError",
    "键错误", "KeyError",
    "下标越界", "IndexError",
    "零除", "ZeroDivisionError",
    "文件错误", "OSError",
    "运行时错误", "RuntimeError",
    "断言错误", "AssertionError",
    "全部", "Exception",
}


def type_of_zh(zh: str) -> dict:
    """内建表里的中文返回类型 → IR 类型节点。"""
    return {"type": ZH_TYPE.get(zh, zh)}


def zh_label(t: str) -> str:
    """规范类型名 → 中文（报错用；不泄漏 Int/String 这类英文名）。"""
    return TYPE_ZH.get(t, t)


def builtin_accepts(want_zh: str, got: str) -> bool:
    """内建函数的形参类型能否接受实参类型。

    宽松是刻意的：类型推断本身是轻量的，宁可漏报也不许误报 ——
    误报会让 AI 去改本来正确的代码。
    """
    want = ZH_TYPE.get(want_zh, want_zh)
    if want in ("Any",) or got in ("Any", "Custom", "未知"):
        return True
    # 形参声明成联合（`列表/集合`）：任一成员类型可接受
    if "/" in want:
        return any(builtin_accepts(part, got) for part in want.split("/"))
    if want == got:
        return True
    # 整数可以当浮数用（反过来不行：会静默丢精度）
    if want == "Float" and got == "Int":
        return True
    # 捕获变量（错误）当「结果」用：`失败原因(错)` 取的就是错误本身的原因，
    # 与 compatible() 里的 Result←Error 分支保持一致，宁漏报不误报。
    if want == "Result" and got == "Error":
        return True
    return False


@dataclass
class Sym:
    name: str
    type: dict
    mutable: bool
    line: int
    # 类型是源码里显式写出来的（`变量 x: 文本` / 参数注解），而不是从初值推出来的。
    # 只有显式类型才在重新赋值时硬校验，避免把 `变量 x = 0` 这种推断变量
    # 后续换用途的历史写法一刀切掉。
    explicit_type: bool = False


class Scope:
    def __init__(self):
        self.stack: list[dict[str, Sym]] = [{}]

    def push(self):
        self.stack.append({})

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()

    def define(self, s: Sym) -> Sym | None:
        cur = self.stack[-1]
        old = cur.get(s.name)
        cur[s.name] = s
        return old

    def lookup(self, name: str) -> Sym | None:
        for frame in reversed(self.stack):
            if name in frame:
                return frame[name]
        return None


class Checker:
    def __init__(self, ir: dict, lang: str,
                 externals: dict[str, dict] | None = None,
                 is_entry: bool = True,
                 source_path: str | None = None):
        self.ir = ir
        self.lang = lang
        self.source_path = source_path
        self.diags: list[Diagnostic] = []
        self.funcs: dict[str, dict] = {}
        self.scope = Scope()
        self.cur_fn: dict | None = None
        # 被导入模块的符号表：{模块名: {"exports": {...}, "private": {...}}}
        self.externals = externals or {}
        self.is_entry = is_entry
        # 本文件导入了哪些模块名，只有这些能被 `模块.函数()` 访问
        self.imported: set[str] = set()
        # 模块别名：{别名: 真实模块名}（`导入 工具 作为 T`）
        self.import_aliases: dict[str, str] = {}
        # FFI 外部库：{库名: 声明效应}。FFI 黑盒调用一律按最坏效应，
        # 除非显式声明更小的效应（D2-A：宁可多写声明，也不许假装纯函数）。
        self.ffi_libs: dict[str, str] = {}
        # 当前表达式是否处在 `等待` 的里面（>0 表示在）。
        # 用一个计数而不是布尔，是因为 `等待 等待 x` 这种嵌套也不该误报。
        self.in_await = 0
        # 当前是否抑制效应传播（>0 表示抑制）。
        # 仅 `任务(...)` 的实参求值期间短暂抬高：任务的效应发生在它自己的
        # 协程里，不该泄漏到启动它的函数身上；且只求这一次，避免重复报错。
        self.suppress_effects = 0

    def _collect_imports(self) -> None:
        for imp in self.ir["program"]["modules"][0].get("imports") or []:
            real = imp["module"]
            self.imported.add(real)
            if imp.get("alias"):
                # 别名指向真实模块：`T.函数()` 与 `工具.函数()` 等价。
                self.imported.add(imp["alias"])
                self.import_aliases[imp["alias"]] = real
            if imp.get("kind") == "ffi":
                self.ffi_libs[real] = imp.get("effect") or "External"

    # ── 诊断收集 ──────────────────────────────────────────────────────
    def err(self, code: str, msg: str, span: dict, hints: list[FixHint] | None = None,
            effects: list[str] | None = None) -> None:
        self.diags.append(Diagnostic(
            code=code, message_zh=msg, severity="error",
            file=span["file"], line=span["line"], col=span["col"],
            end_col=span["endCol"], fix_hints=hints or [], effects=effects or [],
        ))

    def warn(self, code: str, msg: str, span: dict, hints: list[FixHint] | None = None) -> None:
        self.diags.append(Diagnostic(
            code=code, message_zh=msg, severity="warning",
            file=span["file"], line=span["line"], col=span["col"],
            end_col=span["endCol"], fix_hints=hints or [],
        ))

    # ── 入口 ──────────────────────────────────────────────────────────
    def run(self) -> list[Diagnostic]:
        mod = self.ir["program"]["modules"][0]
        self._collect_imports()
        # 结构体：{类型名: [字段…]}，构造与字段访问都按它校验。
        self.structs: dict[str, dict] = {
            t["name"]: t for t in (mod.get("types") or [])
        }
        # 别的模块 `导出 类型` 的结构体：登记成 `模块.类型`，导入方要用它做注解、
        # 构造实例时都按这份字段定义校验。
        for mname, sym in self.externals.items():
            for tname, decl in (sym.get("types") or {}).items():
                self.structs[f"{mname}.{tname}"] = decl
        # 顶层常量：先按源码顺序逐个定型，函数体里就能当只读变量用。
        self.scope = Scope()
        self.const_syms: list[Sym] = []
        for c in mod.get("consts") or []:
            t = self.type_of(c["value"])
            sym = Sym(c["name"], t, False, c["span"]["line"])
            if self.scope.define(sym) is not None:
                self.err(Code.DUP_DECL, f"常量名重复：`{c['name']}`", c["span"],
                         [FixHint("rename", f"{c['span']['file']}:{c['span']['line']}",
                                  "给其中一个常量改名")])
            self.const_syms.append(sym)
        for fn in mod["functions"]:
            if fn["name"] in self.funcs:
                self.err(Code.DUP_DECL, f"函数名重复：`{fn['name']}`", fn["span"],
                         [FixHint("rename", f"{fn['span']['file']}:{fn['span']['line']}",
                                  "给其中一个函数改名")])
            self.funcs[fn["name"]] = fn

        entry = self.ir["program"]["entry"]
        # 只有程序入口文件才必须有入口函数；被导入的模块没有入口是正常的
        if self.is_entry and entry not in self.funcs:
            t = mod["functions"][0]["span"]
            self.err(Code.NO_ENTRY,
                     f"找不到入口函数：`{entry}`",
                     t, [FixHint("add_entry", f"{t['file']}:{t['line']}",
                                 f"补一个 `{self._kw('fn')} {self._kw('main')}() {{ … }}`"
                                 " 作为程序起点")])
        # 入口函数是程序起点，没有调用方；带参数既没法传，也让"谁能跑这个程序"
        # 变得模糊。运行期一跑就崩 AGAL-RT-0099，编译期就该拦住。
        if self.is_entry and entry in self.funcs and self.funcs[entry].get("params"):
            ef = self.funcs[entry]
            self.err(Code.ENTRY_PARAMS,
                     f"入口函数 `{entry}` 不能有参数（程序起点没有调用方）",
                     ef["span"],
                     [FixHint("remove_params", f"{ef['span']['file']}:{ef['span']['line']}",
                              "删掉入口函数的参数，需要的数据在函数内部读")])

        for fn in mod["functions"]:
            self.check_function(fn)
        self.check_dependencies(mod)
        self.check_interfaces(mod)
        self.check_declarations(mod)
        return self.diags

    def check_dependencies(self, mod: dict) -> None:
        """顶层依赖块：包名不重复，版本必须是语义化版本，并与 agl.toml 对齐。"""
        seen: set[str] = set()
        project = self._project_dependencies()
        for dep in mod.get("dependencies") or []:
            if dep["name"] in seen:
                self.err(Code.DUP_DECL, f"依赖包重复：`{dep['name']}`", dep["span"],
                         [FixHint("rename_dep", f"{dep['span']['file']}:{dep['span']['line']}",
                                  "同一个包只留一条版本声明")])
            seen.add(dep["name"])
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", dep["version"]):
                self.err(
                    Code.CONFIG_INVALID,
                    f"依赖 `{dep['name']}` 的版本 `{dep['version']}` 不是语义化版本",
                    dep["span"],
                    [FixHint("dep_semver", f"{dep['span']['file']}:{dep['span']['line']}",
                             "写成 major.minor.patch，如 \"1.2.0\"")],
                )
                continue
            if project is None:
                continue
            if dep["name"] not in project:
                self.err(
                    Code.CONFIG_INVALID,
                    f"依赖 `{dep['name']}` 没有在 agl.toml 的 [dependencies] 里声明",
                    dep["span"],
                    [FixHint("dep_config", f"{dep['span']['file']}:{dep['span']['line']}",
                             f"在 agl.toml 加 `\"{dep['name']}\" = \"{dep['version']}\"`")],
                )
            elif project[dep["name"]] != dep["version"]:
                self.err(
                    Code.CONFIG_INVALID,
                    f"依赖 `{dep['name']}` 版本锁定不一致：源码写 {dep['version']}，"
                    f"agl.toml 写 {project[dep['name']]}",
                    dep["span"],
                    [FixHint("dep_lock", f"{dep['span']['file']}:{dep['span']['line']}",
                             "两边改成同一个版本，或删掉源码 `依赖` 块")],
                )

    def _project_dependencies(self) -> dict[str, str] | None:
        """读入口所在工程的 `agl.toml [dependencies]`；没有配置就返回 None。"""
        if not self.source_path:
            return None
        from pathlib import Path
        from compiler.modules import find_project

        proj = find_project(Path(self.source_path))
        if proj.root is None or proj.config_error:
            return None
        return proj.dependencies

    # ── 接口契约 ──────────────────────────────────────────────────────
    def _iface_type_label(self, t: dict) -> str:
        base = TYPE_ZH.get(t.get("type", ""), t.get("type", "?"))
        args = t.get("args") or []
        if args:
            return f"{base}<{'、'.join(self._iface_type_label(a) for a in args)}>"
        if t.get("type") == "Custom":
            return f"类型 {t.get('declared', '?')}"
        return base

    @staticmethod
    def _same_type(a: dict, b: dict) -> bool:
        return json.dumps(a, sort_keys=True, ensure_ascii=False) == \
            json.dumps(b, sort_keys=True, ensure_ascii=False)

    def check_interfaces(self, mod: dict) -> None:
        """接口只做编译期检查：实现函数必须存在，签名必须一致。"""
        seen: set[str] = set()
        for iface in mod.get("interfaces") or []:
            if iface["name"] in seen:
                self.err(Code.DUP_DECL, f"接口名重复：`{iface['name']}`", iface["span"],
                         [FixHint("rename", f"{iface['span']['file']}:{iface['span']['line']}",
                                  "给其中一个接口改名")])
            seen.add(iface["name"])
            for m in iface["members"]:
                fn = self.funcs.get(m["name"])
                span = m["span"]
                if fn is None:
                    self.err(
                        Code.IFACE_MISSING,
                        f"接口 `{iface['name']}` 声明了 `{m['name']}`，但文件里没有这个函数",
                        span,
                        [FixHint("implement", f"{span['file']}:{span['line']}",
                                 f"补一个 `{self._kw('fn')} {m['name']}(…) {{ … }}`，"
                                 f"或把接口里的这条签名删掉"),
                         FixHint("rename", f"{span['file']}:{span['line']}",
                                 "实现函数名要和接口完全一致")],
                    )
                    continue
                # 参数个数 / 名字 / 类型
                reason = None
                want, got = m["params"], fn.get("params") or []
                if len(want) != len(got):
                    reason = f"参数个数不一致：接口要求 {len(want)} 个，实现给了 {len(got)} 个"
                else:
                    for i, (wp, gp) in enumerate(zip(want, got), 1):
                        if wp["name"] != gp["name"]:
                            reason = (f"第 {i} 个参数名不一致：接口写的是 `{wp['name']}`，"
                                      f"实现写的是 `{gp['name']}`")
                            break
                        if not self._same_type(wp["type"], gp["type"]):
                            reason = (f"参数 `{wp['name']}` 类型不一致：接口要求 "
                                      f"{self._iface_type_label(wp['type'])}，实现给的是 "
                                      f"{self._iface_type_label(gp['type'])}")
                            break
                # 返回类型
                if reason is None:
                    wr = m.get("return") or {"type": "Null"}
                    gr = fn.get("return") or {"type": "Null"}
                    if not self._same_type(wr, gr):
                        reason = (f"返回类型不一致：接口要求 {self._iface_type_label(wr)}，"
                                  f"实现给的是 {self._iface_type_label(gr)}")
                if reason:
                    self.err(
                        Code.IFACE_SIGNATURE,
                        f"接口 `{iface['name']}` 的 `{m['name']}` 实现签名对不上：{reason}",
                        span,
                        [FixHint("align_signature", f"{span['file']}:{span['line']}",
                                 "接口和实现的参数名、参数类型、返回类型必须逐字一致"),
                         FixHint("edit_impl", f"{fn['span']['file']}:{fn['span']['line']}",
                                 f"去改实现函数 `{m['name']}` 的签名")],
                    )

    # ── 声明式块（阶段 2：工具 / 智能体 / 规则） ────────────────────
    @staticmethod
    def _decl_lit(e: dict | None):
        """声明块里的配置值必须是字面量；不是字面量返回 `_NOT_LITERAL`。"""
        if not e:
            return None
        k = e.get("expr")
        if k in ("string", "int", "float", "bool"):
            return e.get("value")
        if k == "name":
            return e.get("name")
        if k == "list":
            return [Checker._decl_lit(x) for x in e.get("items") or []]
        return Checker._NOT_LITERAL

    _NOT_LITERAL = object()

    def check_declarations(self, mod: dict) -> None:
        decls = mod.get("declarations") or []
        if not decls:
            return
        seen: set[str] = set()
        for d in decls:
            if d["name"] in seen or d["name"] in self.funcs:
                self.err(
                    Code.DUP_DECL,
                    f"声明名重复：`{d['name']}`（工具/智能体/规则/函数不能同名）",
                    d["span"],
                    [FixHint("rename_decl", f"{d['span']['file']}:{d['span']['line']}",
                             "给其中一个声明改名")],
                )
            seen.add(d["name"])
            if d["kind"] == "tool":
                self._check_tool_decl(d)
            elif d["kind"] == "agent":
                self._check_agent_decl(d)
            elif d["kind"] == "rule":
                self._check_rule_decl(d)

    def _decl_config(self, d: dict) -> dict:
        cfg = d.get("config") or {}
        if not isinstance(cfg, dict):
            self.err(Code.CONFIG_INVALID, f"`{d['name']}` 的配置块必须是键值表", d["span"])
            return {}
        return cfg

    def _check_tool_decl(self, d: dict) -> None:
        cfg = self._decl_config(d)
        protocol = self._decl_lit(cfg.get("protocol"))
        if protocol is self._NOT_LITERAL or not isinstance(protocol, str):
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 的 `协议` 必须是字符串字面量", d["span"],
                     [FixHint("set_protocol", f"{d['span']['file']}:{d['span']['line']}",
                              '协议只能是 "mcp" / "local-skill" / "acp"')])
            protocol = None
        elif protocol not in ("mcp", "local-skill", "acp"):
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 的协议 `{protocol}` 不支持；"
                     '只能是 "mcp" / "local-skill" / "acp"', d["span"])
        endpoint = self._decl_lit(cfg.get("endpoint"))
        if endpoint is self._NOT_LITERAL or (endpoint is not None and not isinstance(endpoint, str)):
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 的 `地址` 必须是字符串字面量", d["span"])
        elif protocol in ("mcp", "acp") and not endpoint:
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 用协议 `{protocol}` 时必须写 `地址`", d["span"])
        retry = self._decl_lit(cfg.get("max_retry"))
        if retry is not None and (retry is self._NOT_LITERAL or not isinstance(retry, int) or retry < 0):
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 的 `最大重试次数` 必须是 0 或正整数", d["span"])
        timeout = self._decl_lit(cfg.get("timeout_sec"))
        if timeout is not None and (timeout is self._NOT_LITERAL or not isinstance(timeout, int) or timeout <= 0):
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 的 `超时秒数` 必须是正整数", d["span"])
        perms = self._decl_lit(cfg.get("permissions"))
        if perms is not None:
            ok = (isinstance(perms, list)
                  and all(isinstance(x, str) for x in perms))
            if not ok:
                self.err(Code.CONFIG_INVALID,
                         f"工具 `{d['name']}` 的 `权限` 必须是字符串数组", d["span"])
        subscribe = self._decl_lit(cfg.get("subscribe"))
        if subscribe is not None and (subscribe is self._NOT_LITERAL
                                      or not isinstance(subscribe, bool)):
            self.err(Code.CONFIG_INVALID,
                     f"工具 `{d['name']}` 的 `订阅` 必须是 真 / 假", d["span"])

    def _check_agent_decl(self, d: dict) -> None:
        target = self._decl_lit(d.get("target"))
        if target is self._NOT_LITERAL or not isinstance(target, str) or not target.strip():
            self.err(Code.CONFIG_INVALID,
                     f"智能体 `{d['name']}` 必须写非空的 `目标 = \"…\"`", d["span"],
                     [FixHint("add_agent_target", f"{d['span']['file']}:{d['span']['line']}",
                              '在智能体块里补 `目标 = "要完成的事"`')])
        cfg = self._decl_config(d)
        turns = self._decl_lit(cfg.get("max_turns"))
        if turns is not None and (turns is self._NOT_LITERAL
                                  or not isinstance(turns, int) or turns <= 0):
            self.err(Code.CONFIG_INVALID,
                     f"智能体 `{d['name']}` 的 `最大轮数` 必须是正整数", d["span"])

    def _check_rule_decl(self, d: dict) -> None:
        f = d.get("fields") or {}
        missing = [k for k in ("version", "scope", "priority", "content") if k not in f]
        if missing:
            self.err(Code.CONFIG_INVALID,
                     f"规则 `{d['name']}` 缺少必填字段：{' / '.join(missing)}", d["span"])
        version = self._decl_lit(f.get("version"))
        if version is not None and (version is self._NOT_LITERAL
                                    or not isinstance(version, int) or version < 1):
            self.err(Code.CONFIG_INVALID,
                     f"规则 `{d['name']}` 的 `版本` 必须是正整数", d["span"])
        scope = self._decl_lit(f.get("scope"))
        if scope is not None:
            ok = (scope == "全项目")
            if not ok:
                se = f.get("scope") or {}
                ok = se.get("expr") == "scope" and se.get("level") in ("模块", "目标") \
                    and isinstance(se.get("value"), str) and bool(se["value"])
            if not ok:
                self.err(Code.CONFIG_INVALID,
                         f"规则 `{d['name']}` 的 `生效范围` 只能是 "
                         "全项目 / 模块 名字 / 目标 名字", d["span"])
        priority = self._decl_lit(f.get("priority"))
        if priority is not None and priority not in ("高", "普通", "低", "high", "normal", "low"):
            self.err(Code.CONFIG_INVALID,
                     f"规则 `{d['name']}` 的 `优先级` 只能是 高 / 普通 / 低", d["span"])
        content = self._decl_lit(f.get("content"))
        if content is not None and (content is self._NOT_LITERAL or not isinstance(content, str)
                                    or not content.strip()):
            self.err(Code.CONFIG_INVALID,
                     f"规则 `{d['name']}` 的 `内容` 必须是非空字符串", d["span"])
        expires = self._decl_lit(f.get("expires"))
        if expires is not None and (expires is self._NOT_LITERAL
                                    or not isinstance(expires, str)
                                    or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expires)):
            self.err(Code.CONFIG_INVALID,
                     f"规则 `{d['name']}` 的 `过期时间` 必须写成 YYYY-MM-DD", d["span"])

    # ── 函数 ──────────────────────────────────────────────────────────
    def check_function(self, fn: dict) -> None:
        self.cur_fn = fn
        self.in_await = 0
        self.suppress_effects = 0
        # 重复效应
        seen: set[str] = set()
        for e in fn.get("effects", []):
            if e in seen:
                self.err(Code.DUP_EFFECT, f"效应声明重复：`{self._effect_text(e)}`", fn["span"],
                         [FixHint("remove_dup", f"{fn['span']['file']}:{fn['span']['line']}",
                                  "删掉重复的那一个")])
            seen.add(e)

        self.scope = Scope()
        for s in getattr(self, "const_syms", []):
            self.scope.define(s)
        self.scope.push()
        for p in fn["params"]:
            self.scope.define(Sym(p["name"], p["type"], True, fn["span"]["line"],
                                  explicit_type=True))
            if "default" in p:
                # 默认值在函数定义时求值（早于顶层常量与运行时模板），
                # 所以只允许字面量表达式；引用变量/调用函数会在 import 阶段炸。
                self.check_default_expr(p["default"])

        # 校验块：必须是可静态判断的比较
        for c in fn.get("checks", []):
            self.check_static(c)

        for s in fn["body"]:
            self.stmt(s)

    def check_static(self, e: dict) -> None:
        if e.get("expr") in ("binop", "unop") or e.get("expr") == "bool":
            return
        if e.get("expr") == "call":
            # 允许 长度(x) 之类的静态可判调用，暂不深究
            return
        self.err(Code.CHECK_NOT_STATIC,
                 "校验块里只能写可静态判断的条件（比较、逻辑组合），这里写的是运行时才能算的表达式",
                 e["span"],
                 [FixHint("rewrite_check", f"{e['span']['file']}:{e['span']['line']}",
                          "改成常量之间的比较，如 `路径 不等于 \"\"`")])

    def check_default_expr(self, e: dict) -> None:
        """默认参数值只允许字面量表达式（含字面量之间的运算与容器）。

        默认值在 `def` 执行时求值，那时运行时模板和顶层常量都还没落地；
        引用名字 / 调用函数会直接 `NameError`。与其运行时炸，不如定义时就拒绝。
        """
        k = e.get("expr")
        if k in ("unop", "binop"):
            for sub in ("operand", "left", "right"):
                if isinstance(e.get(sub), dict):
                    self.check_default_expr(e[sub])
            return
        if k in ("int", "float", "string", "bool", "null"):
            return
        if k in ("list", "map", "set"):
            self.err(Code.TYPE_MISMATCH,
                     "可变容器不能做默认值：默认值只求值一次，会被所有调用共享，"
                     "一次 `追加`/`删除` 就污染后续调用；用 `空` 做默认值，在函数体里新建",
                     e.get("span") or {"file": "", "line": 1, "col": 0, "endCol": 0},
                     [FixHint("no_mutable_default", "",
                              "改成 `= 空`，函数开头 `如果 参数 等于 空 { 参数 = [] }`")])
            return
        self.err(Code.TYPE_MISMATCH,
                 "默认参数值只能是字面量表达式（不引用变量、不调用函数）；"
                 "需要运行时值，请在函数体里判断",
                 e.get("span") or {"file": "", "line": 1, "col": 0, "endCol": 0},
                 [FixHint("literal_default", "",
                          "把默认值改成立面值，如 `0` / `\"\"` / `[]`")])

    # ── 语句 ──────────────────────────────────────────────────────────
    def stmt(self, s: dict) -> None:
        k = s.get("stmt")
        if k == "let":
            t = self.type_of(s["value"])
            declared = s.get("type")
            if declared and not self.compatible(declared, t):
                self.err(Code.TYPE_MISMATCH,
                         f"类型不匹配：声明的是 `{self._type_label_node(declared)}`，"
                         f"给的是 `{self._type_label_node(t)}`",
                         s["span"], [FixHint("fix_type", f"{s['span']['file']}:{s['span']['line']}",
                                             "改值，或改声明的类型")])
            if self.scope.lookup(s["name"]):
                self.err(Code.DUP_DECL, f"名字 `{s['name']}` 在同一作用域重复声明", s["span"],
                         [FixHint("rename", f"{s['span']['file']}:{s['span']['line']}", "换个名字")])
            self.scope.define(Sym(s["name"], declared or t, s.get("mutable", True),
                                  s["span"]["line"], explicit_type=bool(declared)))
            return

        if k == "match":
            subject_t = self.type_of(s["subject"])
            def collect_list_binds(lst_pat: dict, subj_t: dict
                                   ) -> list[tuple[str, dict]]:
                """递归收集列表模式里的绑定名及其元素类型。"""
                out: list[tuple[str, dict]] = []
                for it in lst_pat.get("items") or []:
                    kind = it.get("kind")
                    if kind == "bind":
                        out.append((str(it.get("name")), subj_t))
                    elif kind == "list":
                        nested_t = {"type": "Any"}
                        if subj_t.get("type") == "List" and subj_t.get("args"):
                            nested_t = subj_t["args"][0]
                        out.extend(collect_list_binds(it.get("pattern") or {}, nested_t))
                    elif kind == "map":
                        out.extend(collect_map_binds(it.get("pattern") or {}))
                    elif kind in ("wild", "literal"):
                        continue
                    else:
                        self.err(Code.EXPECT,
                                 "列表模式的元素只允许字面量、`_`、`变量 名`、`[ … ]` 或 `{ … }`",
                                 it.get("span") or lst_pat.get("span") or s["span"])
                return out

            def collect_map_binds(map_pat: dict) -> list[tuple[str, dict]]:
                """递归收集映射模式里的绑定名；映射值的静态类型取 Any。"""
                out: list[tuple[str, dict]] = []
                for e in map_pat.get("entries") or []:
                    val = e.get("value") or {}
                    kind = val.get("kind")
                    if kind == "bind":
                        out.append((str(val.get("name")), {"type": "Any"}))
                    elif kind == "list":
                        out.extend(collect_list_binds(val.get("pattern") or {},
                                                      {"type": "Any"}))
                    elif kind == "map":
                        out.extend(collect_map_binds(val.get("pattern") or {}))
                    elif kind in ("wild", "literal"):
                        continue
                    else:
                        self.err(Code.EXPECT,
                                 "映射模式的值只允许字面量、`_`、`变量 名`、`[ … ]` 或 `{ … }`",
                                 val.get("span") or map_pat.get("span") or s["span"])
                return out

            seen: list[str] = []
            covered: list[str] = []
            has_fallback = False
            for arm in s["arms"]:
                pat = arm["pattern"]
                bind = arm.get("bind")
                wildcard = (pat.get("expr") == "name" and pat.get("name") == "_")
                list_pat = pat.get("expr") == "list_pattern"
                list_binds = (collect_list_binds(pat, subject_t)
                              if list_pat else [])
                map_pat = pat.get("expr") == "map_pattern"
                map_binds = (collect_map_binds(pat) if map_pat else [])
                if wildcard and arm.get("guard") is None:
                    has_fallback = True
                elif (not wildcard and not list_pat
                      and pat.get("expr") in ("string", "int", "float", "bool", "null")):
                    key = f"{pat['expr']}:{pat.get('value')!r}"
                    # 带守卫的分支不一定覆盖该字面量，允许同值多守卫；
                    # 只有前面已经有无守卫分支时，后面同值分支才真的不可达。
                    if key in covered:
                        self.warn(Code.MATCH_DUP_ARM,
                                  "这个匹配分支和前面的分支条件重复，永远不会被选中",
                                  pat.get("span") or s["span"],
                                  [FixHint("remove_arm", f"{s['span']['file']}:{s['span']['line']}",
                                           "删掉重复的分支，或改成别的字面量")])
                    if arm.get("guard") is None:
                        covered.append(key)
                    seen.append(key)
                if not (pat.get("expr") in ("string", "int", "float", "bool", "null")
                        or wildcard or bind or list_pat or map_pat):
                    self.err(Code.EXPECT,
                             "匹配模式只允许字面量、`_` 通配符、`变量 名` 绑定、`[ … ]` 列表模式或 `{ … }` 映射模式",
                             pat.get("span") or s["span"],
                             [FixHint("use_literal", f"{s['span']['file']}:{s['span']['line']}",
                                      "用 整数/文本/布尔/空 字面量、`_`、`变量 名`、`[ … ]` 或 `{ … }`")])
                    continue
                # 绑定名只在「守卫 + 分支体」这个臂作用域内可见，退出后不泄漏。
                binds = list(list_binds) + list(map_binds)
                if bind or binds:
                    self.scope.push()
                if bind:
                    self.scope.define(Sym(str(bind), subject_t, False, s["span"]["line"]))
                if binds:
                    for b_name, b_type in binds:
                        self.scope.define(Sym(str(b_name), b_type, False, s["span"]["line"]))
                if arm.get("guard") is not None:
                    guard_t = self.type_of(arm["guard"])
                    if guard_t.get("type") not in ("Bool", "Any"):
                        self.err(Code.TYPE_MISMATCH,
                                 "匹配守卫必须是布尔表达式",
                                 arm["guard"].get("span") or s["span"])
                for x in arm["body"]:
                    self.stmt(x)
                if bind or binds:
                    self.scope.pop()
            # 只有「真假」是有限可穷尽的：其它类型不可能列全，不打扰。
            if (subject_t.get("type") == "Bool" and not has_fallback
                    and not {"bool:True", "bool:False"}.issubset(set(seen))):
                self.warn(Code.MATCH_NOT_EXHAUSTIVE,
                          "`匹配` 没有兜底分支，真假两种值没有都覆盖，未命中的情况会静默跳过",
                          s["span"],
                          [FixHint("add_default", f"{s['span']['file']}:{s['span']['line']}",
                                   "补一个 `否则 => …`（或 `_ => …`）分支")])
            return
        if k == "with":
            self.type_of(s["expr"])
            if s.get("name"):
                self.scope.define(Sym(s["name"], {"type": "Any"}, True, s["span"]["line"]))
            for x in s["body"]:
                self.stmt(x)
            return
        if k == "let_multi":
            t = self.type_of(s["value"])
            declared = s.get("type")
            if declared and not self.compatible(declared, t):
                self.err(Code.TYPE_MISMATCH,
                         f"类型不匹配：声明的是 `{self._type_label_node(declared)}`，"
                         f"给的是 `{self._type_label_node(t)}`",
                         s["span"], [FixHint("fix_type", f"{s['span']['file']}:{s['span']['line']}",
                                             "改值，或改声明的类型")])
            for name in s["names"]:
                if self.scope.lookup(name):
                    self.err(Code.DUP_DECL, f"名字 `{name}` 在同一作用域重复声明", s["span"],
                             [FixHint("rename", f"{s['span']['file']}:{s['span']['line']}", "换个名字")])
                self.scope.define(Sym(name, declared or t, s.get("mutable", True),
                                      s["span"]["line"], explicit_type=bool(declared)))
            return
        if k == "assign_multi":
            t = self.type_of(s["value"])
            for target in s["targets"]:
                sym = self.scope.lookup(target["name"])
                if sym is None:
                    self.undef(target)
                    continue
                if not sym.mutable:
                    self.err(Code.CONST_REASSIGN, f"`{target['name']}` 是常量，不能重新赋值",
                             s["span"], [FixHint("make_let", f"{sym.line}",
                                                 "把它改成 `变量` 声明")])
                    continue
                if sym.explicit_type and not self.compatible(sym.type, t):
                    self.err(Code.TYPE_MISMATCH,
                             f"`{target['name']}` 声明的类型是 "
                             f"`{self._type_label_node(sym.type)}`，这次给的是 "
                             f"`{self._type_label_node(t)}`",
                             s["span"], [FixHint("fix_type", f"{s['span']['file']}:{s['span']['line']}",
                                                 "改值，或改声明类型")])
            return
        if k == "assign":
            target = s["target"]
            t = self.type_of(s["value"])
            if target.get("expr") == "name":
                sym = self.scope.lookup(target["name"])
                if sym is None:
                    self.undef(target)
                elif not sym.mutable:
                    self.err(Code.CONST_REASSIGN, f"`{target['name']}` 是常量，不能重新赋值",
                             s["span"], [FixHint("make_let", f"{sym.line}",
                                                 "把它改成 `变量` 声明")])
                elif sym.explicit_type and not self.compatible(sym.type, t):
                    self.err(Code.TYPE_MISMATCH,
                             f"`{target['name']}` 声明的类型是 "
                             f"`{self._type_label_node(sym.type)}`，这次给的是 "
                             f"`{self._type_label_node(t)}`",
                             s["span"], [FixHint("fix_type", f"{s['span']['file']}:{s['span']['line']}",
                                                 "改值，或改声明类型")])
            else:
                # 成员 / 下标 等复合左值：检查它里面的表达式（函数调用等）
                self.check_lvalue_readonly(target)
            return

        if k == "aug_assign":
            # `x += 1`：先当「读取」校验左值存在且类型成立，再判断能不能写。
            target = s["target"]
            sym = None
            if target.get("expr") == "name":
                sym = self.scope.lookup(target["name"])
                if sym is None:
                    self.undef(target)
                elif not sym.mutable:
                    self.err(Code.CONST_REASSIGN, f"`{target['name']}` 是常量，不能做增广赋值",
                             s["span"], [FixHint("make_let", f"{sym.line}",
                                                 "把它改成 `变量` 声明")])
            else:
                # 成员 / 下标 等复合左值：先确保它成立（增广会先读后写）
                self.check_lvalue_readonly(target)
            result = self.type_of({"expr": "binop", "op": s["op"], "left": target,
                                   "right": s["value"], "span": s["span"]})
            if sym is not None and sym.explicit_type \
                    and s["op"] in ("+", "-", "*", "/", "%") \
                    and not self.compatible(sym.type, result):
                self.err(Code.TYPE_MISMATCH,
                         f"`{target['name']}` 声明的类型是 "
                         f"`{self._type_label_node(sym.type)}`，这次运算结果是 "
                         f"`{self._type_label_node(result)}`",
                         s["span"], [FixHint("fix_type", f"{s['span']['file']}:{s['span']['line']}",
                                             "改值，或改声明类型")])
            return

        if k == "return":
            if "value" in s:
                t = self.type_of(s["value"])
                declared = (self.cur_fn or {}).get("return")
                # 没有写 `->` 的函数沿用旧口径（返回类型为 `空`），
                # 只有显式写了返回类型才做返回值校验，避免把历史用法一刀切。
                if declared and declared.get("type") != "Null" \
                        and not self.compatible(declared, t):
                    self.err(Code.TYPE_MISMATCH,
                             f"返回类型不匹配：函数声明返回 "
                             f"`{self._type_label_node(declared)}`，这里返回的是 "
                             f"`{self._type_label_node(t)}`",
                             s["span"], [FixHint("fix_return",
                                                 f"{s['span']['file']}:{s['span']['line']}",
                                                 "改返回值，或改函数声明的返回类型")])
            return

        if k == "expr":
            self.type_of(s["value"])
            return

        if k == "on_event":
            # 订阅回调：任务表达式先求值（确认它存在、类型没错），
            # 再把回调参数定义进一个新作用域 —— 回调里的名字不外泄。
            self.type_of(s["task"])
            self.scope.push()
            self.scope.define(Sym(s["param"], s["param_type"], False, s["span"]["line"]))
            for x in s["body"]:
                self.stmt(x)
            self.scope.pop()
            return

        if k == "if":
            self._check_cond(s["cond"])
            self.scope.push()
            for x in s["then"]:
                self.stmt(x)
            self.scope.pop()
            if "else" in s:
                self.scope.push()
                for x in s["else"]:
                    self.stmt(x)
                self.scope.pop()
            return

        if k == "while":
            self._check_cond(s["cond"])
            self.scope.push()
            for x in s["body"]:
                self.stmt(x)
            if s.get("else") is not None:
                # 否则块复用循环帧（能看到循环变量与循环体的声明，与 Python
                # 函数作用域一致；导入器另有提升机制保证块内声明在外可见）。
                for x in s["else"]:
                    self.stmt(x)
            self.scope.pop()
            return

        if k == "for":
            self.type_of(s["iter"])
            self.scope.push()
            # 循环变量类型由元素未知，给 Custom 占位，避免误报
            self.scope.define(Sym(s["item"], {"type": "Custom", "declared": "元素"},
                                  True, s["span"]["line"]))
            for x in s["body"]:
                self.stmt(x)
            if s.get("else") is not None:
                for x in s["else"]:
                    self.stmt(x)
            self.scope.pop()
            return

        if k == "try":
            # 后端发原生 Python try/finally（函数作用域）：finally 本就能看到
            # try 体里声明的变量。所以 try 帧必须留到 finally 检查完才弹——
            # 旧实现 try 体查完就 pop，finally 再另起全新帧，导致 finally 里
            # 引用 try 体的变量被误报 0302「使用了没有声明的名字」。
            self.scope.push()
            for x in s["body"]:
                self.stmt(x)
            if s.get("catch"):
                # 捕获变量只在 catch 块里可见：catch 保留独立帧，用完即弹
                self.scope.push()
                if s.get("catch_name"):
                    self.scope.define(Sym(s["catch_name"], {"type": "Error"}, True,
                                          s["span"]["line"]))
                if s.get("catch_type"):
                    if s["catch_type"] not in _EXC_TYPES:
                        self.err(Code.TYPE_MISMATCH,
                                 f"`{s['catch_type']}` 不是已知的异常类型："
                                 "可用 值错误/类型错误/键错误/下标越界/零除/"
                                 "文件错误/运行时错误/断言错误/全部",
                                 s["span"],
                                 [FixHint("fix_type", f"{s['span']['file']}:{s['span']['line']}",
                                          "换一个已知的异常类型名")])
                for x in s["catch"]:
                    self.stmt(x)
                self.scope.pop()
            if s.get("else") is not None:
                # 否则块复用 try 帧：能看到 try 体声明的变量（与 Python
                # 函数作用域一致，finally 同款处理）。
                for x in s["else"]:
                    self.stmt(x)
            if s.get("finally") is not None:
                # finally 复用 try 帧（不另起新帧）：能看到 try 体声明的变量。
                # 用 is not None 而不是真值判断，否则空 `最后 {}`（finally 为 []）
                # 会被当成没有 finally 而整段跳过。
                for x in s["finally"]:
                    self.stmt(x)
            self.scope.pop()
            return

        # break / continue 无需检查

    # ── 类型 ──────────────────────────────────────────────────────────
    def undef(self, e: dict) -> None:
        name = e.get("name", "?")
        self.err(Code.UNDEFINED, f"使用了没有声明的名字 `{name}`", e["span"],
                 [FixHint("declare", f"{e['span']['file']}:{e['span']['line']}",
                          f"先声明它（`变量 {name} = …`），或检查是不是拼错了")])

    def type_of_target_call(self, e: dict) -> dict:
        """`模块.函数(...)` 的检查与类型推断。

        只处理"目标是导入进来的模块"这一种；其余（如 `列表.追加()`）
        属于对象方法，阶段 1 不做成员推断，交给运行时。
        """
        name = e["name"]
        target = e["target"]
        span = e["span"]

        if target.get("expr") != "name":
            self.type_of(target)
            return {"type": "Custom", "declared": "未知"}

        mod_name = target["name"]
        # 别名先还原成真实模块名，后面的导入表/符号表都按真实名查。
        mod_name = self.import_aliases.get(mod_name, mod_name)
        # FFI 黑盒调用：外部库的方法，编译器看不见签名，只能做到"认得它是 FFI"
        # 并强制调用方声明相应效应。具体参数/返回类型未知（外部库的真相在宿主侧）。
        if mod_name in self.ffi_libs:
            # 打标记：后端据此生成 `_agl_ffi_call(lib, method, *args)`，不按名字猜。
            e["ffi"] = mod_name
            eff = self.ffi_libs[mod_name]
            # 效应强制：调用外部库 = 调用方必须具备该效应（最坏情况 External）。
            # 缺声明即编译期报错（PURE_CALLS_EFFECT），绝不假装纯函数。
            self.check_effects(e, f"{mod_name}.{name}", {"effects": [eff]})
            return {"type": "Custom", "declared": "未知"}

        if mod_name not in self.imported:
            # 不是导入的模块：可能是普通对象的方法调用，阶段 1 不深究
            self.type_of(target)
            return {"type": "Custom", "declared": "未知"}

        ext = self.externals.get(mod_name)
        if ext is None:
            self.err(Code.MODULE_NOT_FOUND,
                     f"找不到模块 `{mod_name}` 的符号表（编译器内部问题，或该模块编译失败）",
                     span, [FixHint("report_bug", f"{span['file']}:{span['line']}",
                                    "把这段代码和报错一起上报")])
            return {"type": "Custom", "declared": "未知"}

        fn = ext["exports"].get(name)
        # 跨模块结构体构造：`模型.用户(编号: 1, …)`。字段定义来自被导入模块的
        # `导出 类型`，构造出的值按 `模型.用户` 这个限定名做类型身份。
        ext_types = ext.get("types") or {}
        if name in ext_types:
            # 跨模块结构体构造：`模型.用户(...)`。就地改写成 `struct_call`，
            # 后端按 `_agl_模块._agl_t_用户(...)` 发射；不再保留的类型名做运行时查找。
            e["expr"] = "struct_call"
            e["module"] = mod_name
            e.pop("target", None)
            return self._check_struct_args(ext_types[name], f"{target['name']}.{name}",
                                           e["args"], span, f"{mod_name}.{name}")
        if fn is None:
            ext_consts = ext.get("consts") or {}
            if name in ext_consts:
                self.err(Code.TYPE_MISMATCH,
                         f"`{target['name']}.{name}` 是常量，不能当函数调用",
                         span, [FixHint("check_name", f"{span['file']}:{span['line']}",
                                        "去掉括号，直接读这个常量")])
                return {"type": "Custom", "declared": "未知"}
            if name in ext["private"] or name in (ext.get("private_consts") or []):
                self.err(Code.NOT_EXPORTED,
                         f"`{mod_name}.{name}` 没有导出，外部不能使用",
                         span, [FixHint("add_export", f"{mod_name}.agl",
                                        f"在 {mod_name}.agl 里给 `{name}` 加上 `导出` 标记")])
            else:
                self.err(Code.NO_SUCH_MEMBER,
                         f"模块 `{mod_name}` 里没有 `{name}`",
                         span, [FixHint("check_name", f"{span['file']}:{span['line']}",
                                        f"检查拼写，或到 {mod_name}.agl 里看有哪些函数")])
            return {"type": "Custom", "declared": "未知"}

        self.resolve_args(fn["params"], e["args"], span, f"{mod_name}.{name}")
        # 跨模块同样传播效应：调用别的模块的网络函数，自己也得声明网络
        self.check_effects(e, f"{mod_name}.{name}", fn)
        # 返回类型里的结构体名要加上模块前缀，导入方才能按字段校验成员访问。
        return self._qualify_type(fn.get("return") or {"type": "Null"}, mod_name)

    def _qualify_type(self, t: dict, mod: str) -> dict:
        """把别的模块签名里的裸结构体名改成 `模块.类型`。"""
        if t.get("type") == "Custom":
            name = t.get("declared")
            if name and name in (self.externals.get(mod, {}).get("types") or {}):
                return {"type": "Custom", "declared": f"{mod}.{name}"}
        return t

    # ── 赋值左值：只读属性拦截 ──────────────────────────────────────────
    def check_lvalue_readonly(self, target: dict) -> None:
        """`人.名字 = …` / `名单.长度 = …` 一律拒绝：AGL 的属性都是只读的。

        结构体字段、`.长度`、`.消息` 在语言层都是只读的（设计 §3.19 / §3.16.3）。
        放过的话，后端会把它们原样发成宿主属性赋值：有的静默生效、有的运行期
        才崩，审计者看不出哪一行越权了。
        """
        self.type_of(target)
        if target.get("expr") != "member":
            return
        holder = target["target"]
        holder_t = self.type_of(holder)
        field = target["field"]
        decl = holder_t.get("declared")
        if holder_t.get("type") == "Custom" and decl in self.structs:
            self.err(Code.CONST_REASSIGN,
                     f"结构体 `{decl}` 的字段 `{field}` 是只读的，不能赋值",
                     target["span"],
                     [FixHint("no_field_write", f"{target['span']['file']}:{target['span']['line']}",
                              "结构体一旦构造就不再改字段；需要新值就构造一个新结构体")])
        elif field in ("长度", "length", "消息", "message") and \
                holder_t.get("type") in ("String", "List", "Map", "Set", "Error", "Custom"):
            self.err(Code.CONST_REASSIGN,
                     f"`{field}` 是只读属性，不能赋值",
                     target["span"],
                     [FixHint("no_field_write", f"{target['span']['file']}:{target['span']['line']}",
                              "只读属性只能读；换成普通变量存结果")])

    # ── 内建函数 ──────────────────────────────────────────────────────
    def resolve_args(self, params: list[dict], args: list[dict], span: dict, fname: str) -> None:
        """位置实参 + 命名实参的数量/名字校验（默认参数按缺省补齐）。

        规则与 Python 一致：位置实参不能超过参数总数；命名实参必须对得上参数名、
        不能重复、不能和位置实参抢同一个参数；必填参数必须有值。
        """
        pos = [a for a in args if a.get("expr") != "named"]
        named = [a for a in args if a.get("expr") == "named"]
        idx = {p["name"]: i for i, p in enumerate(params)}
        if len(pos) > len(params):
            self.err(Code.TYPE_MISMATCH,
                     f"`{fname}` 最多 {len(params)} 个参数，这里给了 {len(pos)} 个位置实参",
                     span, [FixHint("fix_arity", f"{span['file']}:{span['line']}",
                                    "按函数定义补齐或删掉多余参数")])
            return
        seen: set[str] = set()
        for a in named:
            nm = a["name"]
            if nm not in idx:
                self.err(Code.TYPE_MISMATCH,
                         f"`{fname}` 没有名为 `{nm}` 的参数",
                         a["span"], [FixHint("check_name", f"{a['span']['file']}:{a['span']['line']}",
                                             "检查参数名，或改用位置实参")])
            elif nm in seen:
                self.err(Code.TYPE_MISMATCH, f"参数 `{nm}` 重复传值", a["span"])
            elif idx[nm] < len(pos):
                self.err(Code.TYPE_MISMATCH,
                         f"参数 `{nm}` 已经由第 {idx[nm] + 1} 个位置实参传了值，不能重复",
                         a["span"])
            seen.add(nm)
        filled = set(range(len(pos))) | {idx[nm] for nm in seen if nm in idx}
        missing = [p["name"] for i, p in enumerate(params)
                   if "default" not in p and i not in filled]
        if missing:
            self.err(Code.TYPE_MISMATCH,
                     f"`{fname}` 缺少参数：`{'`、`'.join(missing)}`",
                     span, [FixHint("fix_arity", f"{span['file']}:{span['line']}",
                                    "补上缺少的实参，或给参数加默认值")])
        # 数量/名字都合法后，再按参数声明做类型校验。顺序很重要：先报缺参/
        # 多参，避免这里因为 params[i] 不存在而抛 IndexError。
        self.check_arg_types(params, args, span, fname)

    def check_arg_types(self, params: list[dict], args: list[dict],
                        span: dict, fname: str) -> None:
        """函数实参按参数声明类型校验，语义与赋值/返回完全一致（0301）。

        只对**有类型声明**的参数做校验；`任意` 由 compatible() 放行。
        """
        idx = {p["name"]: i for i, p in enumerate(params)}
        pos = [a for a in args if a.get("expr") != "named"]
        for i, a in enumerate(pos):
            if i >= len(params):
                break
            want = params[i].get("type")
            if not want:
                continue
            got = self.type_of(a)
            if not self.compatible(want, got):
                self.err(Code.TYPE_MISMATCH,
                         f"`{fname}` 的参数 `{params[i]['name']}` 声明为 "
                         f"`{self._type_label_node(want)}`，这次给的是 "
                         f"`{self._type_label_node(got)}`",
                         a.get("span") or span,
                         [FixHint("fix_type", f"{span['file']}:{span['line']}",
                                  "改实参，或改函数参数声明的类型")])
        for a in args:
            if a.get("expr") != "named":
                continue
            nm = a.get("name")
            if nm not in idx:
                continue
            p = params[idx[nm]]
            want = p.get("type")
            if not want:
                continue
            got = self.type_of(a["value"])
            if not self.compatible(want, got):
                self.err(Code.TYPE_MISMATCH,
                         f"`{fname}` 的参数 `{nm}` 声明为 "
                         f"`{self._type_label_node(want)}`，这次给的是 "
                         f"`{self._type_label_node(got)}`",
                         a.get("span") or span,
                         [FixHint("fix_type", f"{span['file']}:{span['line']}",
                                  "改实参，或改函数参数声明的类型")])

    def check_builtin_call(self, e: dict, bi) -> dict:
        """内建函数的签名校验，并在 IR 上打 `builtin` 标记。

        标记只写一次、只由语义层写：后端拿到 `builtin` 就直接换成宿主侧的落地实现，
        不再靠函数名猜 —— 名字可以撞车，标记不会。

        实参类型只在此处求值一次。求值上下文由内建决定：
          · `任务(...)` 的实参是"一个还没跑的异步调用"，要在 `in_await` 抬高、
            效应抑制的上下文里求，否则会误报「异步函数没等待」和「没声明效应」；
          · 其余内建的实参是普通值，正常求。
        """
        name = bi.zh if self.lang == LANG_ZH else bi.en
        if any(a.get("expr") == "named" for a in e["args"]):
            self.err(Code.BUILTIN_ARITY,
                     f"内建函数 `{name}` 不支持命名实参",
                     e["span"], [FixHint("use_positional",
                                         f"{e['span']['file']}:{e['span']['line']}",
                                         "改回按顺序写位置实参")])
            for a in e["args"]:
                self.type_of(a.get("value") if a.get("expr") == "named" else a)
            return type_of_zh(bi.ret)
        n = len(e["args"])
        need = bi.min_args()
        if bi.rest:
            ok, expect = n >= need, f"至少 {need} 个"
        elif bi.opt_tail:
            # 末尾可选参数：个数可以在 [最少, 全部] 之间
            ok, expect = need <= n <= len(bi.params), f"{need}~{len(bi.params)} 个"
        else:
            ok, expect = n == need, f"{need} 个"
        pos = f"{e['span']['file']}:{e['span']['line']}"
        if not ok:
            self.err(Code.BUILTIN_ARITY,
                     f"`{name}` 需要 {expect}参数，这里给了 {n} 个",
                     e["span"],
                     [FixHint("fix_arity", pos, f"用法：{bi.summary_zh}")])
            # 个数不对就不再查类型，否则会连出一串噪音
            return type_of_zh(bi.ret)

        # 实参类型在此处求且只求一次。
        # `任务(...)` 的实参是异步调用，抬高 in_await 让里面不报「没等待」，
        # 抑制效应让被启动任务的效应不外泄到当前函数；求完立刻还原。
        if bi.canonical == "task":
            # 任务只能提交「异步调用」：`任务(睡眠(10))`（需要等待的内建）
            # 或 `任务(慢())`（异步函数）。提交普通值（`任务(1)`）语义不明，
            # 编译期直接拒绝，不等到运行期才莫名其妙。
            if e["args"]:
                a0 = e["args"][0]
                async_ok = False
                async_effects: list[str] = []
                async_name = ""
                if a0.get("expr") == "call" and not a0.get("target"):
                    nm = a0.get("name")
                    if nm:
                        fn = self.funcs.get(nm)
                        if fn is not None and fn.get("is_async"):
                            async_ok = True
                            async_effects = fn.get("effects") or []
                            async_name = nm
                        else:
                            bb = KeywordTable.load().builtin_entry(nm)
                            if bb is not None and bb.needs_await:
                                async_ok = True
                                async_effects = [bb.effect] if bb.effect else []
                                async_name = nm
                if not async_ok:
                    self.err(Code.TASK_ARG_NOT_ASYNC,
                             "`任务()` 的参数要是一个异步调用，"
                             "如 `任务(睡眠(10))` 或 `任务(慢())`；"
                             "传普通值没有意义",
                             e["span"],
                             [FixHint("pass_async",
                                      f"{e['span']['file']}:{e['span']['line']}",
                                      "把要跑的异步操作写进 `任务(…)`，别传普通值")])
                else:
                    # 任务体有自己的协程，但“启动一个会联网/读文件的任务”仍然是
                    # 当前函数做出的选择。这里不再静默放行：当前函数没有声明对应
                    # 效应时给一条告警，避免零效应函数悄悄把有副作用的任务放出去。
                    have = set((self.cur_fn or {}).get("effects") or [])
                    missing = [x for x in async_effects if x not in have]
                    if missing:
                        shown = "、".join(self._effect_text(x) for x in missing)
                        self.warn(
                            "AGL-W-0401",
                            f"`任务({async_name}(…))` 会启动带效应 `{shown}` 的异步任务，"
                            f"但当前函数没有声明这些效应；请确认这是有意为之，"
                            f"或在当前函数头部补上 `{self._kw('effect')} {shown}`",
                            e["span"],
                            [FixHint("add_effect", f"{e['span']['file']}:{e['span']['line']}",
                                     f"补 `{self._kw('effect')} {shown}`，或改用不带效应的任务")],
                        )
            self.in_await += 1
            self.suppress_effects += 1
            arg_types = [self.type_of(a) for a in e["args"]]
            self.suppress_effects -= 1
            self.in_await -= 1
        else:
            # `按键排序(xs, 键函数)` 的第二个参数是「函数引用」：
            # AGL 没有函数值类型，类型检查不适用于它 —— 只认「名字」本身，
            # 不查它的类型（否则函数名会被当成变量报 0302）。
            # 「必须本文件定义」的校验由导入器做（sorted 翻译时）；
            # 手写 AGL 传错名字由运行期 NameError 回译兜底。
            arg_types = []
            for a in e["args"]:
                if bi.canonical == "sort_by" and len(arg_types) == 1:
                    if a.get("expr") != "name":
                        self.err(Code.BUILTIN_ARG_TYPE,
                                 f"`{name}` 的第 2 个参数要是函数名"
                                 "（键函数）：AGL 没有函数值，别的表达式传不进来",
                                 a["span"],
                                 [FixHint("fix_arg_type", pos,
                                          f"用法：{bi.summary_zh}")])
                    arg_types.append({"type": "Any"})
                    continue
                arg_types.append(self.type_of(a))

        if bi.needs_await and self.in_await == 0:
            shown = bi.zh if self.lang == LANG_ZH else bi.en
            self.err(Code.ASYNC_NOT_AWAITED,
                     f"`{shown}` 要写 `等待 {shown}(…)`" if self.lang == LANG_ZH
                     else f"`{shown}` must be awaited: `await {shown}(…)`",
                     e["span"],
                     [FixHint("add_await", pos,
                              "在前面补 `等待`（英文 `await`）")])

        for i, (arg, got) in enumerate(zip(e["args"], arg_types)):
            want = bi.params[i] if i < len(bi.params) else "任意"
            if not builtin_accepts(want, got.get("type", "Custom")):
                self.err(Code.BUILTIN_ARG_TYPE,
                         f"`{name}` 的第 {i + 1} 个参数要{zh_label(want)}，"
                         f"这里给的是{zh_label(got.get('type', 'Custom'))}",
                         arg["span"],
                         [FixHint("fix_arg_type", pos, f"用法：{bi.summary_zh}")])

        # 内建函数也可能带效应（`睡眠` 带 `计时`）。通道必须留着：
        # 少了它，将来加一个会读时间的内建函数就能绕过效应检查。
        if bi.effect:
            self.check_effects(e, name, {"effects": [bi.effect]})

        e["builtin"] = bi.canonical
        return type_of_zh(bi.ret)

    def type_of(self, e: dict) -> dict:
        k = e.get("expr")
        if k == "named":
            # 命名实参只出现在调用实参位置；类型就是它值的类型。
            return self.type_of(e["value"])
        if k in ("int", "float", "bool", "null"):
            return {"type": {"int": "Int", "float": "Float",
                             "bool": "Bool", "null": "Null"}[k]}

        if k == "match":
            # 表达式位置 `匹配`：逐臂在独立作用域里推类型，公共类型一致就返回它，
            # 否则返回 Any（与列表/映射字面量的宽松口径一致）。
            self.type_of(e["subject"])

            def _list_binds(pat: dict) -> list[tuple[str, dict]]:
                out: list[tuple[str, dict]] = []
                for it in pat.get("items") or []:
                    kind = it.get("kind")
                    if kind == "bind":
                        out.append((str(it.get("name")), {"type": "Any"}))
                    elif kind == "list":
                        out.extend(_list_binds(it.get("pattern") or {}))
                    elif kind == "map":
                        out.extend(_map_binds(it.get("pattern") or {}))
                return out

            def _map_binds(pat: dict) -> list[tuple[str, dict]]:
                out: list[tuple[str, dict]] = []
                for ent in pat.get("entries") or []:
                    val = ent.get("value") or {}
                    kind = val.get("kind")
                    if kind == "bind":
                        out.append((str(val.get("name")), {"type": "Any"}))
                    elif kind == "list":
                        out.extend(_list_binds(val.get("pattern") or {}))
                    elif kind == "map":
                        out.extend(_map_binds(val.get("pattern") or {}))
                return out

            types: list[dict] = []
            for arm in e["arms"]:
                pat = arm["pattern"]
                list_pat = pat.get("expr") == "list_pattern"
                map_pat = pat.get("expr") == "map_pattern"
                binds = (_list_binds(pat) if list_pat else []) + \
                        (_map_binds(pat) if map_pat else [])
                bind = arm.get("bind")
                if bind or binds:
                    self.scope.push()
                if bind:
                    self.scope.define(Sym(str(bind), {"type": "Any"}, False,
                                           pat.get("span", {}).get("line", 1)))
                for b_name, b_type in binds:
                    self.scope.define(Sym(str(b_name), b_type, False,
                                           pat.get("span", {}).get("line", 1)))
                if arm.get("guard") is not None:
                    gt = self.type_of(arm["guard"])
                    if gt.get("type") not in ("Bool", "Any"):
                        self.err(Code.TYPE_MISMATCH,
                                 "匹配守卫必须是布尔表达式",
                                 arm["guard"].get("span") or e["span"])
                types.append(self.type_of(arm["body"]))
                if bind or binds:
                    self.scope.pop()
            if types and all(t.get("type") == types[0].get("type") for t in types):
                return types[0]
            return {"type": "Any"}

        # 容器字面量：元素类型写得出来就带上，`["甲"]` 是 `列表<文本>` 而不是笼统的
        # 「列表」。带了这个 elem 信息，`名单[0]` 才知道自己是什么。
        # 元素类型不一致（或干脆是空容器）时老实返回不带 args 的类型 ——
        # 那本来就说不出"这是一种什么的列表"。
        if k == "list":
            shown = {"type": "List"}
            types = [self.type_of(i) for i in e["items"]]
            if types and all(t["type"] == types[0]["type"] for t in types):
                shown["args"] = [types[0]]
            return shown

        if k == "map":
            shown = {"type": "Map"}
            ktypes = [self.type_of(x["key"]) for x in e["entries"]]
            vtypes = [self.type_of(x["value"]) for x in e["entries"]]
            if ktypes and vtypes \
                    and all(t["type"] == ktypes[0]["type"] for t in ktypes) \
                    and all(t["type"] == vtypes[0]["type"] for t in vtypes):
                shown["args"] = [ktypes[0], vtypes[0]]
            return shown

        if k == "set":
            # 集合字面量：元素类型一致就带上（`{1, 2}` 是 `集合<整数>`），
            # 与列表字面量的处理对称。
            shown = {"type": "Set"}
            types = [self.type_of(i) for i in e["items"]]
            if types and all(t["type"] == types[0]["type"] for t in types):
                shown["args"] = [types[0]]
            return shown

        if k == "string":
            return {"type": "String"}

        if k == "await":
            if not (self.cur_fn or {}).get("is_async"):
                fn_name = (self.cur_fn or {}).get("name", "?")
                # 两个分支各写一句完整的话：`异步`+`函数` 中文连着写是「异步函数」，
                # 英文 `async`+`fn` 连着写会变成 "asyncfn" —— 拼关键字拼不出好句子。
                if self.lang == LANG_ZH:
                    msg = (f"`{self._kw('await')}` 只能写在异步函数里，"
                           f"而 `{fn_name}` 不是异步函数")
                    fix = (f"把 `{fn_name}` 改成 `异步 函数 {fn_name}(…)`；"
                           f"或者去掉 `{self._kw('await')}`，按普通调用写")
                else:
                    msg = (f"`{self._kw('await')}` can only be used inside an async fn, "
                           f"but `{fn_name}` is not async")
                    fix = (f"change `{fn_name}` to `async fn {fn_name}(…)`; "
                           f"or drop `{self._kw('await')}`")
                target_line = (self.cur_fn or {}).get("span", {}).get(
                    "line", e["span"]["line"])
                self.err(Code.AWAIT_OUTSIDE_ASYNC, msg, e["span"],
                         [FixHint("make_async",
                                  f"{e['span']['file']}:{target_line}", fix)])
            self.in_await += 1
            inner = self.type_of(e["value"])
            self.in_await -= 1
            return inner

        if k == "name":
            name = e["name"]
            bi = KeywordTable.load().builtin_entry(name)
            if bi is not None:
                return type_of_zh(bi.ret)
            # FFI 外部库名只作为调用目标出现（`requests.get(...)`），
            # 它本身不是本作用域的变量，不能报"未定义"。
            if name in self.ffi_libs:
                return {"type": "Custom", "declared": "外部库"}
            sym = self.scope.lookup(name)
            if sym is None:
                self.undef(e)
                return {"type": "Custom", "declared": "未知"}
            return sym.type

        if k == "call":
            name = e["name"]
            if e.get("target") is not None:
                return self.type_of_target_call(e)
            # 调用解析顺序（定死，改动即破坏向后兼容）：
            #   用户自己的函数 → 内建函数 → 报未定义。
            # 用户定义优先，是为了让"新增一个内建函数"永远不会让既有代码失效。
            fn = self.funcs.get(name)
            if fn is None and name in self.structs:
                return self._check_struct_args(self.structs[name], name, e["args"], e["span"], name)
            if fn is not None:
                self.resolve_args(fn["params"], e["args"], e["span"], name)
                # 实参表达式也要走一遍类型求值：实参里的内建调用
                # （如 `合并映射(...)`、`拼接(...)`）需要在这里被打上 builtin 标记，
                # 否则后端只会按普通函数名原样发射，运行期 NameError。
                for a in e["args"]:
                    self.type_of(a)
                # 异步函数必须被等待：不等待拿到的是"任务"，不是结果。
                # 这里报错而不是告警，是因为静默拿到任务对象后，
                # 后面所有用到它的地方都会以莫名其妙的方式错下去。
                if fn.get("is_async") and self.in_await == 0:
                    self.err(Code.ASYNC_NOT_AWAITED,
                             f"`{name}` 是异步函数，调用它要写 `等待 {name}(…)`"
                             if self.lang == LANG_ZH else
                             f"`{name}` is async; call it as `await {name}(…)`",
                             e["span"],
                             [FixHint("add_await", f"{e['span']['file']}:{e['span']['line']}",
                                      "在前面补 `等待`（英文 `await`）")])
                # 效应传播：调用方必须声明被调方的全部效应
                self.check_effects(e, name, fn)
                return fn.get("return") or {"type": "Null"}

            bi = KeywordTable.load().builtin_entry(name)
            if bi is not None:
                return self.check_builtin_call(e, bi)

            self.err(Code.UNDEFINED, f"调用了不存在的函数 `{name}`", e["span"],
                     [FixHint("declare", f"{e['span']['file']}:{e['span']['line']}",
                              "检查函数名是否拼错，或先定义这个函数")])
            return {"type": "Custom", "declared": "未知"}

        if k == "member":
            tgt = e["target"]
            # 跨模块常量：`模型.上限`。模块名不能被本地同名变量遮蔽。
            if (tgt.get("expr") == "name" and tgt["name"] in self.imported
                    and self.scope.lookup(tgt["name"]) is None):
                real = self.import_aliases.get(tgt["name"], tgt["name"])
                ext = self.externals.get(real) or {}
                consts = ext.get("consts") or {}
                field = e["field"]
                if field in consts:
                    return self.type_of(consts[field]["value"])
                if field in (ext.get("private_consts") or []):
                    self.err(Code.NOT_EXPORTED,
                             f"`{real}.{field}` 没有导出，外部不能使用",
                             e["span"], [FixHint("add_export", f"{real}.agl",
                                                 f"在 {real}.agl 里给 `{field}` 加上 `导出` 标记")])
                    return {"type": "Custom", "declared": "未知"}
                if field in (ext.get("types") or {}):
                    return {"type": "Custom", "declared": f"{real}.{field}"}
            base = self.type_of(e["target"])
            if e["field"] in ("消息", "message"):
                # `捕获 (错)` 绑定的错误对象：`.消息` 是一句中文说明。
                # 其它类型的值没有消息；`任意` 放行，运行时再定。
                if base.get("type") not in ("Error", "Custom", "Any"):
                    self.err(Code.TYPE_MISMATCH,
                             f"{TYPE_ZH.get(base.get('type'), '这个值')}没有 `.{e['field']}`"
                             f"（只有错误才有消息）",
                             e["span"])
                return {"type": "String"}
            attr = KeywordTable.load().attr_entry(e["field"])
            if attr is not None:
                # 内置只读属性只对能数字长的东西有意义：文本、列表、映射表。
                # `任意` 也放行：类型未知的变量（导入的 Python 参数 / 提升声明）
                # 运行时才见分晓，编译期不许假装知道它没有长度。
                if base.get("type") not in ("String", "List", "Map", "Set", "Custom", "Any"):
                    holder = TYPE_ZH.get(base.get("type"), "这个值")
                    self.err(Code.TYPE_MISMATCH,
                             f"{holder}没有 `.{attr.zh}`（只有文本、列表、映射表、集合有）",
                             e["span"],
                             [FixHint("remove_member", f"{e['span']['file']}:{e['span']['line']}",
                                      f"删掉 `.{attr.zh}`，或换成有长度的值")])
                    return {"type": "Int"}
                return {"type": "Int"}
            # 结构体字段：类型已知就按声明查，查不到当场报错。
            declared = base.get("declared")
            if base.get("type") == "Custom" and declared in self.structs:
                decl = self.structs[declared]
                for f in decl["fields"]:
                    if f["name"] == e["field"] or f["name"] == e.get("field_en"):
                        return dict(f["type"])
                self.err(Code.TYPE_MISMATCH,
                         f"结构体 `{declared}` 没有字段 `{e['field']}`",
                         e["span"],
                         [FixHint("check_field", f"{e['span']['file']}:{e['span']['line']}",
                                  f"可用字段：{'、'.join(f['name'] for f in decl['fields'])}")])
                return {"type": "Custom", "declared": "未知"}
            return {"type": "Custom", "declared": f"{base.get('type')}.{e['field']}"}

        if k == "index":
            tgt = self.type_of(e["target"])
            self.type_of(e["index"])
            # 从带泛型的容器里取元素，元素类型本来就写在类型里
            # （`列表<文本>` 的第 0 项当然是文本），没道理丢掉这个信息 ——
            # 丢掉之后一切都变成"元素"，既查不出 `名单[0] + "好"` 的正确与否，
            # 也让人以为 AGL 看不懂自己写的类型。
            if tgt["type"] == "List" and tgt.get("args"):
                return dict(tgt["args"][0])
            if tgt["type"] == "Map" and len(tgt.get("args") or []) >= 2:
                return dict(tgt["args"][1])
            return {"type": "Custom", "declared": "元素"}

        if k == "unop":
            return self.type_of(e["operand"])

        if k == "binop":
            lt = self.type_of(e["left"])
            rt = self.type_of(e["right"])
            op = e["op"]
            if op in ("&&", "||", "==", "!=", ">", "<", ">=", "<="):
                lt_t, rt_t = lt["type"], rt["type"]
                if op in (">", "<", ">=", "<="):
                    if (lt_t == "String") != (rt_t == "String") and \
                       ("Custom" not in (lt_t, rt_t)):
                        self.err(Code.CMP_TEXT_NUM,
                                 f"文本和数字不能直接比较（左边是 {self._type_label(lt_t)}，"
                                 f"右边是 {self._type_label(rt_t)}）",
                                 e["span"], [FixHint("convert", f"{e['span']['file']}:{e['span']['line']}",
                                                     "先把两边转成同一种类型再比较")])
                    # 真假值 × 数字：宿主语言里 `真 < 1` 会悄悄把真假当 0/1 比，
                    # 链式比较（`3 < 2 < 1` → `(3<2)<1` → `假<1`）会因此得出反直觉的真。
                    # 类别差太远的比较直接编译期拒绝，不许溜到运行期给错答案。
                    bool_num = {("Bool", "Int"), ("Bool", "Float")}
                    if (lt_t, rt_t) in bool_num or (rt_t, lt_t) in bool_num:
                        self.err(Code.EQ_MIXED_KIND,
                                 f"`{op}` 两侧类型差太远（{self._type_label(lt_t)} 和 "
                                 f"{self._type_label(rt_t)}），真假值不能和数字比大小",
                                 e["span"],
                                 [FixHint("compare_same_kind",
                                          f"{e['span']['file']}:{e['span']['line']}",
                                          "把两侧转成同一种类型再比，或检查是不是写错了")])
                # `等于` / `不等于` 也要看类别：文本×数字 / 真假×数字 / 真假×文本
                # 不可能是同一个值，写了等于白写。空 和 Custom 豁免：
                # 判空（`x 等于 空`）是常用写法，类型未知的变量也不该误报。
                if op in ("==", "!="):
                    if "Custom" not in (lt_t, rt_t) and "Null" not in (lt_t, rt_t):
                        kinds = {("String", "Int"), ("String", "Float"),
                                 ("Bool", "Int"), ("Bool", "Float"),
                                 ("Bool", "String")}
                        if (lt_t, rt_t) in kinds or (rt_t, lt_t) in kinds:
                            self.err(Code.EQ_MIXED_KIND,
                                     f"`等于` / `不等于` 两侧类型差太远"
                                     f"（{self._type_label(lt_t)} 和 {self._type_label(rt_t)}），"
                                     "它们不可能是同一个值",
                                     e["span"],
                                     [FixHint("compare_same_kind",
                                              f"{e['span']['file']}:{e['span']['line']}",
                                              "把两侧转成同一种类型再比，或检查是不是写错了")])
                return {"type": "Bool"}
            if op == "+" and lt["type"] == "String" and rt["type"] == "String":
                return {"type": "String"}
            # 任一侧是"任意"（含未声明名字的 Custom 占位）：类型未知，放行，
            # 运行时不匹配照样崩，跟 Python 一致。但结果必须也是「任意」，
            # 旧版错推成文本，毒化下游——`任意+数字` 之后再相加就被报
            # 「文本只能和文本相加」。
            if op == "+" and (lt["type"] in ("Any", "Custom", "未知")
                              or rt["type"] in ("Any", "Custom", "未知")):
                return {"type": "Any"}
            if lt["type"] in NUMERIC and rt["type"] in NUMERIC:
                return {"type": "Float" if "Float" in (lt["type"], rt["type"]) else "Int"}
            if op == "+" and lt["type"] == "List" and rt["type"] == "List":
                # 列表 + 列表是合法拼接（[1] + [2] → [1, 2]），结果仍是列表；
                # 旧版落到默认 Int，导致 `拼接结果.长度` 被误报「整数没有 .长度」。
                return {"type": "List"}
            if op == "+" and (lt["type"] == "String" or rt["type"] == "String"):
                self.err(Code.TYPE_MISMATCH,
                         f"文本只能和文本相加，这里是 "
                         f"{self._type_label(lt['type'])} 和 {self._type_label(rt['type'])}",
                         e["span"], [FixHint("convert", f"{e['span']['file']}:{e['span']['line']}",
                                             "先用 `转文本(...)` 把数字转成文本")])
                return {"type": "Custom", "declared": "未知"}
            if op == "+":
                # 两侧类型都确定但凑不到一起（列表+数字、映射表+数字、
                # 映射表+映射表……）：编译期就报中文诊断，别落默认整数，
                # 让 `[1,2]+5` 溜到运行期才 TypeError。
                self.err(Code.TYPE_MISMATCH,
                         f"不能把{self._type_label(lt['type'])}和"
                         f"{self._type_label(rt['type'])}相加"
                         f"（只有文本能接文本、数字能加数字、列表能拼列表）",
                         e["span"], [FixHint("convert", f"{e['span']['file']}:{e['span']['line']}",
                                             "改成同类的值相加，或检查是不是写错了")])
                return {"type": "Custom", "declared": "未知"}
            return {"type": "Int"}

        return {"type": "Custom", "declared": "未知"}

    def check_effects(self, call: dict, callee_name: str, callee: dict) -> None:
        # `任务(...)` 的实参在抑制上下文中求值：被启动任务的效应留在它自己的
        # 协程里，不向上泄漏到启动它的函数。这里直接放行，避免误报未声明效应。
        if self.suppress_effects > 0:
            return
        need = callee.get("effects") or []
        if not need:
            return
        have = set((self.cur_fn or {}).get("effects") or [])
        for e in need:
            if e in have:
                continue
            fn_name = (self.cur_fn or {}).get("name", "?")
            fn_span = (self.cur_fn or {}).get("span", call["span"])
            shown = self._effect_text(e)
            self.err(Code.PURE_CALLS_EFFECT,
                     f"函数 `{fn_name}` 没有声明效应 `{shown}`，但它调用了带该效应的 `{callee_name}`",
                     call["span"],
                     [FixHint("add_effect", f"{fn_span['file']}:{fn_span['line']}",
                              f"在 `{fn_name}` 的头部补上 `{self._kw('effect')} {shown}`")],
                     effects=[e])

    def _type_label(self, t: str) -> str:
        """类型名按当前分支的写法显示：中文分支显示「整数」，不是 `Int`。

        规范名（Int / String / Result）是内部的。写给普通人看的那一句话里
        出现 `Int`，等于让他去猜一门他没见过的语言。
        """
        return zh_label(t) if self.lang == LANG_ZH else t

    def _type_label_node(self, t: dict) -> str:
        """带可空后缀的类型显示：`文本?`。"""
        base = self._type_label(t.get("type", "未知"))
        return base + ("?" if t.get("nullable") else "")

    def _effect_text(self, effect: str) -> str:
        """效应名按当前分支的写法显示（中文分支显示「网络」而不是 Net）。"""
        from compiler.lexer import KeywordTable
        return KeywordTable.load().text_for(effect, self.lang)

    def _kw(self, canon: str) -> str:
        from compiler.lexer import KeywordTable
        return KeywordTable.load().text_for(canon, self.lang)

    def _check_cond(self, cond: dict) -> None:
        """`如果` / `当` 的条件必须是真假。其它类型（数字、文本）不许隐式真值化。"""
        t = self.type_of(cond)
        # `结果` 也允许：成功为真、失败为假，是 AGL 的既定语义（黄金用例在用）。
        if t.get("type") not in ("Bool", "Any", "Result", "Error"):
            self.err(Code.TYPE_MISMATCH,
                     f"条件要 `{self._type_label('Bool')}`，给的是 "
                     f"`{self._type_label(t.get('type', '未知'))}`；比较要写完整，"
                     f"如 `数量 大于 0`",
                     cond["span"],
                     [FixHint("fix_condition", f"{cond['span']['file']}:{cond['span']['line']}",
                              "用 等于/不等于/大于/小于 等比较式，或 `非`/`且`/`或` 组合")])

    def _check_struct_args(self, decl: dict, display: str, args: list[dict],
                           span: dict, type_key: str) -> dict:
        """结构体构造：字段必须全部用命名实参给齐，顺序随意；字段值按声明类型校验。"""
        fnames = [f["name"] for f in decl["fields"]]
        ftypes = {f["name"]: f["type"] for f in decl["fields"]}
        given: dict[str, dict] = {}
        for a in args:
            if a.get("expr") != "named":
                self.err(Code.TYPE_MISMATCH,
                         f"构造 `{display}` 要用命名实参：`{display}(字段: 值, …)`",
                         a["span"])
                self.type_of(a)
                continue
            got = self.type_of(a["value"])
            if a["name"] not in fnames:
                self.err(Code.TYPE_MISMATCH,
                         f"结构体 `{display}` 没有字段 `{a['name']}`",
                         a["span"], [FixHint("check_name", f"{a['span']['file']}:{a['span']['line']}",
                                             f"可用字段：{'、'.join(fnames)}")])
            elif a["name"] in given:
                self.err(Code.TYPE_MISMATCH, f"字段 `{a['name']}` 重复赋值", a["span"])
            else:
                given[a["name"]] = a
                want = ftypes[a["name"]]
                if not self.compatible(want, got):
                    self.err(Code.TYPE_MISMATCH,
                             f"字段 `{a['name']}` 要 `{self._type_label(want['type'])}`，"
                             f"给的是 `{self._type_label(got['type'])}`",
                             a["value"]["span"],
                             [FixHint("fix_field_type", f"{a['value']['span']['file']}:{a['value']['span']['line']}",
                                      "改值，或改结构体字段的声明类型")])
        missing = [f for f in fnames if f not in given]
        if missing:
            self.err(Code.TYPE_MISMATCH,
                     f"构造 `{display}` 缺少字段：`{'`、`'.join(missing)}`",
                     span, [FixHint("fix_arity", f"{span['file']}:{span['line']}",
                                    "把字段补齐；字段都是必填的")])
        return {"type": "Custom", "declared": type_key}

    def compatible(self, declared: dict, got: dict) -> bool:
        # 声明为「任意」就是不约束类型：给什么都行。
        # builtin_accepts 对 want==Any 早有放行分支，这里补上保持两套一致，
        # 否则 `变量 x: 任意 = 5` 会被误报 0301。
        if declared["type"] == "Any":
            return True
        # 反过来，实际给的是「任意」也必须放行：`取到(r)` 解包、任一侧为「任意」的
        # 运算、没有兜底的 `匹配` 都会产出「任意」，静态上本就说不出它是什么，
        # 运行时才现形。只放行「声明为任意」而不放行「给的是任意」，会把
        # `函数 网页演示() -> 文本 { 返回 取到(r) }` 这类历史写法误判成 0301。
        if got.get("type") == "Any":
            return True
        # 可空类型：`空` 只能进可空位置；可空值不能偷偷流进非可空位置。
        if got.get("type") == "Null":
            return bool(declared.get("nullable")) or declared["type"] == "Null"
        if got.get("nullable") and not declared.get("nullable"):
            return False
        # 去掉 nullable 后按基础类型比较，`文本? = "甲"` 必须放行。
        declared_base = {k: v for k, v in declared.items() if k != "nullable"}
        got_base = {k: v for k, v in got.items() if k != "nullable"}
        if declared_base["type"] == "Custom" or got_base["type"] == "Custom":
            return True
        if declared_base["type"] == got_base["type"]:
            return True
        if declared_base["type"] == "Float" and got_base["type"] == "Int":
            return True
        if declared_base["type"] == "Result" and got_base["type"] in ("Result", "Error"):
            return True
        return False


def check(ir: dict, lang: str,
          externals: dict[str, dict] | None = None,
          is_entry: bool = True,
          source_path: str | None = None) -> list[Diagnostic]:
    """externals: 被导入模块的符号表；is_entry: 这个文件是否是程序入口。"""
    return Checker(ir, lang, externals, is_entry, source_path).run()
