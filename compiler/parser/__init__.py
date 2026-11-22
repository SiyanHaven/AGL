# -*- coding: utf-8 -*-
"""M2 语法分析器。产出 AST —— 阶段 1 与 IR **同构**。

三个铁律：
  1. **span 只在这里生成**，下游（M4/M5）禁止补 span，否则错误回译会指错行；
  2. 语法判断**只看 canonical**，这样中英两分支共用同一个解析器；
  3. 内建名（主入口/打印/成功/失败）在 AST 里就写成规范英文（main/print/Ok/Err），
     用户标识符一律保留原文 —— 这是双分支 IR 能比对的前提。

函数头修饰符（校验 / 效应）的顺序**不限**，两种写法都接受：
    函数 f(x: 整数) -> 整数
    校验 { x 大于 0 }
    效应 读写
    { ... }
"""
from __future__ import annotations

from compiler.diagnostics import AglError, Code, FixHint, error
from compiler.lexer import (
    COMMENT, EOF, FLOAT, IDENT, INT, KEYWORD, LANG_ZH, NEWLINE, OP, STRING,
    KeywordTable, Token,
)

# 基础类型：canonical → IR 类型名
BASIC_TYPES = {"Int", "Float", "Bool", "String", "Null", "List", "Map", "Result", "Error", "Custom"}
EFFECT_TAGS = {"IO", "Net", "model", "Time", "External"}

# 二元运算符优先级（数字越大越紧）
BIN_LEVELS = [
    {"||"},
    {"&&"},
    {"==", "!=", ">", "<", ">=", "<="},
    {"+", "-"},
    {"*", "/", "%"},
]

# 语句起始关键字
STMT_KW = {"let", "const", "return", "if", "while", "for", "try",
           "break", "continue", "match"}


class Parser:
    def __init__(self, tokens: list[Token], filename: str, lang: str):
        self.all = tokens
        self.toks = [t for t in tokens if t.kind != COMMENT]
        self.comments = [t for t in tokens if t.kind == COMMENT]
        self.i = 0
        self.file = filename
        self.lang = lang
        self.table = KeywordTable.load()
        self.exports: list[str] = []
        self.imports: list[dict] = []
        self.brace_stack: list[Token] = []   # 未闭合的 `{`，用于精确定位缺括号
        self.entry_name: str | None = None    # 入口函数的源码原文名
        self.custom_types: set[str] = set()   # 已声明的结构体名（供类型标注识别）
        # 预扫一遍 `类型 X {` 的声明名：这样结构体可以前向引用、互相引用，
        # 不必按声明顺序书写。只认「type 关键字 + 标识符」这一种形态。
        for j, t in enumerate(self.toks[:-1]):
            nxt = self.toks[j + 1]
            if t.kind == KEYWORD and t.canonical == "type" and nxt.kind == IDENT:
                self.custom_types.add(nxt.text)

    # ── Token 导航 ────────────────────────────────────────────────────
    def peek(self, k: int = 0) -> Token:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def eat(self) -> Token:
        t = self.peek()
        if t.kind != EOF:
            self.i += 1
        return t

    def at_kind(self, kind: str) -> bool:
        return self.peek().kind == kind

    def at_canon(self, *canons: str) -> bool:
        return self.peek().canonical in canons

    def at_text(self, *texts: str) -> bool:
        """按源码原文判断，用于阶段 2 声明关键字（避免占用普通标识符）。"""
        return self.peek().text in texts

    _AUG_OPS = ("+=", "-=", "*=", "/=", "%=")

    def _aug_op(self) -> str | None:
        """若下一个符号是增广赋值算子，吃掉它并返回对应的二元算子字符。

        返回 None 表示不是增广赋值，调用方继续按普通赋值 / 表达式语句处理。
        """
        for sym in self._AUG_OPS:
            if self.at_canon(sym):
                self.eat()
                return sym[0]
        return None

    def skip_newlines(self) -> None:
        while self.peek().kind == NEWLINE:
            self.eat()

    def _show(self, canon: str) -> str:
        """把 canonical 转成当前分支的写法，用于人看的报错。"""
        for k in self.table.keywords:
            if k.en == canon:
                return k.zh if self.lang == LANG_ZH else k.en
        for o in self.table.ops:
            if o["en"] == canon:
                return o["zh"] if self.lang == LANG_ZH else o["en"]
        return canon

    def _src_line(self, line: int) -> str | None:
        return None  # 由 CLI 注入源码行

    def fail(self, code: str, msg: str, tok: Token | None = None,
             hints: list[FixHint] | None = None, end_col: int = 0) -> None:
        t = tok or self.peek()
        raise error(code, msg, file=self.file, line=t.line, col=t.col,
                    end_col=end_col or t.end_col, fix_hints=hints)

    def expect(self, canon: str, what: str = "") -> Token:
        if not self.at_canon(canon):
            t = self.peek()
            shown = self._show(canon)
            label = f"{shown}" + (f"（{what}）" if what else "")
            raise error(
                Code.EXPECT,
                f"这里期望 {label}，实际看到 `{t.text}`",
                file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                fix_hints=[FixHint("insert_token", f"{self.file}:{t.line}",
                                   f"在这里补上 {shown}")],
            )
        return self.eat()

    def expect_block_open(self) -> Token:
        if not self.at_canon("{"):
            t = self.peek()
            raise error(
                Code.NEED_BLOCK,
                f"这里需要一个块，用 `{{` 开始，实际看到 `{t.text}`",
                file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                fix_hints=[FixHint("add_block", f"{self.file}:{t.line}", "补上 `{ … }`")],
            )
        tok = self.eat()
        self.brace_stack.append(tok)
        return tok

    def eat_close(self) -> Token:
        """消费一个 `}` 并弹出括号栈。"""
        tok = self.eat()
        if self.brace_stack:
            self.brace_stack.pop()
        return tok

    def unclosed_block_error(self) -> None:
        """用括号栈定位「到底是哪个 `{` 没关上」。"""
        t = self.peek()
        if self.brace_stack:
            open_tok = self.brace_stack[-1]
            raise error(
                Code.BRACE,
                f"花括号不匹配：第 {open_tok.line} 行的 `{{` 到文件结束都没有对应的 `}}`",
                file=self.file, line=open_tok.line, col=open_tok.col,
                end_col=open_tok.end_col,
                fix_hints=[FixHint("add_brace", f"{self.file}:{t.line}",
                                   f"在第 {t.line} 行前补一个 `}}`，闭合第 {open_tok.line} 行开始的块")],
            )
        raise error(Code.BRACE, "花括号不匹配，缺少 `}`",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                    fix_hints=[FixHint("add_brace", f"{self.file}:{t.line}", "补上 `}`")])

    def expect_ident(self, what: str = "名字") -> Token:
        t = self.peek()
        if t.kind == IDENT:
            if t.text == "_":
                raise error(
                    Code.RESERVED_UNDERSCORE,
                    "`_` 是保留字（§3.3 匹配通配符 / 丢弃绑定），不能当变量名或参数名",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                    fix_hints=[FixHint("rename_ident", f"{self.file}:{t.line}",
                                       "换一个有意义的名字，如 `丢弃` / `临时`")],
                )
            return self.eat()
        # 内建名（主入口 / 打印 …）也可作声明名
        if t.kind == KEYWORD and self.table.kind_of(t.canonical) == "builtin":
            return self.eat()
        raise error(
            Code.EXPECT,
            f"这里期望一个{what}，实际看到 `{t.text}`",
            file=self.file, line=t.line, col=t.col, end_col=t.end_col,
            fix_hints=[FixHint("insert_name", f"{self.file}:{t.line}", f"补上{what}")],
        )

    # ── span 工具 ─────────────────────────────────────────────────────
    def span(self, first: Token, last: Token) -> dict:
        return {
            "file": self.file,
            "line": first.line,
            "col": first.col,
            "endLine": last.end_line,
            "endCol": last.end_col,
        }

    def span_from(self, first: Token) -> dict:
        """从 first 到上一个已消费 token。"""
        last = self.toks[max(0, self.i - 1)]
        return self.span(first, last)

    # ── 程序 ──────────────────────────────────────────────────────────
    def parse_program(self) -> dict:
        first = self.toks[0]
        funcs: list[dict] = []
        consts: list[dict] = []
        types: list[dict] = []
        interfaces: list[dict] = []
        declarations: list[dict] = []
        dependencies: list[dict] = []
        self.skip_newlines()
        while not self.at_kind(EOF):
            if self.at_canon("export"):
                first_tok = self.eat()
                self.skip_newlines()
                # `导出` 必须紧跟一个声明，否则这个标记没有任何作用对象。
                if self.at_canon("fn", "async"):
                    fn = self.parse_function()
                    self.exports.append(fn["name"])
                    funcs.append(fn)
                    self.skip_newlines()
                    continue
                if self.at_canon("type"):
                    td = self.parse_type_decl()
                    self.exports.append(td["name"])
                    types.append(td)
                    self.skip_newlines()
                    continue
                if self.at_canon("const"):
                    c = self.parse_let()
                    self.exports.append(c["name"])
                    consts.append(c)
                    self.skip_newlines()
                    continue
                if self.at_canon("interface"):
                    iface = self.parse_interface()
                    self.exports.append(iface["name"])
                    interfaces.append(iface)
                    self.skip_newlines()
                    continue
                if self.at_text("工具", "智能体", "规则", "tool", "agent", "rule"):
                    d = self.parse_declaration()
                    self.exports.append(d["name"])
                    declarations.append(d)
                    self.skip_newlines()
                    continue
                t = self.peek()
                raise error(
                    Code.EXPECT,
                    f"{self._show('export')} 后面要跟一个声明"
                    f"（{self._show('fn')} / {self._show('type')} / "
                    f"{self._show('const')} / {self._show('interface')}），"
                    f"实际看到 `{t.text}`",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                    fix_hints=[FixHint("export_decl", f"{self.file}:{t.line}",
                                       f"写成 {self._show('export')} {self._show('fn')} 名字(...) {{ }}")],
                )
            if self.at_canon("interface"):
                interfaces.append(self.parse_interface())
                self.skip_newlines()
                continue
            if self.at_canon("deps"):
                dependencies.extend(self.parse_dependencies())
                self.skip_newlines()
                continue
            if self.at_canon("import"):
                self.imports.append(self.parse_import())
                self.skip_newlines()
                continue
            if self.at_canon("module"):
                # 阶段 1 单文件：module 声明仅作标记，忽略
                self.eat()
                self.expect_ident("模块名")
                self.skip_newlines()
                continue
            if self.at_canon("type"):
                # 结构体声明：`类型 用户 { 编号: 整数 … }`
                types.append(self.parse_type_decl())
                self.skip_newlines()
                continue
            if self.at_canon("const"):
                # 顶层常量：`常量 圆周率 = 3.14`。值只求一次，全文件只读。
                consts.append(self.parse_let())
                self.skip_newlines()
                continue
            if self.at_text("工具", "智能体", "规则", "tool", "agent", "rule"):
                declarations.append(self.parse_declaration())
                self.skip_newlines()
                continue
            if self.at_canon("fn", "async"):
                funcs.append(self.parse_function())
                self.skip_newlines()
                continue
            t = self.peek()
            raise error(
                Code.EXPECT,
                f"这里期望一个声明（{self._show('fn')}），实际看到 `{t.text}`",
                file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                fix_hints=[FixHint("add_function", f"{self.file}:{t.line}",
                                   f"用 {self._show('fn')} 定义一个函数")],
            )

        if not funcs:
            t = self.toks[0]
            raise error(Code.EXPECT, "文件里没有任何函数定义",
                        file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                        fix_hints=[FixHint("add_function", f"{self.file}:1",
                                           f"至少写一个 {self._show('fn')} {self._show('main')}() {{ }}")])

        entry = self.entry_name or "main"
        last = self.toks[-1]
        module = {
            "name": self.file.rsplit(".", 1)[0],
            "imports": self.imports,
            "exports": self.exports,
            "consts": consts,
            "types": types,
            "functions": funcs,
            "span": self.span(first, last),
        }
        # 接口是纯编译期契约，运行期不存在；没有接口时连字段都不出现，
        # 这样老 IR 的字节形态保持不变。
        if interfaces:
            module["interfaces"] = interfaces
        if declarations:
            module["declarations"] = declarations
        if dependencies:
            module["dependencies"] = dependencies
        return {"entry": entry, "modules": [module]}

    # ── 声明式块（阶段 2：工具 / 智能体 / 规则） ────────────────────
    _DECL_FIELDS = {
        "版本": "version", "version": "version",
        "生效范围": "scope", "scope": "scope",
        "优先级": "priority", "priority": "priority",
        "内容": "content", "content": "content",
        "过期时间": "expires", "expires": "expires",
        "目标": "target", "target": "target",
    }
    _CONFIG_FIELDS = {
        "协议": "protocol", "protocol": "protocol",
        "地址": "endpoint", "endpoint": "endpoint",
        "最大重试次数": "max_retry", "max_retry": "max_retry",
        "超时秒数": "timeout_sec", "timeout_sec": "timeout_sec",
        "权限": "permissions", "permissions": "permissions",
        "订阅": "subscribe", "subscribe": "subscribe",
        "模型": "model", "model": "model",
        "最大轮数": "max_turns", "max_turns": "max_turns",
        "入口": "entry", "entry": "entry",
    }

    def parse_declaration(self) -> dict:
        """`工具 / 智能体 / 规则` 顶层声明。"""
        first = self.eat()
        kind = {"工具": "tool", "智能体": "agent", "规则": "rule"}.get(
            first.text, first.canonical)
        name_tok = self.expect_ident("声明名")
        name = self._decl_name(name_tok)
        node: dict = {"kind": kind, "name": name, "span": self.span_from(first)}
        self.skip_newlines()
        self.expect_block_open()
        params: list[dict] = []
        ret = {"type": "Null"}
        config: dict | None = None
        fields: dict[str, dict] = {}
        target: dict | None = None
        while True:
            self.skip_newlines()
            if self.at_canon("}"):
                self.eat()
                break
            if self.at_text("入参", "input"):
                if kind == "rule":
                    self.fail(Code.EXPECT, "规则块里不能写入参")
                params = self.parse_decl_params()
                self.skip_newlines()
                continue
            if self.at_text("返回", "return"):
                if kind == "rule":
                    self.fail(Code.EXPECT, "规则块里不能写返回类型")
                self.eat()
                ret = self.parse_type()
                self.skip_newlines()
                continue
            if self.at_text("配置", "config"):
                if kind == "rule":
                    self.fail(Code.EXPECT, "规则块里不能写配置块")
                self.eat()
                self.expect_block_open()
                config = {}
                while True:
                    self.skip_newlines()
                    if self.at_canon("}"):
                        self.eat()
                        break
                    key_tok = self.eat()
                    key = self._CONFIG_FIELDS.get(key_tok.text, key_tok.canonical)
                    if key not in ("protocol", "endpoint", "max_retry",
                                   "timeout_sec", "permissions", "subscribe",
                                   "model", "max_turns", "entry"):
                        self.fail(Code.EXPECT,
                                  f"配置块里不认识的字段 `{key_tok.text}`；"
                                  "工具只允许 协议/地址/入口/最大重试次数/超时秒数/权限/订阅")
                    self.expect("=", "配置赋值")
                    self.skip_newlines()
                    config[key] = self.parse_expr()
                    self.skip_newlines()
                continue
            key_tok = self.eat()
            key = self._DECL_FIELDS.get(key_tok.text, key_tok.canonical)
            if key == "target":
                self.expect("=", "目标赋值")
                self.skip_newlines()
                target = self.parse_expr()
                self.skip_newlines()
                continue
            if kind != "rule":
                self.fail(Code.EXPECT, f"{kind} 声明块里不认识的字段 `{key_tok.text}`")
            self.expect("=", "规则字段赋值")
            self.skip_newlines()
            fields[key] = self.parse_rule_value(key)
            self.skip_newlines()
        node["params"] = params
        node["return"] = ret
        if config is not None:
            node["config"] = config
        if target is not None:
            node["target"] = target
        if fields:
            node["fields"] = fields
        return node

    def parse_decl_params(self) -> list[dict]:
        self.eat()  # input
        self.expect("(", "入参表")
        params: list[dict] = []
        self.skip_newlines()
        if not self.at_canon(")"):
            while True:
                self.skip_newlines()
                p = self.expect_ident("入参名")
                pname = self._decl_name(p)
                self.expect(":", "入参类型")
                ptype = self.parse_type()
                params.append({"name": pname, "type": ptype})
                self.skip_newlines()
                if self.at_canon(","):
                    self.eat()
                    continue
                break
        self.expect(")", "入参表结束")
        return params

    def parse_rule_value(self, key: str) -> dict:
        if key == "priority":
            t = self.peek()
            canon = {"高": "high", "普通": "normal", "低": "low"}.get(t.text, t.text)
            if canon in ("high", "normal", "low"):
                self.eat()
                return {"expr": "name", "name": canon}
            return self.parse_expr()
        if key == "scope":
            t = self.peek()
            if t.text in ("全项目", "模块", "目标", "all_project", "module", "target"):
                self.eat()
                text = {"all_project": "全项目", "module": "模块",
                        "target": "目标"}.get(t.text, t.text)
                if text == "全项目":
                    return {"expr": "string", "value": "全项目"}
                sub = self.expect_ident("作用范围")
                return {"expr": "scope", "level": text,
                        "value": self._decl_name(sub)}
            if t.kind in (IDENT, KEYWORD) and t.canonical == "全项目":
                self.eat()
                return {"expr": "string", "value": "全项目"}
            return self.parse_expr()
        if key == "expires":
            # 日期按 `2026-12-31` 读成字符串；不要走减法。
            t = self.peek()
            if t.kind == INT:
                start = self.eat().text
                parts = [start]
                for _ in range(2):
                    if self.at_canon("-"):
                        self.eat()
                        t2 = self.eat()
                        parts.append(t2.text)
                    else:
                        break
                if len(parts) == 3:
                    return {"expr": "string", "value": "-".join(parts)}
            return self.parse_expr()
        return self.parse_expr()

    def parse_import(self) -> dict:
        first = self.eat()  # import
        # FFI 黑盒调用：`导入 外部 requests [效应 外部调用]`
        # `外部` 是修饰符，声明后面跟的是外部 Python 库而非 AGL 模块。
        is_ffi = False
        if self.at_canon("ffi"):
            self.eat()
            is_ffi = True
        t = self.peek()
        if t.kind == STRING:
            mod = self.eat().text.strip('"')
        elif t.kind in (IDENT, KEYWORD):
            # 模块名按源码原文取。关键字也可以当模块名（`导入 模型` 很自然），
            # 否则一个叫 `模型.agl` 的文件永远没法被导入。
            mod = self.eat().text
        else:
            self.fail(Code.EXPECT, f"导入后面要跟模块名，实际看到 `{t.text}`")
        node: dict = {"module": mod, "names": [], "span": self.span_from(first)}
        if is_ffi:
            # 默认最坏效应：外部调用。除非显式声明更小的效应。
            node["kind"] = "ffi"
            node["effect"] = "External"
            # 可选：明确声明该外部库实际带什么效应（调用方仍需据此声明）
            if self.at_canon("effect"):
                tags = self._parse_effect_tags()
                if tags:
                    node["effect"] = tags[0]
        else:
            node["kind"] = "module"
            # `导入 工具 作为 T`：给模块起别名，后续 `T.函数()` 一样能用。
            if self.at_canon("as"):
                self.eat()
                node["alias"] = self._decl_name(self.expect_ident("模块别名"))
        return node

    def _parse_effect_tags(self) -> list[str]:
        """读一组效应标签（`效应 网络` / `效应 读写, 网络`）。调用前须已吃掉 `效应`。"""
        self.eat()  # effect
        tags: list[str] = []
        while True:
            t = self.peek()
            if t.kind == KEYWORD and t.canonical in EFFECT_TAGS:
                tags.append(t.canonical)
                self.eat()
                continue
            if t.kind == OP and t.canonical == ",":
                self.eat()
                continue
            break
        if not tags:
            t = self.peek()
            self.fail(Code.EXPECT,
                      "效应声明后面要跟至少一个效应标签（读写/网络/大模型/计时/外部调用）", t)
        return tags

    # ── 函数 ──────────────────────────────────────────────────────────
    def parse_function(self) -> dict:
        first = self.peek()
        is_async = False
        if self.at_canon("async"):
            is_async = True
            self.eat()
            self.skip_newlines()
        self.expect("fn")
        name_tok = self.expect_ident("函数名")
        name = self._decl_name(name_tok)
        if name_tok.kind == KEYWORD and name_tok.canonical == "main":
            self.entry_name = name

        self.expect("(", "参数表")
        params: list[dict] = []
        seen: set[str] = set()
        seen_default = False
        self.skip_newlines()
        if not self.at_canon(")"):
            while True:
                self.skip_newlines()
                p = self.expect_ident("参数名")
                pname = self._decl_name(p)
                if pname in seen:
                    raise error(Code.DUP_PARAM, f"函数参数重复：`{p.text}`",
                                file=self.file, line=p.line, col=p.col, end_col=p.end_col,
                                fix_hints=[FixHint("rename_param", f"{self.file}:{p.line}", "改一个参数名")])
                seen.add(pname)
                self.expect(":", "参数类型")
                ptype = self.parse_type()
                pnode: dict = {"name": pname, "type": ptype}
                # 默认参数：`a: 整数 = 0`。有默认值之后，后面的参数也必须带默认值
                # （与 Python 一致，否则位置实参会错位）。
                if self.at_canon("="):
                    self.eat()
                    self.skip_newlines()
                    pnode["default"] = self.parse_expr()
                    seen_default = True
                elif seen_default:
                    self.fail(Code.EXPECT,
                              f"参数 `{pname}` 没有默认值：带默认值的参数之后不能再有必填参数",
                              self.peek())
                params.append(pnode)
                self.skip_newlines()
                if self.at_canon(","):
                    self.eat()
                    continue
                break
        self.expect(")", "参数表结束")
        self.skip_newlines()

        ret = {"type": "Null"}
        if self.at_canon("->"):
            self.eat()
            ret = self.parse_type()
            self.skip_newlines()

        checks: list[dict] = []
        effects: list[str] = []
        while True:
            self.skip_newlines()
            if self.at_canon("check"):
                self.eat()
                self.skip_newlines()
                self.expect_block_open()
                while True:
                    self.skip_newlines()
                    if self.at_canon("}"):
                        break
                    if self.at_kind(EOF):
                        self.fail(Code.BRACE, "校验块没有闭合，缺少 `}`")
                    checks.append(self.parse_expr())
                self.eat_close()
                continue
            if self.at_canon("effect"):
                effects.extend(self._parse_effect_tags())
                continue
            break

        self.skip_newlines()
        self.expect_block_open()
        body = self.parse_stmts_until_brace()
        close = self.eat_close()

        fn = {
            "name": name,
            "params": params,
            "return": ret,
            "effects": effects,
            "is_async": is_async,
            "checks": checks,
            "body": body,
            "span": self.span(first, close),
        }
        return fn

    def parse_interface(self) -> dict:
        """接口契约：只写签名，不写函数体。

            接口 支付 {
              函数 付款(金额: 整数) -> 结果<文本>
            }

        接口是纯编译期检查：同名函数必须存在，参数名/类型/返回类型必须一致。
        """
        first = self.eat()  # interface
        name_tok = self.expect_ident("接口名")
        self.skip_newlines()
        self.expect_block_open()
        members: list[dict] = []
        seen: set[str] = set()
        while True:
            self.skip_newlines()
            if self.at_canon("}"):
                break
            if self.at_kind(EOF):
                self.fail(Code.BRACE, "接口没有闭合，缺少 `}`")
            if not self.at_canon("fn"):
                t = self.peek()
                raise error(
                    Code.IFACE_MEMBER,
                    f"接口里只能写函数签名，实际看到 `{t.text}`",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                    fix_hints=[FixHint("iface_member", f"{self.file}:{t.line}",
                                       f"写成 {self._show('fn')} 名字(参数: 类型) -> 返回类型"),
                                FixHint("iface_body", f"{self.file}:{t.line}",
                                        "实现写到接口外面的普通函数里")],
                )
            mfirst = self.eat()
            mname_tok = self.expect_ident("函数名")
            mname = self._decl_name(mname_tok)
            if mname in seen:
                raise error(
                    Code.IFACE_MEMBER,
                    f"接口 `{name_tok.text}` 里重复声明了 `{mname}`",
                    file=self.file, line=mname_tok.line, col=mname_tok.col, end_col=mname_tok.end_col,
                    fix_hints=[FixHint("rename_iface_member", f"{self.file}:{mname_tok.line}",
                                       "删掉重复的那一条")],
                )
            seen.add(mname)
            self.expect("(", "参数表")
            params: list[dict] = []
            pseen: set[str] = set()
            self.skip_newlines()
            if not self.at_canon(")"):
                while True:
                    self.skip_newlines()
                    p = self.expect_ident("参数名")
                    pname = self._decl_name(p)
                    if pname in pseen:
                        raise error(
                            Code.DUP_PARAM, f"接口参数重复：`{p.text}`",
                            file=self.file, line=p.line, col=p.col, end_col=p.end_col,
                            fix_hints=[FixHint("rename_param", f"{self.file}:{p.line}",
                                               "改一个参数名")])
                    pseen.add(pname)
                    self.expect(":", "参数类型")
                    params.append({"name": pname, "type": self.parse_type()})
                    self.skip_newlines()
                    if self.at_canon(","):
                        self.eat()
                        continue
                    break
            self.expect(")", "参数表结束")
            self.skip_newlines()
            ret = {"type": "Null"}
            if self.at_canon("->"):
                self.eat()
                ret = self.parse_type()
                self.skip_newlines()
            last = self.toks[max(0, self.i - 1)]
            members.append({
                "name": mname,
                "params": params,
                "return": ret,
                "span": self.span(mfirst, last),
            })
        close = self.eat_close()
        return {
            "name": name_tok.text,
            "members": members,
            "span": self.span(first, close),
        }

    def parse_dependencies(self) -> list[dict]:
        """顶层依赖块：`依赖 { 工具包 = "1.2.0" }`。

        只做声明与编译期校验；真正的包解析/下载不在阶段 1。
        版本号必须写成字符串，避免 `1.2.0` 被词法拆成三个数字。
        """
        first = self.eat()  # deps
        self.skip_newlines()
        self.expect_block_open()
        deps: list[dict] = []
        while True:
            self.skip_newlines()
            if self.at_canon("}"):
                break
            if self.at_kind(EOF):
                self.fail(Code.BRACE, "依赖块没有闭合，缺少 `}`")
            name_tok = self.expect_ident("依赖包名")
            self.expect("=", "版本锁定值")
            t = self.peek()
            if t.kind != STRING:
                raise error(
                    Code.CONFIG_INVALID,
                    f"依赖 `{name_tok.text}` 的版本号要写成字符串，如 \"1.2.0\"",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                    fix_hints=[FixHint("dep_version", f"{self.file}:{t.line}",
                                       "把版本号放在英文双引号里")],
                )
            ver_tok = self.eat()
            deps.append({
                "name": self._decl_name(name_tok),
                "version": ver_tok.text.strip('"'),
                "span": self.span(name_tok, ver_tok),
            })
            self.skip_newlines()
            if self.at_canon(","):
                self.eat()
                continue
        self.eat_close()
        return deps

    def _decl_name(self, tok: Token) -> str:
        """声明位置的名字**一律保留源码原文**，不翻译。

        唯一的例外处理是：不允许把 `打印 / 成功 / 失败` 这些内建函数重定义成
        用户函数，否则调用位置的规范化和用户定义会打架。
        """
        if tok.kind == KEYWORD and self.table.kind_of(tok.canonical) == "builtin":
            if tok.canonical in ("print", "Ok", "Err"):
                raise error(
                    Code.EXPECT,
                    f"`{tok.text}` 是内建函数名，不能重新定义",
                    file=self.file, line=tok.line, col=tok.col, end_col=tok.end_col,
                    fix_hints=[FixHint("rename", f"{self.file}:{tok.line}", "换一个名字")],
                )
        return tok.text

    # ── 类型 ──────────────────────────────────────────────────────────
    def parse_type(self) -> dict:
        t = self.peek()
        if t.kind == KEYWORD and self.table.kind_of(t.canonical) == "type":
            base = t.canonical
            self.eat()
        elif t.kind == IDENT:
            # 自定义结构体类型：只要本文用 `类型 X { … }` 声明过（声明顺序不限）就认；
            # 也可能是别的模块导出的类型 `模型.用户`。没声明过的裸名字（拼错的
            # `字符串`）仍然当场拒绝，不许静默放过。
            self.eat()
            text = t.text
            while self.at_canon(".") and self.peek(1).kind == IDENT:
                self.eat()
                text += "." + self.eat().text
            if "." in text or text in self.custom_types:
                base = "Custom"
            else:
                raise error(Code.UNKNOWN_TYPE,
                            f"`{t.text}` 不是类型。可用的类型有 AGL 内建类型"
                            f"（整数 / 浮数 / 真假 / 文本 / 空 / 列表 / 映射表 / 结果 / 错误 / 任意）"
                            "和本文 `类型` 声明过的结构体",
                            file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                            fix_hints=[FixHint("use_builtin_type", f"{self.file}:{t.line}",
                                               f"改用 AGL 类型，如 {self._show('Int')} / {self._show('String')}")])
        else:
            raise error(Code.EXPECT, f"这里期望一个类型，实际看到 `{t.text}`",
                        file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                        fix_hints=[FixHint("insert_type", f"{self.file}:{t.line}",
                                           f"补一个类型，如 {self._show('Int')} / {self._show('String')}")])
        node: dict = {"type": base}
        if base == "Custom":
            node["declared"] = text

        if self.at_canon("<"):
            self.eat()
            args: list[dict] = []
            while True:
                self.skip_newlines()
                args.append(self.parse_type())
                self.skip_newlines()
                if self.at_canon(","):
                    self.eat()
                    continue
                break
            self.expect(">", "泛型结束")
            node["args"] = args
        if self.at_canon("?"):
            self.eat()
            node["nullable"] = True
        return node

    def parse_type_decl(self) -> dict:
        """`类型 用户 { 编号: 整数 名字: 文本 }` —— 字段一行一个，逗号可选。"""
        first = self.eat()
        name_tok = self.expect_ident("类型名")
        name = self._decl_name(name_tok)
        # 先登记名字，再解析字段：这样递归类型（`子: 列表<节点>`）也能用。
        self.custom_types.add(name)
        self.skip_newlines()
        self.expect_block_open()
        fields: list[dict] = []
        while True:
            self.skip_newlines()
            if self.at_canon("}"):
                break
            if self.at_kind(EOF):
                self.unclosed_block_error()
            f = self.expect_ident("字段名")
            fname = self._decl_name(f)
            if fname in ("长度", "length", "消息", "message"):
                raise error(Code.EXPECT,
                            f"字段名 `{fname}` 与内置只读属性同名，换一个名字"
                            "（`.长度` / `.消息` 是语言保留属性，任何值上都有）",
                            file=self.file, line=f.line, col=f.col, end_col=f.end_col)
            if any(x["name"] == fname for x in fields):
                raise error(Code.DUP_DECL, f"字段重复：`{fname}`",
                            file=self.file, line=f.line, col=f.col, end_col=f.end_col)
            self.expect(":", "字段类型")
            ftype = self.parse_type()
            fields.append({"name": fname, "type": ftype,
                           "span": self.span(f, self.toks[self.i - 1])})
            self.skip_newlines()
            if self.at_canon(","):
                self.eat()
                continue
        close = self.eat()  # }
        if not fields:
            raise error(Code.EXPECT, f"结构体 `{name}` 至少要有一个字段",
                        file=self.file, line=name_tok.line, col=name_tok.col,
                        end_col=name_tok.end_col)
        self.custom_types.add(name)
        return {"name": name, "fields": fields,
                "span": self.span(first, close)}

    # ── 语句 ──────────────────────────────────────────────────────────
    def parse_stmts_until_brace(self) -> list[dict]:
        stmts: list[dict] = []
        while True:
            self.skip_newlines()
            if self.at_canon("}"):
                break
            if self.at_kind(EOF):
                self.unclosed_block_error()
            stmts.append(self.parse_stmt())
        return stmts

    def parse_stmt(self) -> dict:
        t = self.peek()
        if t.kind == KEYWORD:
            c = t.canonical
            if c in ("let", "const"):
                return self.parse_let()
            if c == "return":
                return self.parse_return()
            if c == "if":
                return self.parse_if()
            if c == "while":
                return self.parse_while()
            if c == "for":
                return self.parse_for()
            if c == "try":
                return self.parse_try()
            if c == "break":
                self.eat()
                return {"stmt": "break", "span": self.span_from(t)}
            if c == "continue":
                self.eat()
                return {"stmt": "continue", "span": self.span_from(t)}
            if c == "match":
                return self.parse_match()
            if c == "with":
                return self.parse_with()
            if c in ("on_progress", "on_finish", "on_fail"):
                return self.parse_on_event()
        # 表达式语句 / 赋值 / 增广赋值
        first = self.peek()
        expr = self.parse_expr()
        aug = self._aug_op()
        if aug:
            value = self.parse_expr()
            self._check_assign_target(expr)
            return {"stmt": "aug_assign", "target": expr, "op": aug, "value": value,
                    "span": self.span(first, self.toks[self.i - 1])}
        if self.at_canon("="):
            self.eat()
            value = self.parse_expr()
            self._check_assign_target(expr)
            return {"stmt": "assign", "target": expr, "value": value,
                    "span": self.span(first, self.toks[self.i - 1])}
        if self.at_canon(",") and expr.get("expr") == "name":
            # 解包重赋值：`甲, 乙 = 值`（目标必须都是名字，且须已声明）
            targets = [expr]
            while self.at_canon(","):
                self.eat()
                t = self.parse_expr()
                if t.get("expr") != "name":
                    raise error(
                        Code.EXPECT,
                        "解包赋值左边只能是一串名字（`甲, 乙 = …`）",
                        file=self.file, line=expr.get("span", {}).get("line", 1),
                        col=expr.get("span", {}).get("col", 0),
                        end_col=t.get("span", {}).get("endCol", 0))
                targets.append(t)
            self.expect("=", "赋值")
            value = self.parse_expr()
            return {"stmt": "assign_multi", "targets": targets, "value": value,
                    "span": self.span(first, self.toks[self.i - 1])}
        return {"stmt": "expr", "value": expr, "span": self.span(first, self.toks[self.i - 1])}

    def _check_assign_target(self, target: dict) -> None:
        """赋值 / 增广赋值的左边只允许名字、成员、下标。

        字面量当左值（`空 = 1`、`真 = 1`、`1 = 2`、`1 += 2`）会让后端生成
        非法宿主代码，错误一直漏到运行期才以宿主 traceback 爆出来 ——
        在语法层就用 AGL 中文诊断拦下。
        """
        if target.get("expr") in ("name", "member", "index"):
            return
        span = target.get("span") or {}
        line = span.get("line", 1)
        col = span.get("col", 0)
        end_col = span.get("endCol", col + 1)
        raise error(
            Code.EXPECT,
            "赋值左边必须是一个名字（或成员、下标），不能是字面量",
            file=self.file, line=line, col=col, end_col=end_col,
            fix_hints=[FixHint("assign_to_name", f"{self.file}:{line}",
                               "把值赋给一个先声明的变量，如 `变量 名字 = …`")],
        )

    def parse_let(self) -> dict:
        first = self.eat()  # let / const
        mutable = first.canonical == "let"
        name_tok = self.expect_ident("变量名")
        name = self._decl_name(name_tok)
        names = [name]
        # 解包声明：`变量 甲, 乙 = 值`（右侧是列表/文本等可解包值）
        while self.at_canon(","):
            self.eat()
            names.append(self._decl_name(self.expect_ident("变量名")))
        decl_type = None
        if self.at_canon(":"):
            self.eat()
            decl_type = self.parse_type()
        self.expect("=", "赋值")
        value = self.parse_expr()
        if len(names) > 1:
            node = {"stmt": "let_multi", "names": names, "mutable": mutable,
                    "value": value, "span": self.span(first, self.toks[self.i - 1])}
            if decl_type:
                node["type"] = decl_type
            return node
        node = {"stmt": "let", "name": name, "mutable": mutable, "value": value,
                "span": self.span(first, self.toks[self.i - 1])}
        if decl_type:
            node["type"] = decl_type
        return node

    def parse_list_pattern(self) -> dict:
        """`[变量 首, 1, [变量 甲, _]]` —— 列表模式，元素可递归嵌套。

        调用前当前记号必须是 `[`；返回 `expr:"list_pattern"` 的节点。
        """
        open_tok = self.eat()
        items: list[dict] = []
        self.skip_newlines()
        while not self.at_canon("]"):
            if self.at_canon("let"):
                self.eat()
                self.skip_newlines()
                it_tok = self.expect_ident("列表模式绑定的名字")
                items.append({"kind": "bind", "name": it_tok.text,
                              "span": self.span(it_tok, it_tok)})
            elif self.at_canon("["):
                nested = self.parse_list_pattern()
                items.append({"kind": "list", "pattern": nested,
                              "span": nested.get("span")})
            elif self.at_canon("{"):
                nested = self.parse_map_pattern()
                items.append({"kind": "map", "pattern": nested,
                              "span": nested.get("span")})
            else:
                item_expr = self.parse_expr()
                item_kind = item_expr.get("expr")
                if item_kind == "name" and item_expr.get("name") == "_":
                    items.append({"kind": "wild", "name": "_",
                                  "span": item_expr.get("span")})
                elif item_kind in ("int", "float", "string", "bool", "null"):
                    items.append({"kind": "literal", "value": item_expr,
                                  "span": item_expr.get("span")})
                else:
                    span = item_expr.get("span") or {}
                    raise error(
                        Code.EXPECT,
                        "列表模式的元素只允许字面量、`_`、`变量 名` 或 `[ … ]`",
                        file=self.file, line=span.get("line", 1),
                        col=span.get("col", 0), end_col=span.get("endCol", 0),
                    )
            self.skip_newlines()
            if self.at_canon(","):
                self.eat()
                self.skip_newlines()
            else:
                break
        close_tok = self.expect("]")
        return {"expr": "list_pattern", "items": items,
                "span": self.span(open_tok, close_tok)}

    def parse_map_pattern(self) -> dict:
        """`{ "键": 模式, … }` 映射模式；只检查这些键存在且值匹配，允许多余键。"""
        open_tok = self.eat()  # {
        entries: list[dict] = []
        self.skip_newlines()
        while not self.at_canon("}"):
            key = self.parse_expr()
            if key.get("expr") not in ("string", "int", "float", "bool", "null"):
                span = key.get("span") or {}
                raise error(
                    Code.EXPECT,
                    "映射模式的键只允许字面量（文本 / 整数 / 浮数 / 布尔 / 空）",
                    file=self.file, line=span.get("line", 1),
                    col=span.get("col", 0), end_col=span.get("endCol", 0),
                )
            self.expect(":")
            self.skip_newlines()
            if self.at_canon("["):
                nested = self.parse_list_pattern()
                val = {"kind": "list", "pattern": nested,
                       "span": nested.get("span")}
            elif self.at_canon("{"):
                nested = self.parse_map_pattern()
                val = {"kind": "map", "pattern": nested,
                       "span": nested.get("span")}
            elif self.at_canon("let"):
                self.eat()
                self.skip_newlines()
                name_tok = self.expect_ident("映射模式绑定的名字")
                val = {"kind": "bind", "name": name_tok.text,
                       "span": self.span(name_tok, name_tok)}
            else:
                val_expr = self.parse_expr()
                val_kind = val_expr.get("expr")
                if val_kind == "name" and val_expr.get("name") == "_":
                    val = {"kind": "wild", "name": "_",
                           "span": val_expr.get("span")}
                elif val_kind in ("int", "float", "string", "bool", "null"):
                    val = {"kind": "literal", "value": val_expr,
                           "span": val_expr.get("span")}
                else:
                    span = val_expr.get("span") or {}
                    raise error(
                        Code.EXPECT,
                        "映射模式的值只允许字面量、`_`、`变量 名`、`[ … ]` 或 `{ … }`",
                        file=self.file, line=span.get("line", 1),
                        col=span.get("col", 0), end_col=span.get("endCol", 0),
                    )
            entries.append({"key": key, "value": val})
            self.skip_newlines()
            if self.at_canon(","):
                self.eat()
                self.skip_newlines()
            else:
                break
        close_tok = self.expect("}")
        return {"expr": "map_pattern", "entries": entries,
                "span": self.span(open_tok, close_tok)}

    def parse_match(self) -> dict:
        """`匹配 (表达式) { 字面量 { … } … 否则 { … } }` —— 从上往下第一个相等的分支执行。

        模式允许字面量（整数/浮数/文本/布尔/空）、`_` 通配符（匹配任意），
        或 `变量 名` 绑定（匹配任意并把主体绑定到该名字，块内可读）。
        无兜底且无匹配时什么都不执行（与 Python `match` 一致）。
        """
        first = self.eat()  # match
        self.skip_newlines()
        # 两种写法都认：设计文档的 `匹配 值 { … }`，以及带括号的 `匹配 (值) { … }`。
        if self.at_canon("("):
            self.eat()
            subject = self.parse_expr()
            self.expect(")")
        else:
            subject = self.parse_expr()
        self.skip_newlines()
        self.expect_block_open()
        arms: list[dict] = []
        while not self.at_canon("}"):
            self.skip_newlines()
            if self.at_canon("else"):
                else_tok = self.eat()
                self.skip_newlines()
                if self.at_canon("if"):
                    self.eat()
                    self.skip_newlines()
                    guard = self.parse_expr()
                    self.skip_newlines()
                else:
                    guard = None
                if self.at_canon("=>"):
                    self.eat()
                    self.skip_newlines()
                    if self.at_canon("{"):
                        self.expect_block_open()
                        body = self.parse_stmts_until_brace()
                        self.eat_close()
                    else:
                        body = [self.parse_stmt()]
                else:
                    self.expect_block_open()
                    body = self.parse_stmts_until_brace()
                    self.eat_close()
                arm = {"pattern": {"expr": "name", "name": "_",
                                   "span": self.span(else_tok, else_tok)},
                       "body": body}
                if guard is not None:
                    arm["guard"] = guard
                arms.append(arm)
                self.skip_newlines()
                if guard is None:
                    break
                continue
            bind = None
            if self.at_canon("let"):
                kw = self.eat()  # 变量 / let
                self.skip_newlines()
                bind_tok = self.expect_ident("匹配绑定的名字")
                bind = bind_tok.text
                pat = {"expr": "name", "name": "_",
                       "span": self.span(bind_tok, bind_tok)}
            elif self.at_canon("["):
                pat = self.parse_list_pattern()
            elif self.at_canon("{"):
                pat = self.parse_map_pattern()
            else:
                pat = self.parse_expr()
            pat_kind = pat.get("expr")
            if bind is None and pat_kind not in ("int", "float", "string", "bool", "null", "list_pattern", "map_pattern"):
                if pat_kind == "name" and pat.get("name") == "_":
                    pat = {"expr": "name", "name": "_", "span": pat.get("span")}
                else:
                    span = pat.get("span") or {}
                    raise error(
                        Code.EXPECT,
                        "匹配模式只允许字面量、`_` 通配符、`变量 名` 绑定、`[ … ]` 列表模式或 `{ … }` 映射模式："
                        "（整数 / 浮数 / 文本 / 布尔 / 空）",
                        file=self.file, line=span.get("line", 1),
                        col=span.get("col", 0), end_col=span.get("endCol", 0),
                    )
            self.skip_newlines()
            guard = None
            if self.at_canon("if"):
                self.eat()
                self.skip_newlines()
                guard = self.parse_expr()
                self.skip_newlines()
            # 两种分支写法都认：设计文档里的 `值 => 语句`，以及大括号块 `值 { … }`。
            if self.at_canon("=>"):
                self.eat()
                self.skip_newlines()
                if self.at_canon("{"):
                    self.expect_block_open()
                    body = self.parse_stmts_until_brace()
                    self.eat_close()
                else:
                    body = [self.parse_stmt()]
            else:
                self.expect_block_open()
                body = self.parse_stmts_until_brace()
                self.eat_close()
            arm = {"pattern": pat, "body": body}
            if bind is not None:
                arm["bind"] = bind
            if guard is not None:
                arm["guard"] = guard
            arms.append(arm)
            self.skip_newlines()
            if self.at_canon("else"):
                else_tok = self.eat()
                self.skip_newlines()
                if self.at_canon("if"):
                    # `否则 如果 条件 { … }`：通配模式 + 守卫，后面还能再跟 `否则`。
                    self.eat()
                    self.skip_newlines()
                    guard = self.parse_expr()
                    self.skip_newlines()
                    if self.at_canon("=>"):
                        self.eat()
                        self.skip_newlines()
                        if self.at_canon("{"):
                            self.expect_block_open()
                            body = self.parse_stmts_until_brace()
                            self.eat_close()
                        else:
                            body = [self.parse_stmt()]
                    else:
                        self.expect_block_open()
                        body = self.parse_stmts_until_brace()
                        self.eat_close()
                    arms.append({"pattern": {"expr": "name", "name": "_",
                                             "span": self.span(else_tok, else_tok)},
                                 "guard": guard, "body": body})
                    self.skip_newlines()
                    continue
                if self.at_canon("=>"):
                    self.eat()
                    self.skip_newlines()
                    if self.at_canon("{"):
                        self.expect_block_open()
                        body = self.parse_stmts_until_brace()
                        self.eat_close()
                    else:
                        body = [self.parse_stmt()]
                else:
                    self.expect_block_open()
                    body = self.parse_stmts_until_brace()
                    self.eat_close()
                arms.append({"pattern": {"expr": "name", "name": "_",
                                         "span": self.span(else_tok, else_tok)},
                            "body": body})
                self.skip_newlines()
                break
        self.eat_close()  # }
        return {"stmt": "match", "subject": subject, "arms": arms,
                "span": self.span_from(first)}

    def parse_match_expr(self) -> dict:
        """表达式位置 `匹配 值 { 模式 => 表达式 … }`。

        与语句版区别只有两点：分支体必须是单个表达式（`=>` 必写），
        没有兜底分支时整条表达式取 `空`。模式语法与语句版完全一致。
        """
        first = self.eat()  # match
        self.skip_newlines()
        if self.at_canon("("):
            self.eat()
            subject = self.parse_expr()
            self.expect(")")
        else:
            subject = self.parse_expr()
        self.skip_newlines()
        self.expect_block_open()
        arms: list[dict] = []
        closed = False
        while not self.at_canon("}"):
            self.skip_newlines()
            if self.at_canon("else"):
                else_tok = self.eat()
                self.skip_newlines()
                guard = None
                if self.at_canon("if"):
                    self.eat()
                    self.skip_newlines()
                    guard = self.parse_expr()
                    self.skip_newlines()
                self.expect("=>", "匹配分支")
                self.skip_newlines()
                arm = {"pattern": {"expr": "name", "name": "_",
                                   "span": self.span(else_tok, else_tok)},
                       "body": self.parse_expr()}
                if guard is not None:
                    arm["guard"] = guard
                arms.append(arm)
                self.skip_newlines()
                if guard is None:
                    closed = True
                    break
                if self.at_canon(","):
                    self.eat()
                continue
            bind = None
            if self.at_canon("let"):
                kw = self.eat()
                self.skip_newlines()
                bind_tok = self.expect_ident("匹配绑定的名字")
                bind = bind_tok.text
                pat = {"expr": "name", "name": "_",
                       "span": self.span(bind_tok, bind_tok)}
            elif self.at_canon("["):
                pat = self.parse_list_pattern()
            elif self.at_canon("{"):
                pat = self.parse_map_pattern()
            else:
                pat = self.parse_expr()
            pat_kind = pat.get("expr")
            if bind is None and pat_kind not in (
                    "int", "float", "string", "bool", "null",
                    "list_pattern", "map_pattern"):
                if pat_kind == "name" and pat.get("name") == "_":
                    pat = {"expr": "name", "name": "_", "span": pat.get("span")}
                else:
                    span = pat.get("span") or {}
                    raise error(
                        Code.EXPECT,
                        "匹配模式只允许字面量、`_` 通配符、`变量 名` 绑定、"
                        "`[ … ]` 列表模式或 `{ … }` 映射模式",
                        file=self.file, line=span.get("line", 1),
                        col=span.get("col", 0), end_col=span.get("endCol", 0),
                    )
            self.skip_newlines()
            guard = None
            if self.at_canon("if"):
                self.eat()
                self.skip_newlines()
                guard = self.parse_expr()
                self.skip_newlines()
            self.expect("=>", "匹配表达式分支")
            self.skip_newlines()
            arm = {"pattern": pat, "body": self.parse_expr()}
            if bind is not None:
                arm["bind"] = bind
            if guard is not None:
                arm["guard"] = guard
            arms.append(arm)
            self.skip_newlines()
            if self.at_canon(","):
                self.eat()
                self.skip_newlines()
        if not closed:
            self.eat_close()
        else:
            self.expect("}", "匹配表达式结尾")
        return {"expr": "match", "subject": subject, "arms": arms,
                "span": self.span_from(first)}

    def parse_with(self) -> dict:
        """`使用 表达式 作为 名 { … }`（或省略 `作为 名`）——上下文管理器。"""
        first = self.eat()  # with
        self.skip_newlines()
        expr = self.parse_expr()
        name = None
        if self.at_canon("as"):
            self.eat()
            name = self._decl_name(self.expect_ident("绑定名"))
        self.skip_newlines()
        self.expect_block_open()
        body = self.parse_stmts_until_brace()
        self.eat_close()
        return {"stmt": "with", "expr": expr, "name": name, "body": body,
                "span": self.span_from(first)}

    def parse_return(self) -> dict:
        first = self.eat()
        # 返回值必须和 `返回` 在同一行。
        # 这里**不能跳过换行**：跳了以后裸 `返回`（紧接着写下一行语句，也就是
        # 提前返回的常见写法）会把下一行整条语句吞成返回值 ——
        # 于是「返回; 打印(...)」变成「返回 打印(...)」，语义直接反了。
        if self.at_kind(NEWLINE) or self.at_canon("}") or self.at_kind(EOF):
            return {"stmt": "return", "span": self.span_from(first)}
        value = self.parse_expr()
        return {"stmt": "return", "value": value,
                "span": self.span(first, self.toks[self.i - 1])}

    def parse_on_event(self) -> dict:
        """`当收到进度(句柄, 说明: 文本) { … }` —— 订阅回调的固定写法。

        语法是定死的三种，参数也定死两个：先是任务，再是「名字: 类型」。
        不给自由发挥的空间，是因为事件名自由发挥之后，审计就没法做了。
        """
        first = self.eat()
        event = {"on_progress": "progress", "on_finish": "finish",
                 "on_fail": "fail"}[first.canonical]
        self.expect("(", "回调参数")
        task = self.parse_expr()
        self.expect(",", "任务和回调参数之间要一个逗号")
        name_tok = self.expect_ident("回调参数名")
        name = self._decl_name(name_tok)
        self.expect(":", "回调参数的类型")
        ptype = self.parse_type()
        self.expect(")", "回调参数列表结尾")
        self.skip_newlines()
        self.expect_block_open()
        body = self.parse_stmts_until_brace()
        self.eat_close()
        return {"stmt": "on_event", "event": event, "task": task,
                "param": name, "param_type": ptype, "body": body,
                "span": self.span_from(first)}

    def parse_if(self) -> dict:
        first = self.eat()  # if
        cond = self.parse_expr()
        self.skip_newlines()
        self.expect_block_open()
        then = self.parse_stmts_until_brace()
        self.eat_close()
        node = {"stmt": "if", "cond": cond, "then": then,
                "span": self.span_from(first)}
        self.skip_newlines()
        if self.at_canon("else"):
            self.eat()
            self.skip_newlines()
            if self.at_canon("if"):
                nested = self.parse_if()
                node["else"] = [nested]
            else:
                self.expect_block_open()
                node["else"] = self.parse_stmts_until_brace()
                self.eat_close()
                self.skip_newlines()
                if self.at_canon("else"):
                    t = self.peek()
                    raise error(Code.EXPECT,
                                f"连续两个 `{self._show('else')}` 无法判断归属，请用 `}} {self._show('else')} {{` 显式分层",
                                file=self.file, line=t.line, col=t.col, end_col=t.end_col)
            node["span"] = self.span_from(first)
        return node

    def parse_while(self) -> dict:
        first = self.eat()
        cond = self.parse_expr()
        self.skip_newlines()
        self.expect_block_open()
        body = self.parse_stmts_until_brace()
        self.eat_close()
        node = {"stmt": "while", "cond": cond, "body": body,
                "span": self.span_from(first)}
        self.skip_newlines()
        if self.at_canon("else"):
            self.eat()
            self.skip_newlines()
            self.expect_block_open()
            node["else"] = self.parse_stmts_until_brace()
            self.eat_close()
            node["span"] = self.span_from(first)
        return node

    def parse_for(self) -> dict:
        first = self.eat()  # for
        item_tok = self.expect_ident("循环变量")
        item = self._decl_name(item_tok)
        self.expect("in", "循环关键字")
        iterable = self.parse_expr()
        self.skip_newlines()
        self.expect_block_open()
        body = self.parse_stmts_until_brace()
        self.eat_close()
        node = {"stmt": "for", "item": item, "iter": iterable, "body": body,
                "span": self.span_from(first)}
        self.skip_newlines()
        if self.at_canon("else"):
            self.eat()
            self.skip_newlines()
            self.expect_block_open()
            node["else"] = self.parse_stmts_until_brace()
            self.eat_close()
            node["span"] = self.span_from(first)
        return node

    def parse_try(self) -> dict:
        first = self.eat()  # try
        self.skip_newlines()
        self.expect_block_open()
        body = self.parse_stmts_until_brace()
        self.eat_close()
        node = {"stmt": "try", "body": body, "span": self.span_from(first)}
        self.skip_newlines()
        if self.at_canon("catch"):
            self.eat()
            self.skip_newlines()
            if self.at_canon("("):
                self.eat()
                name_tok = self.expect_ident("错误变量名")
                node["catch_name"] = self._decl_name(name_tok)
                if self.at_canon(":"):
                    self.eat()
                    type_tok = self.expect_ident("异常类型名")
                    node["catch_type"] = self._decl_name(type_tok)
                self.expect(")")
                self.skip_newlines()
            self.expect_block_open()
            node["catch"] = self.parse_stmts_until_brace()
            self.eat_close()
            self.skip_newlines()
        if self.at_canon("else"):
            if "catch" not in node:
                t = self.peek()
                raise error(
                    Code.EXPECT,
                    f"`{self._show('else')}` 只能跟在 `{self._show('catch')}` 后面："
                    f"没出错才执行的分支必须先有兜底",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                )
            self.eat()
            self.skip_newlines()
            self.expect_block_open()
            node["else"] = self.parse_stmts_until_brace()
            self.eat_close()
            node["span"] = self.span_from(first)
            self.skip_newlines()
        if self.at_canon("finally"):
            self.eat()
            self.skip_newlines()
            self.expect_block_open()
            node["finally"] = self.parse_stmts_until_brace()
            self.eat_close()
            node["span"] = self.span_from(first)
        elif "catch" not in node:
            t = first
            raise error(
                Code.EXPECT,
                f"`{self._show('try')}` 后面必须有 `{self._show('catch')}` 或 `{self._show('finally')}` 收尾",
                file=self.file, line=t.line, col=t.col, end_col=t.end_col,
            )
        return node

    # ── 表达式 ────────────────────────────────────────────────────────
    def parse_expr(self) -> dict:
        return self.parse_binop(0)

    def parse_binop(self, level: int) -> dict:
        if level >= len(BIN_LEVELS):
            return self.parse_unary()
        first = self.peek()
        left = self.parse_binop(level + 1)
        while True:
            t = self.peek()
            if t.kind == OP and t.canonical in BIN_LEVELS[level]:
                op = self.eat().canonical
                # 运算符后允许换行：长条件可以写成 `a &&\n b`。
                # 只在运算符之后跨行，避免在语句边界误吞换行。
                self.skip_newlines()
                right = self.parse_binop(level + 1)
                left = {"expr": "binop", "op": op, "left": left, "right": right,
                        "span": self.span(first, self.toks[self.i - 1])}
                continue
            return left

    def parse_unary(self) -> dict:
        t = self.peek()
        if t.kind == KEYWORD and t.canonical == "await":
            self.eat()
            operand = self.parse_unary()
            return {"expr": "await", "value": operand,
                    "span": self.span(t, self.toks[self.i - 1])}
        if t.kind == OP and t.canonical in ("!", "-"):
            self.eat()
            operand = self.parse_unary()
            return {"expr": "unop", "op": t.canonical, "operand": operand,
                    "span": self.span(t, self.toks[self.i - 1])}
        return self.parse_postfix()

    def parse_postfix(self) -> dict:
        first = self.peek()
        node = self.parse_primary()
        while True:
            t = self.peek()
            if t.kind == OP and t.canonical == "(":
                self.eat()
                args: list[dict] = []
                self.skip_newlines()
                if not self.at_canon(")"):
                    while True:
                        self.skip_newlines()
                        # 命名实参：`名字: 值`（只对用户函数生效，内建会报错）。
                        # 判据是「标识符 + 冒号」，普通实参不会长这样。
                        nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
                        if (self.peek().kind == IDENT and nxt is not None
                                and nxt.canonical == ":"):
                            name_tok = self.eat()
                            self.eat()  # :
                            self.skip_newlines()
                            value = self.parse_expr()
                            args.append({"expr": "named", "name": name_tok.text,
                                         "value": value,
                                         "span": self.span(name_tok, self.toks[self.i - 1])})
                        else:
                            args.append(self.parse_expr())
                        self.skip_newlines()
                        if self.at_canon(","):
                            self.eat()
                            continue
                        break
                self.expect(")", "参数表结束")
                # `工具.打招呼()` 与 `打印(x)` 都归一成 call，
                # 区别在于前者带 target —— 少这一个字段，模块调用就没法表达。
                if node.get("expr") == "member":
                    cname, ctarget = node["field"], node["target"]
                elif node.get("expr") == "name":
                    cname, ctarget = node["name"], None
                else:
                    cname, ctarget = "?", None
                node = {"expr": "call", "name": cname, "args": args,
                        "span": self.span(first, self.toks[self.i - 1])}
                if ctarget is not None:
                    node["target"] = ctarget
                continue
            if t.kind == OP and t.canonical == ".":
                self.eat()
                field_tok = self.peek()
                if field_tok.kind not in (IDENT, KEYWORD):
                    self.fail(Code.EXPECT, f"`.` 后面要跟成员名，实际看到 `{field_tok.text}`")
                self.eat()
                node = {"expr": "member", "target": node, "field": field_tok.text,
                        "span": self.span(first, self.toks[self.i - 1])}
                continue
            if t.kind == OP and t.canonical == "[":
                self.eat()
                idx = self.parse_expr()
                self.expect("]", "索引结束")
                node = {"expr": "index", "target": node, "index": idx,
                        "span": self.span(first, self.toks[self.i - 1])}
                continue
            return node

    def parse_primary(self) -> dict:
        t = self.peek()

        if t.kind == INT:
            self.eat()
            return {"expr": "int", "value": int(t.text), "span": self.span(t, t)}
        if t.kind == FLOAT:
            self.eat()
            return {"expr": "float", "value": float(t.text), "span": self.span(t, t)}
        if t.kind == STRING:
            self.eat()
            raw = t.text
            # 三引号多行字符串：去掉 3+3 个引号；单引号字符串去掉 1+1 个。
            body = raw[3:-3] if raw.startswith('"""') else raw[1:-1]
            return {"expr": "string", "value": _unescape(body), "span": self.span(t, t)}

        if t.kind == KEYWORD:
            if t.canonical == "match":
                return self.parse_match_expr()
            kind = self.table.kind_of(t.canonical)
            if kind == "literal":
                self.eat()
                return {"expr": "bool", "value": t.canonical == "true", "span": self.span(t, t)}
            if t.canonical == "Null":
                self.eat()
                return {"expr": "null", "value": None, "span": self.span(t, t)}
            if kind == "builtin":
                self.eat()
                return {"expr": "name", "name": t.canonical, "span": self.span(t, t)}
            # 集合字面量：`集合(1, 2)` 等价于 `建集合([1, 2])`。
            # 类型名 `集合` 本来不是值，只有紧跟 `(` 时才是构造。
            if t.canonical in ("Set", "set"):
                self.eat()
                if not self.at_canon("("):
                    raise error(Code.EXPECT,
                                f"`{t.text}` 在表达式里只能写成 `{t.text}(...)"
                                "（集合字面量），或改用类型标注位置",
                                file=self.file, line=t.line, col=t.col,
                                end_col=t.end_col)
                self.eat()  # (
                items: list[dict] = []
                self.skip_newlines()
                if not self.at_canon(")"):
                    while True:
                        items.append(self.parse_expr())
                        self.skip_newlines()
                        if self.at_canon(","):
                            self.eat()
                            self.skip_newlines()
                            continue
                        break
                close = self.peek()
                self.expect(")", "集合字面量结尾")
                return {"expr": "call", "name": "make_set",
                        "args": [{"expr": "list", "items": items,
                                  "span": self.span(t, close)}],
                        "span": self.span(t, close)}
            # 模块名可以是关键字（`导入 模型`）：`模型.函数()` 里的 `模型`
            # 是导入的模块名前缀，不是关键字用法。
            mod_names = {imp["module"] for imp in self.imports}
            mod_names |= {imp["alias"] for imp in self.imports if imp.get("alias")}
            if t.text in mod_names:
                self.eat()
                return {"expr": "name", "name": t.text, "span": self.span(t, t)}
            # 其他关键字出现在表达式位置 = 语法错误
            raise error(Code.EXPECT,
                        f"这里期望一个值或表达式，但 `{t.text}` 是关键字",
                        file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                        fix_hints=[FixHint("insert_expr", f"{self.file}:{t.line}",
                                           "这里要写一个值、变量名或函数调用")])

        if t.kind == IDENT:
            self.eat()
            return {"expr": "name", "name": t.text, "span": self.span(t, t)}

        if t.kind == OP:
            if t.canonical == "(":
                self.eat()
                inner = self.parse_expr()
                self.expect(")", "括号结束")
                return inner
            if t.canonical == "[":
                self.eat()
                items: list[dict] = []
                self.skip_newlines()
                if not self.at_canon("]"):
                    while True:
                        self.skip_newlines()
                        items.append(self.parse_expr())
                        self.skip_newlines()
                        if self.at_canon(","):
                            self.eat()
                            continue
                        break
                self.expect("]", "列表结束")
                return {"expr": "list", "items": items,
                        "span": self.span(t, self.toks[self.i - 1])}
            if t.canonical == "{":
                self.eat()
                # 花括号字面量两种形状，按第一个元素后面跟什么判断：
                #   `{1, 2, 3}`  → 集合字面量（逗号分隔、元素后不跟冒号）
                #   `{"a": 1}`   → 映射表字面量（键值对）
                #   `{}`         → 空映射表（与 Python 的 dict 语义一致，保持现状）
                self.skip_newlines()
                if self.at_canon("}"):
                    self.eat()
                    return {"expr": "map", "entries": [],
                            "span": self.span(t, self.toks[self.i - 1])}
                first = self.parse_expr()
                self.skip_newlines()
                if self.at_canon(":"):
                    # 映射表：第一个元素是键，继续读键值对
                    self.eat()
                    v = self.parse_expr()
                    entries = [{"key": first, "value": v}]
                    self.skip_newlines()
                    while self.at_canon(","):
                        self.eat()
                        self.skip_newlines()
                        if self.at_canon("}"):
                            break
                        k2 = self.parse_expr()
                        self.expect(":", "键值分隔")
                        v2 = self.parse_expr()
                        entries.append({"key": k2, "value": v2})
                        self.skip_newlines()
                    self.expect("}", "映射表结束")
                    return {"expr": "map", "entries": entries,
                            "span": self.span(t, self.toks[self.i - 1])}
                # 集合字面量：逗号分隔的若干元素
                items = [first]
                self.skip_newlines()
                while self.at_canon(","):
                    self.eat()
                    self.skip_newlines()
                    if self.at_canon("}"):
                        break
                    items.append(self.parse_expr())
                    self.skip_newlines()
                self.expect("}", "集合结束")
                return {"expr": "set", "items": items,
                        "span": self.span(t, self.toks[self.i - 1])}

        raise error(Code.EXPECT,
                    f"这里期望一个值或表达式，实际看到 `{t.text}`" if t.kind != EOF else "文件意外结束",
                    file=self.file, line=t.line, col=t.col, end_col=t.end_col,
                    fix_hints=[FixHint("insert_expr", f"{self.file}:{t.line}", "补上缺失的表达式")])


def _unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            n = raw[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse(tokens: list[Token], filename: str, lang: str) -> dict:
    try:
        return Parser(tokens, filename, lang).parse_program()
    except RecursionError:
        # 递归下降解析器遇到 ~900 层嵌套会耗尽 Python 栈。不能把裸的
        # RecursionError 甩给用户——那没有文件、没有行号，也看不出是语法问题。
        tok = tokens[-1] if tokens else None
        raise error(
            Code.RECURSION_LIMIT,
            "语法嵌套太深：括号/块一层套一层，已经超出解析器能处理的深度；"
            "请把这段表达式或块拆成中间变量，减少嵌套层数",
            file=filename,
            line=tok.line if tok else 1,
            col=tok.col if tok else 1,
            end_col=tok.end_col if tok else 1,
        )


_TOP_LEVEL_START = {
    "export", "interface", "deps", "import", "module", "type", "const",
    "fn", "async",
}
_TOP_LEVEL_TEXT = {"工具", "智能体", "规则", "tool", "agent", "rule"}


def _recover_boundary(tokens: list[Token], start: int) -> int:
    """从出错处向后找下一个顶层声明起点，用于一次收集多个声明错误。

    只按花括号深度切分，不试图猜缺失的 `}`；如果括号一直没闭合，就停在
    文件末尾，让诊断如实显示「后面已经无法继续解析」。
    """
    depth = 0
    for j in range(max(0, start), len(tokens)):
        t = tokens[j]
        if t.kind == EOF:
            return len(tokens) - 1
        if t.canonical == "{":
            depth += 1
        elif t.canonical == "}":
            depth = max(0, depth - 1)
            continue
        if j <= start or depth != 0:
            continue
        prev = tokens[j - 1]
        if prev.kind != NEWLINE:
            continue
        if t.kind == KEYWORD and t.canonical in _TOP_LEVEL_START:
            return j
        if t.text in _TOP_LEVEL_TEXT:
            return j
    return len(tokens) - 1


def parse_many(tokens: list[Token], filename: str, lang: str) -> tuple[dict | None, list]:
    """尽力一次收集多个顶层声明错误；成功时返回 AST 与空列表。

    恢复粒度是「顶层声明」：一个函数体内部仍然会在第一个错误处停下，
    但不会因为第一个函数写错就吞掉后面所有函数的错误。
    """
    errors: list = []
    rest = tokens
    while rest:
        p = Parser(rest, filename, lang)
        try:
            return p.parse_program(), errors
        except AglError as e:
            errors.append(e.diag)
            boundary = _recover_boundary(p.toks, p.i)
            if boundary <= p.i or boundary >= len(p.toks):
                break
            rest = p.toks[boundary:]
        except RecursionError:
            tok = rest[-1] if rest else None
            errors.append(error(
                Code.RECURSION_LIMIT,
                "语法嵌套太深：括号/块一层套一层，已经超出解析器能处理的深度；"
                "请把这段表达式或块拆成中间变量，减少嵌套层数",
                file=filename,
                line=tok.line if tok else 1,
                col=tok.col if tok else 1,
                end_col=tok.end_col if tok else 1,
            ).diag)
            break
    return None, errors

