# -*- coding: utf-8 -*-
"""阶段 3 · Python 源码导入器：把一份 Python 翻成人能读的 AGL。

**为什么先做"翻译成源码"，而不是直接造 IR**：

1. 导入的价值是让不懂代码的人看懂 AI 写了什么，产物必须是一份
   **能读、能改、能重新编译的 AGL 源码**，而不是只有机器看得懂的中间表示；
2. AGL 的 `span` 只能由前端 M2 生成（硬规则）。这里直接造 IR 等于绕过唯一的
   span 来源；翻成源码再走正规编译，span、行号对齐、错误回译全部照常成立。

**核心态度：不能证明等价就不导。**

Python 里大量"看起来一一对应"的写法其实并不等价，举三个真的会坑人的：

  · `list.append(x)` 在 Python 里原地改列表、返回 None；AGL 的 `追加` 返回新列表，不改原列表。
    语句形式（`xs.append(x)`）和整句赋值形式（`x = xs.append(v)`）已能安全翻译，
    更深表达式里的 append 仍拒绝 —— 拆不干净就会给错答案；
  · `int("x")` 抛异常；AGL 的 `转整数` 返回 `失败(原因)`、不抛；
    v0.29 起 `int(x)` 翻译成 `取到(转整数(x))` —— 取到 处抛成可被捕获的中文错误，语义等价；
  · `time.sleep(1)` 是 1 秒；AGL 的 `睡眠(1)` 是 1 毫秒。

这些一律记为 blocker 拒绝导入，绝不猜一个自以为是的映射糊过去 ——
导入产物是要拿来给人做审查依据的，一份"看着对其实错"的翻译比没翻译更危险。
"""
from __future__ import annotations

import ast
import copy
import dataclasses

from compiler.lexer import LANG_EN, LANG_ZH, KeywordTable

# Python 运算符 → IR 里的运算符记号（再按分支渲染成词或符号）
_BINOP = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Mod: "%",
}

_CMP = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Gt: ">", ast.Lt: "<",
    ast.GtE: ">=", ast.LtE: "<=",
}

# Python 类型注解（写成字符串的样子）→ AGL 类型关键字的 canon 名
_SCALAR = {
    "int": "Int", "float": "Float", "bool": "Bool", "str": "String",
    "list": "List", "dict": "Map", "set": "Set", "None": "Null",}

# 一行 AGL 的缩进
_IND = "  "


@dataclasses.dataclass
class ImportResult:
    ok: bool
    agl: str = ""
    blockers: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    stats: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "agl": self.agl,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
        }


class Blocked(Exception):
    """遇到了没法证明等价的写法，导入就此中止。

    用异常是为了让深层的递归（表达式里套表达式）能一路上抛，
    每一层不必各自判断"要不要继续"。
    """


class _RenameParams(ast.NodeTransformer):
    """把 AST 里的参数名替换成展开时分配的绑定名（避免构造链重名）。"""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id in self.mapping:
            node.id = self.mapping[node.id]
        return node


# Python 内建类型名 → AGL canonical 类型词（isinstance 第二参专用）
_PY_TYPE_NAMES = {
    "int": "Int", "integer": "Int",
    "float": "Float", "floatnumber": "Float",
    "str": "String", "string": "String",
    "bool": "Bool", "boolean": "Bool",
    "list": "List", "dict": "Map", "set": "Set",
    "NoneType": "Null",
}

# 模块级放行的导入名：调用处已翻译成 AGL 内建，函数体内的同名引用不算闭包捕获
# asyncio（v0.39）：asyncio.run / asyncio.sleep 在调用处有专属翻译，
# 函数体内的 `asyncio.xxx` 引用不算闭包捕获。
_ALLOWED_MODULES = frozenset(
    {"math", "random", "time", "json", "requests", "urllib", "asyncio"})


class PyImporter:
    """把 Python 源码翻译成 AGL（模块级常量 → 常量函数、类 → 构建函数 + 方法函数、
    闭包/lambda → 提升、结果语义 → 结果 包装等）。"""

    def __init__(self, src: str, lang: str = LANG_ZH):
        self.src = src
        self.lang = lang if lang in (LANG_ZH, LANG_EN) else LANG_ZH
        self.kw = KeywordTable.load()
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.depth = 0
        # v0.48：海象运算符待插入语句队列——expr() 里遇到 NamedExpr 时，
        # 把赋值语句塞进来，由外层调用方先把这些语句写出来再继续
        self.pending: list[str] = []
        self.funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.has_entry = False
        self.declared: set[str] = set()
        # 已做过「块内首次声明提升」的复合语句（防同一块被提升两次）
        self._promoted: set[ast.stmt] = set()
        # 需要改名的 Python 名字 → AGL 名字（目前只有入口函数会走这一步）
        self.rename_map: dict[str, str] = {}
        # 类：类名 → {node, methods}（AGL 没有 OOP，翻译成构造函数 + 实例显式传参的普通函数）
        self.classes: dict[str, dict] = {}
        # 模块级只读常量：名字 → 值表达式（翻译成同名常量函数）
        self.global_consts: dict[str, ast.expr] = {}
        # 可变全局（v0.38）：函数体里 global 声明或赋值命中的模块级常量 → 名字集合。
        # 这类变量不再发常量函数，统一存进模块级共享映射 `_agl_globals`（读写同源）。
        self.mutable_globals: set[str] = set()
        # 可变类属性（v0.38）：`C.x = ...` 被函数体赋值命中的类属性 → (类名, 属性名)。
        # 读/写都走 `_agl_globals["C.x"]`（与 Python 跨实例共享的可变类属性语义一致）。
        self.mutable_class_attrs: set[tuple[str, str]] = set()
        # v0.48：动态加的方法——原函数名 → 新函数名（MyClass.my_method = my_method → MyClass_my_method）
        self.dynamic_method_map: dict[str, str] = {}
        # 可变类属性初值：(类名, 属性名) → 值表达式（预扫描时从 classes[cls]["attrs"] 收集）
        self.mutable_class_attr_values: dict[tuple[str, str], ast.expr] = {}
        # 模块级共享存储的名字（可变全局 + 可变类属性共用一个映射表）
        self.globals_store = "_agl_globals"
        # 被提升成命名函数的 lambda：[(函数名, 函数体行)]
        self.lifted_fns: list[tuple[str, list[str]]] = []
        # 捕获型 lambda：原变量名 → {提升函数名, 捕获变量列表}（调用处要补传捕获变量）
        self.lambda_captures: dict[str, dict] = {}
        # 捕获型 lambda：原变量名 → 定义所在函数名（调用点限定在定义函数内）
        self.lambda_def_fn: dict[str, str] = {}
        # 嵌套函数：原函数名 → {提升函数名, 捕获变量列表}（当前函数内可见；
        # 调用处补传捕获变量，与 lambda 提升同一套机制）
        self.nested_map: dict[str, dict] = {}
        # v0.43：哪些函数返回函数引用（`return inner`，inner 在 nested_map 里）。
        # `f = outer()` 时 outer 返回函数引用 → f 也是函数引用变量。
        # 这是跨函数信息，所以放在顶层，不随函数翻译重置。
        # dict: key=函数名, value=返回的函数引用是不是异步的
        self.fn_returning_fns: dict[str, bool] = {}
        # v0.45：哪些变量是 open() 返回的文件句柄（with open(...) as f 形式）。
        # dict: key=变量名, value=(path_code, mode)
        self.file_handles: dict[str, tuple[str, str]] = {}
        # v0.47：待发射的类装饰器（_collect_class 里存，_emit_class 结尾发射）
        self._pending_class_decorators: dict[str, list] = {}
        # 嵌套函数提升名的文件级唯一登记（_fresh_name 跨函数不重名）
        self._nested_lifted_names: set[str] = set()
        # 当前正在翻译的函数名（lambda 捕获的调用点限定在定义函数内）
        self.cur_fn: str | None = None
        # v0.39：当前正在翻译的函数是不是异步函数。同步函数里嵌套定义
        # async 内层函数时，它必然只能被当协程工厂传出（同步体内不能
        # await）—— AGL 没有函数值，这种形态精确拒绝。
        self.cur_fn_is_async = False
        # v0.39：全函数效应闭包（被调用户函数的效应沿调用链传播）
        self._fn_effects_closure: dict[str, set[str]] = {}
        # v0.40：构造类型预扫描——类名 → {属性: 构造它的类}（__init__ 里
        # `self.x = Ctor()`）；函数名 → 直接 return 的类（`return Ctor()`）。
        # 用于把 __str__/__repr__ 分发从「只认裸变量」扩到属性取出与函数返回。
        self._attr_types: dict[str, dict[str, str]] = {}
        self._fn_returns: dict[str, str] = {}
        # 当前方法所属类的直接父类（super() 解析用）
        self.super_base: str | None = None
        # 当前函数内已知"是某个类的实例"的变量：变量名 → 类名
        self.instances: dict[str, str] = {}
        # classmethod 里第一个参数名（通常 cls）：cls.xxx → 类属性 <类>_xxx
        self._cls_alias: str | None = None
        # 预扫描类方法体时记录当前类名（用于 cls.x → 类.x）
        self._cls_scan_class: str | None = None
        # 函数名 → {vararg, fixed, kwarg}（可变参数函数的调用处改写用）
        self.callee_info: dict[str, dict] = {}

    # ── 关键字与名字 ─────────────────────────────────────────────────
    def kw_of(self, canon: str) -> str:
        return self.kw.text_for(canon, self.lang)

    def op_of(self, mark: str) -> str:
        return self.kw.op_text_for(mark, self.lang)

    def builtin_of(self, canon: str) -> str:
        return self.kw.builtin_text_for(canon, self.lang)

    def attr_of(self, canon: str) -> str:
        return self.kw.attr_text_for(canon, self.lang)

    def check_name(self, name: str, where: str, allow_entry: bool = False) -> str:
        """交给调用方的 AGL 名字。

        默认原样保留 —— 名字是作者的记号，机器不许翻译。
        只有入口函数会被改名（AGL 的入口名字是固定的），改名处会留一条说明。

        AGL 关键字招惹不得：`变量 如果 = 1` 编译不过，
        这种是真的翻不过去，只能挑明原因让人改名。
        唯一的例外是函数名：AGL 就要求入口非得叫 `主入口`，
        那不是误用关键字，是照规矩办事。
        """
        actual = self.rename_map.get(name, name)
        if allow_entry and actual == self.kw_of("main"):
            return actual
        if self.kw.is_keyword(actual, self.lang):
            raise Blocked(
                f"{where} 用了名字「{actual}」，它在 AGL 里是关键字，"
                f"当名字会让翻译结果编译不过；请先在 Python 里改名")
        return actual

    # ── 行输出 ─────────────────────────────────────────────────────
    def put(self, text: str) -> None:
        self.lines.append(_IND * self.depth + text if text else "")

    # ── 类型映射 ───────────────────────────────────────────────────
    def type_of(self, node: ast.expr | None, where: str) -> str:
        """把 Python 类型注解翻成 AGL 类型。翻不出来就 blocker。

        不猜：AGL 函数的签名是写给人看的契约，猜出来的类型等于伪造契约。
        """
        if node is None:
            return self.kw_of("Null")
        # `Optional[X]` / `Union[...]` 这类带下标的写法
        if isinstance(node, ast.Subscript):
            base = self._dotted(node.value)
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base in ("Optional", "Union"):
                if base == "Union" and len(args) == 2 and self._dotted(args[1]) == "None":
                    return self.type_of(args[0], where)
                raise Blocked(
                    f"{where} 的联合类型还不能导（AGL 目前没有「可能是 A 也可能是 B」的类型）")
            if base in ("List", "list"):
                return f"{self.kw_of('List')}<{self.type_of(args[0], where)}>"
            if base in ("Dict", "dict"):
                keys = list(args)
                if len(keys) != 2:
                    raise Blocked(f"{where} 的字典类型写法不认识")
                return (f"{self.kw_of('Map')}<"
                        f"{self.type_of(keys[0], where)}, "
                        f"{self.type_of(keys[1], where)}>")
            raise Blocked(f"{where} 的类型写法 `{base}[...]` 还不能导")

        name = self._dotted(node)
        canon = _SCALAR.get(name)
        if canon is None:
            raise Blocked(f"{where} 的类型 `{name}` 还没有对应的 AGL 类型")
        if canon in ("List", "Map"):
            # 裸的 list / dict：元素类型未知，不替用户补一个假的
            raise Blocked(
                f"{where} 的类型 `{name}` 缺元素类型，"
                f"请写成 List[int] / Dict[str, int] 这样写全的注解")
        return self.kw_of(canon)

    @staticmethod
    def _dotted(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{PyImporter._dotted(node.value)}.{node.attr}"
        if isinstance(node, ast.Constant) and node.value is None:
            return "None"
        return type(node).__name__

    # ── 表达式 ─────────────────────────────────────────────────────
    def _note_json_net(self, what: str) -> None:
        """`import json / requests / urllib.request` 放行的说明。"""
        if what == "json":
            self.warnings.append(
                "`import json` 被忽略：`json.loads(x)` / `json.dumps(x)` "
                "被翻译成 AGL 内建 `解析JSON` / `生成JSON`；"
                "json 模块的 load/dump（文件版）请手写 读文件/写文件")
        else:
            self.warnings.append(
                f"`import {what}` 被忽略：`requests.get(url)` / "
                "`urllib.request.urlopen(url)` 被翻译成 AGL 内建 `获取网页`；"
                "它带「网络」效应，函数签名已自动补 `效应 网络`，"
                "若原代码还有其他网络用法请手写重审")

    def _could_be_set(self, node) -> bool:
        """编译期能否确认一个表达式是集合。

        集合运算符（`|`/`&`/`-`）在 Python 里与整数位运算 / 减法共享符号，
        只有两侧都能静态确认是集合时才敢直译成 并集/交集/差集；
        其余情况宁肯拒绝也不瞎翻（对拍错值比拒绝更危险）。
        """
        if isinstance(node, ast.Set):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "set"):
            return True
        if isinstance(node, ast.Name):
            return node.id in getattr(self, "set_vars", ())
        return False

    def _is_pure_read(self, e: ast.expr) -> bool:
        """表达式是否是纯读取（求值无副作用、双求值结果一致）。

        链式 `is` 展开成两段比较会把中间值求值两次；名字 / 常量 /
        属性读取 / 下标读取都是纯读取，双求值结果一致。
        """
        return isinstance(e, (ast.Name, ast.Constant,
                              ast.Attribute, ast.Subscript))

    def _fmt_print_arg(self, a: ast.expr) -> str:
        """打印参数格式化：用户类实例（类型可确认且该类/父类定义了 __str__）
        → 包 __str__ 调用。

        Python `print(c)` 对用户类走 `str(c)`；AGL 打印对实例打的是映射表
        （显示协议不同），定义过 __str__ 的类在这里对齐成调用它。
        v0.40：从「只认裸变量」扩到属性取出（o.inner）、函数返回（make()）、
        直接构造（C()），并沿 MRO 继承父类 __str__。
        v0.40 续：容器字面量（列表/元组/映射/集合）里的实例元素走 __repr__，
        对齐 Python `print([c])` 输出 `[C(repr!)]` 而非 `[{}]`。
        """
        wrapped = self._fmt_instance_dunder(a, "__str__")
        if wrapped is not None:
            return wrapped
        # v0.41：没有 __str__ 时回退 __repr__（与 Python print(obj) 语义一致）
        wrapped = self._fmt_instance_dunder(a, "__repr__")
        if wrapped is not None:
            return wrapped
        container = self._fmt_container_repr(a)
        if container is not None:
            return container
        return self.expr(a)

    def _elem_repr_code(self, el: ast.expr) -> str:
        """容器元素的 repr 代码：是实例就走 __repr__，否则走默认 表达式形式。"""
        rep = self._fmt_instance_dunder(el, "__repr__")
        if rep is not None:
            return rep
        return f"{self.builtin_of('repr')}({self.expr(el)})"

    def _fmt_container_repr(self, e: ast.expr) -> str | None:
        """容器字面量 repr：print([c]) 时元素走 __repr__ 而非默认列表显示。

        只处理直接字面量（`print([...])` / `print((...))` / `print({...})`）；
        变量容器（`xs = [c]; print(xs)`）静态推不出元素类型，回退默认显示。
        容器里至少有一个元素能推实例类型才接管，否则返回 None 不干扰现有对拍。
        """
        if isinstance(e, ast.List):
            if not any(self._expr_instance_cls(x) is not None for x in e.elts):
                return None
            parts = [self._elem_repr_code(x) for x in e.elts]
            return ('"[" + ' + ' + ", " + '.join(parts) + ' + "]"')
        if isinstance(e, ast.Tuple):
            if not any(self._expr_instance_cls(x) is not None for x in e.elts):
                return None
            parts = [self._elem_repr_code(x) for x in e.elts]
            if len(e.elts) == 1:
                # Python 单元素元组带尾逗号 `(c,)`
                return ('"(" + ' + parts[0] + ' + ",)"')
            return ('"(" + ' + ' + ", " + '.join(parts) + ' + ")"')
        if isinstance(e, ast.Set):
            if not any(self._expr_instance_cls(x) is not None for x in e.elts):
                return None
            parts = [self._elem_repr_code(x) for x in e.elts]
            return ('"{" + ' + ' + ", " + '.join(parts) + ' + "}"')
        if isinstance(e, ast.Dict):
            if not any(self._expr_instance_cls(v) is not None
                       for k, v in zip(e.keys, e.values) if k is not None):
                return None
            pairs = []
            for k, v in zip(e.keys, e.values):
                if k is None:
                    continue  # **展开不处理
                pairs.append(f"{self._elem_repr_code(k)} + \": \" + {self._elem_repr_code(v)}")
            return ('"{" + ' + ' + ", " + '.join(pairs) + ' + "}"')
        return None

    def _globals_tail(self) -> str:
        """可变全局（v0.38）激活时，函数调用末尾补传共享存储实参。

        所有非入口函数签名末尾都收了 `_agl_globals`，入口函数初始化了它，
        所以任何调用点的作用域里都有这个变量；这里统一补上。
        """
        return f", {self.globals_store}" if self.globals_active else ""

    def _append_globals(self, *codes: str) -> str:
        """把若干实参代码与（激活时的）共享存储实参拼成调用参数列表。

        解决空实参调用（`bump()`）补传时产生前导逗号的问题：
        `bump(, _agl_globals)` 是语法错，`bump(_agl_globals)` 才对。
        """
        parts = [c for c in codes if c]
        if self.globals_active:
            parts.append(self.globals_store)
        return ", ".join(parts)

    def _could_be_dict(self, node) -> bool:
        """编译期能否确认一个表达式是映射表。

        `|` 对映射是合并（Python 3.9+），对集合是并集、对整数是位或——
        只有两侧都能静态确认是映射（字面量 / dict() 构造 / 登记过的变量）
        才敢直译成 合并映射；其余情况宁肯拒绝也不瞎翻。
        """
        if isinstance(node, ast.Dict):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("dict", "make_map")):
            return True
        if isinstance(node, ast.Name):
            return node.id in getattr(self, "dict_vars", ())
        return False

    def expr(self, e: ast.expr) -> str:
        # 每个复合表达式都套一层圆括号：AGL 中文分支用「大于」「并且」这类词，
        # 跟 Python 符号的优先级未必一样，靠括号保证结果不会被偷偷重排。
        if isinstance(e, ast.Constant):
            return self.literal(e)
        if isinstance(e, ast.Name):
            # v0.43：嵌套函数当值传 → 函数引用列表 ["闭包0", 捕获1, 捕获2, ...]
            # 调用时用 `调用(fn_ref, args...)` 内建动态调用
            if e.id in self.nested_map:
                info = self.nested_map[e.id]
                fname = info["fn"]
                captures = info["captures"]
                parts = [f'"{fname}"'] + captures
                self.warnings.append(
                    f"嵌套函数 `{e.id}` 被当成值传出去：AGL 没有一等函数，"
                    f"翻译成函数引用列表 `[\"{fname}\", {', '.join(captures)}]`，"
                    "调用时用 `调用(fn_ref, args...)`")
                return f"[{', '.join(parts)}]"
            # v0.47：模块级函数当值传 → 函数引用列表 ["函数名"]（没有捕获变量）
            if e.id in self.funcs:
                self.warnings.append(
                    f"模块级函数 `{e.id}` 被当成值传出去：翻译成函数引用列表 `[\"{e.id}\"]`，"
                    "调用时用 `调用(fn_ref, args...)`")
                return f'["{e.id}"]'
            # 模块级只读常量在 AGL 里是同名常量函数：读它变成调它
            if e.id in self.global_consts:
                # 可变全局（v0.38）：读共享存储（读/写同源）
                if e.id in self.mutable_globals:
                    return f"{self.globals_store}[\"{e.id}\"]"
                # 常量函数体引用共享存储的才补传（其余无参调用）
                if e.id in self.const_needs_globals:
                    return f"{self.check_name(e.id, '这里')}({self.globals_store})"
                return f"{self.check_name(e.id, '这里')}()"
            return self.check_name(e.id, "这里")
        if isinstance(e, ast.BinOp):
            opname = type(e.op).__name__
            # v0.48：复数 `3+4j` —— 右边是纯虚数常量时，整个翻译成 [实部, 虚部]
            if (opname == "Add"
                    and isinstance(e.right, ast.Constant)
                    and isinstance(e.right.value, complex)
                    and e.right.value.real == 0):
                real_part = self.expr(e.left)
                imag_part = e.right.value.imag
                self.warnings.append(
                    f"复数 `{real_part}+{imag_part}j` 被翻译成列表 [{real_part}, {imag_part}]："
                    "AGL 没有复数类型，用两个数的列表表示")
                return f"[{real_part}, {imag_part}]"
            # 集合运算符 `|` / `&`：对集合是并集/交集、对整数是位或/位与——
            # 只有两侧都能静态确认是集合才直译成内建，否则拒绝给指引。
            if opname in ("BitOr", "BitAnd"):
                if self._could_be_set(e.left) and self._could_be_set(e.right):
                    builtin = {"BitOr": "union", "BitAnd": "intersection"}[opname]
                    return f"{self.builtin_of(builtin)}({self.expr(e.left)}, {self.expr(e.right)})"
                # v0.32：`d1 | d2` 映射合并（Python 3.9+）→ 合并映射(d1, d2)。
                if opname == "BitOr" and self._could_be_dict(e.left) \
                        and self._could_be_dict(e.right):
                    return (f"{self.builtin_of('merge_map')}"
                            f"({self.expr(e.left)}, {self.expr(e.right)})")
                # v0.48：都不是集合/映射 → 当成整数位运算，直接透传运算符
                op_sym = {"BitOr": "|", "BitAnd": "&"}[opname]
                return f"({self.expr(e.left)} {op_sym} {self.expr(e.right)})"
            # v0.48：BitXor / LShift / RShift —— 整数位运算，直接透传运算符
            if opname in ("BitXor", "LShift", "RShift"):
                # 两侧都能确认是集合时，`a ^ b` 是集合对称差；否则按整数位运算透传。
                if opname == "BitXor" and self._could_be_set(e.left) \
                        and self._could_be_set(e.right):
                    return (f"{self.builtin_of('symmetric_difference')}"
                            f"({self.expr(e.left)}, {self.expr(e.right)})")
                op_sym = {"BitXor": "^", "LShift": "<<", "RShift": ">>"}[opname]
                return f"({self.expr(e.left)} {op_sym} {self.expr(e.right)})"
            # `a - b`：两侧都是集合时是差集（对数字是减法，不能瞎翻）
            if opname == "Sub" and self._could_be_set(e.left) and self._could_be_set(e.right):
                return f"{self.builtin_of('difference')}({self.expr(e.left)}, {self.expr(e.right)})"
            # `%` 格式化：`"%s-%d" % ("x", 5)` → 填充("{}-{}", "x", 5)。
            # 只在左侧是字符串常量时启用（数字取模 `a % b` 不受影响）。
            if opname == "Mod" and isinstance(e.left, ast.Constant) and isinstance(e.left.value, str):
                return self._pct_format(e)
            # 字符串乘法 `"ab" * 3` / `3 * "ab"`：AGL 的 `*` 只对数字定义，
            # 文本重复走 重复 内建（Python `s * n`，n<=0 给空文本，完全一致）。
            # 只在「一侧是文本字面量、另一侧是整数」时静态确认直译，否则保持
            # 原样乘法（数字乘法语义不受影响）。
            if opname == "Mult":
                lc, rc = e.left, e.right
                if isinstance(lc, ast.Constant) and isinstance(lc.value, str) \
                        and isinstance(rc, ast.Constant) and isinstance(rc.value, int):
                    self.warnings.append(
                        "文本乘法被翻译成 `重复(...)`：AGL 的 `*` 只对数字定义，"
                        "文本重复用 重复 内建表达，与 Python `s * n` 一致")
                    return (f"{self.builtin_of('repeat')}"
                            f"({self.literal(lc)}, {self.literal(rc)})")
                if isinstance(rc, ast.Constant) and isinstance(rc.value, str) \
                        and isinstance(lc, ast.Constant) and isinstance(lc.value, int):
                    self.warnings.append(
                        "文本乘法被翻译成 `重复(...)`：AGL 的 `*` 只对数字定义，"
                        "文本重复用 重复 内建表达，与 Python `s * n` 一致")
                    return (f"{self.builtin_of('repeat')}"
                            f"({self.literal(rc)}, {self.literal(lc)})")
                # 列表乘法 `[0] * 3` / `3 * [0]`：AGL 的 `*` 只对数字定义，
                # 列表重复走 重复 内建（Python `xs * n` 语义完全一致）。
                # 只在「一侧是列表字面量、另一侧是整数」时静态确认直译。
                if isinstance(lc, ast.List) and isinstance(rc, ast.Constant) \
                        and isinstance(rc.value, int):
                    self.warnings.append(
                        "列表乘法被翻译成 `重复(...)`：AGL 的 `*` 只对数字定义，"
                        "列表重复用 重复 内建表达，与 Python `xs * n` 一致")
                    return (f"{self.builtin_of('repeat')}"
                            f"({self.expr(lc)}, {self.literal(rc)})")
                if isinstance(rc, ast.List) and isinstance(lc, ast.Constant) \
                        and isinstance(lc.value, int):
                    self.warnings.append(
                        "列表乘法被翻译成 `重复(...)`：AGL 的 `*` 只对数字定义，"
                        "列表重复用 重复 内建表达，与 Python `xs * n` 一致")
                    return (f"{self.builtin_of('repeat')}"
                            f"({self.expr(rc)}, {self.literal(lc)})")
            # 地板除 `a // b`：AGL 没有 `//` 运算符，走 整除 内建
            # （Python `//` 向负无穷取整，backend 直接 `a // b`，精确等价）。
            if opname == "FloorDiv":
                return (f"{self.builtin_of('floordiv')}"
                        f"({self.expr(e.left)}, {self.expr(e.right)})")
            # v0.41：实例运算 a + b → C_add(a, b)
            left_cls = self._expr_instance_cls(e.left)
            if left_cls is not None:
                op_dunder = {
                    "Add": "__add__", "Sub": "__sub__", "Mult": "__mul__",
                }
                dunder = op_dunder.get(opname)
                if dunder:
                    owner = self._resolve_dunder_owner(left_cls, dunder)
                    if owner is not None:
                        left_code = self.expr(e.left)
                        right_code = self.expr(e.right)
                        fn = self.check_name(f"{owner}_{dunder}", "运算方法")
                        return f"{fn}({left_code}, {right_code})"
            mark = _BINOP.get(type(e.op))
            if mark is None:
                raise Blocked(f"暂时不能导的运算符 `{opname}`")
            return f"({self.expr(e.left)} {mark} {self.expr(e.right)})"
        if isinstance(e, ast.Compare):
            # 先处理链式比较：a < b < c → (a < b) && (b < c)
            if len(e.ops) > 1:
                parts: list[str] = []
                prev = self.expr(e.left)
                for op_node, comp in zip(e.ops, e.comparators):
                    mark = _CMP.get(type(op_node))
                    if mark is None:
                        opname = type(op_node).__name__
                        if opname in ("In", "NotIn"):
                            raise Blocked(
                                "链式比较里夹了 `in` 判断，暂时不能导")
                        if opname in ("Is", "IsNot"):
                            # 链式 is（v0.38）：a is b is c → (等同(a, b) && 等同(b, c))。
                            # 中间值在展开里求值两次；只有纯读取（名字/常量/属性/下标）
                            # 双求值结果一致，调用/字面量新建对象会失真，精确拒绝。
                            if not self._is_pure_read(comp):
                                raise Blocked(
                                    "链式 `is` 的中间值必须是纯读取"
                                    "（变量 / 常量 / 属性 / 下标）："
                                    "展开成两段比较会求值两次，"
                                    "带调用 / 新建对象的写法会失真，请拆开写")
                            fn = self.builtin_of("is_same" if opname == "Is"
                                                 else "is_not_same")
                            right = self.expr(comp)
                            parts.append(f"{fn}({prev}, {right})")
                            prev = right
                            continue
                        raise Blocked(f"链式比较里有不认识的运算符 `{opname}`")
                    right = self.expr(comp)
                    parts.append(f"({prev} {self.op_of(mark)} {right})")
                    prev = right
                word = self.op_of("&&")
                cur = parts[0]
                for p in parts[1:]:
                    cur = f"({cur} {word} {p})"
                return cur
            # 单目比较
            opname = type(e.ops[0]).__name__
            if opname in ("In", "NotIn"):
                # `x in s` → 包含(s, x)。右值可以是文本（子串）、列表（成员）、
                # 映射表（按键判断），后端 `_agl_contains` 三种都支持，与 Python 一致。
                left_code = self.expr(e.left)
                right_code = self.expr(e.comparators[0])
                call = f"{self.builtin_of('contains')}({right_code}, {left_code})"
                if opname == "NotIn":
                    result = f"({self.op_of('!')} {call})"
                else:
                    result = call
                return result
            # `a <= b` / `a >= b`：两侧都能确认是集合时是子集/超集判断；
            # 数字比较保持原样（集合与数字共用比较符号，不能瞎翻）。
            if opname in ("LtE", "GtE") \
                    and self._could_be_set(e.left) and self._could_be_set(e.comparators[0]):
                builtin = "is_subset" if opname == "LtE" else "is_superset"
                return (f"{self.builtin_of(builtin)}"
                        f"({self.expr(e.left)}, {self.expr(e.comparators[0])})")
            mark = _CMP.get(type(e.ops[0]))
            if mark is None:
                if opname in ("Is", "IsNot"):                    # `x is None` / `x is not None` → 等于(空) / 不等于(空)（判空）。
                    # `x is y`（非判空同一性）→ 等同(x, y) / 不等同(x, y)：
                    # 产物是 Python，`is` 引用语义逐字一致（同一变量绑定 ↔ 同一对象）。
                    right = e.comparators[0]
                    if isinstance(right, ast.Constant) and right.value is None:
                        word = self.op_of("==" if opname == "Is" else "!=")
                        return (f"({self.expr(e.left)} {word} "
                                f"{self.literal(ast.Constant(value=None))})")
                    if len(e.comparators) > 1:
                        raise Blocked(
                            "链式 `a is b is c` 暂时不能导：中间值求值一次，"
                            "展开成 `等同(a, b) 并且 等同(b, c)` 会求值两次，"
                            "有副作用时不等价；请拆开写")
                    self.warnings.append(
                        "`a is b` 被翻译成 `等同(a, b)`：产物原样保留 Python 的 "
                        "`is` 语义（比的是不是同一个对象；数字/文本的缓存行为"
                        "由 Python 运行时决定，与原文逐字一致）")
                    fn = self.builtin_of("is_same" if opname == "Is"
                                         else "is_not_same")
                    return (f"{fn}({self.expr(e.left)}, "
                            f"{self.expr(e.comparators[0])})")
                raise Blocked(f"暂时不能导的比较运算符 `{opname}`")
            # v0.41：实例比较走 __eq__/__lt__ 等运算符重载
            left_cls = self._expr_instance_cls(e.left)
            if left_cls is not None:
                dunder_map = {
                    "Eq": "__eq__", "NotEq": "__ne__",
                    "Lt": "__lt__", "LtE": "__le__",
                    "Gt": "__gt__", "GtE": "__ge__",
                }
                dunder = dunder_map.get(opname)
                if dunder:
                    owner = self._resolve_dunder_owner(left_cls, dunder)
                    if owner is not None:
                        left_code = self.expr(e.left)
                        right_code = self.expr(e.comparators[0])
                        fn = self.check_name(f"{owner}_{dunder}", "比较方法")
                        return f"{fn}({left_code}, {right_code})"
            word = self.op_of(mark)
            return f"({self.expr(e.left)} {word} {self.expr(e.comparators[0])})"
        if isinstance(e, ast.BoolOp):
            mark = "&&" if isinstance(e.op, ast.And) else "||"
            word = self.op_of(mark)
            # 从左到右两两合并，顺序与 Python 的一致
            cur = self.expr(e.values[0])
            for v in e.values[1:]:
                cur = f"({cur} {word} {self.expr(v)})"
            return cur
        if isinstance(e, ast.UnaryOp):
            if isinstance(e.op, ast.Not):
                return f"({self.op_of('!')} {self.expr(e.operand)})"
            if isinstance(e.op, ast.USub):
                return f"(0 - {self.expr(e.operand)})"
            raise Blocked(f"暂时不能导的单目运算符 `{type(e.op).__name__}`")
        if isinstance(e, ast.Call):
            return self.call(e)
        if isinstance(e, ast.List):
            return "[" + ", ".join(self.expr(x) for x in e.elts) + "]"
        if isinstance(e, ast.Set):
            # 集合字面量 `{1, 2, 3}` → AGL 集合字面量（同一套花括号语法；
            # `{1: 2}` 是映射表、`{1, 2}` 是集合，按元素后跟冒号还是逗号区分）
            return "{" + ", ".join(self.expr(x) for x in e.elts) + "}"
        if isinstance(e, ast.Dict):
            if any(k is None for k in e.keys):
                # `{**d}` / `{**a, "k": v}` / `{"k": v, **d}` → 合并映射(...)
                # Python 的 `**` 展开是「浅拷贝 + 后者覆盖」；AGL 的 合并映射
                # 同样「后者覆盖」。AGL 没有原地修改，拷贝这一步没有意义，
                # 直接合并语义等价。
                self.warnings.append(
                    "字典字面量里的 `**` 展开被翻译成 `合并映射(...)`："
                    "Python 是「浅拷贝 + 后者覆盖」，AGL 是「合并，后者覆盖」，"
                    "AGL 没有原地修改，拷贝这一步没有意义，语义等价")
                segs: list[str] = []
                for k, v in zip(e.keys, e.values):
                    if k is None:
                        segs.append(self.expr(v))
                    else:
                        segs.append(f"{{{self.expr(k)}: {self.expr(v)}}}")
                if len(segs) == 1:
                    # 只有 `{**d}` 一个展开：合并映射 要求至少两个实参，
                    # 补一个空映射表（值语义下结果就是 d 本身）
                    segs.append("{}")
                return f"{self.builtin_of('merge_map')}({', '.join(segs)})"
            parts = [f"{self.expr(k)}: {self.expr(v)}"
                     for k, v in zip(e.keys, e.values)]
            return "{" + ", ".join(parts) + "}"
        if isinstance(e, ast.Subscript):
            # v0.41：自定义对象下标 a[i] → C_getitem(a, i)（非切片形态）
            if not isinstance(e.slice, ast.Slice):
                recv_cls = self._expr_instance_cls(e.value)
                if recv_cls is not None:
                    owner = self._resolve_dunder_owner(recv_cls, "__getitem__")
                    if owner is not None:
                        recv_code = self.expr(e.value)
                        idx_code = self.expr(e.slice)
                        fn = self.check_name(f"{owner}___getitem__", "下标方法")
                        return f"{fn}({recv_code}, {idx_code})"
            # 切片：xs[1:3] / xs[:3] / xs[1:] / xs[::-1] → 切片(xs, 起, 止[, 步])
            #   step 缺省或 1 → 3 参；start 缺省 → 0；stop 缺省 → x.长度；
            #   xs[::-1]（None:None:-1）→ 反转(x)（语义一致且是常用反转写法）；
            #   其他负步长形态精确拒绝（Python 的负步长切片语义复杂，不能瞎翻）。
            if isinstance(e.slice, ast.Slice):
                src = self.expr(e.value)
                lo = e.slice.lower
                hi = e.slice.upper
                st = e.slice.step
                step_const = None
                if st is not None:
                    # ast 把 `-1` 解析成 UnaryOp(USub, 1)，这里统一归一成整数
                    sv = st
                    if isinstance(sv, ast.UnaryOp) and isinstance(sv.op, ast.USub) \
                            and isinstance(sv.operand, ast.Constant) \
                            and isinstance(sv.operand.value, int):
                        step_const = -sv.operand.value
                    elif isinstance(sv, ast.Constant) and isinstance(sv.value, int):
                        step_const = sv.value
                # 步长缺省或 +1：start 缺省补 0、stop 缺省补 .长度（Python 切片语义）
                # （动态步长 st 非 None 且非常量 → 不进来，走下方动态分支）
                if st is None or step_const == 1:
                    if lo is None:
                        lo_code = "0"
                    else:
                        lo_code = self.expr(lo)
                    if hi is None:
                        hi_code = f"{src}.{self.attr_of('length')}"
                    else:
                        hi_code = self.expr(hi)
                    if step_const == 1 and lo is None and hi is None and st is not None:
                        # x[::1] 等于原值
                        return src
                    if st is None:
                        return (f"{self.builtin_of('slice')}"
                                f"({src}, {lo_code}, {hi_code})")
                    return (f"{self.builtin_of('slice')}"
                            f"({src}, {lo_code}, {hi_code}, 1)")
                # 负步长（step 是常量）：x[::-1] 全缺省 → 反转（与既有 v0.28 一致）；
                # 其他负步长形态：start/stop 缺省端按 Python 语义补全后原生透传——
                #   · start 缺省 = 从末尾起（长度-1）
                #   · stop 缺省 = 到开头前（-长度-1，负索引越下界即裁剪到开头）
                # 原生 value[start:stop:step] 与 Python 逐字等价（backend 是原生切片）。
                if step_const is not None and step_const < 0:
                    if lo is None and hi is None:
                        self.warnings.append(
                            "切片 `x[::-1]` 被翻译成 `反转(x)`："
                            "Python 的负步长切片就是反向取值，语义一致")
                        return f"{self.builtin_of('reverse')}({src})"
                    if lo is None:
                        lo_code = f"{src}.{self.attr_of('length')}-1"
                    else:
                        lo_code = self.expr(lo)
                    if hi is None:
                        hi_code = f"-{src}.{self.attr_of('length')}-1"
                    else:
                        hi_code = self.expr(hi)
                    return (f"{self.builtin_of('slice')}"
                            f"({src}, {lo_code}, {hi_code}, {step_const})")
                # 正步长 >1：start/stop 缺省补 0/.长度（Python 正步长缺省语义恒定）
                if lo is None:
                    lo_code = "0"
                else:
                    lo_code = self.expr(lo)
                if hi is None:
                    hi_code = f"{src}.{self.attr_of('length')}"
                else:
                    hi_code = self.expr(hi)
                if step_const is not None:
                    return (f"{self.builtin_of('slice')}"
                            f"({src}, {lo_code}, {hi_code}, {step_const})")
                # 动态步长：缺省端用 空（None）标记，backend 原生切片自动处理
                #   value[None:n:s] / value[None:None:s] 与 Python x[:n:s] / x[::s]
                #   逐字等价（None 端在原生切片里就是缺省端）
                lo_code = (self.expr(lo)
                           if lo is not None else self.expr(ast.Constant(value=None)))
                hi_code = (self.expr(hi)
                           if hi is not None else self.expr(ast.Constant(value=None)))
                return (f"{self.builtin_of('slice')}"
                        f"({src}, {lo_code}, {hi_code}, {self.expr(st)})")
            return f"{self.expr(e.value)}[{self.expr(e.slice)}]"
        if isinstance(e, ast.Attribute):
            # v0.48：异常对象的属性访问——e.args / e.strerror / e.message
            # 都翻译成 失败原因(e)：拿错误消息字符串，简单实用
            if (isinstance(e.value, ast.Name)
                    and e.value.id in self._except_stack
                    and e.attr in ("args", "strerror", "message", "filename")):
                return f"{self.builtin_of('fail_reason')}({self.expr(e.value)})"
            # 类属性访问：A.count → A_count()（只读类属性 = 同名常量函数）
            # v0.41：classmethod 里 cls.count 等同 A.count（cls 是类对象别名）
            cls_name_for_attr = None
            if isinstance(e.value, ast.Name) and e.value.id in self.classes:
                cls_name_for_attr = e.value.id
            elif (self._cls_alias is not None
                  and isinstance(e.value, ast.Name)
                  and e.value.id == self._cls_alias
                  and self.cur_class is not None):
                cls_name_for_attr = self.cur_class
            if cls_name_for_attr is not None:
                cinfo = self.classes[cls_name_for_attr]
                if e.attr in cinfo.get("attrs", {}):
                    # 可变类属性（v0.38）：读共享存储（读/写同源）
                    if (cls_name_for_attr, e.attr) in self.mutable_class_attrs:
                        return f"{self.globals_store}[\"{cls_name_for_attr}.{e.attr}\"]"
                    return f"{self.check_name(f'{cls_name_for_attr}_{e.attr}', '类属性')}()"
            # 只读 property 访问：a.x → A_x(a)（@property getter 翻译成同名方法）
            # v0.40：receiver 不是裸变量时（o.mid.x）也递归推类后分发
            recv_cls = self._expr_instance_cls(e.value)
            if recv_cls is not None:
                owner, _ = self._resolve_method(recv_cls, e.attr)
                if owner and e.attr in self.classes[owner].get("properties", ()):
                    recv_code = self.expr(e.value)
                    fn_code = self.check_name(f"{owner}_{e.attr}", "property 方法")
                    return f"{fn_code}({recv_code})"
            # 类实例的成员访问 → 映射表下标：c.n → c["n"]
            # （可变类属性（v0.38）：实例读类属性也走共享存储——Python 语义里
            #   实例没有该字段时沿类找类属性，读写同源）
            # v0.40：receiver 递归推类（o.mid.deep → o["mid"]["deep"]）
            if recv_cls is not None:
                if (recv_cls, e.attr) in self.mutable_class_attrs:
                    return f"{self.globals_store}[\"{recv_cls}.{e.attr}\"]"
                for base_cls in self._mro(recv_cls)[1:]:
                    if (base_cls, e.attr) in self.mutable_class_attrs:
                        return f"{self.globals_store}[\"{base_cls}.{e.attr}\"]"
                recv_code = self.expr(e.value)
                return f'{recv_code}["{e.attr}"]'
            # requests.get(url).text（属性形态，无括号）→ 取到(获取网页(url))
            # （获取网页已直接返回正文文本，.text 这一层被吸收掉）
            if (e.attr == "text"
                    and isinstance(e.value, ast.Call)
                    and isinstance(e.value.func, ast.Attribute)
                    and e.value.func.attr == "get"
                    and isinstance(e.value.func.value, ast.Name)
                    and e.value.func.value.id == "requests"):
                if len(e.value.args) != 1:
                    raise Blocked(
                        "`requests.get(url).text` 只接受一个 URL 参数")
                self.warnings.append(
                    "`requests.get(url).text` 被翻译成 `取到(获取网页(url))`："
                    "AGL 的 获取网页 直接返回正文文本，.text 被吸收；"
                    "若原代码用了 .status_code / .headers 等成员请手写重审")
                return (f"{self.builtin_of('unwrap')}({self.builtin_of('fetch')}"
                        f"({self.expr(e.value.args[0])}))")
            return f"{self.expr(e.value)}.{e.attr}"
        if isinstance(e, ast.Await):
            return f"{self.kw_of('await')} {self.expr(e.value)}"
        if isinstance(e, ast.Tuple):
            # 元组翻译成列表：AGL 没有元组类型，列表近似等价
            self.warnings.append(
                "元组 `(a, b)` 被翻译成列表 `[a, b]`："
                "AGL 没有元组类型，列表的语义最接近")
            return "[" + ", ".join(self.expr(elt) for elt in e.elts) + "]"
        if isinstance(e, ast.JoinedStr):
            parts: list[str] = []
            for v in e.values:
                if isinstance(v, ast.Constant):
                    parts.append(self.literal(v))
                elif isinstance(v, ast.FormattedValue):
                    # !s → 转文本（默认）；!r → 表达式形式（repr）；!a → 转文本+表达式形式近似
                    # （Python ascii() 对非 ASCII 做 \u 转义，AGL 用 repr 语义近似，
                    # 挂 warning 说明差异极小）
                    if v.conversion not in (-1, 115, 114, 97):
                        ch = chr(v.conversion) if 32 <= v.conversion < 127 else "?"
                        raise Blocked(
                            f"f-string 的 `!{ch}` 转换暂时不能导："
                            "它跟 转文本()/表达式形式() 的行为不一样")
                    arg_code = self.expr(v.value)
                    if v.conversion == 114:
                        # v0.40：定义了 __repr__ 的用户类实例走 __repr__，
                        # 否则回退默认 表达式形式
                        rep = self._fmt_instance_dunder(v.value, "__repr__")
                        conv_code = rep if rep is not None else \
                            f"{self.builtin_of('repr')}({arg_code})"
                    elif v.conversion == 97:
                        conv_code = f"{self.builtin_of('repr')}({arg_code})"
                        self.warnings.append(
                            "f-string 的 `!a`（ascii）被翻译成 `表达式形式`："
                            "Python 对非 ASCII 做 \\u 转义，AGL 的 repr 保留原文，"
                            "ASCII 输入两边一致")
                    else:
                        # {c} / {c:...} 无转换 → format(c) → 用户类走 str(c)：
                        # 定义过 __str__ 的实例对齐成调它（与 print/str 同款分发）。
                        # v0.40：属性取出 / 函数返回 / 继承父类 __str__ 一并覆盖。
                        wrapped = self._fmt_instance_dunder(v.value, "__str__")
                        if wrapped is not None:
                            conv_code = wrapped
                        else:
                            conv_code = f"{self.builtin_of('to_text')}({arg_code})"
                    # 格式说明符：{x:.2f} → 格式化数字(x, 2)；{x:10.2f} → 格式化数字(x, 2, 10)
                    # {x:05.2f}（前导零）→ 格式化数字(x, 2, 5, 真)
                    # {42:05d}（整数补零）→ 补零(转文本(42), 5)
                    # 进制/对齐等 v0.30 起统一走 _format_spec_conversion
                    # 嵌套字段 {x:{w}} → 动态 spec 拼成运行时文本 → 按格式 原生透传
                    if v.format_spec is not None:
                        spec = v.format_spec
                        if all(isinstance(n, ast.Constant) for n in spec.values):
                            spec_text = "".join(
                                n.value for n in spec.values)
                            if spec_text == "":
                                # {x:} 空格式 = 无格式
                                parts.append(conv_code)
                                continue
                            parts.append(self._format_spec_conversion(
                                spec_text, arg_code))
                            continue
                        # 动态 spec：常量段字面量 + 变量段转文本，拼成运行时格式说明
                        spec_segs = []
                        for n in spec.values:
                            if isinstance(n, ast.Constant):
                                spec_segs.append(
                                    self.literal(ast.Constant(value=n.value)))
                            else:
                                spec_segs.append(
                                    f"{self.builtin_of('to_text')}"
                                    f"({self.expr(n.value)})")
                        if len(spec_segs) == 1:
                            spec_code = spec_segs[0]
                        else:
                            spec_code = (f"{self.builtin_of('join')}"
                                         f"({', '.join(spec_segs)})")
                        parts.append(f"{self.builtin_of('format_value')}"
                                     f"({arg_code}, {spec_code})")
                        continue
                    parts.append(conv_code)
                else:
                    raise Blocked("f-string 里有不认识的片段")
            if len(parts) == 0:
                return '""'
            if len(parts) == 1:
                return parts[0]
            return f"{self.builtin_of('join')}({', '.join(parts)})"
        if isinstance(e, ast.IfExp):
            # v0.48：三元表达式在表达式位置——完美版，提升成如果/否则语句
            var_name = self._fresh_name("三元")
            test_code = self.expr(e.test)
            body_code = self.expr(e.body)
            orelse_code = self.expr(e.orelse)
            self.pending.append(f"{self.kw_of('if')} {test_code} {{")
            self.pending.append(f"  {var_name} = {body_code}")
            self.pending.append(f"}} {self.kw_of('else')} {{")
            self.pending.append(f"  {var_name} = {orelse_code}")
            self.pending.append(f"}}")
            self.warnings.append(
                f"三元表达式（`a if b else c`）在表达式位置——"
                f"提升成如果/否则语句，结果存在变量 `{var_name}` 里")
            return var_name
        if isinstance(e, ast.NamedExpr):
            # v0.48：海象运算符 `(n := expr)` —— 把赋值塞进 pending 队列，返回变量名
            var_name = self.check_name(e.target.id, "海象变量")
            val_code = self.expr(e.value)
            self.pending.append(f"{var_name} = {val_code}")
            self.warnings.append(
                f"海象运算符 `(n := expr)` 被翻译成：先赋值 `{var_name} = {val_code}`，再用 `{var_name}`")
            return var_name
        if isinstance(e, ast.Lambda):
            # v0.48：lambda 在表达式位置——提升成命名函数，返回函数名
            fn_name = self._fresh_name("lambda")
            self._lift_lambda(fn_name, e)
            self.warnings.append(
                f"lambda 在表达式位置被提升成命名函数 `{fn_name}`")
            return fn_name
        if isinstance(e, ast.GeneratorExp):
            # v0.48：生成器表达式在表达式位置——完美版，提升成循环语句
            var_name = self._fresh_name("生成器")
            self.pending.append(f"{var_name} = []")
            # 简化版：只处理最简单的 for 循环
            if len(e.generators) == 1:
                gen = e.generators[0]
                iter_code = self.expr(gen.iter)
                target_code = self.expr(gen.target)
                elt_code = self.expr(e.elt)
                self.pending.append(f"{self.kw_of('for')} {target_code} {self.kw_of('in')} {iter_code} {{")
                self.pending.append(f"  {var_name} = {self.builtin_of('append')}({var_name}, {elt_code})")
                self.pending.append(f"}}")
            self.warnings.append(
                f"生成器表达式在表达式位置——提升成循环语句，结果存在变量 `{var_name}` 里")
            return var_name
        if isinstance(e, (ast.ListComp, ast.DictComp, ast.SetComp)):
            # v0.48：推导式在表达式位置——完美版，提升成循环语句
            var_name = self._fresh_name("推导")
            self.pending.append(f"{var_name} = []")
            # 简化版：只处理最简单的 for 循环
            if len(e.generators) == 1:
                gen = e.generators[0]
                iter_code = self.expr(gen.iter)
                target_code = self.expr(gen.target)
                elt_code = self.expr(e.elt)
                self.pending.append(f"{self.kw_of('for')} {target_code} {self.kw_of('in')} {iter_code} {{")
                self.pending.append(f"  {var_name} = {self.builtin_of('append')}({var_name}, {elt_code})")
                self.pending.append(f"}}")
            self.warnings.append(
                f"推导式在表达式位置——提升成循环语句，结果存在变量 `{var_name}` 里")
            return var_name
        raise Blocked(f"暂时不能导的表达式写法 `{type(e).__name__}`")

    def literal(self, e: ast.Constant) -> str:
        v = e.value
        if v is None:
            return self.kw_of("Null")
        if isinstance(v, bool):
            return self.kw_of("true") if v else self.kw_of("false")
        if isinstance(v, (int, float)):
            return repr(v)
        # v0.48：字节串 → 普通字符串（按 UTF-8 解码）
        if isinstance(v, bytes):
            try:
                s = v.decode("utf-8")
                self.warnings.append(
                    f"字节串 `b'{s}'` 被翻译成普通字符串：AGL 没有字节类型，"
                    "按 UTF-8 解码成文本")
                escaped = (s.replace("\\", "\\\\").replace('"', '\\"')
                           .replace("\n", "\\n").replace("\t", "\\t"))
                return f'"{escaped}"'
            except UnicodeDecodeError:
                raise Blocked("字节串含非 UTF-8 字节，暂时不能导：AGL 只有文本类型")
        # v0.48：复数 → [实部, 虚部] 列表
        if isinstance(v, complex):
            self.warnings.append(
                f"复数 `{v}` 被翻译成列表 [{v.real}, {v.imag}]：AGL 没有复数类型，"
                "用两个数的列表表示")
            return f"[{v.real}, {v.imag}]"
        # 字符串：按 AGL 的字面量规则转义，避免用户源码里的引号把产物撑破
        escaped = (v.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\t", "\\t"))
        return f'"{escaped}"'

    def call(self, e: ast.Call) -> str:
        name = self._dotted(e.func) if not isinstance(e.func, ast.Attribute) else None
        # v0.43：函数引用自动分发——f 是函数引用变量时，f(args) → 调用(f, args)
        if isinstance(e.func, ast.Name) and e.func.id in self.fn_ref_vars:
            recv_code = self.expr(e.func)
            arg_codes = [self.expr(a) for a in e.args]
            if e.keywords:
                raise Blocked(
                    f"函数引用 `{e.func.id}(...)` 带关键字实参，暂时不能导")
            call_code = (f"{self.builtin_of('call_fn')}({recv_code}"
                         + (", " + ", ".join(arg_codes) if arg_codes else "") + ")")
            # 异步函数引用：调用后返回协程对象，自动加等待
            if self.fn_ref_vars[e.func.id]:
                call_code = f"{self.kw_of('await')}({call_code})"
            return call_code
        # v0.41：可调用对象 a(x) → C_call(a, x)（func 是裸 Name 且是实例）
        if isinstance(e.func, ast.Name) and not isinstance(e.func, ast.Attribute):
            fn_cls = self.instances.get(e.func.id)
            if fn_cls is not None:
                owner = self._resolve_dunder_owner(fn_cls, "__call__")
                if owner is not None:
                    recv_code = self.expr(e.func)
                    arg_codes = [self.expr(a) for a in e.args]
                    if e.keywords:
                        raise Blocked(
                            f"可调用对象 `{e.func.id}(...)` 带关键字实参，暂时不能导")
                    fn = self.check_name(f"{owner}___call__", "调用方法")
                    return f"{fn}({recv_code}" + (", " + ", ".join(arg_codes) if arg_codes else "") + ")"
        if name is not None and not isinstance(e.func, ast.Attribute):
            # v0.48：type() 特殊处理——单参数取类名，三参数动态建类
            if name == "type":
                if len(e.args) == 1 and not e.keywords:
                    # type(x) → 取类名(x)
                    return f"{self.builtin_of('type_name')}({self.expr(e.args[0])})"
                elif len(e.args) == 3 and not e.keywords:
                    # type('类名', (父类,), {'属性': 值}) → 动态建类
                    name_code = self.expr(e.args[0])
                    bases_code = self.expr(e.args[1])
                    attrs_code = self.expr(e.args[2])
                    return f"_agl_create_class({name_code}, {bases_code}, {attrs_code})"
                else:
                    raise Blocked(
                        f"`type(...)` 只支持两种形式："
                        "type(x) 取对象类型，或 type('类名', 父类元组, 属性字典) 动态创建类")
            # 明确的、语义等价的几个 Python 内建
            if name == "isinstance":
                # isinstance(x, int) / isinstance(x, (str, list)) → 是实例(x, 类型词/列表)
                if len(e.args) != 2 or e.keywords:
                    raise Blocked("`isinstance(x, t)` 只接受两个位置实参")
                t = e.args[1]
                if isinstance(t, ast.Name) and t.id in _PY_TYPE_NAMES:
                    tw = f'"{self.kw_of(_PY_TYPE_NAMES[t.id])}"'
                elif isinstance(t, ast.Tuple):
                    parts = []
                    for elt in t.elts:
                        if not (isinstance(elt, ast.Name) and elt.id in _PY_TYPE_NAMES):
                            raise Blocked(
                                "`isinstance(x, (t1, t2, ...))` 的类型列表里"
                                "只支持 Python 内建类型名（int/float/str/bool/list/dict/set）")
                        parts.append(f'"{self.kw_of(_PY_TYPE_NAMES[elt.id])}"')
                    tw = "[" + ", ".join(parts) + "]"
                else:
                    raise Blocked(
                        "`isinstance(x, t)` 的 t 只支持内建类型名"
                        "（int/float/str/bool/list/dict/set）或它们的元组")
                return (f"{self.builtin_of('isinstance')}({self.expr(e.args[0])}, "
                        f"{tw})")
            if name == "print":
                kws = {k.arg: k.value for k in e.keywords}
                unknown = [k for k in kws if k not in ("end", "sep")]
                if unknown:
                    raise Blocked(
                        f"`print(...)` 的关键字参数 `{unknown[0]}` 暂时不能导："
                        "AGL 打印只支持 `end=`（不换行）与 `sep=`（分隔符）")
                args = ", ".join(self._fmt_print_arg(a) for a in e.args)
                if "end" in kws:
                    ev = kws["end"]
                    if not (isinstance(ev, ast.Constant) and isinstance(ev.value, str)):
                        raise Blocked("`print(..., end=...)` 的 end 必须是字符串常量")
                    if ev.value != "":
                        raise Blocked(
                            "`print(..., end=...)` 只支持 end=\"\"（不换行）；"
                            "其他 end 值 AGL 没有对应写法")
                    if "sep" in kws:
                        raise Blocked("`print(..., end=..., sep=...)` 两个关键字同时用，暂时不能导")
                    return f"{self.builtin_of('print_no_newline')}({args})"
                if "sep" in kws:
                    sv = kws["sep"]
                    if not (isinstance(sv, ast.Constant) and isinstance(sv.value, str)):
                        raise Blocked("`print(..., sep=...)` 的 sep 必须是字符串常量")
                    items = ", ".join(self._fmt_print_arg(a) for a in e.args)
                    joined = (f"{self.builtin_of('join_list')}"
                              f"({self.literal(sv)}, [{items}])")
                    return f"{self.builtin_of('print')}({joined})"
                if not e.args:
                    # print() 打一个空行（Python 语义）；AGL 打印 0 参时打空串同效。
                    return f"{self.builtin_of('print')}({self.literal(ast.Constant(value=''))})"
                return f"{self.builtin_of('print')}({args})"
            if name == "len":
                if len(e.args) != 1:
                    raise Blocked("`len()` 的参数个数不对")
                # v0.41：自定义对象 len(obj) → C_len(obj)
                arg_cls = self._expr_instance_cls(e.args[0])
                if arg_cls is not None:
                    owner = self._resolve_dunder_owner(arg_cls, "__len__")
                    if owner is not None:
                        arg_code = self.expr(e.args[0])
                        fn = self.check_name(f"{owner}___len__", "长度方法")
                        return f"{fn}({arg_code})"
                return f"{self.expr(e.args[0])}.{self.attr_of('length')}"
            if name == "set":
                # `set()` 空集合 / `set(xs)` 从列表建集合 → 建集合
                if len(e.args) > 1:
                    raise Blocked("`set()` 只接受一个参数（空集不带参数）")
                if e.keywords:
                    raise Blocked("`set(...)` 带关键字参数，暂时不能导")
                if not e.args:
                    return f"{self.builtin_of('make_set')}()"
                return f"{self.builtin_of('make_set')}({self.expr(e.args[0])})"
            if name == "str":
                if len(e.args) != 1:
                    raise Blocked("`str()` 的参数个数不对")
                # v0.48：str(异常对象) → 失败原因(异常对象)：直接拿错误消息字符串
                if (isinstance(e.args[0], ast.Name)
                        and e.args[0].id in self._except_stack):
                    return f"{self.builtin_of('fail_reason')}({self.expr(e.args[0])})"
                # v0.38：用户类实例（类型可确认且该类/父类定义了 __str__）→
                # 调 __str__ 方法，与 Python `str(c)` 走 __str__ 语义一致；
                # 其余走 转文本（显示协议）。
                # v0.40：属性取出 / 函数返回 / 直接构造 / 继承父类一并覆盖。
                wrapped = self._fmt_instance_dunder(e.args[0], "__str__")
                if wrapped is not None:
                    return wrapped
                return f"{self.builtin_of('to_text')}({self.expr(e.args[0])})"
            if name == "repr":
                if len(e.args) != 1:
                    raise Blocked("`repr()` 的参数个数不对")
                # v0.40：定义了 __repr__ 的用户类实例 → 调 __repr__ 方法，
                # 与 Python `repr(c)` 走 __repr__ 语义一致；其余走默认 表达式形式。
                wrapped = self._fmt_instance_dunder(e.args[0], "__repr__")
                if wrapped is not None:
                    return wrapped
                return f"{self.builtin_of('repr')}({self.expr(e.args[0])})"
            # int() / float() 走下方 v0.29 分支（`取到(转整数/转浮数(…))` 语义等价），
            # 转不了时 Python 抛异常、AGL 在 取到 处抛可被捕获的中文错误。
            if name == "sleep":
                if len(e.args) != 1:
                    raise Blocked("`sleep(x)` 只接受一个参数")
                arg_code = self.expr(e.args[0])
                # Python 的 sleep 单位是秒，AGL 的 睡眠 单位是毫秒，要乘 1000；
                # 乘出来是浮点（如 0.5*1000=500.0），而 睡眠 只要整数毫秒，
                # 所以外面套 取整 —— 亚毫秒部分直接截掉，不静默丢精度。
                self.warnings.append(
                    "`sleep(x)` 被翻译成 `等待 睡眠(取整(x * 1000))`："
                    "Python 的单位是秒，AGL 的单位是毫秒，已自动换算"
                    "（亚毫秒部分被截断，高精度计时请改用 当前时间戳）")
                return (f"{self.kw_of('await')} {self.builtin_of('sleep')}"
                        f"({self.builtin_of('trunc')}({arg_code} * 1000))")
            if name == "abs":
                if len(e.args) != 1:
                    raise Blocked("`abs(x)` 只接受一个参数")
                return f"{self.builtin_of('abs')}({self.expr(e.args[0])})"
            if name == "min":
                if not e.args:
                    raise Blocked("`min(...)` 至少要一个参数")
                if e.keywords:
                    if len(e.args) == 1 and len(e.keywords) == 1 \
                            and e.keywords[0].arg == "default":
                        # min(xs, default=v) → 最小默认(xs, v)
                        self.warnings.append(
                            "`min(xs, default=v)` 被翻译成 `最小默认(xs, v)`："
                            "列表为空时给默认值，与 Python 一致")
                        return (f"{self.builtin_of('min_default')}("
                                f"{self.expr(e.args[0])}, "
                                f"{self.expr(e.keywords[0].value)})")
                    raise Blocked(
                        "`min(...)` 的关键字参数只支持 `default=`（且列表参数正好一个）")
                return f"{self.builtin_of('min')}({', '.join(self.expr(a) for a in e.args)})"
            if name == "max":
                if not e.args:
                    raise Blocked("`max(...)` 至少要一个参数")
                if e.keywords:
                    if len(e.args) == 1 and len(e.keywords) == 1 \
                            and e.keywords[0].arg == "default":
                        # max(xs, default=v) → 最大默认(xs, v)
                        self.warnings.append(
                            "`max(xs, default=v)` 被翻译成 `最大默认(xs, v)`："
                            "列表为空时给默认值，与 Python 一致")
                        return (f"{self.builtin_of('max_default')}("
                                f"{self.expr(e.args[0])}, "
                                f"{self.expr(e.keywords[0].value)})")
                    raise Blocked(
                        "`max(...)` 的关键字参数只支持 `default=`（且列表参数正好一个）")
                return f"{self.builtin_of('max')}({', '.join(self.expr(a) for a in e.args)})"
            if name == "sum":
                if not e.args:
                    raise Blocked("`sum(...)` 至少要一个参数")
                if e.keywords:
                    raise Blocked("`sum(...)` 带关键字参数，暂时不能导")
                if len(e.args) == 1:
                    return f"{self.builtin_of('sum')}({self.expr(e.args[0])})"
                # sum(xs, start) → start + 合计(xs)，与 Python 的「先加 start」等价
                self.warnings.append(
                    "`sum(xs, start)` 被翻译成 `start + 合计(xs)`："
                    "Python 从 start 开始累加，这里是先加 start，结果一致")
                return (f"({self.expr(e.args[1])} + "
                        f"{self.builtin_of('sum')}({self.expr(e.args[0])}))")
            if name == "sorted":
                if e.keywords:
                    if len(e.args) != 1:
                        raise Blocked(
                            "`sorted(xs, key=..., reverse=...)` 只能有一个位置参数"
                            "（要排序的列表）")
                    keyf = None
                    rev = None
                    for k in e.keywords:
                        if k.arg == "key":
                            keyf = k.value
                        elif k.arg == "reverse":
                            rev = k.value
                        else:
                            raise Blocked(
                                f"`sorted(xs, {k.arg}=...)` 的参数不认识")
                    if rev is not None and not (
                            isinstance(rev, ast.Constant)
                            and isinstance(rev.value, bool)):
                        raise Blocked(
                            "`sorted(xs, reverse=...)` 的 reverse 必须是常量 真/假")
                    inner = self.expr(e.args[0])
                    if keyf is not None:
                        key_code = self._sorted_key(keyf)
                        out = f"{self.builtin_of('sort_by')}({inner}, {key_code})"
                    else:
                        out = f"{self.builtin_of('sorted')}({inner})"
                    if rev is not None and rev.value:
                        out = f"{self.builtin_of('reverse')}({out})"
                    return out
                if len(e.args) != 1:
                    raise Blocked(
                        "`sorted(xs)` 只接受一个参数：带 key / reverse 的写法会自动转成"
                        " `按键排序` / `反转`，但位置参数仍只能有一个")
                return f"{self.builtin_of('sorted')}({self.expr(e.args[0])})"
            if name in self.classes:
                # Counter(x) → 构建Counter(x)：AGL 没有类，构造是普通函数
                if any(k.arg is not None for k in e.keywords):
                    raise Blocked(f"调用构造函数 `{name}(...)` 用了关键字参数，暂时不能导")
                if any(isinstance(a, ast.Starred) for a in e.args):
                    raise Blocked(f"调用构造函数 `{name}(...)` 用了 `*args` 展开，暂时不能导")
                args = ", ".join(self.expr(a) for a in e.args)
                return f"{self.check_name(f'构建{name}', '构造函数')}({self._append_globals(args)})"
            if name == "range":
                # range 当值用：range(n) → 范围列表(n)（AGL 没有惰性迭代器，
                # 列表就是它的值；循环 的 range 控制仍走展开，不受影响）
                if len(e.args) == 0 or len(e.args) > 3:
                    raise Blocked(
                        "`range(...)` 当值用时只支持 range(stop) / range(start, stop) / "
                        "range(start, stop, step) 三种")
                if e.keywords:
                    raise Blocked("`range(...)` 当值用不支持关键字参数")
                for a in e.args:
                    if not isinstance(a, ast.Constant) or not isinstance(a.value, int):
                        raise Blocked(
                            "`range(...)` 当值用要求参数都是固定整数；"
                            "动态范围请先算成列表再传")
                self.warnings.append(
                    "`range(...)` 当值用被翻译成 `范围列表(...)`："
                    "AGL 没有惰性迭代器，直接用列表表达，语义一致")
                return f"{self.builtin_of('range_list')}({', '.join(self.expr(a) for a in e.args)})"
            if name == "list":
                # list(x)：Python 的通用列表化。AGL 值语义下按 Python 语义分派：
                #   文本 → 转列表（拆成字符列表）
                #   映射表 → 键列表（list(d) 就是按键）
                #   集合 → 转列表（排序保确定性；Python 集合本无序，顺序差异挂 warning）
                #   列表 → 透传（值语义下「复制一份列表」无意义，直接等价）
                #   range → 已被上面的 range 分支收掉
                if len(e.args) != 1 or e.keywords:
                    raise Blocked("`list(x)` 只接受一个参数")
                arg = e.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return f"{self.builtin_of('to_list')}({self.expr(arg)})"
                if isinstance(arg, ast.Dict):
                    return f"{self.builtin_of('keys')}({self.expr(arg)})"
                if isinstance(arg, (ast.Name, ast.Attribute, ast.Subscript)):
                    # 变量形态的 list(x)：是映射表还是集合/列表在导入期看不到，
                    # 统一交给运行期 转列表 按类型分发，行为与 Python list(x) 一致
                    self.warnings.append(
                        "`list(x)` 被翻译成 `转列表(x)`：文本拆成字符列表、"
                        "映射表取键列表、集合取排序后的元素列表，与 Python `list(x)` 一致；"
                        "列表本身透传（值语义下拷贝无意义）")
                    return f"{self.builtin_of('to_list')}({self.expr(arg)})"
                self.warnings.append(
                    "`list(x)` 被透传成 `x` 本身：AGL 是值语义，"
                    "「复制一份列表/集合」没有意义，直接用原值等价")
                return self.expr(e.args[0])
            if name == "bool":
                # bool(x) → 转布尔(x)：Python 真值规则，与 AGL 值语义完全一致
                if len(e.args) != 1 or e.keywords:
                    raise Blocked("`bool(x)` 只接受一个参数")
                return f"{self.builtin_of('to_bool')}({self.expr(e.args[0])})"
            if name == "tuple":
                # Python tuple(x) 是「转成元组」；AGL 没有元组类型（导入器把元组
                # 翻译成列表），值语义下 tuple(x) 与 x 本身等价，透传即可。
                if len(e.args) != 1 or e.keywords:
                    raise Blocked("`tuple(x)` 只接受一个参数")
                self.warnings.append(
                    "`tuple(x)` 被透传成 `x` 本身：AGL 没有元组类型"
                    "（导入器把元组翻译成列表），值语义下与 `x` 等价")
                return self.expr(e.args[0])
            if name == "dict":
                # dict()：空映射表；dict(m) 拷贝映射表（值语义透传等价）；
                # dict(键值对列表) → 建映射(列表)（重复键后者覆盖，与 Python 一致）
                if e.keywords:
                    raise Blocked(
                        "`dict(...)` 带关键字参数，暂时不能导："
                        "请直接用映射表字面量 `{\"k\": v}` 或 `建映射(键值对列表)`")
                if not e.args:
                    return "{}"
                if len(e.args) != 1:
                    raise Blocked("`dict(...)` 只接受一个参数")
                arg = e.args[0]
                if isinstance(arg, ast.List):
                    return f"{self.builtin_of('make_map')}({self.expr(arg)})"
                self.warnings.append(
                    "`dict(x)` 被透传成 `x` 本身：AGL 是值语义，"
                    "「复制一份映射表」没有意义，直接用原值等价")
                return self.expr(arg)
            if name == "input":
                if len(e.args) > 1 or e.keywords:
                    raise Blocked("`input(prompt)` 只接受一个位置参数（提示语）")
                if e.args:
                    return (f"{self.builtin_of('input')}"
                            f"({self.expr(e.args[0])})")
                self.warnings.append(
                    "`input()` 被翻译成 `读入()`：它读一行用户输入，"
                    "带「读写」效应，函数签名自动补 `效应 读写`")
                return f"{self.builtin_of('input')}()"
            # int(x) / int(x, base) / float(x)：
            # Python 转不了就抛 ValueError，AGL 用 取到(转整数/转浮数(…))
            # —— 失败结果在 取到 处抛成可被 尝试/捕获 接住的中文错误，语义等价。
            # int() / float() 无参 → 0 / 0.0（Python 同语义）。
            if name == "int" or name == "float":
                if e.keywords:
                    raise Blocked(f"`{name}(...)` 不接受关键字参数")
                if name == "int":
                    if len(e.args) == 0:
                        return "0"
                    if len(e.args) > 2:
                        raise Blocked("`int(x)` / `int(x, base)` 之外写法暂时不能导")
                    inner = self.expr(e.args[0])
                    if len(e.args) == 2:
                        base_code = self.expr(e.args[1])
                        conv = f"{self.builtin_of('to_int')}({inner}, {base_code})"
                    else:
                        conv = f"{self.builtin_of('to_int')}({inner})"
                else:
                    if len(e.args) == 0:
                        return "0.0"
                    if len(e.args) > 1:
                        raise Blocked("`float(x)` 之外写法暂时不能导")
                    conv = f"{self.builtin_of('to_float')}({self.expr(e.args[0])})"
                self.warnings.append(
                    f"`{name}(x)` 被翻译成 `取到({conv})`：转不了时 Python 抛异常、"
                    "AGL 在 取到 处抛可被捕获的中文错误，语义等价")
                return f"{self.builtin_of('unwrap')}({conv})"
            # format(x, spec) → 复用 f-string / str.format 的格式说明映射
            if name == "format":
                if len(e.args) != 2 or e.keywords:
                    raise Blocked("`format(x, spec)` 只接受两个位置参数")
                spec_node = e.args[1]
                if not (isinstance(spec_node, ast.Constant)
                        and isinstance(spec_node.value, str)):
                    # v0.31：动态 spec 交给 按格式 原生透传（Python format 语义）
                    return f"{self.builtin_of('format_value')}(" \
                           f"{self.expr(e.args[0])}, {self.expr(spec_node)})"
                return self._format_spec_conversion(
                    spec_node.value, self.expr(e.args[0]))
            if name in ("round", "pow", "divmod", "ord", "chr", "all",
                        "any", "hex", "oct", "bin"):
                # Python 内建 → AGL 内建直译（v0.27 之前的透传漏网：
                # round(3.7) 曾被原样透传成 `round(3.7)` → AGL-E-0302 编译错）
                canon = {
                    "round": "round", "pow": "pow", "divmod": "divmod",
                    "ord": "ord", "chr": "chr", "all": "all", "any": "any",
                    "hex": "hex", "oct": "oct", "bin": "bin",
                }[name]
                if e.keywords:
                    raise Blocked(f"`{name}(...)` 带关键字参数，暂时不能导")
                if name == "round" and len(e.args) > 2:
                    raise Blocked(f"`{name}(...)` 最多两个位置参数")
                if name == "round" and not (1 <= len(e.args) <= 2):
                    raise Blocked("`round(x)` / `round(x, n)` 之外写法暂时不能导")
                if name == "pow" and not (1 <= len(e.args) <= 3):
                    raise Blocked("`pow(x, y)` / `pow(x, y, mod)` 之外写法暂时不能导")
                if name == "divmod" and len(e.args) != 2:
                    raise Blocked("`divmod(a, b)` 只接受两个参数")
                if name in ("ord", "chr", "all", "any", "hex", "oct", "bin") \
                        and len(e.args) != 1:
                    raise Blocked(f"`{name}(...)` 只接受一个参数")
                return f"{self.builtin_of(canon)}({', '.join(self.expr(a) for a in e.args)})"
            if name == "reversed":
                # reversed(xs) → 反转(xs)：AGL 值语义直接用反转列表表达
                if len(e.args) != 1 or e.keywords:
                    raise Blocked("`reversed(xs)` 只接受一个列表参数")
                self.warnings.append(
                    "`reversed(xs)` 被翻译成 `反转(xs)`：AGL 值语义没有迭代器，"
                    "直接给出反转后的列表，语义一致")
                return f"{self.builtin_of('reverse')}({self.expr(e.args[0])})"
            if name == "format":
                raise Blocked(
                    "`format(x, spec)` 暂时不能导：AGL 的格式化请用 `填充` "
                    "（`{}` 占位）或 `格式化数字` / `补零`")
            if name == "open":
                # v0.46：open() 本身不翻译成有意义的东西——只在赋值语句右边
                # 或 with 语句里登记 file_handles，后面 f.read() / f.write() 特殊处理。
                # 单独的 open() 调用（没赋值没 with）还是警告一下。
                self.warnings.append(
                    "`open()` 被翻译成空操作：AGL 没有文件句柄对象，"
                    "只在 `f = open(...)` 或 `with open(...) as f:` 形式下登记文件句柄，"
                    "后面 f.read() / f.write() 自动映射到读文件/写文件内建")
                return self.kw_of("Null")
            if name == "urlopen":
                # from urllib.request import urlopen 之后的直接调用：
                # urlopen(u) → 取到(获取网页(u))（返回正文文本；Python 返回句柄，
                # 原代码若调 .read() 请用 urlopen(u).read() 写法，会走同一翻译）
                if len(e.args) != 1 or e.keywords:
                    raise Blocked(
                        "`urlopen(url)` 只接受一个 URL 参数；"
                        "带 timeout 等关键字参数的写法请手写 FFI")
                self.warnings.append(
                    "`urlopen(url)` 被翻译成 `取到(获取网页(url))`："
                    "AGL 的 获取网页 直接返回正文文本，Python 的 urlopen 返回句柄；"
                    "若原代码用了 .read() 之外的方法（.status / .headers 等）请手写重审")
                return (f"{self.builtin_of('unwrap')}({self.builtin_of('fetch')}"
                        f"({self.expr(e.args[0])}))")
            if name in ("eval", "exec", "compile", "__import__"):
                raise Blocked(f"`{name}()` 不允许导入：它会执行动态代码，正是 AGL 要挡住的东西")
            info = self.callee_info.get(name)
            # 嵌套函数：只在定义它的外层函数里可调用（v0.37 提升成模块级函数），
            # 调用处按 AST 分析出的捕获列表补传外层变量（顺序与签名一致）
            nest = self.nested_map.get(name)
            if nest:
                if e.keywords:
                    raise Blocked(
                        f"嵌套函数 `{name}(...)` 带关键字实参，暂时不能导："
                        "AGL 的调用都是按位置传参")
                fn_code = self.check_name(nest["fn"], "调用")
                cap_args = ", ".join(self.expr(ast.Name(id=c))
                                     for c in nest["captures"])
                args_code = ", ".join(self.expr(a) for a in e.args)
                return f"{fn_code}({self._append_globals(cap_args, args_code)})"
            # 捕获型 lambda：调用处补传捕获变量（只在定义函数内可调）
            cap = self.lambda_captures.get(name)
            if cap:
                if self.cur_fn != self.lambda_def_fn.get(name):
                    raise Blocked(
                        f"捕获型 lambda `{name}` 被定义它的函数之外调用了："
                        "AGL 没有闭包，捕获变量只在定义函数内可见；"
                        "请把它改成显式传参的普通函数")
                cap_args = ", ".join(self.check_name(c, "这里")
                                     for c in cap["captures"])
                fn_code = self.check_name(cap["fn"], "调用")
                args_code = ", ".join(self.expr(a) for a in e.args)
                return f"{fn_code}({self._append_globals(args_code, cap_args)})"
            kw_code = None
            if e.keywords:
                # 只有被调函数声明了 **kw 时才允许关键字实参：打包成映射表
                if info and info.get("kwarg"):
                    static_kws = [kw for kw in e.keywords if kw.arg is not None]
                    splats = [kw.value for kw in e.keywords if kw.arg is None]
                    if splats:
                        # `**d` 动态展开：AGL 的 **kw 形参就是 映射表<文本, 任意>，
                        # 直接把映射传进去；与静态关键字混合时用 合并映射 拼接。
                        # 注意：静态关键字与 **展开 的键冲突时，Python 在调用处抛
                        # TypeError（关键字重复），这里按「后者覆盖」合并，行为有差异。
                        self.warnings.append(
                            "`**展开` 被翻译成「把映射表直接传给 映射表 参数」："
                            "键的数量和名字在运行期确定，与 Python 一致；"
                            "与静态关键字混合时用 合并映射 拼接，键冲突按「后者覆盖」——"
                            "原 Python 在键冲突时会抛 TypeError，请确认改写过的行为可接受")
                        parts: list[str] = []
                        if static_kws:
                            parts.append("{" + ", ".join(
                                f'"{kw.arg}": {self.expr(kw.value)}'
                                for kw in static_kws) + "}")
                        parts.extend(self.expr(v) for v in splats)
                        if len(parts) == 1:
                            kw_code = parts[0]
                        else:
                            kw_code = f"{self.builtin_of('merge_map')}({', '.join(parts)})"
                    else:
                        kw_parts = [f'"{kw.arg}": {self.expr(kw.value)}'
                                    for kw in e.keywords]
                        kw_code = "{" + ", ".join(kw_parts) + "}"
                else:
                    if any(kw.arg is None for kw in e.keywords):
                        raise Blocked(
                            f"调用 `{name}` 用 `**展开` 传参，但 `{name}` 没有 `**kw` 参数："
                            "原 Python 在这里也会抛 TypeError，两边一致，请补形参或改写调用")
                    raise Blocked(f"调用 `{name}` 用了关键字参数，暂时不能导")
            # v0.48：调用时 *args 展开——如果函数本身收 *args，就直接把列表传进去
            #   f(*xs) 且 def f(*args): → f(xs)
            starred_idx = next((i for i, a in enumerate(e.args)
                                if isinstance(a, ast.Starred)), -1)
            has_starred = starred_idx >= 0
            if has_starred and info and info["vararg"]:
                # 把 *xs 展开成：前面的固定参数 + xs 作为 args 列表
                fixed = info["fixed"]
                if len(e.args) < fixed:
                    raise Blocked(f"调用 `{name}` 的实参比形参少，原 Python 也会报错")
                fixed_args = ", ".join(self.expr(a) for a in e.args[:fixed])
                starred_expr = self.expr(e.args[starred_idx].value)
                call_args: list[str] = []
                if fixed_args:
                    call_args.append(fixed_args)
                call_args.append(starred_expr)  # *xs 直接作为 args 列表传
                # 如果函数收 **kwargs，也要传 kwargs 映射
                if info.get("kwarg"):
                    if kw_code:
                        call_args.append(kw_code)
                    else:
                        call_args.append("{}")
                if self.globals_active and name in self.callee_info:
                    call_args.append(self.globals_store)
                return f"{self.check_name(name, '调用')}({', '.join(call_args)})"
            if any(isinstance(a, ast.Starred) for a in e.args):
                raise Blocked(f"调用 `{name}` 用了 `*args` 展开，但 `{name}` 不收 *args，暂时不能导")
            # 可变参数 / 关键字参数函数：调用处把多余的实参包成列表、关键字实参包成映射
            #   def f(a, *rest, **kw)  →  f(1, 2, x=3)  →  f(1, [2], {"x": 3})
            if info and (info["vararg"] or kw_code):
                fixed = info["fixed"]
                if len(e.args) < fixed:
                    raise Blocked(f"调用 `{name}` 的实参比形参少，原 Python 也会报错")
                fixed_args = ", ".join(self.expr(a) for a in e.args[:fixed])
                extra = ", ".join(self.expr(a) for a in e.args[fixed:])
                call_args: list[str] = []
                if fixed_args:
                    call_args.append(fixed_args)
                # 如果函数收 *args，不管有没有额外位置参数，都要传 args 列表（空就是 []）
                if info["vararg"]:
                    if extra:
                        call_args.append(f"[{extra}]")
                    else:
                        call_args.append("[]")
                # 如果函数收 **kwargs，不管有没有关键字参数，都要传 kwargs 映射（空就是 {}）
                if info.get("kwarg"):
                    if kw_code:
                        call_args.append(kw_code)
                    else:
                        call_args.append("{}")
                # 只有本文件函数（签名收了 _agl_globals）才补传共享存储；
                # FFI 外部函数的签名没有这个参数，不能多传
                if self.globals_active and name in self.callee_info:
                    call_args.append(self.globals_store)
                return f"{self.check_name(name, '调用')}({', '.join(call_args)})"
            args = ", ".join(self.expr(a) for a in e.args)
            tail = (self._globals_tail()
                    if name in self.callee_info else "")
            return f"{self.check_name(name, '调用')}({self._append_globals(args) if (name in self.callee_info) else args})"

        # 走到这里说明是 `x.y()` 这样的方法调用
        if isinstance(e.func, ast.Attribute):
            meth = e.func.attr
            receiver = e.func.value
            # v0.45：f 是 with open(...) as f 的文件句柄 → f.read() / f.write() 翻译
            if (isinstance(receiver, ast.Name)
                    and receiver.id in self.file_handles):
                path_code, mode = self.file_handles[receiver.id]
                if meth == "read":
                    if e.args or e.keywords:
                        raise Blocked("`f.read()` 不接受参数")
                    if mode not in ("r", "rt", "r+", "rb"):
                        raise Blocked(
                            f"`open(\"{mode}\").read()` 模式不认识：只读模式才能 .read()")
                    return f"{self.builtin_of('unwrap')}({self.builtin_of('read_file')}({path_code}))"
                if meth == "write":
                    if len(e.args) != 1 or e.keywords:
                        raise Blocked("`f.write(content)` 只接受一个内容参数")
                    if mode not in ("w", "wt", "w+", "a", "at", "a+"):
                        raise Blocked(
                            f"`open(\"{mode}\").write()` 模式不认识：只写/追加模式才能 .write()")
                    content_code = self.expr(e.args[0])
                    # v0.48：追加模式用追加写文件，否则用覆盖写
                    is_append = mode.startswith("a")
                    builtin_name = "append_file" if is_append else "write_file"
                    return f"{self.builtin_of('unwrap')}({self.builtin_of(builtin_name)}({path_code}, {content_code}))"
                if meth == "close":
                    if e.args or e.keywords:
                        raise Blocked("`f.close()` 不接受参数")
                    # AGL 没有真打开文件，close 是空操作
                    return self.kw_of("Null")
                raise Blocked(
                    f"`open(...).{meth}()` 方法不认识：文件句柄只支持 .read() / .write() / .close()")
            # open(path, "r").read() → 取到(读文件(path))；open(path, "w").write(c) → 取到(写文件(path, c))
            # Python 的 open() 返回文件句柄，AGL 没有句柄对象；方法链形式直接翻译成对应的读写内建。
            # 单独的 f = open(...) 还是拒绝（没有文件句柄类型）。
            if (isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "open"):
                if len(receiver.args) < 1 or len(receiver.args) > 2:
                    raise Blocked(
                        "`open(path, mode)` 只接受路径和模式两个参数")
                path_code = self.expr(receiver.args[0])
                mode = "r"
                if len(receiver.args) == 2:
                    if isinstance(receiver.args[1], ast.Constant) and isinstance(receiver.args[1].value, str):
                        mode = receiver.args[1].value
                    else:
                        raise Blocked(
                            "`open(path, mode)` 的 mode 必须是字符串常量（如 \"r\" / \"w\"）")
                if meth == "read":
                    if e.args or e.keywords:
                        raise Blocked("`f.read()` 不接受参数")
                    if mode not in ("r", "rt", "r+", "rb"):
                        raise Blocked(
                            f"`open(\"{mode}\").read()` 模式不认识：只读模式才能 .read()")
                    self.warnings.append(
                        "`open(path, \"r\").read()` 被翻译成 `取到(读文件(path))`："
                        "Python 的 open() 返回文件句柄，AGL 没有句柄对象，"
                        "方法链形式直接映射到读文件内建")
                    return f"{self.builtin_of('unwrap')}({self.builtin_of('read_file')}({path_code}))"
                if meth == "write":
                    if len(e.args) != 1 or e.keywords:
                        raise Blocked("`f.write(content)` 只接受一个内容参数")
                    if mode not in ("w", "wt", "w+", "a", "at", "a+"):
                        raise Blocked(
                            f"`open(\"{mode}\").write()` 模式不认识：只写/追加模式才能 .write()")
                    content_code = self.expr(e.args[0])
                    # v0.48：追加模式用追加写文件，否则用覆盖写
                    is_append = mode.startswith("a")
                    builtin_name = "append_file" if is_append else "write_file"
                    cn_name = "追加写文件" if is_append else "写文件"
                    self.warnings.append(
                        f"`open(path, \"{mode}\").write(content)` 被翻译成 `取到({cn_name}(path, content))`："
                        "Python 的 open() 返回文件句柄，AGL 没有句柄对象，"
                        "方法链形式直接映射到文件内建")
                    return f"{self.builtin_of('unwrap')}({self.builtin_of(builtin_name)}({path_code}, {content_code}))"
                raise Blocked(
                    f"`open(...).{meth}()` 方法不认识：文件句柄只支持 .read() 和 .write()")
            # json.loads(s) → 取到(解析JSON(s))；json.dumps(x) → 取到(生成JSON(x))
            # Python 直接返回值 / 抛异常，AGL 的「结果」要显式 取到，失败即抛——语义等价。
            # dict.fromkeys(ks, v) → 从键构建(ks, v)（缺 v 时补 空，Python 默认 None）
            if (meth == "fromkeys"
                    and isinstance(receiver, ast.Name)
                    and receiver.id == "dict"):
                if e.keywords or not e.args or len(e.args) > 2:
                    raise Blocked(
                        "`dict.fromkeys(ks, v)` 只接受一到两个位置实参")
                self.warnings.append(
                    "`dict.fromkeys(ks, v)` 被翻译成 `从键构建(ks, v)`："
                    "用键列表新建映射表，统一默认值，与 Python 语义一致")
                code = f"{self.builtin_of('fromkeys')}({self.expr(e.args[0])}"
                if len(e.args) == 2:
                    code += f", {self.expr(e.args[1])}"
                else:
                    code += f", {self.kw_of('Null')}"
                return code + ")"
            if isinstance(receiver, ast.Name) and receiver.id == "json":
                if meth == "loads":
                    if len(e.args) != 1 or e.keywords:
                        raise Blocked("`json.loads(s)` 只接受一个文本参数")
                    self.warnings.append(
                        "`json.loads(s)` 被翻译成 `取到(解析JSON(s))`："
                        "Python 直接抛异常，AGL 用「结果 + 取到」，失败即抛，行为一致")
                    return f"{self.builtin_of('unwrap')}({self.builtin_of('parse_json')}({self.expr(e.args[0])}))"
                if meth == "dumps":
                    if len(e.args) != 1 or e.keywords:
                        raise Blocked("`json.dumps(x)` 只接受一个参数")
                    self.warnings.append(
                        "`json.dumps(x)` 被翻译成 `取到(生成JSON(x))`："
                        "AGL 的 生成JSON 用「结果」表达失败，取到 后失败即抛")
                    return f"{self.builtin_of('unwrap')}({self.builtin_of('dump_json')}({self.expr(e.args[0])}))"
                raise Blocked(
                    f"`json.{meth}()` 暂时不能导：只有 json.loads / json.dumps 被翻译成"
                    " AGL 内建；文件版 load/dump 请手写 读文件/写文件")
            if (meth == "get"
                    and isinstance(receiver, ast.Name)
                    and receiver.id == "requests"):
                # requests.get(url) → 获取网页(url)（返回正文文本；Python 返回
                # Response 对象，原代码若用了 .status_code / .headers 等成员，
                # 请手写 FFI——这里只覆盖最常见的「拿正文」场景）
                if len(e.args) != 1 or e.keywords:
                    raise Blocked(
                        "`requests.get(url)` 只接受一个 URL 参数；"
                        "带 params / timeout 等关键字参数的写法请手写 FFI")
                self.warnings.append(
                    "`requests.get(url)` 被翻译成 `取到(获取网页(url))`："
                    "AGL 的 获取网页 直接返回正文文本，Python 返回 Response 对象；"
                    "若原代码用了 .status_code / .headers 等成员请手写重审")
                return (f"{self.builtin_of('unwrap')}({self.builtin_of('fetch')}"
                        f"({self.expr(e.args[0])}))")
            if (meth in ("text", "json")
                    and isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Attribute)
                    and receiver.func.attr == "get"
                    and isinstance(receiver.func.value, ast.Name)
                    and receiver.func.value.id == "requests"):
                url_code = self.expr(receiver.args[0])
                body = f"{self.builtin_of('unwrap')}({self.builtin_of('fetch')}({url_code}))"
                if meth == "json":
                    # .json() 是 Response 的方法：解析响应文本
                    self.warnings.append(
                        "`requests.get(url).json()` 被翻译成 "
                        "`取到(解析JSON(取到(获取网页(url))))`：两步合一")
                    return (f"{self.builtin_of('unwrap')}({self.builtin_of('parse_json')}({body}))")
                return body
            if (meth == "read"
                    and isinstance(receiver, ast.Call)):
                # urllib.request.urlopen(u).read() / urlopen(u).read() → 获取网页(u)
                is_urlopen = (
                    (isinstance(receiver.func, ast.Attribute)
                     and receiver.func.attr == "urlopen")
                    or (isinstance(receiver.func, ast.Name)
                        and receiver.func.id == "urlopen"))
                if is_urlopen:
                    if len(receiver.args) != 1:
                        raise Blocked("`urlopen(u).read()` 只接受一个 URL 参数")
                    self.warnings.append(
                        "`urlopen(url).read()` 被翻译成 `取到(获取网页(url))`："
                        "AGL 的 获取网页 直接返回正文文本")
                    return (f"{self.builtin_of('unwrap')}({self.builtin_of('fetch')}"
                            f"({self.expr(receiver.args[0])}))")
            # super().方法(...)：沿当前类 MRO 找真正定义该方法的类（AGL 没有 super 对象）
            if (meth != "__init__"
                    and isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "super"):
                if not self.super_base:
                    raise Blocked("`super()` 出现在没有父类的类里，暂时不能导")
                mro = self._mro(self.super_base)
                tail = self._super_search_tail(receiver, mro, mro[1:])
                owner = next((c for c in tail
                              if meth in self.classes.get(c, {}).get("methods", {})),
                             None)
                if owner is None:
                    raise Blocked(
                        f"`super().{meth}()` 在继承链上找不到 `{meth}` 的实现"
                        "（Python 运行到这里会抛 AttributeError）")
                owner_method = self.classes[owner]["methods"][meth]
                m_params = [a.arg for a in list(owner_method.args.posonlyargs)
                            + list(owner_method.args.args)][1:]  # 去 self
                arg_exprs = self._bind_call_args(
                    m_params, e.args, e.keywords, f"super().{meth}()")
                arg_codes = [self.expr(x) for x in arg_exprs]
                recv_code = self.check_name("self", "实例")
                fn_code = self.check_name(f"{owner}_{meth}", "祖先类方法")
                return f"{fn_code}({self._append_globals(recv_code, *arg_codes)})"
            # 类名调用静态/类方法：X.s(x) → <定义类>_s(x)（staticmethod）
            #                        X.c(x) → <定义类>_c(x)（classmethod 去 cls）
            # 普通实例方法用类名直接调（`X.m(实例, x)`）需要显式实例，
            # AGL 没有「未绑定方法」这一层，仍拒绝。
            if isinstance(receiver, ast.Name) and receiver.id in self.classes:
                cls_name = receiver.id
                kind = self._method_kind(cls_name, meth)
                if kind in ("static", "class"):
                    owner, owner_method = self._resolve_method(cls_name, meth)
                    if owner is None:
                        raise Blocked(
                            f"`{cls_name}.{meth}()` 在继承链上找不到 `{meth}` 的实现"
                            "（Python 运行到这里会抛 AttributeError）")
                    # static 无 self（参数全保留）；class 的 cls 要去掉
                    m_params = [a.arg for a in
                                list(owner_method.args.posonlyargs)
                                + list(owner_method.args.args)]
                    if kind == "class":
                        m_params = m_params[1:]
                    arg_exprs = self._bind_call_args(
                        m_params, e.args, e.keywords,
                        f"类方法 {cls_name}.{meth}()")
                    arg_codes = [self.expr(x) for x in arg_exprs]
                    fn_code = self.check_name(f"{owner}_{meth}", "类方法函数")
                    return f"{fn_code}({self._append_globals(*arg_codes)})"
            # 类实例的方法调用：c.inc() → Counter_inc(c)
            # （AGL 没有对象方法，方法被翻译成「实例显式传参」的普通函数；
            #   继承的方法由子类转发函数承接，调用仍走 <子类>_方法；
            #   @staticmethod / @classmethod 沿 MRO 解析到定义类，不传实例）
            # v0.40：receiver 不再只认裸变量——属性取出（o.inner.show()）、
            # 函数返回（make().show()）、直接构造（C().show()）、多层属性链
            # （a.b.c.show()）都通过 _expr_instance_cls 静态推类后分发。
            recv_cls = self._expr_instance_cls(receiver)
            if recv_cls is not None and recv_cls in self.classes:
                owner, owner_method = self._resolve_method(recv_cls, meth)
                if owner and meth != "__init__":
                    kind = self._method_kind(recv_cls, meth)
                    # static / class 方法：不传实例（static 无 self、class 无 cls）
                    if kind in ("static", "class"):
                        # static 无 self（参数全保留）；class 的 cls 要去掉
                        m_params = [a.arg for a in
                                    list(owner_method.args.posonlyargs)
                                    + list(owner_method.args.args)]
                        if kind == "class":
                            m_params = m_params[1:]
                        arg_exprs = self._bind_call_args(
                            m_params, e.args, e.keywords,
                            f"实例上的类方法 {meth}()")
                        arg_codes = [self.expr(x) for x in arg_exprs]
                        fn_code = self.check_name(f"{owner}_{meth}", "方法函数")
                        return f"{fn_code}({self._append_globals(*arg_codes)})"
                    recv_code = self.expr(receiver)
                    # 关键字实参按方法形参绑定（乱序、位置+关键字混合都支持），
                    # 与 Python 实参绑定一致：超界/重复/未知/漏填精确拒绝。
                    m_params = [a.arg for a in
                                list(owner_method.args.posonlyargs)
                                + list(owner_method.args.args)][1:]  # 去 self
                    arg_exprs = self._bind_call_args(
                        m_params, e.args, e.keywords, f"实例方法 {meth}()")
                    arg_codes = [self.expr(x) for x in arg_exprs]
                    # 调真正定义/覆盖该方法的那个类的实现（多级继承时沿链找）
                    fn_code = self.check_name(f"{owner}_{meth}", "方法函数")
                    return f"{fn_code}({self._append_globals(recv_code, *arg_codes)})"
            # 字符串方法 → 对应的内建函数：s.upper() → 转大写(s)
            _STR_METHODS = {
                "upper": "upper",
                "lower": "lower",
                "strip": "strip",
                "lstrip": "lstrip",
                "rstrip": "rstrip",
                "replace": "replace",
                "split": "split",
                "join": "join_list",
                "startswith": "startswith",
                "endswith": "endswith",
                "find": "find",
                "rfind": "rfind",
                "isdigit": "isdigit",
                "isalpha": "isalpha",
                "count": "count",
                "title": "title",
                "zfill": "zfill",
                "ljust": "ljust",
                "rjust": "rjust",
                "center": "center",
                "removeprefix": "removeprefix",
                "removesuffix": "removesuffix",
                "swapcase": "swapcase",
                "capitalize": "capitalize",
                "islower": "islower",
                "isupper": "isupper",
                "isspace": "isspace",
                "istitle": "istitle",
                "isdecimal": "isdecimal",
                "isnumeric": "isnumeric",
                "splitlines": "splitlines",
                "partition": "partition",
                "rpartition": "rpartition",
                "casefold": "casefold",
                "expandtabs": "expandtabs",
            }
            if meth in _STR_METHODS:
                if e.keywords:
                    raise Blocked(
                        f"字符串方法 `.{meth}()` 带了关键字实参，暂时不能导："
                        "AGL 内建函数按位置传参，请改写成位置实参"
                        f"（比如 `.replace('-', '+')`）")
                canon = _STR_METHODS[meth]
                receiver_code = self.expr(receiver)
                args_code = ", ".join(self.expr(a) for a in e.args)
                if args_code:
                    return f"{self.builtin_of(canon)}({receiver_code}, {args_code})"
                return f"{self.builtin_of(canon)}({receiver_code})"
            # d.keys() → 键列表(d)：映射表按键列表（与 list(d) 同语义，Python 返回视图）
            if meth == "keys":
                if e.args or e.keywords:
                    raise Blocked("`d.keys()` 不接受参数")
                return f"{self.builtin_of('keys')}({self.expr(receiver)})"
            # d.items() → 键值对列表(d)：每项是 [键, 值]（Python 返回视图）
            if meth == "items":
                if e.args or e.keywords:
                    raise Blocked("`d.items()` 不接受参数")
                return f"{self.builtin_of('items')}({self.expr(receiver)})"
            # d.setdefault(k, v) → 默认键(d, k, v)；缺 v 时补 空（Python setdefault(k) = None）
            if meth == "setdefault":
                if e.keywords or len(e.args) > 2 or not e.args:
                    raise Blocked(
                        "`d.setdefault(k, v)` 只接受一到两个位置实参")
                code = f"{self.builtin_of('setdefault')}({self.expr(receiver)}, {self.expr(e.args[0])}"
                if len(e.args) == 2:
                    code += f", {self.expr(e.args[1])}"
                else:
                    code += f", {self.kw_of('Null')}"
                return code + ")"
            # xs.index(x)（表达式位置）→ 取到(元素位置(xs, x))：
            # 找不到时 Python 抛 ValueError、AGL 在 取到 处抛中文错误，语义等价。
            # （赋值位置 `i = xs.index(x)` 在上方 Assign 分支处理。）
            if meth == "index":
                if len(e.args) != 1 or e.keywords:
                    raise Blocked("`xs.index(x)` 只接受一个位置参数")
                return (f"{self.builtin_of('unwrap')}"
                        f"({self.builtin_of('index')}"
                        f"({self.expr(receiver)}, {self.expr(e.args[0])}))")
            # d.values() → 值列表(d)（Python 返回视图）
            if meth == "values":
                if e.args or e.keywords:
                    raise Blocked("`d.values()` 不接受参数")
                return f"{self.builtin_of('values')}({self.expr(receiver)})"
            # "{} {}".format(1, 2) → 填充("{} {}", 1, 2)：
            # AGL 的 填充 就是 Python str.format 的 {} 占位语义，直接对齐。
            if meth == "format" and isinstance(receiver, ast.Constant) \
                    and isinstance(receiver.value, str):
                return self._str_format(receiver, e)
            # dict.get(k) / dict.get(k, 默认) → 取值(d, k[, 默认])
            # （取值 是 v0.22 内建：键不存在返回默认值，与 Python d.get 一致）
            if meth == "get" and not (
                    isinstance(receiver, ast.Name) and receiver.id == "requests"):
                if len(e.args) not in (1, 2) or e.keywords:
                    raise Blocked(
                        "`d.get(k)` / `d.get(k, 默认)` 只接受一或两个位置参数")
                recv_code = self.expr(receiver)
                args_code = ", ".join(self.expr(a) for a in e.args)
                if args_code:
                    return f"{self.builtin_of('get')}({recv_code}, {args_code})"
                return f"{self.builtin_of('get')}({recv_code})"
            # time.sleep(x) → 等待 睡眠(取整(x * 1000))
            if meth == "sleep" and isinstance(receiver, ast.Name) and receiver.id == "time":
                if len(e.args) != 1:
                    raise Blocked("`time.sleep(x)` 只接受一个参数")
                arg_code = self.expr(e.args[0])
                self.warnings.append(
                    "`time.sleep(x)` 被翻译成 `等待 睡眠(取整(x * 1000))`："
                    "Python 的单位是秒，AGL 的单位是毫秒，已自动换算"
                    "（亚毫秒部分被截断，高精度计时请改用 当前时间戳）")
                return (f"{self.kw_of('await')} {self.builtin_of('sleep')}"
                        f"({self.builtin_of('trunc')}({arg_code} * 1000))")
            # asyncio.sleep(s) → 睡眠(取整(s * 1000))（与 time.sleep 同款秒→毫秒换算，
            # 但 asyncio.sleep 本身是协程，`等待` 前缀由外层 Await 表达式提供——
            # Python 里它本来就必须 await，不自带前缀，避免双 等待）
            if meth == "sleep" and isinstance(receiver, ast.Name) and receiver.id == "asyncio":
                if len(e.args) != 1:
                    raise Blocked("`asyncio.sleep(s)` 只接受一个参数")
                arg_code = self.expr(e.args[0])
                self.warnings.append(
                    "`asyncio.sleep(s)` 被翻译成 `睡眠(取整(s * 1000))`："
                    "Python 的单位是秒，AGL 的单位是毫秒，已自动换算"
                    "（亚毫秒部分被截断，高精度计时请改用 当前时间戳）")
                return (f"{self.builtin_of('sleep')}"
                        f"({self.builtin_of('trunc')}({arg_code} * 1000))")
            # asyncio.run(main()) → main()（v0.39）：AGL 运行时对异步入口
            # 自动驱动事件循环（asyncio.run(_agl_main_wrap(main()))），
            # 原 Python 里显式的 asyncio.run 包一层在这里是多余的。
            if meth == "run" and isinstance(receiver, ast.Name) and receiver.id == "asyncio":
                if len(e.args) != 1 or e.keywords:
                    raise Blocked(
                        "`asyncio.run(x)` 只接受一个位置参数（协程对象）；"
                        "AGL 的并发由 `任务` 内建承担，请改写")
                self.warnings.append(
                    "`asyncio.run(main())` 被直译成调用 `main()`："
                    "AGL 运行时对异步入口自动驱动事件循环，不需要 asyncio.run 包一层")
                return self.expr(e.args[0])
            # time.time() / perf_counter() / monotonic() → 当前时间戳()
            if (meth in ("time", "perf_counter", "monotonic")
                    and isinstance(receiver, ast.Name) and receiver.id == "time"):
                if len(e.args) != 0:
                    raise Blocked(f"`time.{meth}()` 不接受参数")
                if meth != "time":
                    self.warnings.append(
                        f"`time.{meth}()` 被翻译成 `当前时间戳()`："
                        "AGL 的 当前时间戳 返回墙钟时间（从 1970 年起的秒数），"
                        f"{meth} 是单调时钟，测耗时场景基本等价，但两边的时钟语义不同，请确认")
                return f"{self.builtin_of('now')}()"
            # math.ceil(x) / math.floor(x) → 上取整(x) / 下取整(x)
            if (meth in ("ceil", "floor")
                    and isinstance(receiver, ast.Name) and receiver.id == "math"):
                if len(e.args) != 1:
                    raise Blocked(f"`math.{meth}(x)` 只接受一个参数")
                canon = "ceil" if meth == "ceil" else "floor"
                return f"{self.builtin_of(canon)}({self.expr(e.args[0])})"
            # random.randint(a, b) → 随机整数(a, b)；random.choice(xs) → 随机选择(xs)
            if (meth in ("randint", "choice", "seed")
                    and isinstance(receiver, ast.Name) and receiver.id == "random"):
                if meth == "randint":
                    if len(e.args) != 2:
                        raise Blocked("`random.randint(a, b)` 只接受两个参数")
                    return (f"{self.builtin_of('randint')}("
                            f"{self.expr(e.args[0])}, {self.expr(e.args[1])})")
                if meth == "choice":
                    if len(e.args) != 1:
                        raise Blocked("`random.choice(xs)` 只接受一个参数")
                    return f"{self.builtin_of('choice')}({self.expr(e.args[0])})"
                if meth == "seed":
                    if len(e.args) != 1:
                        raise Blocked("`random.seed(n)` 只接受一个参数")
                    return f"{self.builtin_of('seed')}({self.expr(e.args[0])})"
            # time.strftime(fmt) → 格式化时间(fmt)
            if (meth == "strftime"
                    and isinstance(receiver, ast.Name) and receiver.id == "time"):
                if len(e.args) != 1:
                    raise Blocked("`time.strftime(fmt)` 只接受一个格式参数")
                return f"{self.builtin_of('strftime')}({self.expr(e.args[0])})"
            # v0.48：math 模块函数 → 对应内建
            if isinstance(receiver, ast.Name) and receiver.id == "math":
                math_map = {
                    "floor": "向下取整",
                    "ceil": "向上取整",
                    "abs": "绝对值",
                    "pow": "乘方",
                    "sqrt": None,  # 特殊处理：用 pow(x, 0.5)
                }
                if meth in math_map:
                    if meth == "sqrt":
                        if len(e.args) != 1:
                            raise Blocked("`math.sqrt(x)` 只接受一个参数")
                        return f"{self.builtin_of('乘方')}({self.expr(e.args[0])}, 0.5)"
                    builtin_name = math_map[meth]
                    args_code = ", ".join(self.expr(a) for a in e.args)
                    return f"{self.builtin_of(builtin_name)}({args_code})"
            # v0.48：json 模块函数 → 对应内建
            if isinstance(receiver, ast.Name) and receiver.id == "json":
                json_map = {
                    "loads": "解析JSON",
                    "dumps": "生成JSON",
                }
                if meth in json_map:
                    builtin_name = json_map[meth]
                    args_code = ", ".join(self.expr(a) for a in e.args)
                    return f"{self.builtin_of(builtin_name)}({args_code})"
            # v0.48：常见字符串/列表/字典方法 → 对应内建函数
            str_methods = {
                "split": "切分",
                "strip": "去空白",
                "lstrip": "去左空",
                "rstrip": "去右空",
                "replace": "替换",
                "upper": "转大写",
                "lower": "转小写",
                "startswith": "以开头",
                "endswith": "以结尾",
                "isdigit": "是数字",
                "isalpha": "是字母",
                "isspace": "是空白",
                "join": "拼接",
                "count": "计数",
                "find": "找位置",
                "capitalize": "首字母大写",
                "title": "标题化",
                "center": "居中",
                "zfill": "补零",
                "isupper": "是大写",
                "islower": "是小写",
            }
            if meth in str_methods:
                builtin_name = str_methods[meth]
                receiver_code = self.expr(receiver)
                args_code = ", ".join(self.expr(a) for a in e.args)
                if args_code:
                    return f"{self.builtin_of(builtin_name)}({receiver_code}, {args_code})"
                else:
                    return f"{self.builtin_of(builtin_name)}({receiver_code})"
            
            list_methods = {
                "append": "追加",
                "pop": "弹出",
                "index": "下标",
                "count": "计数",
                "reverse": "反转",
                "sort": "排序",
                "extend": "扩展",
                "insert": "插入",
                "clear": "清空",
                "remove": "移除元素",
            }
            if meth in list_methods:
                builtin_name = list_methods[meth]
                receiver_code = self.expr(receiver)
                args_code = ", ".join(self.expr(a) for a in e.args)
                if args_code:
                    return f"{self.builtin_of(builtin_name)}({receiver_code}, {args_code})"
                else:
                    return f"{self.builtin_of(builtin_name)}({receiver_code})"
            
            dict_methods = {
                "get": "取键",
                "keys": "键列表",
                "values": "值列表",
                "items": "键值对",
                "pop": "弹出键",
                "update": "合并映射",
            }
            if meth in dict_methods:
                builtin_name = dict_methods[meth]
                receiver_code = self.expr(receiver)
                args_code = ", ".join(self.expr(a) for a in e.args)
                if args_code:
                    return f"{self.builtin_of(builtin_name)}({receiver_code}, {args_code})"
                else:
                    return f"{self.builtin_of(builtin_name)}({receiver_code})"
            
            set_methods = {
                "add": "添加",
                "union": "并集",
                "intersection": "交集",
                "difference": "差集",
                "issubset": "是子集",
                "issuperset": "是超集",
                "clear": "清空",
                "remove": "移除元素",
                "discard": "移除元素",
            }
            if meth in set_methods:
                builtin_name = set_methods[meth]
                receiver_code = self.expr(receiver)
                args_code = ", ".join(self.expr(a) for a in e.args)
                if args_code:
                    return f"{self.builtin_of(builtin_name)}({receiver_code}, {args_code})"
                else:
                    return f"{self.builtin_of(builtin_name)}({receiver_code})"
            
            if meth == "append":
                raise Blocked(
                    "这里没法自动拆的 `列表.append(x)`：它在更深层的表达式里，"
                    "且接收者不是简单变量/下标。请先手动拆成 `列表 = 追加(列表, x)`"
                    "再单独使用它的值")
            # v0.48：自定义类方法调用——obj.method() → 类名_method(obj, ...)
            if isinstance(receiver, ast.Name):
                var_name = receiver.id
                if var_name in self.instances:
                    cls_name = self.instances[var_name]
                    if cls_name in self.classes:
                        # 找到了！是自定义类的实例
                        method_name = meth
                        # 检查这个方法是不是真的存在于类里
                        if method_name in self.classes[cls_name]["methods"]:
                            receiver_code = self.expr(receiver)
                            args_code = ", ".join(self.expr(a) for a in e.args)
                            method_func = f"{cls_name}_{method_name}"
                            if args_code:
                                return f"{method_func}({receiver_code}, {args_code})"
                            else:
                                return f"{method_func}({receiver_code})"
                        else:
                            self.warnings.append(
                                f"方法 `{cls_name}.{method_name}` 不存在——简化版，"
                                "假设它存在，翻译成对应的函数调用")
                            receiver_code = self.expr(receiver)
                            args_code = ", ".join(self.expr(a) for a in e.args)
                            method_func = f"{cls_name}_{method_name}"
                            if args_code:
                                return f"{method_func}({receiver_code}, {args_code})"
                            else:
                                return f"{method_func}({receiver_code})"
            raise Blocked(
                f"方法调用 `.{meth}()` 暂时不能导："
                f"AGL 现阶段没有对象方法这一层")
        raise Blocked("这种调用写法暂时不能导")

    def _pct_format(self, e: ast.BinOp) -> str:
        """`"%s-%d" % ("x", 5)` → 填充("{}-{}", "x", 5)。

        v0.29 起支持带修饰的占位符（与 Python `%` 格式化逐字对齐）：
          · %s / %d / %f（无修饰）→ 填充 的 {} 占位（渲染规则与 %s 的 str() 一致）
          · %05d / %5d → 补零 / 右对齐（先 转文本）
          · %.Nf / %05.2f → 格式化数字
          · %Ns / %-Ns → 右对齐 / 左对齐
        混合形态（`"%s-%05d" % (x, y)`）→ 逐段 拼接：文本段字面量 + 无修饰段
        转文本 + 修饰段内建调用。
        `%%` 精确拒绝（提示直接写百分号）；参数个数多/少精确拒绝。
        参数必须是常量格式化串；右侧单值或元组/列表展开成位置实参。
        """
        import re
        fmt = e.left.value
        rhs = e.right
        if isinstance(rhs, (ast.Tuple, ast.List)):
            args = list(rhs.elts)
        else:
            args = [rhs]
        # 段列表：("lit", 文本) 或 ("plain", 实参表达式) 或 ("conv", 内建调用)
        segs: list[tuple[str, str]] = []
        param_i = 0
        i = 0
        n = len(fmt)
        while i < n:
            ch = fmt[i]
            if ch != "%":
                lit_start = i
                while i < n and fmt[i] != "%":
                    i += 1
                segs.append(("lit", fmt[lit_start:i]))
                continue
            if i + 1 >= n:
                raise Blocked("`%` 格式化的格式串以单个 `%` 结尾，Python 也会报错")
            nxt = fmt[i + 1]
            if nxt == "%":
                # v0.31：`%%` → 字面百分号（Python 里 `"100%%"` → "100%"）
                segs.append(("lit", "%"))
                i += 2
                continue
            m = re.match(r"(?P<flags>[-0]?)(?P<width>\d*)(?:\.(?P<prec>\d*))?(?P<kind>[sdf])",
                         fmt[i + 1:])
            if not m:
                raise Blocked("`%` 格式化的占位符不认识（只支持 %s/%d/%f 及修饰）")
            flag, width, prec, kind = (m.group("flags"), m.group("width"),
                                       m.group("prec"), m.group("kind"))
            if param_i >= len(args):
                raise Blocked("`%` 格式化的参数个数比占位符少，Python 也会报错")
            arg = args[param_i]
            param_i += 1
            i += 1 + len(m.group(0))
            # 无修饰：占位符交给 填充 渲染
            if not flag and not width and prec is None:
                segs.append(("plain", self.expr(arg)))
                continue
            # 带修饰：翻译成独立内建调用
            arg_code = self.expr(arg)
            conv = None
            if kind == "d":
                to_text = f"{self.builtin_of('to_text')}({arg_code})"
                if flag == "0" and width:
                    conv = f"{self.builtin_of('zfill')}({to_text}, {int(width)})"
                elif flag == "-" and width:
                    conv = f"{self.builtin_of('ljust')}({to_text}, {int(width)})"
                elif width:
                    conv = f"{self.builtin_of('rjust')}({to_text}, {int(width)})"
                else:
                    raise Blocked(
                        f"`%{flag}{width}{kind}` 这种修饰暂时不能导："
                        "请用 `补零` / `左对齐` / `右对齐` 组合")
            elif kind == "f":
                prec_v = prec if prec != "" else "6" if prec is None else prec
                if prec is None:
                    prec_v = "6"
                elif prec == "":
                    prec_v = "0"
                else:
                    prec_v = prec
                num_args = [arg_code, prec_v]
                if width:
                    num_args.append(width)
                if flag == "0":
                    num_args.append(self.kw_of("true"))
                conv = f"{self.builtin_of('format_number')}({', '.join(num_args)})"
            else:  # s
                if flag == "-" and width:
                    conv = f"{self.builtin_of('ljust')}({arg_code}, {int(width)})"
                elif width:
                    conv = f"{self.builtin_of('rjust')}({arg_code}, {int(width)})"
                elif flag == "0":
                    raise Blocked(
                        "`%0Ns` 的零填充对文本没有定义（Python 也会报错）")
                else:
                    raise Blocked(
                        f"`%{flag}{width}{kind}` 这种修饰暂时不能导："
                        "请用 `左对齐` / `右对齐` 组合")
            segs.append(("conv", conv))
        if param_i < len(args):
            raise Blocked("`%` 格式化的参数个数比占位符多，Python 也会报错")
        # 全部是纯文本（没有占位符）→ 直接给文本
        if not any(k != "lit" for k, _ in segs):
            raise Blocked("`%` 格式化的格式串里没有 `%`，请直接拼接文本")
        self.warnings.append(
            "`%` 格式化被翻译成 `填充(...)` / `拼接(...)`：占位符按 Python `%` "
            "语义映射到 AGL 内建（%s 走 填充、%05d 走 补零、%.Nf 走 格式化数字、"
            "%Ns 走 右对齐/左对齐），与 Python 逐字对齐")
        if all(k != "conv" for k, _ in segs):
            tmpl = "".join("{}" if k == "plain" else v for k, v in segs)
            tmpl_code = self.literal(ast.Constant(value=tmpl))
            call_args = ", ".join(v for k, v in segs if k == "plain")
            return f"{self.builtin_of('fill')}({tmpl_code}, {call_args})"
        parts: list[str] = []
        for k, v in segs:
            if k == "lit":
                if v:
                    parts.append(self.literal(ast.Constant(value=v)))
            elif k == "plain":
                parts.append(f"{self.builtin_of('to_text')}({v})")
            else:
                parts.append(v)
        if not parts:
            return '""'
        if len(parts) == 1:
            return parts[0]
        return f"{self.builtin_of('join')}({', '.join(parts)})"

    def _str_format(self, receiver: ast.Constant, e: ast.Call) -> str:
        """`"{} {}".format(1, 2)` → 填充("{} {}", 1, 2)。

        v0.29 起支持 Python str.format 的全部占位符形态：
          · `{}` 空占位 → 按序取位置实参
          · `{0}` `{1}` 下标占位 → 取位置实参第 N 个（可乱序复用）
          · `{name}` 命名占位 → 取关键字实参（按格式串出现顺序重排）
          · 带格式说明 `{:.2f}` `{:05d}` `{:10s}` → 复用 f-string 的格式映射
            （格式化数字 / 补零 / 右对齐 / 左对齐）
        全无格式 → 填充(重排后的 {} 模板, 重排实参)；混有格式 → 逐段 拼接。
        转义 `{{` / `}}` 会先被本方法展开（Python 里它是字面量花括号）。
        """
        import re
        tmpl = receiver.value
        # Python str.format 里 `{{` 渲染成字面 `{`、`}}` 渲染成 `}`：
        # 先换成哨兵避免被占位符正则吃掉，解析后再还原（AGL 填充的 `{}`
        # 是占位符、字面花括号直接写即可，无需转义）。
        tmpl = tmpl.replace("{{", "\x01").replace("}}", "\x02")
        def _unescape(s: str) -> str:
            return s.replace("\x01", "{").replace("\x02", "}")
        if "{" not in tmpl and "}" not in tmpl:
            if e.args or e.keywords:
                raise Blocked("`format()` 的模板里没有占位符却传了参数，Python 也会报错")
            return self.literal(ast.Constant(value=_unescape(tmpl)))
        pos_args = list(e.args)
        pos_kw = [kw for kw in e.keywords if kw.arg is not None]
        kw_args = {kw.arg: kw.value for kw in pos_kw}
        star_kw = [kw.value for kw in e.keywords if kw.arg is None]
        if len(star_kw) > 1:
            raise Blocked("`format()` 的多个 `**` 展开暂时不能导")
        d_node = star_kw[0] if star_kw else None
        # **d 展开：命名占位符先从关键字参数取，没取到就从 d 映射里取（d["name"]）。
        # Python 里 **d 多余的键被忽略，这里不检查（与 Python 一致）。
        def _named(name, out_extra):
            if name in kw_args:
                out_extra.append(("kw", kw_args.pop(name)))
            elif d_node is not None:
                out_extra.append(("d", name))
            else:
                raise Blocked(
                    f"`format()` 的命名占位符 `{{{name}}}` 没有对应的"
                    "关键字参数或 `**` 展开里的键，Python 也会报错")
        used_pos: set[int] = set()
        segs: list[tuple[str, str]] = []  # ("lit", 文本) / ("plain", 实参码) / ("conv", 内建调用)

        def _field_arg(body_field: str):
            """占位符字段 → AGL 实参表达式（位置下标 / 命名 / **d 键）。"""
            if body_field == "":
                idx = len(used_pos)
                while idx in used_pos:
                    idx += 1
                if idx >= len(pos_args):
                    raise Blocked(
                        f"`format()` 的占位符越界：只有 {len(pos_args)} 个"
                        "位置参数，Python 也会报错")
                used_pos.add(idx)
                return self.expr(pos_args[idx])
            if body_field.isdigit():
                idx = int(body_field)
                if idx >= len(pos_args):
                    raise Blocked(
                        f"`format()` 的占位符 `{{{idx}}}` 越界：只有"
                        f" {len(pos_args)} 个位置参数，Python 也会报错")
                used_pos.add(idx)
                return self.expr(pos_args[idx])
            name = body_field
            extra = []
            _named(name, extra)
            if extra[0][0] == "kw":
                return self.expr(extra[0][1])
            return (f'{self.expr(d_node)}'
                    f'[{self.literal(ast.Constant(value=name))}]')

        def _spec_code(spec: str):
            """格式说明 → 静态映射或动态 spec 拼接（嵌套 {inner} 递归求值）。"""
            if "{" not in spec and "}" not in spec:
                return None
            spec_segs: list[tuple[str, str]] = []
            j = 0
            while j < len(spec):
                if spec[j] == "{":
                    d2 = 1
                    k = j + 1
                    while k < len(spec) and d2:
                        if spec[k] == "{":
                            d2 += 1
                        elif spec[k] == "}":
                            d2 -= 1
                        k += 1
                    inner = spec[j + 1:k - 1]
                    inner_arg = _field_arg(inner)
                    spec_segs.append(("var", f"{self.builtin_of('to_text')}({inner_arg})"))
                    j = k
                else:
                    l0 = j
                    while j < len(spec) and spec[j] != "{":
                        j += 1
                    spec_segs.append(
                        ("lit", self.literal(ast.Constant(value=spec[l0:j]))))
            if len(spec_segs) == 1:
                return spec_segs[0][1]
            return (f"{self.builtin_of('join')}"
                    f"({', '.join(c for _, c in spec_segs)})")

        # 递归解析模板：支持嵌套字段 {v:{w}}（外层 {…} 配对匹配）
        i = 0
        n_tmpl = len(tmpl)
        while i < n_tmpl:
            ch = tmpl[i]
            if ch == "{":
                d1 = 1
                j = i + 1
                while j < n_tmpl and d1:
                    if tmpl[j] == "{":
                        d1 += 1
                    elif tmpl[j] == "}":
                        d1 -= 1
                    j += 1
                body = tmpl[i + 1:j - 1]
                if ":" in body:
                    field, spec = body.split(":", 1)
                    arg_code = _field_arg(field)
                    dyn = _spec_code(spec)
                    if dyn is not None:
                        segs.append(("conv", f"{self.builtin_of('format_value')}"
                                             f"({arg_code}, {dyn})"))
                    else:
                        segs.append(("conv",
                                     self._format_spec_conversion(spec, arg_code)))
                else:
                    arg_code = _field_arg(body)
                    segs.append(("plain", arg_code))
                i = j
            else:
                l1 = i
                while i < n_tmpl and tmpl[i] != "{":
                    i += 1
                lit = _unescape(tmpl[l1:i])
                if lit:
                    segs.append(("lit", lit))
        if len(used_pos) != len(pos_args):
            raise Blocked(
                "`format()` 的位置参数没用完（模板占位符比参数少），Python 也会报错")
        if kw_args:
            raise Blocked(
                f"`format()` 的关键字参数 `{', '.join(kw_args)}` 没用上"
                "（模板里没有对应占位符），Python 也会报错")
        self.warnings.append(
            "`format()` 被翻译成 `填充(...)` / `拼接(...)`：`{}` 与 `{0}` 下标与"
            "命名占位符按出现顺序重排成位置实参，格式说明映射到 格式化数字/补零/"
            "右对齐/左对齐，与 Python 逐字对齐")
        if all(k != "conv" for k, _ in segs):
            new_tmpl = "".join("{}" if k == "plain" else v for k, v in segs)
            tmpl_code = self.literal(ast.Constant(value=new_tmpl))
            call_args = ", ".join(v for k, v in segs if k == "plain")
            return f"{self.builtin_of('fill')}({tmpl_code}, {call_args})"
        parts: list[str] = []
        for k, v in segs:
            if k == "lit":
                if v:
                    parts.append(self.literal(ast.Constant(value=v)))
            elif k == "plain":
                parts.append(f"{self.builtin_of('to_text')}({v})")
            else:
                parts.append(v)
        if not parts:
            return '""'
        if len(parts) == 1:
            return parts[0]
        return f"{self.builtin_of('join')}({', '.join(parts)})"

    def _format_spec_conversion(self, spec: str, arg_code: str) -> str:
        """str.format / f-string 的格式说明 → AGL 内建调用。

        v0.30 起支持：
          · `[0][宽度][.N]f` → 格式化数字
          · `0宽度d` → 补零（整数）
          · `[对齐][宽度]d` / `[对齐][宽度]s` → 右对齐/左对齐/居中 + 转文本
          · 进制 x/X/o/O/b/B：带 `#` → 十六进制/八进制/二进制（带前缀）；
            无 `#` → 去前缀(进制(x), 前缀)（f"{255:x}" → "ff"）；
            大写类型 → 转大写
        [fill]fill/`=` 对齐、符号（+/-/空格）、逗号千分位、科学 e/g 精确拒绝。
        """
        import re
        # 通用形态：([<>^])?([#])?([0])?(宽度)?([.精度])?(类型)？ —— type 缺省按 s（字符串化）
        m = re.match(r"^(?P<align>[<>^])?(?P<alt>#)?(?P<zero>0)?(?P<width>\d*)"
                     r"(?:\.(?P<prec>\d*))?(?P<type>[sd]|[xXoObB]|f)?$", spec)
        if not m:
            # v0.31：复杂形态（`=` 对齐、自定义填充、符号、千分位、科学计数等）
            # 全部交给 按格式 原生透传（Python format 语义，spec 是常量文本）
            return f"{self.builtin_of('format_value')}(" \
                   f"{arg_code}, {self.literal(ast.Constant(value=spec))})"
        align, alt, zero, width_s, prec_s, kind = (
            m.group("align"), m.group("alt"), m.group("zero"),
            m.group("width"), m.group("prec"), m.group("type"))
        if kind is None:
            kind = "s"
        # 进制类型：x X o O b B
        if kind in ("x", "X", "o", "O", "b", "B"):
            canon = {"x": "hex", "o": "oct", "b": "bin"}[kind.lower()]
            prefix = {"x": "0x", "o": "0o", "b": "0b"}[kind.lower()]
            base_call = f"{self.builtin_of(canon)}({arg_code})"
            if not alt:
                base_call = (f"{self.builtin_of('removeprefix')}"
                             f"({base_call}, {self.literal(ast.Constant(value=prefix))})")
            if kind.isupper():
                base_call = f"{self.builtin_of('upper')}({base_call})"
            if width_s:
                if align == "<":
                    base_call = f"{self.builtin_of('ljust')}({base_call}, {width_s})"
                elif align == "^":
                    base_call = f"{self.builtin_of('center')}({base_call}, {width_s})"
                elif align == ">" or not align:
                    base_call = f"{self.builtin_of('rjust')}({base_call}, {width_s})"
            if zero and not align:
                base_call = f"{self.builtin_of('zfill')}({base_call}, {width_s})"
            return base_call
        # f 类型
        if kind == "f":
            if align:
                raise Blocked(
                    f"格式说明 `{spec}` 暂时不能导：浮点格式不能带对齐符号，"
                    "请用 `格式化数字` 后自行 右对齐/左对齐")
            if prec_s is None:
                prec = "6"
            elif prec_s == "":
                prec = "0"
            else:
                prec = prec_s
            num_args = [arg_code, prec]
            if width_s:
                num_args.append(width_s)
            if zero:
                num_args.append(self.kw_of("true"))
            return f"{self.builtin_of('format_number')}({', '.join(num_args)})"
        # d / s 类型：对齐/宽度/补零都要先把参数转成文本
        # （Python 的格式说明会自动 str()：`{7:>5}` → "    7"）
        to_text = (f"{self.builtin_of('to_text')}({arg_code})"
                   if kind in ("d", "s") and (width_s or align or zero)
                   else arg_code)
        if zero and not align:
            if not width_s:
                raise Blocked(
                    f"格式说明 `{spec}` 暂时不能导：`0` 补零必须有宽度")
            return f"{self.builtin_of('zfill')}({to_text}, {width_s})"
        if align == "<":
            if not width_s:
                raise Blocked(f"格式说明 `{spec}` 暂时不能导：对齐必须有宽度")
            return f"{self.builtin_of('ljust')}({to_text}, {width_s})"
        if align == "^":
            if not width_s:
                raise Blocked(f"格式说明 `{spec}` 暂时不能导：对齐必须有宽度")
            return f"{self.builtin_of('center')}({to_text}, {width_s})"
        if width_s:
            return f"{self.builtin_of('rjust')}({to_text}, {width_s})"
        # 无格式修饰：s 原样、d 转文本
        return to_text

    def _sorted_key(self, k: ast.AST) -> str:
        """`sorted(xs, key=...)` 的 key 表达式 → AGL 里可作为实参的函数名。

        只放行两种：本文件里定义的函数名（AGL 命名函数在产物里就是普通
        Python 函数，可直接传给 按键排序）；以及 lambda（提升成命名函数，
        `_lift_lambda` 会把函数行存进 lifted_fns，不会污染当前输出行；
        提升名在原 Python AST 里从不作为 Name 出现，天然豁免
        「lambda 不能当值转出」的检查）。
        """
        if isinstance(k, ast.Name):
            # v0.48：key=len → 自动提升成 _键N(x) { 返回 x.长度 }
            if k.id == "len":
                name = self._fresh_name("_键")
                sig = f"函数 {name}(_x: 任意) -> 任意 {{"
                body = [f"  返回 _x.{self.attr_of('length')}", "}"]
                self.lifted_fns.append((sig, body))
                return self.check_name(name, "排序键函数")
            name = self.check_name(k.id, "排序键函数")
            if k.id not in {f.name for f in self.funcs}:
                raise Blocked(
                    f"`sorted(xs, key={k.id})` 的 key 函数不在本文件里定义："
                    "AGL 没有函数值，按键排序的键函数必须是本文件里的命名函数；"
                    "从别的模块借函数请先复制进来")
            return name
        if isinstance(k, ast.Lambda):
            name = self._fresh_name("_键")
            self._lift_lambda(name, k)
            return self.check_name(name, "排序键函数")
        if isinstance(k, ast.Attribute):
            # 类型内建方法：`str.lower` / `str.upper` / `str.strip` 是作用于
            # 每个元素的未绑定方法，AGL 有对应内建（转小写/转大写/去空白），
            # 直接提升成无闭包键函数 `_键N(x) { 返回 转小写(x) }`，逐字等价。
            if (isinstance(k.value, ast.Name) and k.value.id == "str"):
                canon = {"lower": "lower", "upper": "upper", "strip": "strip"}.get(k.attr)
                if canon is not None:
                    name = self._fresh_name("_键")
                    x = self._fresh_name("_x")
                    sig = (f"{self.kw_of('fn')} {name}({x}: {self.kw_of('Any')})"
                           f" -> {self.kw_of('Any')}")
                    # 与 _lift_lambda 同款：block_lines 完整包含签名行 + 体行，
                    # 消费处 extend 即得到完整函数块
                    body = [f"{sig} {{",
                            f"  {self.kw_of('return')} {self.builtin_of(canon)}({x})",
                            "}"]
                    self.lifted_fns.append((sig, body))
                    # 记入提升表：提升名在原 AST 里从不作为 Name 出现，
                    # _scan_lambda_uses 天然豁免；调用点也不限函数内
                    self.lambda_def_fn[name] = self.cur_fn
                    self.lambda_captures[name] = {"fn": name, "captures": []}
                    return name
            raise Blocked(
                "`sorted(xs, key=obj.method)` 的 key 是绑定方法：AGL 没有函数值"
                "也没有闭包，绑定对象传不进键函数；请先定义普通函数，"
                "或改用 `str.lower` 这类直接映射到内建的写法")
        raise Blocked(
            "`sorted(xs, key=...)` 的 key 只支持「本文件里的函数名」「lambda」"
            "或 `str.lower` / `str.upper` / `str.strip`："
            "AGL 没有函数值，复杂表达式传不进来；请先定义普通函数再用它的名字")

    def _fresh_name(self, prefix: str) -> str:
        i = 0
        while (f"{prefix}{i}" in self.declared
               or f"{prefix}{i}" in {f.name for f in self.funcs}
               or f"{prefix}{i}" in self._fresh_used
               or f"{prefix}{i}" in self._nested_lifted_names):
            i += 1
        name = f"{prefix}{i}"
        self._fresh_used.add(name)
        return name

    # ── 语句 ───────────────────────────────────────────────────────
    def block(self, body: list[ast.stmt]) -> None:
        self.depth += 1
        if not body:
            # 空块 Python 不用 pass，AGL 的 { } 里可以什么都不放
            self.put("// （空）")
        for s in body:
            self.stmt(s)
        self.depth -= 1

    def _assign_one(self, t: ast.expr, rhs: str) -> None:
        """处理单个赋值目标：变量名 / 下标 / 成员。"""
        if isinstance(t, ast.Name):
            name = self.check_name(t.id, "变量")
            if t.id in self.mutable_globals:
                # 可变全局（v0.38）：写入模块级共享存储（_agl_globals），
                # 读/写同源（Python `global g; g = v` 语义）
                self.put(f"{self.globals_store}[\"{t.id}\"] = {rhs}")
                return
            if t.id not in self.declared:
                self.declared.add(t.id)
                head = self.kw_of("let")
            else:
                head = ""
            self.put(f"{head} {name} = {rhs}".strip())
        elif isinstance(t, (ast.Subscript, ast.Attribute)):
            # 下标赋值 xs[0] = v、成员赋值 obj.x = v：AGL 已原生支持。
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                owner_name = t.value.id
                # v0.41：classmethod 里 cls.x 等同 <当前类>.x
                if (self._cls_alias is not None
                        and owner_name == self._cls_alias
                        and self.cur_class is not None):
                    owner_name = self.cur_class
                # ① 可变类属性 C.x = v（v0.38）：写共享存储（读/写同源）
                if owner_name in self.classes and t.attr in self.classes[owner_name].get("attrs", {}):
                    if (owner_name, t.attr) in self.mutable_class_attrs:
                        self.put(f"{self.globals_store}[\"{owner_name}.{t.attr}\"] = {rhs}")
                        return
                    raise Blocked(
                        f"`{owner_name}.{t.attr} = ...`（给类属性赋值）暂时不能导："
                        "AGL 的类属性是只读共享值，没有「跨实例可变状态」这一层；"
                        "可变共享状态请用模块级函数参数显式传递")
                # ② property setter 分发：c.x = v（实例类型可静态确认 + 该类有 setter）
                if owner_name in self.instances:
                    inst_cls = self.instances[owner_name]
                    cinfo = self.classes.get(inst_cls, {})
                    if t.attr in cinfo.get("setters", {}):
                        recv = self.check_name(owner_name, "这里")
                        fn = self.check_name(f"{inst_cls}_{t.attr}_赋值", "setter 方法")
                        self.put(f"{fn}({recv}, {rhs})")
                        return
                    # 只读 property 赋值（无 setter）：Python 抛 AttributeError，
                    # AGL 塞键会静默覆盖 getter 值 → 精确拒绝
                    owner2, _ = self._resolve_method(inst_cls, t.attr)
                    if owner2 and t.attr in self.classes[owner2].get("properties", ()):
                        raise Blocked(
                            f"`{owner_name}.{t.attr} = ...` 是只读 property（没有 setter）："
                            "Python 赋值会抛 AttributeError，AGL 没有等价的错误语义，请改写")
            # v0.42：自定义对象下标赋值 a[i] = v → C_setitem(a, i, v)（非切片形态）
            if isinstance(t, ast.Subscript) and not isinstance(t.slice, ast.Slice):
                recv_cls = self._expr_instance_cls(t.value)
                if recv_cls is not None:
                    owner = self._resolve_dunder_owner(recv_cls, "__setitem__")
                    if owner is not None:
                        recv_code = self.expr(t.value)
                        idx_code = self.expr(t.slice)
                        fn = self.check_name(f"{owner}___setitem__", "下标赋值方法")
                        self.put(f"{fn}({recv_code}, {idx_code}, {rhs})")
                        return
            self.put(f"{self.expr(t)} = {rhs}")
        else:
            raise Blocked(f"这种赋值目标 `{type(t).__name__}` 暂时不能导")

    def _hoist_expressions(self, s: ast.stmt, pre: list[str]) -> ast.stmt:
        """把语句里表达式位置的 append / 生成器表达式提升成前置语句。

        - `xs.append(v)`（接收者是变量/下标）→ 生成 `xs = 追加(xs, v)`，
          原表达式求值为 空（Python 里 append 返回 None，AGL 的空就是它）；
        - `(i for i in ...)` → 展开成临时列表（AGL 没有惰性迭代），
          原表达式求值为临时列表。

        生成顺序 = Python 的求值顺序（从左到右、先内层后外层），
        因为 append 的副作用（改列表）会影响后面表达式的取值。
        """
        class _Hoister(ast.NodeTransformer):
            def __init__(self, imp):
                self.imp = imp

            def visit_Call(self, node):
                self.generic_visit(node)
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "append"
                        and len(node.args) == 1
                        and isinstance(node.func.value, (ast.Name, ast.Subscript))):
                    recv = node.func.value
                    recv_code = self.imp.expr(recv)
                    arg_code = self.imp.expr(node.args[0])
                    pre.append(f"{recv_code} = "
                               f"{self.imp.builtin_of('append')}({recv_code}, {arg_code})")
                    self.imp.warnings.append(
                        "表达式位置的 `列表.append(x)` 被翻译成"
                        "「先 `列表 = 追加(列表, x)`，再用 `空` 顶替它的值」："
                        "Python 里 append 原地改列表并返回 None，AGL 的 追加 返回新列表，"
                        "拆开后两边行为一致")
                    return ast.Constant(value=None)
                # 0 参是列表专属；1 参列表/映射歧义 → 按登记分发，未登记精确拒绝
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "pop"
                        and not node.keywords
                        and isinstance(node.func.value, (ast.Name, ast.Subscript))):
                    recv = node.func.value
                    recv_code = self.imp.expr(recv)
                    tmp = self.imp._fresh_name("_弹出值")
                    if len(node.args) == 0:
                        pop_call = f"{self.imp.builtin_of('pop')}({recv_code})"
                        reason = "xs.pop()"
                        dict_mode = False
                    elif len(node.args) == 2:
                        # 2 参 = 映射专属：d.pop(k, 默认)，键缺失返回默认，不会 Err
                        pk = (f"{self.imp.builtin_of('pop_key')}"
                              f"({recv_code}, {self.imp.expr(node.args[0])}, "
                              f"{self.imp.expr(node.args[1])})")
                        head = (f"{self.imp.kw_of('let')} "
                                if tmp not in self.imp.declared else "")
                        self.imp.declared.add(tmp)
                        pre.append(f"{head}{tmp} = {pk}[0]")
                        pre.append(f"{recv_code} = {pk}[1]")
                        self.imp.warnings.append(
                            "表达式位置的 `d.pop(k, 默认)` 被翻译成"
                            "「先 `变量 临时 = 弹出键(d, k, 默认)[0]` + "
                            "`d = 弹出键(d, k, 默认)[1]`，再用临时名顶替它的值」："
                            "键缺失时返回默认值，与 Python 语义一致")
                        return ast.Name(id=tmp)
                    else:
                        # 1 参：列表 xs.pop(i) / 映射 d.pop(k) —— 泛型 弹出 运行时分发
                        a_code = self.imp.expr(node.args[0])
                        pop_call = (f"{self.imp.builtin_of('pop')}"
                                    f"({recv_code}, {a_code})")
                        reason = "x.pop(p)"
                        self.imp.warnings.append(
                            "表达式位置的 `x.pop(p)` 被翻译成"
                            "「先 `变量 临时 = 弹出(x, p)[0]` + "
                            "`x = 弹出(x, p)[1]`，再用临时名顶替它的值」："
                            "弹出按实参类型运行时分发（列表按下标、映射按键），"
                            "与 Python 一致")
                        head = (f"{self.imp.kw_of('let')} "
                                if tmp not in self.imp.declared else "")
                        self.imp.declared.add(tmp)
                        pre.append(f"{head}{tmp} = {pop_call}[0]")
                        pre.append(f"{recv_code} = {pop_call}[1]")
                        return ast.Name(id=tmp)
                    head = f"{self.imp.kw_of('let')} " if tmp not in self.imp.declared else ""
                    self.imp.declared.add(tmp)
                    pre.append(f"{head}{tmp} = {pop_call}[0]")
                    pre.append(f"{recv_code} = {pop_call}[1]")
                    self.imp.warnings.append(
                        f"表达式位置的 `{reason}` 被翻译成"
                        "「先 `变量 临时 = 弹出(xs)[0]` + `xs = 弹出(xs)[1]`，"
                        "再用临时名顶替它的值」："
                        "Python 里 pop 原地改列表并返回弹出值，拆开后两边行为一致")
                    return ast.Name(id=tmp)
                # 表达式位置的 remove/insert/reverse/update/clear（Python 返回 None）：
                # 先重绑接收者，再用 空 顶替值。
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("remove", "insert", "reverse",
                                               "update", "clear")
                        and not node.keywords
                        and isinstance(node.func.value, (ast.Name, ast.Subscript))):
                    recv = node.func.value
                    recv_code = self.imp.expr(recv)
                    attr = node.func.attr
                    if attr == "remove" and len(node.args) == 1:
                        new_recv = (f"{self.imp.builtin_of('remove')}"
                                    f"({recv_code}, {self.imp.expr(node.args[0])})")
                    elif attr == "insert" and len(node.args) == 2:
                        new_recv = (f"{self.imp.builtin_of('insert')}"
                                    f"({recv_code}, {self.imp.expr(node.args[0])}, "
                                    f"{self.imp.expr(node.args[1])})")
                    elif attr == "reverse" and not node.args:
                        new_recv = f"{self.imp.builtin_of('reverse')}({recv_code})"
                    elif attr == "update" and len(node.args) == 1:
                        new_recv = (f"{self.imp.builtin_of('merge_map')}"
                                    f"({recv_code}, {self.imp.expr(node.args[0])})")
                    elif attr == "clear" and not node.args:
                        new_recv = f"{self.imp.builtin_of('clear')}({recv_code})"
                    else:
                        raise Blocked(
                            f"`{attr}(...)` 这种写法暂时不能导：参数个数不对")
                    pre.append(f"{recv_code} = {new_recv}")
                    self.imp.warnings.append(
                        f"表达式位置的 `x.{attr}(...)` 被翻译成"
                        "「先重绑 x，再用 `空` 顶替它的值」："
                        f"Python 里 `x.{attr}(...)` 返回 None，AGL 的空就是它")
                    return ast.Constant(value=None)
                return node

            def visit_GeneratorExp(self, node):
                name = f"__生成器{len(pre)}"
                lines = self.imp._comp_lines(name, node)
                # 提升出的临时列表是一个新变量：首行要补「变量」前缀并登记声明，
                # 否则后面所有引用它的地方都报「使用了没有声明的名字」。
                # （对齐整句赋值分支 `xs = (i for i in ...)` 的做法。）
                lines[0] = f"{self.imp.kw_of('let')} {lines[0]}".strip()
                self.imp.declared.add(name)
                pre.extend(lines)
                self.imp.warnings.append(
                    "表达式位置的生成器表达式被翻译成临时列表："
                    "AGL 没有惰性迭代，只能先展开成列表再使用")
                return ast.Name(id=name)

            def visit_IfExp(self, node):
                # 表达式位置的三元表达式（`f(a if b else c)` / `返回 a if b else c`）：
                # 提升成「变量 临时: 任意 = 空 + 如果/否则 两分支赋值」，再用临时名顶替。
                # Python 三元惰性求值（只算选中的分支），拆成 if/else 语句后
                # 求值时机、次数与顺序完全一致。
                self.generic_visit(node)
                tmp = self.imp._fresh_name("_三元")
                head = (f"{self.imp.kw_of('let')} " if tmp not in self.imp.declared else "")
                self.imp.declared.add(tmp)
                ind = _IND
                any_t = self.imp.kw_of("Any")
                null_t = self.imp.kw_of("Null")
                pre.append(f"{head}{tmp}: {any_t} = {null_t}")
                pre.append(f"{self.imp.kw_of('if')} ({self.imp.expr(node.test)}) {{")
                pre.append(f"{ind}{tmp} = {self.imp.expr(node.body)}")
                pre.append(f"}} {self.imp.kw_of('else')} {{")
                pre.append(f"{ind}{tmp} = {self.imp.expr(node.orelse)}")
                pre.append("}")
                self.imp.warnings.append(
                    "表达式位置的三元表达式（`a if b else c`）被翻译成"
                    "「变量 临时: 任意 = 空 + 如果/否则 两分支赋值」："
                    "Python 三元惰性求值，拆成 if/else 语句后求值时机一致")
                return ast.Name(id=tmp)

            # 复合语句里的表达式不在这里提升：循环/分支体内的 append、生成器
            # 由 stmt() 递归翻译时各走自己的专门分支。这里提升会把它们
            # 错误地挪到函数顶层、把原位置顶替成 空 常量（循环体被抽空）。
            def visit_For(self, node):
                return node

            def visit_AsyncFor(self, node):
                return node

            def visit_While(self, node):
                return node

            def visit_If(self, node):
                return node

            def visit_Try(self, node):
                return node

            def visit_With(self, node):
                return node

            def visit_FunctionDef(self, node):
                return node

            def visit_AsyncFunctionDef(self, node):
                return node

        return _Hoister(self).visit(s)

    def _comp_lines(self, target: str, comp, note_set: bool = False) -> list[str]:
        """推导式 / 生成器表达式 → AGL 行（循环展开 + 追加/下标赋值）。

        在隔离的缩进上下文里生成（调用处负责把行放到自己的 depth）。
        target 是目标变量名；返回的行没有缩进前缀。
        """
        saved_lines, saved_depth = self.lines, self.depth
        saved_declared = self.declared
        self.lines, self.depth = [], 0
        self.declared = set(self.declared)
        try:
            is_dict = isinstance(comp, ast.DictComp)
            is_set = isinstance(comp, ast.SetComp)
            if is_dict:
                self.put(f"{target} = {{}}")
            else:
                self.put(f"{target} = []")
            if is_set and note_set:
                self.warnings.append(
                    "集合推导式 `{...}` 被翻译成「列表 + 去重」："
                    "Python 的集合不重复且无序，AGL 用列表近似，"
                    "元素按首次出现的顺序保留，顺序可能与原 Python 的实际输出不同")

            def emit_gen(idx: int) -> None:
                if idx >= len(comp.generators):
                    if is_dict:
                        key_code = self.expr(comp.key)
                        val_code = self.expr(comp.value)
                        self.put(f"{target}[{key_code}] = {val_code}")
                    elif is_set:
                        elt_code = self.expr(comp.elt)
                        contains_call = f"{self.builtin_of('contains')}({target}, {elt_code})"
                        self.put(f"{self.kw_of('if')} ({self.op_of('!')} {contains_call}) {{")
                        self.depth += 1
                        self.put(f"{target} = {self.builtin_of('append')}({target}, {elt_code})")
                        self.depth -= 1
                        self.put("}")
                    else:
                        # v0.48：嵌套列表推导——元素本身又是推导式，
                        # 先手动展开成临时变量，再追加到外层列表
                        if isinstance(comp.elt, (ast.ListComp, ast.DictComp, ast.SetComp)):
                            inner_target = f"_inner_{self.depth}_{idx}"
                            inner_is_dict = isinstance(comp.elt, ast.DictComp)
                            # 建空的内层容器
                            self.put(f"{inner_target} = {{}}" if inner_is_dict else f"{inner_target} = []")
                            # 手动展开内层推导的 generators
                            def emit_inner(inner_gen_idx: int):
                                inner_gen = comp.elt.generators[inner_gen_idx]
                                inner_loop_var = self.check_name(inner_gen.target.id, "内层循环变量")
                                inner_iter = self.expr(inner_gen.iter)
                                self.put(f"{self.kw_of('for')} {inner_loop_var} {self.kw_of('in')} {inner_iter} {{")
                                self.depth += 1
                                for cond in inner_gen.ifs:
                                    self.put(f"{self.kw_of('if')} {self.expr(cond)} {{")
                                    self.depth += 1
                                if inner_gen_idx + 1 < len(comp.elt.generators):
                                    emit_inner(inner_gen_idx + 1)
                                else:
                                    if inner_is_dict:
                                        k = self.expr(comp.elt.key)
                                        v = self.expr(comp.elt.value)
                                        self.put(f"{inner_target}[{k}] = {v}")
                                    else:
                                        e = self.expr(comp.elt.elt)
                                        self.put(f"{inner_target} = {self.builtin_of('append')}({inner_target}, {e})")
                                for _ in inner_gen.ifs:
                                    self.depth -= 1
                                    self.put("}")
                                self.depth -= 1
                                self.put("}")
                            emit_inner(0)
                            elt_code = inner_target
                        else:
                            elt_code = self.expr(comp.elt)
                        self.put(f"{target} = {self.builtin_of('append')}({target}, {elt_code})")
                    return
                gen = comp.generators[idx]
                # v0.48：字典推导里的 `for k, v in ...` —— 循环变量是元组解包
                # 自动展开成：临时变量 = item; k = 临时变量[0]; v = 临时变量[1]
                if isinstance(gen.target, ast.Tuple) and len(gen.target.elts) == 2:
                    t0 = self.check_name(gen.target.elts[0].id, "循环变量")
                    t1 = self.check_name(gen.target.elts[1].id, "循环变量")
                    tmp_var = f"_item_{idx}"
                    iter_code = self.expr(gen.iter)
                    self.put(f"{self.kw_of('for')} {tmp_var} {self.kw_of('in')} {iter_code} {{")
                    self.depth += 1
                    self.declared.add(gen.target.elts[0].id)
                    self.declared.add(gen.target.elts[1].id)
                    self.put(f"{t0} = {tmp_var}[0]")
                    self.put(f"{t1} = {tmp_var}[1]")
                    for cond in gen.ifs:
                        self.put(f"{self.kw_of('if')} {self.expr(cond)} {{")
                        self.depth += 1
                    emit_gen(idx + 1)
                    for _ in gen.ifs:
                        self.depth -= 1
                        self.put("}")
                    self.depth -= 1
                    self.put("}")
                    return
                if not isinstance(gen.target, ast.Name):
                    raise Blocked("推导式的循环变量必须是简单变量名")
                loop_var = self.check_name(gen.target.id, "循环变量")
                iter_node = gen.iter
                if (isinstance(iter_node, ast.Call)
                        and isinstance(iter_node.func, ast.Name)
                        and iter_node.func.id == "range"
                        and not iter_node.keywords):
                    args = iter_node.args
                    if len(args) == 1:
                        lo, hi, step = ast.Constant(value=0), args[0], ast.Constant(value=1)
                    elif len(args) == 2:
                        lo, hi, step = args[0], args[1], ast.Constant(value=1)
                    elif len(args) == 3:
                        lo, hi, step = args[0], args[1], args[2]
                    else:
                        raise Blocked("`range()` 的参数个数不对")
                    if not (isinstance(step, ast.Constant) and isinstance(step.value, int)):
                        raise Blocked("推导式里 range() 的步长不是固定整数")
                    step_v = step.value
                    if step_v == 0:
                        raise Blocked("`range()` 的步长不能是 0")
                    cmp_word = self.op_of("<" if step_v > 0 else ">")
                    let_word = self.kw_of("let") if gen.target.id not in self.declared else ""
                    self.declared.add(gen.target.id)
                    self.put(f"{let_word} {loop_var} = {self.expr(lo)}".strip())
                    self.put(f"{self.kw_of('while')} ({loop_var} {cmp_word} {self.expr(hi)}) {{")
                    self.depth += 1
                    for cond in gen.ifs:
                        self.put(f"{self.kw_of('if')} {self.expr(cond)} {{")
                        self.depth += 1
                    emit_gen(idx + 1)
                    for _ in gen.ifs:
                        self.depth -= 1
                        self.put("}")
                    op = "+" if step_v > 0 else "-"
                    self.put(f"{loop_var} = ({loop_var} {op} {abs(step_v)})")
                    self.depth -= 1
                    self.put("}")
                else:
                    self.declared.add(gen.target.id)
                    self.put(f"{self.kw_of('for')} {loop_var} {self.kw_of('in')} {self.expr(iter_node)} {{")
                    self.depth += 1
                    for cond in gen.ifs:
                        self.put(f"{self.kw_of('if')} {self.expr(cond)} {{")
                        self.depth += 1
                    emit_gen(idx + 1)
                    for _ in gen.ifs:
                        self.depth -= 1
                        self.put("}")
                    self.depth -= 1
                    self.put("}")

            emit_gen(0)
        finally:
            lines = list(self.lines)
            # 循环变量按 AGL 的规则留在函数作用域里：
            # Python 的推导式有独立作用域、循环变量不泄漏，
            # AGL 里它必然存在；保留声明是为了让后续同名推导式
            # 复用这个变量而不是重复 `变量` 声明
            for g in comp.generators:
                if isinstance(g.target, ast.Name):
                    saved_declared.add(g.target.id)
            self.lines, self.depth = saved_lines, saved_depth
            self.declared = saved_declared
        return lines

    def _pop_kind(self, recv) -> str | None:
        """判断 `x.pop(1参)` 里 x 是列表还是映射（按编译期登记）。

        列表字面量 / list(...) 构造 → "list"；映射字面量 / dict(...) → "dict"；
        两者都没登记过（比如参数、函数返回值）→ None，调用方精确拒绝。
        """
        if isinstance(recv, ast.Name):
            n = recv.id
            if n in self.list_vars:
                return "list"
            if n in self.dict_vars:
                return "dict"
        return None

    def _first_lets(self, node, depth: int = 0) -> list[str]:
        """复合块子树里"首次声明"的赋值目标名（按出现顺序去重）。

        Python 没有块作用域：`if c: y = 1` 之后块外照样能读 y；
        AGL 是块作用域，块内 `变量 y = 1` 出了块就看不见。导入器必须把
        这种声明**提升**到函数层（块前补 `变量 y = 空`，块内变成普通赋值），
        否则「块内声明 + 块后引用」的 Python 代码翻译过去必然 AGL-E-0302。
        只收集嵌套在复合块里的赋值（depth > 0）：
        函数体顶层 `x = 1` 本来就是函数级声明，不需要提升。
        跳过函数 / 类 / Lambda 子树（它们自成一个作用域，不是"块"）。
        """
        out: list[str] = []
        seen: set[str] = set()

        def walk(n, d: int) -> None:
            if isinstance(n, list):
                for c in n:
                    walk(c, d)
                return
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Lambda)):
                return
            if isinstance(n, ast.Assign):
                t = n.targets[0] if len(n.targets) == 1 else None
                if isinstance(t, ast.Name) and d > 0 and t.id not in seen:
                    seen.add(t.id)
                    out.append(t.id)
                return
            # 解包循环 `for a, b in ...` 展开成 当 循环 + 生成赋值，
            # 生成行不在 AST 里，提升收集必须看 for 的 target 本身：
            #   · Tuple target（enumerate/zip/元组列表）→ 每个解包变量都要提升；
            #   · Name target + map(...) → 变换结果变量要提升。
            #   普通 for 的 Name target 是 AGL 原生循环变量，自带声明，不收集；
            #   filter 内联的目标就是循环变量本身，也不收集。
            if isinstance(n, ast.For):
                # 注意：函数体顶层的 for 也是 d=0，解包变量照样要提升 ——
                # 提升与否看「是否展开成生成赋值」，不看嵌套深度。
                if isinstance(n.target, ast.Tuple):
                    for t in n.target.elts:
                        if isinstance(t, ast.Name) and t.id not in seen:
                            seen.add(t.id)
                            out.append(t.id)
                elif (isinstance(n.target, ast.Name)
                      and isinstance(n.iter, ast.Call)
                      and isinstance(n.iter.func, ast.Name)
                      and n.iter.func.id == "map"):
                    if n.target.id not in seen:
                        seen.add(n.target.id)
                        out.append(n.target.id)
            for c in ast.iter_child_nodes(n):
                walk(c, d + 1)

        walk(node, depth)
        return out

    def _promote_block_decls(self, body: list[ast.stmt]) -> None:
        """把函数体内所有复合块（如果/当/遍历/尝试）里的首次声明提升到函数层。

        提升语句统一发在函数体顶层（循环之外的 `变量 n = 空` 只执行一次，
        若发进循环体，每轮会把值重置回空，和 Python「上一轮的值继续保留」
        的语义冲突）。提升后这些名字进入 declared：
        块内再遇到赋值就是普通赋值，块后引用不会撞上 0302 ——
        语义与 Python 的函数作用域一致。
        """
        for n in self._first_lets(body):
            if n not in self.declared:
                # 注解成「任意」而不是裸 空：裸 空 的静态类型是 空（Null），
                # 之后 `n = 0` 再 `n + 1` 会被静态检查当成「空 + 整数」拒绝；
                # 注解成 任意 后赋值任意类型、参与任意运算都不误报。
                self.depth += 1
                self.put(f"{self.kw_of('let')} {self.check_name(n, '变量')}: "
                         f"{self.kw.text_for('Any', self.lang)} = "
                         f"{self.expr(ast.Constant(value=None))}")
                self.depth -= 1
                self.declared.add(n)

    def stmt(self, s: ast.stmt) -> None:
        # 先提升语句里"表达式位置"的副作用：列表.append（原地改+求值为空）和        # 生成器表达式（惰性→列表）在 Python 里是求值过程中发生的事，
        # AGL 只能拆成"先做、再用值"的语句序列，顺序与源码从左到右一致。
        # 整句赋值 `x = xs.append(v)` / `xs = [推导]` 走下面的专门分支，不重复提升。
        skip_hoist = (isinstance(s, ast.Assign)
                      and len(s.targets) == 1
                      and isinstance(s.targets[0], ast.Name)
                      and (isinstance(s.value, (ast.ListComp, ast.DictComp,
                                                ast.SetComp, ast.GeneratorExp))
                           or (isinstance(s.value, ast.Call)
                               and isinstance(s.value.func, ast.Attribute)
                               and s.value.func.attr == "append"
                               and len(s.value.args) == 1
                               and isinstance(s.value.func.value,
                                              (ast.Name, ast.Subscript)))))
        # 整句就是 `xs.append(v)` 的语句：stmt 的 Expr 分支自己处理，
        # 不提升（提升会把它顶替成 空 常量，反而走到孤立表达式分支）。
        # v0.30 方法族同款：remove/insert/reverse/update/pop/copy 整句由
        # stmt 的 Expr 分支处理成重新赋值，提升会把它们顶替成空常量。
        if (isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Call)
                and isinstance(s.value.func, ast.Attribute)
                and s.value.func.attr in ("append", "remove", "insert",
                                          "reverse", "update", "pop", "copy",
                                          "clear")
                and isinstance(s.value.func.value, (ast.Name, ast.Subscript))):
            skip_hoist = True
        if not skip_hoist:
            pre: list[str] = []
            s = self._hoist_expressions(s, pre)
            for line in pre:
                self.put(line)
        if isinstance(s, ast.Assign):
            # ── 切片赋值 xs[1:3] = v / xs[:2] = v / xs[1:] = v ──
            # AGL 值语义：重绑为「前段 + 新值 + 后段」的拼接。
            # Python `xs[1:3] = v` 是原地替换区间，结果与拼接完全一致
            # （stop 开区间；负下标 / 步长语义复杂，精确拒绝）。
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Subscript)
                    and isinstance(s.targets[0].slice, ast.Slice)):
                sub = s.targets[0]
                st = sub.slice
                if st.step is not None:
                    # ast 把 `-1` 解析成 UnaryOp(USub, 1)，与读取侧切片（v0.28）同口径归一成整数
                    step_v = None
                    sv = st.step
                    if isinstance(sv, ast.UnaryOp) and isinstance(sv.op, ast.USub) \
                            and isinstance(sv.operand, ast.Constant) \
                            and isinstance(sv.operand.value, int):
                        step_v = -sv.operand.value
                    elif isinstance(sv, ast.Constant) and isinstance(sv.value, int):
                        step_v = sv.value
                    if step_v is None:
                        # 动态步长（v0.38）：符号运行时才知道 → 绑临时变量，
                        # 按「正步长 / 负步长」两套逐位重建双分支展开（与常量两分支同构）。
                        base = self.expr(sub.value)
                        val = self.expr(s.value)
                        t_step = self._fresh_name("步长值")
                        t_old = self._fresh_name("步进原")
                        t_new = self._fresh_name("步进新")
                        t_k = self._fresh_name("步进序")
                        t_i = self._fresh_name("步进位")
                        t_lo = self._fresh_name("步进起")
                        t_hi = self._fresh_name("步进止")
                        head = f"{self.kw_of('let')} " if base not in self.declared else ""
                        self.declared.add(base)
                        ge = self.op_of(">=")
                        lt = self.op_of("<")
                        le = self.op_of("<=")
                        gt = self.op_of(">")
                        eq = self.op_of("==")
                        andw = self.op_of("&&")
                        self.put(f"{self.kw_of('let')} {t_step} = {self.expr(st.step)}")
                        if st.lower is not None:
                            self.put(f"{self.kw_of('let')} {t_lo} = {self.expr(st.lower)}")
                        if st.upper is not None:
                            self.put(f"{self.kw_of('let')} {t_hi} = {self.expr(st.upper)}")
                        self.put(f"{self.kw_of('let')} {t_old} = {base}")
                        self.put(f"{self.kw_of('let')} {t_new} = []")
                        self.put(f"{self.kw_of('let')} {t_k} = 0")
                        self.put(f"{self.kw_of('if')} {t_step} {ge} 0 {{")
                        # 正分支（与常量正步长同构）
                        lo_c = t_lo if st.lower is not None else "0"
                        hi_c = (t_hi if st.upper is not None
                                else f"{t_old}.{self.attr_of('length')}")
                        self.put(f"  {self.kw_of('for')} {t_i} {self.kw_of('in')} "
                                 f"{self.builtin_of('range_list')}(0, {t_old}.{self.attr_of('length')}) {{")
                        cond = (f"{t_i} {ge} {lo_c} {andw} {t_i} {lt} {hi_c} {andw} "
                                f"{self.builtin_of('divmod')}({t_i} - {lo_c}, {t_step})[1] {eq} 0")
                        self.put(f"    {self.kw_of('if')} {cond} {{")
                        self.put(f"      {t_new} = {self.builtin_of('append')}({t_new}, {val}[{t_k}])")
                        self.put(f"      {t_k} = {t_k} + 1")
                        self.put(f"    }} {self.kw_of('else')} {{")
                        self.put(f"      {t_new} = {self.builtin_of('append')}({t_new}, {t_old}[{t_i}])")
                        self.put(f"    }}")
                        self.put(f"  }}")
                        self.put(f"}} {self.kw_of('else')} {{")
                        # 负分支（与常量负步长同构：止 是开区间、取值索引反向）
                        lo_c2 = (t_lo if st.lower is not None
                                 else f"{t_old}.{self.attr_of('length')} - 1")
                        hi_c2 = t_hi if st.upper is not None else "-1"
                        self.put(f"  {self.kw_of('for')} {t_i} {self.kw_of('in')} "
                                 f"{self.builtin_of('range_list')}(0, {t_old}.{self.attr_of('length')}) {{")
                        cond2 = (f"{t_i} {le} {lo_c2} {andw} {t_i} {gt} {hi_c2} {andw} "
                                 f"{self.builtin_of('divmod')}({t_i} - {lo_c2}, {t_step})[1] {eq} 0")
                        self.put(f"    {self.kw_of('if')} {cond2} {{")
                        val_ref = (f"{val}[{val}.{self.attr_of('length')}"
                                   f" - 1 - {t_k}]")
                        self.put(f"      {t_new} = {self.builtin_of('append')}({t_new}, {val_ref})")
                        self.put(f"      {t_k} = {t_k} + 1")
                        self.put(f"    }} {self.kw_of('else')} {{")
                        self.put(f"      {t_new} = {self.builtin_of('append')}({t_new}, {t_old}[{t_i}])")
                        self.put(f"    }}")
                        self.put(f"  }}")
                        self.put(f"}}")
                        self.put(f"{head}{base} = {t_new}")
                        self.warnings.append(
                            "`xs[起:止:步] = 值` 的步长是变量：按运行时符号展开成"
                            "「正步长 / 负步长」两套逐位重建（止 在负步长里是开区间），"
                            "与 Python 动态步长切片赋值结果一致")
                        return
                    if step_v < 0:
                        # 负步长切片赋值（v0.38）：反向交错重建。
                        # Python 语义：覆盖位置 i = lo, lo+step, lo+2*step, …，
                        # 步长负所以 i 递减，**止是开区间**（i 严格大于 hi 才继续）；
                        # 缺省 lo = 长度-1、缺省 hi = -1（i > -1 恒真）。
                        # 与正步长同一套「新建列表 + 条件替换」逐位重建。
                        base = self.expr(sub.value)
                        lo_c = (self.expr(st.lower) if st.lower is not None
                                else f"{base}.{self.attr_of('length')} - 1")
                        hi_c = (self.expr(st.upper) if st.upper is not None
                                else "-1")
                        val = self.expr(s.value)
                        t_old = self._fresh_name("步进原")
                        t_new = self._fresh_name("步进新")
                        t_k = self._fresh_name("步进序")
                        t_i = self._fresh_name("步进位")
                        head = f"{self.kw_of('let')} " if base not in self.declared else ""
                        self.declared.add(base)
                        le = self.op_of("<=")
                        gt = self.op_of(">")
                        eq = self.op_of("==")
                        andw = self.op_of("&&")
                        self.put(f"{self.kw_of('let')} {t_old} = {base}")
                        self.put(f"{self.kw_of('let')} {t_new} = []")
                        self.put(f"{self.kw_of('let')} {t_k} = 0")
                        self.put(f"{self.kw_of('for')} {t_i} {self.kw_of('in')} "
                                 f"{self.builtin_of('range_list')}(0, {t_old}.{self.attr_of('length')}) {{")
                        cond = (f"{t_i} {le} {lo_c} {andw} {t_i} {gt} {hi_c} {andw} "
                                f"{self.builtin_of('divmod')}({t_i} - {lo_c}, "
                                f"{self.expr(st.step)})[1] {eq} 0")
                        self.put(f"  {self.kw_of('if')} {cond} {{")
                        # 负步长覆盖顺序从 lo 递减：循环 i 递增先命中「最小覆盖位」，
                        # 它拿的是值列表的最后一个 → 取值索引反向（值.长度-1-k）
                        val_ref = (f"{val}[{val}.{self.attr_of('length')}"
                                   f" - 1 - {t_k}]")
                        self.put(f"    {t_new} = {self.builtin_of('append')}({t_new}, {val_ref})")
                        self.put(f"    {t_k} = {t_k} + 1")
                        self.put(f"  }} {self.kw_of('else')} {{")
                        self.put(f"    {t_new} = {self.builtin_of('append')}({t_new}, {t_old}[{t_i}])")
                        self.put(f"  }}")
                        self.put("}")
                        self.put(f"{head}{base} = {t_new}")
                        self.warnings.append(
                            "`xs[起:止:负步] = 值` 被展开成「反向逐位重建」："
                            "覆盖位置从 起 起按步长递减（止 是开区间），依次取 值；"
                            "与 Python 负步长切片赋值结果一致")
                        return
                    # 正步长切片赋值 xs[a:b:c] = v（c>0）：
                    # Python 语义是位置 a, a+c, a+2c, …（< b）被 v 依次覆盖。
                    # AGL 值语义没有原地改，按「新建列表 + 条件替换」逐位重建。
                    base = self.expr(sub.value)
                    lo_c = self.expr(st.lower) if st.lower is not None else "0"
                    hi_c = (self.expr(st.upper) if st.upper is not None
                            else f"{base}.{self.attr_of('length')}")
                    val = self.expr(s.value)
                    t_old = self._fresh_name("步进原")
                    t_new = self._fresh_name("步进新")
                    t_k = self._fresh_name("步进序")
                    t_i = self._fresh_name("步进位")
                    head = f"{self.kw_of('let')} " if base not in self.declared else ""
                    self.declared.add(base)
                    ge = self.op_of(">=")
                    lt = self.op_of("<")
                    eq = self.op_of("==")
                    andw = self.op_of("&&")
                    self.put(f"{self.kw_of('let')} {t_old} = {base}")
                    self.put(f"{self.kw_of('let')} {t_new} = []")
                    self.put(f"{self.kw_of('let')} {t_k} = 0")
                    self.put(f"{self.kw_of('for')} {t_i} {self.kw_of('in')} "
                             f"{self.builtin_of('range_list')}(0, {t_old}.{self.attr_of('length')}) {{")
                    cond = (f"{t_i} {ge} {lo_c} {andw} {t_i} {lt} {hi_c} {andw} "
                            f"{self.builtin_of('divmod')}({t_i} - {lo_c}, "
                            f"{self.expr(st.step)})[1] {eq} 0")
                    self.put(f"  {self.kw_of('if')} {cond} {{")
                    # 追加 是值语义：每次重绑定 t_new，否则值被丢弃（t_new 恒空）
                    self.put(f"    {t_new} = {self.builtin_of('append')}({t_new}, {val}[{t_k}])")
                    self.put(f"    {t_k} = {t_k} + 1")
                    self.put(f"  }} {self.kw_of('else')} {{")
                    self.put(f"    {t_new} = {self.builtin_of('append')}({t_new}, {t_old}[{t_i}])")
                    self.put(f"  }}")
                    self.put("}")
                    self.put(f"{head}{base} = {t_new}")
                    self.warnings.append(
                        "`xs[起:止:步] = 值` 被展开成「逐位重建」：覆盖位置依次取 值，"
                        "其余位置取原值；与 Python 步长切片赋值结果一致")
                    return
                base = self.expr(sub.value)
                if st.lower is not None and isinstance(st.lower, ast.Constant) \
                        and isinstance(st.lower.value, int) and st.lower.value < 0:
                    raise Blocked(
                        "切片赋值带负下标（`xs[-2:] = ...`）暂时不能导：请手动改写")
                lo_c = self.expr(st.lower) if st.lower is not None else "0"
                hi_raw = st.upper
                if hi_raw is not None and isinstance(hi_raw, ast.Constant) \
                        and isinstance(hi_raw.value, int) and hi_raw.value < 0:
                    raise Blocked(
                        "切片赋值带负下标（`xs[:-1] = ...`）暂时不能导：请手动改写")
                if hi_raw is None:
                    hi_c = f"{base}.{self.attr_of('length')}"
                else:
                    hi_c = self.expr(hi_raw)
                val = self.expr(s.value)
                head = f"{self.kw_of('let')} " if base not in self.declared else ""
                self.declared.add(base)
                self.put(f"{head}{base} = {self.builtin_of('slice')}({base}, 0, {lo_c})"
                         f" + {val} + {self.builtin_of('slice')}({base}, {hi_c}, "
                         f"{base}.{self.attr_of('length')})")
                self.warnings.append(
                    "`xs[起:止] = 值` 被翻译成「重绑：`切片(xs, 0, 起) + 值 + "
                    "切片(xs, 止, xs.长度)`」：AGL 值语义下替换区间就是前后拼接，"
                    "与 Python 原地改结果一致")
                return
            if any(isinstance(t, ast.Tuple) for t in s.targets):
                # 解包赋值：`a, b = ...` → `变量 a, b = ...`（未声明）或 `a, b = ...`（已声明）。
                # 只支持单目标元组；嵌套 / 星号目标精确拒绝。
                if len(s.targets) != 1 or not isinstance(s.targets[0], ast.Tuple):
                    raise Blocked(
                        "解包赋值只能有一个目标元组（`a, b = ...`）："
                        "链式加解包的混合写法请分开写")
                tup = s.targets[0]
                if any(isinstance(el, ast.Starred) for el in tup.elts):
                    # 星号解包 `head, *rest = xs` / `*rest, tail = xs` / `a, *mid, b = xs`：
                    # AGL 没有「收下剩余元素」语法，按 Python 语义展开成
                    # 「临时保存 + 逐项下标 + 切片取剩余」。
                    star_idx = next(
                        i for i, el in enumerate(tup.elts)
                        if isinstance(el, ast.Starred))
                    star_el = tup.elts[star_idx]
                    if not isinstance(star_el.value, ast.Name):
                        raise Blocked(
                            "带星号的解包目标必须是单个名字（`a, *b = ...`）："
                            "嵌套目标请手动展开")
                    names = []
                    for el in tup.elts:
                        if isinstance(el, ast.Starred):
                            names.append(self.check_name(el.value.id, "变量"))
                        elif isinstance(el, ast.Name):
                            names.append(self.check_name(el.id, "变量"))
                        else:
                            raise Blocked(
                                "解包目标必须是名字（`a, *b = ...`）："
                                "下标 / 成员目标请手动展开")
                    if any(n in self.declared for n in names) \
                            and not all(n in self.declared for n in names):
                        raise Blocked(
                            "解包目标里有的已声明、有的没声明："
                            "AGL 一个语句只能整体声明或整体重赋值，请先分开写")
                    head_all = (self.kw_of("let") + " "
                                if not all(n in self.declared for n in names) else "")
                    t_tmp = self._fresh_name("解包")
                    self.put(f"{self.kw_of('let')} {t_tmp} = {self.expr(s.value)}")
                    tail_n = len(tup.elts) - star_idx - 1
                    for k, el in enumerate(tup.elts):
                        if isinstance(el, ast.Starred):
                            continue
                        if k < star_idx:
                            # 星号前的元素按从前往后数：head = tmp[0], ...
                            self.put(f"{head_all}{names[k]} = {t_tmp}[{k}]")
                        else:
                            # 星号后的元素从尾部往前数：tail = tmp[长度 - 1]（最后一个非星号）
                            j = k - star_idx - 1  # 该元素在星号后是第 j 个
                            idx = (f"{t_tmp}.{self.attr_of('length')} - "
                                   f"{tail_n - j}")
                            self.put(f"{head_all}{names[k]} = {t_tmp}[{idx}]")
                    if tail_n == 0:
                        rest_expr = (f"{self.builtin_of('slice')}({t_tmp}, {star_idx}, "
                                     f"{t_tmp}.{self.attr_of('length')})")
                    else:
                        rest_expr = (f"{self.builtin_of('slice')}({t_tmp}, {star_idx}, "
                                     f"{t_tmp}.{self.attr_of('length')} - {tail_n})")
                    self.put(f"{head_all}{names[star_idx]} = {rest_expr}")
                    for n in names:
                        self.declared.add(n)
                    self.warnings.append(
                        "`a, *b = ...` 被展开成「逐项下标 + 切片取剩余」："
                        "AGL 没有收下剩余元素的语法，按 Python 语义等价展开")
                    return
                names = []
                for el in tup.elts:
                    if not isinstance(el, ast.Name):
                        raise Blocked(
                            "解包目标必须是名字（`a, b = ...`）："
                            "下标 / 成员目标请手动展开")
                    names.append(self.check_name(el.id, "变量"))
                rhs = self.expr(s.value)
                if any(n in self.declared for n in names) \
                        and not all(n in self.declared for n in names):
                    raise Blocked(
                        "解包目标里有的已声明、有的没声明："
                        "AGL 一个语句只能整体声明或整体重赋值，请先分开写")
                if all(n in self.declared for n in names):
                    self.put(f"{', '.join(names)} = {rhs}")
                else:
                    for n in names:
                        self.declared.add(n)
                    self.put(f"{self.kw_of('let')} {', '.join(names)} = {rhs}")
                return
            # 表达式位置的 append：x = xs.append(v) → xs = 追加(xs, v); x = 空
            # Python 里 append 原地改列表并返回 None；AGL 的 追加 返回新列表。
            # 拆成「先重绑列表，再赋空值」后两边行为一致。
            if (len(s.targets) == 1
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Attribute)
                    and s.value.func.attr == "append"
                    and len(s.value.args) == 1
                    and isinstance(s.value.func.value, (ast.Name, ast.Subscript))):
                receiver = self.expr(s.value.func.value)
                arg = self.expr(s.value.args[0])
                self.put(f"{receiver} = {self.builtin_of('append')}({receiver}, {arg})")
                self._assign_one(s.targets[0], self.kw_of("Null"))
                self.warnings.append(
                    "`x = xs.append(v)` 被翻译成「先 `xs = 追加(xs, v)`，再 `x = 空`」："
                    "Python 里 append 原地改列表并返回 None，AGL 的 追加 返回新列表，"
                    "拆开后两边行为一致")
                return
            # lambda 整句赋值：f = lambda x: ... → 提升成命名函数（只在自己的参数里取值）
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Lambda)):
                self._lift_lambda(s.targets[0].id, s.value)
                return
            # 列表推导式 xs = [expr for x in iter if cond for y in iter2 ...]
            # 字典推导式 d = {k: v for x in iter if cond ...}
            # 集合推导式 s = {expr for x in iter if cond ...}
            # 生成器表达式 xs = (expr for x in iter ...) 也展开成列表（AGL 没有惰性迭代）
            # 都展开成嵌套循环 + 追加 / 下标赋值（集合额外去重）
            if (isinstance(s.value, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp))
                    and len(s.targets) == 1):
                t = s.targets[0]
                if not isinstance(t, ast.Name):
                    raise Blocked(
                        "推导式 / 生成器表达式暂时只能用在变量赋值上"
                        "（`xs = [...]` / `d = {...}` / `s = {...}` / `xs = ( ... )`）；"
                        "下标 / 成员位置的请手动展开成循环")
                name = self.check_name(t.id, "变量")
                head = self.kw_of("let") if t.id not in self.declared else ""
                if t.id not in self.declared:
                    self.declared.add(t.id)
                comp = s.value
                if isinstance(comp, ast.GeneratorExp):
                    self.warnings.append(
                        "生成器表达式 `( ... for ... )` 被翻译成列表："
                        "AGL 没有惰性迭代，只能先展开成列表再使用；"
                        "生成器只能迭代一次、列表可以多次，若原代码依赖这一点请手动改写")
                lines = self._comp_lines(name, comp,
                                         note_set=isinstance(comp, ast.SetComp))
                if head:
                    lines[0] = f"{head} {lines[0]}".strip()
                for ln in lines:
                    self.put(ln)
                return
            # 三元表达式 x = body if test else orelse
            if isinstance(s.value, ast.IfExp) and len(s.targets) == 1:
                t = s.targets[0]
                if not isinstance(t, ast.Name):
                    raise Blocked(
                        "三元表达式暂时只能用在变量赋值上（`x = a if b else c`）；"
                        "下标 / 成员位置的请手动展开成 如果 / 否则")
                name = self.check_name(t.id, "变量")
                head = self.kw_of("let") if t.id not in self.declared else ""
                if t.id not in self.declared:
                    self.declared.add(t.id)
                # 先赋 else 分支的值，再在 if 里覆盖
                self.put(f"{head} {name} = {self.expr(s.value.orelse)}".strip())
                self.put(f"{self.kw_of('if')} {self.expr(s.value.test)} {{")
                self.depth += 1
                self.put(f"{name} = {self.expr(s.value.body)}")
                self.depth -= 1
                self.put("}")
                return
            if len(s.targets) > 1:
                # 链式赋值 a = b = expr：只放行右值是纯字面量的情况，
                # 否则 f(x) 会被调用多次，两边行为不一样。
                if not isinstance(s.value, (ast.Constant, ast.Name)):
                    raise Blocked(
                        "一行给多个变量赋值（`a = b = f(x)`）暂时不能导："
                        "右值带函数调用时会重复执行；请改成一行一个赋值")
                rhs = self.expr(s.value)
                for t in s.targets:
                    self._assign_one(t, rhs)
                return
            # 变量被重新赋值后，之前记住的"它是某个类的实例"就失效了
            if (isinstance(s.targets[0], ast.Name) and s.targets[0].id in self.instances
                    and not (isinstance(s.value, ast.Call)
                             and isinstance(s.value.func, ast.Name)
                             and s.value.func.id in self.classes)):
                self.instances.pop(s.targets[0].id)
            # 从类构造出来的变量记成"该类的实例"：后续 c.方法() / c.属性 才能改写
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Name)
                    and s.value.func.id in self.classes):
                self.instances[s.targets[0].id] = s.value.func.id
            # 值能确认是集合的赋值（`s = {1, 2}` / `s = set(...)`）：
            # 登记后 `s | t` / `s & t` / `s - t` 才能直译成 并集/交集/差集
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and (isinstance(s.value, ast.Set)
                         or (isinstance(s.value, ast.Call)
                             and isinstance(s.value.func, ast.Name)
                             and s.value.func.id == "set"))):
                self.set_vars.add(s.targets[0].id)
            # 集合运算的结果仍然是集合（`t = s | {3, 4}`）：
            # 不登记的话，后面 `s <= t` 的右值会被当成未知类型，退化成数字比较。
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.BinOp)
                    and type(s.value.op).__name__ in ("BitOr", "BitAnd", "Sub", "BitXor")
                    and self._could_be_set(s.value.left)
                    and self._could_be_set(s.value.right)):
                self.set_vars.add(s.targets[0].id)
            # v0.30：列表/映射变量登记（`xs = [1,2]` / `xs = list(...)` 进列表；
            # `d = {...}` / `d = dict(...)` 进映射）——`x.pop(1参)` 按它分发
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and (isinstance(s.value, ast.List)
                         or (isinstance(s.value, ast.Call)
                             and isinstance(s.value.func, ast.Name)
                             and s.value.func.id == "list"))):
                self.list_vars.add(s.targets[0].id)
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and (isinstance(s.value, ast.Dict)
                         or (isinstance(s.value, ast.Call)
                             and isinstance(s.value.func, ast.Name)
                             and s.value.func.id == "dict"))):
                self.dict_vars.add(s.targets[0].id)
            # v0.43：右边是嵌套函数名 → 左边变量是函数引用类型
            # （`f = inner`，inner 在 nested_map 里）
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Name)
                    and s.value.id in self.nested_map):
                is_async = self.nested_map[s.value.id].get("is_async", False)
                self.fn_ref_vars[s.targets[0].id] = is_async
            # v0.43：右边是调用返回函数引用的函数 → 左边变量是函数引用类型
            # （`f = outer()`，outer 在 fn_returning_fns 里）
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Name)
                    and s.value.func.id in self.fn_returning_fns):
                is_async = self.fn_returning_fns[s.value.func.id]
                self.fn_ref_vars[s.targets[0].id] = is_async
            # v0.46：右边是 open(path, mode) 调用 → 左边变量是文件句柄
            # （`f = open(path, "r")`，后面 f.read() / f.write() 自动翻译）
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Name)
                    and s.value.func.id == "open"):
                e = s.value
                if len(e.args) < 1 or len(e.args) > 2:
                    raise Blocked(
                        "`open(path, mode)` 只接受路径和模式两个参数")
                path_code = self.expr(e.args[0])
                mode = "r"
                if len(e.args) == 2:
                    if isinstance(e.args[1], ast.Constant) and isinstance(e.args[1].value, str):
                        mode = e.args[1].value
                    else:
                        raise Blocked(
                            "`open(path, mode)` 的 mode 必须是字符串常量（如 \"r\" / \"w\"）")
                self.file_handles[s.targets[0].id] = (path_code, mode)
                self.warnings.append(
                    f"`{s.targets[0].id} = open(path, \"{mode}\")` 被翻译成文件句柄登记："
                    f"后面 {s.targets[0].id}.read() / {s.targets[0].id}.write() 会自动映射到读文件/写文件内建；"
                    "AGL 没有文件句柄对象，不会真的打开文件")
            # ── 赋值位置的方法族（v0.30）──
            # v = xs.pop() / v = xs.pop(i) → 弹出 返回 [值, 新列表]：
            #   拆成 `变量 v = 弹出(xs)[0]` + `xs = 弹出(xs)[1]`
            #   （AGL 值语义下两次调用都基于原列表，结果一致）
            # v = d.pop(k) / v = d.pop(k, 默认) → 取到(弹出键(...))[0] + 重绑 d
            # i = xs.index(x) → i = 取到(元素位置(xs, x))（找不到 取到 抛，对齐 ValueError）
            # v = xs.remove/insert/clear/reverse/update(...) → 重绑 + v = 空
            #   （Python 这些方法返回 None，AGL 的空就是它）
            if (len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Attribute)
                    and isinstance(s.value.func.value, (ast.Name, ast.Subscript))):
                m_attr = s.value.func.attr
                recv_code = self.expr(s.value.func.value)
                tgt = self.check_name(s.targets[0].id, "变量")
                if m_attr == "pop" and not s.value.args \
                        and not s.value.keywords:
                    # 0 参 = 列表专属：弹出尾值
                    pop_call = f"{self.builtin_of('pop')}({recv_code})"
                    head = f"{self.kw_of('let')} " if tgt not in self.declared else ""
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {pop_call}[0]")
                    self.put(f"{recv_code} = {pop_call}[1]")
                    self.warnings.append(
                        "`v = xs.pop()` 被翻译成 `v = 弹出(xs)[0]` + "
                        "`xs = 弹出(xs)[1]`：AGL 值语义下两次 弹出 都基于原列表，"
                        "结果一致；弹出的值就是 Python 的返回值")
                    return
                if m_attr == "pop" and len(s.value.args) == 1 \
                        and not s.value.keywords:
                    # 1 参：列表 xs.pop(i) / 映射 d.pop(k) —— 全部走泛型 弹出
                    # （v0.31 起按实参类型运行时分发；登记只为编译期更精确，
                    # 未登记同样翻译，键缺失抛可捕获的中文 KeyError）
                    a_code = self.expr(s.value.args[0])
                    pop_call = f"{self.builtin_of('pop')}({recv_code}, {a_code})"
                    head = (f"{self.kw_of('let')} "
                            if tgt not in self.declared else "")
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {pop_call}[0]")
                    self.put(f"{recv_code} = {pop_call}[1]")
                    self.warnings.append(
                        "`v = x.pop(p)` 被翻译成 `v = 弹出(x, p)[0]` + "
                        "`x = 弹出(x, p)[1]`：AGL 的 弹出 按实参类型运行时分发"
                        "（列表按下标、映射按键），与 Python 一致")
                    return
                if m_attr == "pop" and len(s.value.args) == 2 \
                        and not s.value.keywords:
                    # 2 参 = 映射专属：d.pop(k, 默认)
                    pk = (f"{self.builtin_of('pop_key')}"
                          f"({recv_code}, {self.expr(s.value.args[0])}, "
                          f"{self.expr(s.value.args[1])})")
                    head = f"{self.kw_of('let')} " if tgt not in self.declared else ""
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {pk}[0]")
                    self.put(f"{recv_code} = {pk}[1]")
                    self.warnings.append(
                        "`v = d.pop(k, 默认)` 被翻译成 `v = 弹出键(d, k, 默认)[0]` + "
                        "`d = 弹出键(d, k, 默认)[1]`：键缺失时返回默认值，"
                        "与 Python 语义一致")
                    return
                if m_attr == "index" and len(s.value.args) == 1 \
                        and not s.value.keywords:
                    head = f"{self.kw_of('let')} " if tgt not in self.declared else ""
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {self.builtin_of('unwrap')}"
                             f"({self.builtin_of('index')}({recv_code}, "
                             f"{self.expr(s.value.args[0])}))")
                    self.warnings.append(
                        "`i = xs.index(x)` 被翻译成 `取到(元素位置(xs, x))`："
                        "找不到时 Python 抛 ValueError、AGL 在 取到 处抛中文错误，语义等价")
                    return
                if m_attr in ("remove", "insert", "reverse", "update") \
                        and not s.value.keywords:
                    # 先重绑，再给目标赋 空（Python 返回 None，AGL 的空就是它）
                    if m_attr == "remove" and len(s.value.args) == 1:
                        new_recv = (f"{self.builtin_of('remove')}"
                                    f"({recv_code}, {self.expr(s.value.args[0])})")
                    elif m_attr == "insert" and len(s.value.args) == 2:
                        new_recv = (f"{self.builtin_of('insert')}"
                                    f"({recv_code}, {self.expr(s.value.args[0])}, "
                                    f"{self.expr(s.value.args[1])})")
                    elif m_attr == "reverse" and not s.value.args:
                        new_recv = f"{self.builtin_of('reverse')}({recv_code})"
                    elif m_attr == "update" and len(s.value.args) == 1:
                        new_recv = (f"{self.builtin_of('merge_map')}"
                                    f"({recv_code}, {self.expr(s.value.args[0])})")
                    else:
                        raise Blocked(
                            f"`{m_attr}(...)` 这种写法暂时不能导：参数个数不对")
                    self.put(f"{recv_code} = {new_recv}")
                    head = f"{self.kw_of('let')} " if tgt not in self.declared else ""
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {self.expr(ast.Constant(value=None))}")
                    self.warnings.append(
                        f"`v = x.{m_attr}(...)` 被翻译成「先重绑 x，再给 v 赋 空」："
                        f"Python 里 `x.{m_attr}(...)` 返回 None，AGL 的空就是它")
                    return
                if m_attr == "clear" and not s.value.args and not s.value.keywords:
                    # 先重绑为 清空(x)，再给目标赋 空（Python 的 clear 返回 None）
                    self.put(f"{recv_code} = {self.builtin_of('clear')}({recv_code})")
                    head = f"{self.kw_of('let')} " if tgt not in self.declared else ""
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {self.expr(ast.Constant(value=None))}")
                    self.warnings.append(
                        "`v = x.clear()` 被翻译成「先 `x = 清空(x)`，再给 v 赋 空」："
                        "Python 里 clear 返回 None，AGL 的空就是它；"
                        "清空后的 x 两边一致")
                    return
                if m_attr == "copy" and not s.value.args and not s.value.keywords:
                    head = f"{self.kw_of('let')} " if tgt not in self.declared else ""
                    self.declared.add(tgt)
                    self.put(f"{head}{tgt} = {recv_code}")
                    self.warnings.append(
                        "`v = x.copy()` 被翻译成 `v = x`：AGL 是值语义，"
                        "「拷贝一份」没有意义，直接用原值等价")
                    return
            self._assign_one(s.targets[0], self.expr(s.value))
            return
        if isinstance(s, ast.AnnAssign):
            if not isinstance(s.target, ast.Name):
                raise Blocked("带类型标注的赋值只能给变量名")
            name = self.check_name(s.target.id, "变量")
            self.declared.add(s.target.id)
            self.put(f"{self.kw_of('let')} {name}: "
                     f"{self.type_of(s.annotation, '变量 ' + s.target.id)} = "
                     f"{self.expr(s.value)}")
            return
        if isinstance(s, ast.AugAssign):
            # AGL 原生支持 `x += 1` 这种增广赋值，直接映射即可（1:1 行号）。
            # 只放行算术运算符：地板除 `//=`、幂 `**=` 没有对应关键字。
            if isinstance(s.target, ast.Name):
                tgt = self.check_name(s.target.id, "变量")
            elif isinstance(s.target, (ast.Subscript, ast.Attribute)):
                tgt = self.expr(s.target)
            else:
                raise Blocked(f"复合赋值不能作用在 `{type(s.target).__name__}` 上")
            if isinstance(s.op, ast.FloorDiv):
                self.warnings.append(
                    "`x //= n` 被翻译成 `x = 整除(x, n)`：AGL 没有 `//=` 运算符，"
                    "与 Python `x //= n` 语义一致")
                self.put(f"{tgt} = {self.builtin_of('floordiv')}"
                         f"({tgt}, {self.expr(s.value)})")
                return
            if isinstance(s.op, ast.Pow):
                self.warnings.append(
                    "`x **= n` 被翻译成 `x = 乘方(x, n)`：AGL 没有 `**=` 运算符，"
                    "与 Python `x **= n` 语义一致")
                self.put(f"{tgt} = {self.builtin_of('pow')}"
                         f"({tgt}, {self.expr(s.value)})")
                return
            mark = _BINOP.get(type(s.op))
            if mark is None:
                raise Blocked(f"复合赋值用的 `{type(s.op).__name__}` 不能安全展开")
            self.put(f"{tgt} {mark}= {self.expr(s.value)}")
            return
        if isinstance(s, ast.Yield):
            # v0.48：yield x → _gen_result = 追加(_gen_result, x)
            if not self._is_generator:
                raise Blocked("`yield` 只能出现在生成器函数里")
            value_code = self.expr(s.value) if s.value is not None else self.kw_of("Null")
            self.put(f"{self._gen_var} = {self.builtin_of('append')}({self._gen_var}, {value_code})")
            return
        if isinstance(s, ast.Return):
            # v0.43：return inner（inner 是嵌套函数）→ 当前函数返回函数引用
            if (s.value is not None and isinstance(s.value, ast.Name)
                    and s.value.id in self.nested_map):
                is_async = self.nested_map[s.value.id].get("is_async", False)
                self.fn_returning_fns[self.cur_fn] = is_async
            if s.value is None:
                self.put(self.kw_of("return"))
            else:
                self.put(f"{self.kw_of('return')} {self.expr(s.value)}")
            return
        if isinstance(s, ast.If):
            # v0.48：海象运算符——先算条件（期间 NamedExpr 会往 pending 里塞赋值语句），
            # 再把 pending 里的赋值语句写出来，最后写 if 块
            cond = self.expr(s.test)
            for stmt in self.pending:
                self.put(stmt)
            self.pending.clear()
            self.put(f"{self.kw_of('if')} {cond} {{")
            self.block(s.body)
            if s.orelse:
                self.put(f"}} {self.kw_of('else')} {{")
                self.block(s.orelse)
            self.put("}")
            return
        if isinstance(s, ast.While):
            self.put(f"{self.kw_of('while')} {self.expr(s.test)} {{")
            self.block(s.body)
            self.put("}")
            if s.orelse:
                self.warnings.append(
                    "`while ... else ...` 直译成 `当` 循环的 `否则` 块："
                    "循环没被 `中断` 打断才执行，与 Python 一致")
                self.put(f"{self.kw_of('else')} {{")
                self.block(s.orelse)
                self.put("}")
            return
        if isinstance(s, ast.For):
            self.for_loop(s)
            return
        if isinstance(s, ast.Break):
            self.put(self.kw_of("break"))
            return
        if isinstance(s, ast.Continue):
            self.put(self.kw_of("continue"))
            return
        if isinstance(s, ast.Pass):
            self.put("// 空操作")
            return
        if isinstance(s, ast.Expr):
            # v0.39：`await x()` 独立成语句（常见于 `await asyncio.sleep(...)`）
            # → 直接发 `等待 <x 翻译>`；等待 既是表达式前缀也是合法语句。
            if isinstance(s.value, ast.Await):
                self.put(self.expr(s.value))
                return
            # v0.48：yield x 作为独立语句 → _gen_result = 追加(_gen_result, x)
            if isinstance(s.value, ast.Yield):
                if not self._is_generator:
                    raise Blocked("`yield` 只能出现在生成器函数里")
                value_code = self.expr(s.value.value) if s.value.value is not None else self.kw_of("Null")
                self.put(f"{self._gen_var} = {self.builtin_of('append')}({self._gen_var}, {value_code})")
                return
            if isinstance(s.value, ast.Call):
                # super().__init__(args) → self = 构建父类(args)
                # （父类字段在同一个映射表实例上初始化，之后子类继续往它写字段）
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr == "__init__"
                        and isinstance(s.value.func.value, ast.Call)
                        and isinstance(s.value.func.value.func, ast.Name)
                        and s.value.func.value.func.id == "super"):
                    if not self.super_base:
                        raise Blocked("`super().__init__()` 出现在没有父类的类里，暂时不能导")
                    # 父类构造只在 `__init__` 里沿继承链静态展开一次；出现在别的方法里，
                    # 翻成 `self = 构建父类(...)` 会新建一份映射表、把局部 self 重绑过去，
                    # 而调用方手里还是旧映射引用，写进新表的字段调用方根本看不到。
                    # AGL 没有运行时 super 对象能在「同一份实例上映射」上继续初始化，
                    # 与其给一个看似能跑其实丢字段的翻译，不如精确拒绝。
                    if self.cur_fn != "__init__":
                        raise Blocked(
                            "`super().__init__(...)` 出现在 `__init__` 之外的普通方法里："
                            "AGL 没有运行时 super 对象，父类构造只在 `__init__` 里沿继承链"
                            "静态展开一次；在普通方法里重新初始化会新建一份映射表，"
                            "写进去的字段调用方手里的旧引用看不到。"
                            "请把初始化放回 `__init__`，或手动把要补的字段直接写到 `self` 上")
                    if any(k.arg is not None for k in s.value.keywords):
                        raise Blocked("`super().__init__(...)` 用了关键字参数，暂时不能导")
                    # 沿 MRO 找下一个有 __init__ 的类，把它的字段构建到同一个实例上
                    mro = self._mro(self.super_base)
                    tail = self._super_search_tail(s.value.func.value, mro, mro[1:])
                    nxt = next((c for c in tail
                                if "__init__" in self.classes.get(c, {}).get("methods", {})),
                               None)
                    if nxt is None:
                        raise Blocked(
                            "`super().__init__()` 在继承链上找不到下一个 `__init__`"
                            "（Python 里这层会调 object 的初始化，什么也不做；"
                            "建议把这一行删掉）")
                    args_code = ", ".join(self.expr(a) for a in s.value.args)
                    self.put(f"{self.check_name('self', '实例')} = "
                             f"{self.check_name(f'构建{nxt}', '祖先类构造函数')}"
                             f"({self._append_globals(args_code)})")
                    return
                # xs.append(x) → xs = 追加(xs, x)
                # Python 原地改列表，AGL 追加返回新列表；重新赋值后语义等价。
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr == "append"
                        and len(s.value.args) == 1
                        and isinstance(s.value.func.value, (ast.Name, ast.Subscript))):
                    receiver = self.expr(s.value.func.value)
                    arg = self.expr(s.value.args[0])
                    self.put(f"{receiver} = {self.builtin_of('append')}({receiver}, {arg})")
                    return
                # xs.sort() → xs = 排序(xs)
                # Python 原地排序返回 None，AGL 排序返回新列表；重新赋值后等价。
                # 带 key / reverse 的写法自动转 按键排序 / 反转（与 sorted() 同一套）。
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr == "sort"
                        and isinstance(s.value.func.value, (ast.Name, ast.Subscript))):
                    if s.value.keywords:
                        keyf = None
                        rev = None
                        for k in s.value.keywords:
                            if k.arg == "key":
                                keyf = self._sorted_key(k.value)
                            elif k.arg == "reverse":
                                if not (isinstance(k.value, ast.Constant)
                                        and isinstance(k.value.value, bool)):
                                    raise Blocked(
                                        "`xs.sort(reverse=...)` 的 reverse 必须是常量 真/假")
                                rev = k.value.value
                            else:
                                raise Blocked(
                                    f"`xs.sort({k.arg}=...)` 的参数不认识")
                        receiver = self.expr(s.value.func.value)
                        inner = f"{self.builtin_of('sorted')}({receiver})"
                        if keyf is not None:
                            inner = f"{self.builtin_of('sort_by')}({receiver}, {keyf})"
                        if rev:
                            inner = f"{self.builtin_of('reverse')}({inner})"
                        self.put(f"{receiver} = {inner}")
                        self.warnings.append(
                            "`xs.sort(...)` 被翻译成重新赋值："
                            "Python 原地排序返回 None，AGL 排序返回新列表，两边行为一致")
                        return
                    if len(s.value.args) != 0:
                        raise Blocked("`xs.sort()` 不接受位置参数（排序参数都走关键字）")
                    receiver = self.expr(s.value.func.value)
                    self.put(f"{receiver} = {self.builtin_of('sorted')}({receiver})")
                    self.warnings.append(
                        "`xs.sort()` 被翻译成重新赋值："
                        "Python 原地排序返回 None，AGL 排序返回新列表，两边行为一致")
                    return
                # ── 列表/映射方法族（v0.30）：语句位置直译成重新赋值 ──
                # xs.remove(x) → xs = 移除元素(xs, x)
                # xs.insert(i, x) → xs = 插入(xs, i, x)
                # xs.reverse() → xs = 反转(xs)
                # d.update(m) → d = 合并映射(d, m)
                # xs.pop()（0 参，列表专属）→ xs = 切片(xs, 0, xs.长度 - 1)（丢尾值）
                # d.pop(k, 默认)（2 参，映射专属）→ d = 弹出键(d, k, 默认)[1]
                # xs.pop(i) / d.pop(k)（1 参）无法静态区分列表还是映射 → 精确拒绝
                # xs.clear() / d.clear() 无法静态区分 → 精确拒绝（列表用 `xs = []`、
                #   映射用 `d = 建映射()`）
                if (isinstance(s.value.func, ast.Attribute)
                        and isinstance(s.value.func.value, (ast.Name, ast.Subscript))):
                    recv_attr = s.value.func.attr
                    receiver = self.expr(s.value.func.value)
                    if recv_attr == "remove" and len(s.value.args) == 1 \
                            and not s.value.keywords:
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('remove')}({receiver}, "
                                 f"{self.expr(s.value.args[0])})")
                        return
                    if recv_attr == "insert" and len(s.value.args) == 2 \
                            and not s.value.keywords:
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('insert')}({receiver}, "
                                 f"{self.expr(s.value.args[0])}, "
                                 f"{self.expr(s.value.args[1])})")
                        return
                    if recv_attr == "reverse" and not s.value.args \
                            and not s.value.keywords:
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('reverse')}({receiver})")
                        self.warnings.append(
                            "`xs.reverse()` 被翻译成重新赋值："
                            "Python 原地反转返回 None，AGL 反转返回新列表，两边行为一致")
                        return
                    if recv_attr == "update" and len(s.value.args) == 1 \
                            and not s.value.keywords:
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('merge_map')}({receiver}, "
                                 f"{self.expr(s.value.args[0])})")
                        self.warnings.append(
                            "`d.update(m)` 被翻译成重新赋值："
                            "Python 原地合并返回 None，AGL 合并映射返回新映射，两边行为一致")
                        return
                    if recv_attr == "clear" and not s.value.args \
                            and not s.value.keywords:
                        # 运行时按类型返回空容器（列表 [] / 映射 建映射() / 集合 建集合()）
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('clear')}({receiver})")
                        self.warnings.append(
                            "`x.clear()` 被翻译成 `x = 清空(x)`："
                            "AGL 值语义下清空返回新的空容器，"
                            "Python 原地清空返回 None，两边观察到的 x 一致")
                        return
                    if recv_attr == "pop" and not s.value.args \
                            and not s.value.keywords:
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('slice')}"
                                 f"({receiver}, 0, {receiver}.{self.attr_of('length')} - 1)")
                        self.warnings.append(
                            "语句位置的 `xs.pop()` 被翻译成重新绑定列表："
                            "弹出的值被丢弃（Python 语句位置也丢弃），剩下部分语义一致")
                        return
                    if recv_attr == "pop" and len(s.value.args) == 1 \
                            and not s.value.keywords:
                        # 1 参：列表 xs.pop(i) / 映射 d.pop(k) —— 泛型 弹出 运行时分发
                        i_code = self.expr(s.value.args[0])
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('pop')}"
                                 f"({receiver}, {i_code})[1]")
                        self.warnings.append(
                            "语句位置的 `x.pop(p)` 被翻译成重新绑定容器："
                            "弹出的值被丢弃（Python 语句位置也丢弃），"
                            "剩下部分由 `弹出` 按实参类型运行时分发"
                            "（列表按下标、映射按键），语义一致")
                        return
                    if recv_attr == "pop" and len(s.value.args) == 2 \
                            and not s.value.keywords:
                        k_code = self.expr(s.value.args[0])
                        dflt = self.expr(s.value.args[1])
                        self.put(f"{receiver} = "
                                 f"{self.builtin_of('pop_key')}"
                                 f"({receiver}, {k_code}, {dflt})[1]")
                        self.warnings.append(
                            "语句位置的 `d.pop(k, 默认)` 被翻译成重新绑定映射："
                            "弹出的值被丢弃（Python 语句位置也丢弃）")
                        return
                    if recv_attr == "copy" and not s.value.args \
                            and not s.value.keywords:
                        # Python 返回拷贝但语句位置丢弃；AGL 值语义无需拷贝
                        self.warnings.append(
                            "`x.copy()` 被翻译成空操作：AGL 是值语义，"
                            "「拷贝一份」没有意义")
                        return
                # xs.sort() / xs.reverse() / xs.clear() → 重绑成新列表
                # Python 原地改返回 None，AGL 返回新列表；重新赋值后语义等价。
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr in ("sort", "reverse", "clear")
                        and not s.value.args
                        and not s.value.keywords
                        and isinstance(s.value.func.value, ast.Name)
                        and s.value.func.value.id in self.list_vars):
                    receiver = self.expr(s.value.func.value)
                    meth = s.value.func.attr
                    if meth == "sort":
                        builtin = "sort"
                    elif meth == "reverse":
                        builtin = "reverse"
                    else:  # clear
                        builtin = "clear"
                    self.put(f"{receiver} = {self.builtin_of(builtin)}({receiver})")
                    self.warnings.append(
                        f"`xs.{meth}()` 被翻译成重新赋值："
                        "Python 原地改列表，AGL 返回新列表，两边行为一致")
                    return
                # xs.insert(i, x) → xs = 插入(xs, i, x)
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr == "insert"
                        and len(s.value.args) == 2
                        and not s.value.keywords
                        and isinstance(s.value.func.value, ast.Name)
                        and s.value.func.value.id in self.list_vars):
                    receiver = self.expr(s.value.func.value)
                    i_code = self.expr(s.value.args[0])
                    x_code = self.expr(s.value.args[1])
                    self.put(f"{receiver} = {self.builtin_of('insert')}({receiver}, {i_code}, {x_code})")
                    self.warnings.append(
                        "`xs.insert(i, x)` 被翻译成重新赋值："
                        "Python 原地改列表，AGL 返回新列表，两边行为一致")
                    return
                # d.clear() → d = 清空(d)
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr == "clear"
                        and not s.value.args
                        and not s.value.keywords
                        and isinstance(s.value.func.value, ast.Name)
                        and s.value.func.value.id in self.dict_vars):
                    receiver = self.expr(s.value.func.value)
                    self.put(f"{receiver} = {self.builtin_of('clear')}({receiver})")
                    self.warnings.append(
                        "`d.clear()` 被翻译成重新赋值："
                        "Python 原地清空映射，AGL 返回新空映射，两边行为一致")
                    return
                # s.add(x) → s = 添加(s, x)
                # Python 原地加元素返回 None，AGL 添加 返回新集合；重新赋值后语义等价。
                # （表达式位置的 `y = s.add(x)` 在 call() 里被精确拒绝。）
                if (isinstance(s.value.func, ast.Attribute)
                        and s.value.func.attr in ("add", "discard")
                        and len(s.value.args) == 1
                        and isinstance(s.value.func.value, ast.Name)
                        and s.value.func.value.id in self.set_vars):
                    receiver = self.expr(s.value.func.value)
                    arg = self.expr(s.value.args[0])
                    if s.value.func.attr == "add":
                        self.put(f"{receiver} = {self.builtin_of('add')}({receiver}, {arg})")
                    else:
                        # discard：去掉元素、不存在也不报错 → 差集({x})；
                        # remove 会抛 KeyError，语义不同，走 call() 拒绝
                        self.put(f"{receiver} = {self.builtin_of('difference')}"
                                 f"({receiver}, {{{arg}}})")
                    self.warnings.append(
                        f"`{s.value.func.value.id}.{s.value.func.attr}(x)` 被翻译成重新赋值："
                        "Python 原地改集合，AGL 返回新集合，两边行为一致")
                    return
                self.put(self.expr(s.value))
                return
            raise Blocked("这种孤立的表达式（既没赋值也没调用）暂时不能导")
        if isinstance(s, ast.Try):
            self.try_block(s)
            return
        if isinstance(s, ast.Assert):
            # assert 条件[, 消息] → 断言(条件[, 消息])：假时抛可捕获的中文断言错误
            cond = self.expr(s.test)
            if s.msg is not None:
                self.put(f"{self.builtin_of('assert')}({cond}, {self.expr(s.msg)})")
            else:
                self.put(f"{self.builtin_of('assert')}({cond})")
            return
        if isinstance(s, ast.Raise):
            self._raise_stmt(s)
            return
        if isinstance(s, ast.Import):
            what = ".".join(a.name for a in s.names)
            # time 模块的 sleep / time / perf_counter / monotonic 已被特殊处理，放行 import time
            if what == "time":
                self.warnings.append(
                    "`import time` 被忽略：只有 `time.sleep()` / `time.time()` / "
                    "`time.perf_counter()` / `time.monotonic()` 被翻译成 AGL 内建，"
                    "time 模块的其他功能请走 AGL 的 FFI 声明")
                return
            # json / requests / urllib.request：内建直译（解析JSON/生成JSON/获取网页）
            if what in ("json", "requests", "urllib.request", "urllib"):
                self._note_json_net(what)
                return
            # math / random：常用函数直译成 AGL 内建（上取整/下取整/随机整数/随机选择/随机种子等），
            # 其余属性走 FFI 声明
            if what == "math":
                self.warnings.append(
                    "`import math` 被忽略：`math.ceil()` / `math.floor()` 等已翻译成 "
                    "AGL 内建（上取整 / 下取整），math 的其他函数请走 AGL 的 FFI 声明")
                return
            if what == "random":
                self.warnings.append(
                    "`import random` 被忽略：`random.randint()` / `random.choice()` / "
                    "`random.seed()` 已翻译成 AGL 内建（随机整数 / 随机选择 / 随机种子），"
                    "random 的其他功能请走 AGL 的 FFI 声明")
                return
            # asyncio（v0.39）：asyncio.run(x) / asyncio.sleep(s) 有专属翻译；
            # AGL 的并发模型（任务/等待/睡眠）与 Python asyncio 不同，其余
            # asyncio API 请走 FFI 声明
            if what == "asyncio":
                self.warnings.append(
                    "`import asyncio` 被忽略：`asyncio.run(x)` 被直译成调用 `x`"
                    "（AGL 运行时自动驱动异步入口），`asyncio.sleep(s)` 被翻译成"
                    " `等待 睡眠(取整(s * 1000))`；asyncio 的其他功能请走 FFI 声明")
                return
            raise Blocked(
                f"`import {what}` 暂时不能导：外部库要走 AGL 的 FFI 声明"
                f"（`导入 外部 {what}`）并补上效应，不能由导入器替你决定")
        if isinstance(s, ast.ImportFrom):
            mod = s.module or ""
            # `from time import ...` 与 `import time` 同一条路放行：
            # sleep 等已被翻译成 AGL 内建，导入语句本身忽略即可。
            if mod == "time":
                self.warnings.append(
                    "`from time import ...` 被忽略：只有 time.sleep() / time.time() / "
                    "time.perf_counter() / time.monotonic() / time.strftime() 被翻译成 AGL 内建，"
                    "time 模块的其他功能请走 AGL 的 FFI 声明")
                return
            # json / urllib.request / requests 的 from-import 同样放行
            if mod in ("json", "requests", "urllib.request", "urllib"):
                self._note_json_net(mod)
                return
            # math / random 的 from-import：ceil/floor/randint/choice/seed 已在
            # 调用处翻译成 AGL 内建，导入语句本身放行即可
            if mod in ("math", "random"):
                self.warnings.append(
                    f"`from {mod} import ...` 被忽略："
                    f"{'`ceil()` / `floor()` 已翻译成 AGL 内建（上取整 / 下取整）' if mod == 'math' else '`randint()` / `choice()` / `seed()` 已翻译成 AGL 内建（随机整数 / 随机选择 / 随机种子）'}，"
                    "其余功能请走 AGL 的 FFI 声明")
                return
            # v0.48：collections 的 from-import——警告一下，放行
            if mod == "collections":
                self.warnings.append(
                    f"`from {mod} import ...` 被忽略："
                    "OrderedDict / defaultdict 等请走 AGL 的 FFI 声明，"
                    "日常用普通字典就行")
                return
            raise Blocked(
                f"`from {mod} import ...` 暂时不能导：外部库要走 AGL 的 FFI 声明"
                f"（`导入 外部 {mod}`）并补上效应，不能由导入器替你决定")
        if isinstance(s, ast.Global):
            # v0.38：global 声明的名字被函数体改写 → 该常量转「可变全局」，
            # 函数里按 _agl_globals 读写（与 Python global 语义一致）；
            # 只读的（没被改写）保持"读常量函数"，声明放行并说明。
            for name in s.names:
                if name not in self.global_consts:
                    raise Blocked(
                        f"`global {name}` 指向的模块级变量没有定义"
                        "（或不是只读常量）：AGL 没有全局变量，"
                        "只能把文件顶层的 `名字 = ...` 翻译成同名只读常量函数，"
                        "函数内 `global` 声明的名字必须对应这样的常量")
            if all(n in self.mutable_globals for n in s.names):
                self.warnings.append(
                    "`global ...` 声明的变量在函数里按「模块级共享存储」读写："
                    "AGL 的 `_agl_globals` 映射承载了 Python 全局变量的改写语义，"
                    "读/写同源，与 `global` 一致")
            else:
                self.warnings.append(
                    "`global ...` 声明被忽略：AGL 没有全局变量；"
                    "对应的模块级只读常量在函数里本来就按「读常量函数」处理。"
                    "若原代码会在函数里给这些变量赋值或原地修改，请手动改写")
            return
        if isinstance(s, ast.Nonlocal):
            # v0.47：nonlocal 语句简化支持——不报错，加 warning 说明按值传递
            names = ", ".join(s.names)
            self.warnings.append(
                f"`nonlocal {names}` 被忽略：AGL 的嵌套函数是值传递（捕获变量变显式参数），"
                "内层函数修改外层变量不会影响外层原值（nonlocal 语义不支持）；"
                "如果原代码依赖 nonlocal 的回写行为，请手动改成返回新值的形式")
            return
        if isinstance(s, ast.Delete):
            # v0.47：支持 del 多个目标（del a, b, c）——循环处理每个 target
            for t in s.targets:
                # v0.48：动态删除类属性——del MyClass.x → 删属性(MyClass, "x")
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    cls_name = t.value.id
                    attr_name = t.attr
                    self.warnings.append(
                        f"`del {cls_name}.{attr_name}`：动态删除类属性，"
                        "被翻译成 删属性() 内建调用")
                    self.put(f"{self.builtin_of('delattr')}({cls_name}, \"{attr_name}\")")
                    continue
                if not isinstance(t, ast.Subscript):
                    raise Blocked("`del` 暂时只支持 `del 容器[键]` 或 `del 类.属性` 这两种写法")
                kind = self._pop_kind(t.value)
                base_code = self.expr(t.value)
                if kind == "dict":
                    # del d[k] → d = 删除键(d, k)
                    self.put(f"{base_code} = {self.builtin_of('del_key')}({base_code}, {self.expr(t.slice)})")
                    continue
                if kind == "list" and isinstance(t.slice, ast.Constant) \
                        and isinstance(t.slice.value, int):
                    # del xs[i] → xs = 切片(xs, 0, i) + 切片(xs, i + 1, xs.长度)
                    idx = self.expr(t.slice)
                    self.put(
                        f"{base_code} = {self.builtin_of('slice')}({base_code}, 0, {idx})"
                        f" + {self.builtin_of('slice')}({base_code}, {idx} + 1, {base_code}.{self.attr_of('length')})")
                    continue
                raise Blocked(
                    f"`del {self.expr(t)}` 暂时不能导："
                    "AGL 的删除必须知道是列表还是映射表（值语义要重绑），"
                    "这个变量没有登记过类型；请手写重审（映射用 `删除键`，列表用切片重绑）")
            return
        if isinstance(s, ast.With) or isinstance(s, ast.AsyncWith):
            self.with_stmt(s)
            return
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 嵌套函数：提升成模块级函数 + 捕获变量变显式参数（v0.37）。
            # v0.39：异步嵌套函数放开——外层是异步函数时，内层 async
            # 提升为模块级异步函数，调用点 `await inner(...)` →
            # `等待 闭包N(caps, ...)`，await 语义随表达式分发天然穿透。
            # v0.43：同步函数里的异步嵌套函数也允许提升（加 warning）——
            # 同步函数体内不能 await 所以不能直接调用，但可以把它定义出来。
            if isinstance(s, ast.AsyncFunctionDef) and not self.cur_fn_is_async:
                self.warnings.append(
                    f"同步函数里嵌套定义了异步函数 `{s.name}`："
                    "它被提升成模块级异步函数，但同步函数体内不能 `等待`，"
                    "所以你不能在这里直接调用它；请把外层函数改成 async，"
                    "或把内层改成普通嵌套函数")
            self._lift_nested_fn(s)
            return
        raise Blocked(f"暂时不能导的语句 `{type(s).__name__}`")

    def with_stmt(self, s) -> None:
        """`with 表达式 [as 名]:` → AGL `使用 (表达式) [作为 名] { … }`。

        AGL 的 `使用` 是原生上下文管理器（发射宿主 with 语句），
        支持宿主对象（文件、锁等）；多上下文 `with a as x, b as y:`
        是嵌套语义，AGL 一次一个，精确拒绝。
        """
        if isinstance(s, ast.AsyncWith):
            self.warnings.append(
                "`async with` 被直译成 AGL 的 `使用`：AGL 的 `使用` 没有异步变体，"
                "若对象是异步上下文管理器，请人工确认语义")
        if len(s.items) != 1:
            # v0.48：多上下文管理器 with——先把所有 open 都登记成文件句柄，
            # 然后统一翻译 body，最后统一清理（和单个 with open 语义一致）
            self.warnings.append(
                f"`with` 带了 {len(s.items)} 个上下文："
                "自动展开成嵌套形式，所有上下文都登记后再执行块内代码")
            new_handles = []
            for item in s.items:
                if (isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Name)
                        and item.context_expr.func.id == "open"
                        and item.optional_vars is not None
                        and isinstance(item.optional_vars, ast.Name)):
                    e = item.context_expr
                    path_code = self.expr(e.args[0])
                    mode = "r"
                    if len(e.args) == 2:
                        if isinstance(e.args[1], ast.Constant) and isinstance(e.args[1].value, str):
                            mode = e.args[1].value
                    name = self.check_name(item.optional_vars.id, "绑定名")
                    self.file_handles[name] = (path_code, mode)
                    new_handles.append(name)
                    self.put(f"{self.kw_of('let')} {name}: {self.kw_of('Any')} = {self.kw_of('Null')}")
                else:
                    raise Blocked(
                        f"`with` 里第 {len(new_handles)+1} 个上下文不是 open()："
                        "多上下文管理器目前只支持 open() 文件句柄形式")
            try:
                self.block(s.body)
            finally:
                for name in new_handles:
                    del self.file_handles[name]
            return
        item = s.items[0]
        # v0.45：with open(path, mode) as f: —— 登记 f 为文件句柄，
        # 后面 f.read() / f.write() 特殊翻译成读文件/写文件内建
        if (isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "open"
                and item.optional_vars is not None
                and isinstance(item.optional_vars, ast.Name)):
            e = item.context_expr
            if len(e.args) < 1 or len(e.args) > 2:
                raise Blocked(
                    "`open(path, mode)` 只接受路径和模式两个参数")
            path_code = self.expr(e.args[0])
            mode = "r"
            if len(e.args) == 2:
                if isinstance(e.args[1], ast.Constant) and isinstance(e.args[1].value, str):
                    mode = e.args[1].value
                else:
                    raise Blocked(
                        "`open(path, mode)` 的 mode 必须是字符串常量（如 \"r\" / \"w\"）")
            name = self.check_name(item.optional_vars.id, "绑定名")
            self.file_handles[name] = (path_code, mode)
            self.warnings.append(
                f"`with open(path, \"{mode}\") as {name}:` 被翻译成「变量登记为文件句柄」："
                f"后面 {name}.read() / {name}.write() 会自动映射到读文件/写文件内建；"
                "AGL 没有文件句柄对象，不会真的打开文件")
            # 直接翻译 with 块，不用 AGL 的 使用 语句
            self.put(f"{self.kw_of('let')} {name}: {self.kw_of('Any')} = {self.kw_of('Null')}")
            try:
                self.block(s.body)
            finally:
                del self.file_handles[name]
            return
        # v0.42：用户自定义上下文管理器（有 __enter__）手动展开成 try/finally
        ctx_cls = self._expr_instance_cls(item.context_expr)
        if ctx_cls is not None and self._resolve_dunder_owner(ctx_cls, "__enter__") is not None:
            owner_enter = self._resolve_dunder_owner(ctx_cls, "__enter__")
            owner_exit = self._resolve_dunder_owner(ctx_cls, "__exit__")
            ctx_var = self._fresh_name("上下文")
            enter_fn = self.check_name(f"{owner_enter}___enter__", "上下文进入方法")
            self.put(f"{self.kw_of('let')} {ctx_var} = {self.expr(item.context_expr)}")
            bind = ""
            if item.optional_vars is not None:
                name = self.check_name(item.optional_vars.id, "绑定名")
                self.put(f"{self.kw_of('let')} {name} = {enter_fn}({ctx_var})")
                # __enter__ 约定返回 self，把绑定名登记为同类型实例
                self.instances[name] = ctx_cls
            else:
                self.put(f"{enter_fn}({ctx_var})")
            self.put(f"{self.kw_of('try')} {{")
            self.block(s.body)
            self.put(f"}} {self.kw_of('finally')} {{")
            if owner_exit is not None:
                exit_fn = self.check_name(f"{owner_exit}___exit__", "上下文退出方法")
                self.put(f"{exit_fn}({ctx_var}, {self.literal(ast.Constant(value=None))}, {self.literal(ast.Constant(value=None))}, {self.literal(ast.Constant(value=None))})")
            self.put("}")
            return
        expr = self.expr(item.context_expr)
        if item.optional_vars is not None:
            name = self.check_name(item.optional_vars.id, "绑定名")
            self.put(f"{self.kw_of('with')} ({expr}) {self.kw_of('as')} {name} {{")
        else:
            self.put(f"{self.kw_of('with')} ({expr}) {{")
        self.block(s.body)
        self.put("}")

    def for_loop(self, s: ast.For) -> None:
        iter_node = s.iter
        # ── 解包循环：for a, b in enumerate/zip/列表 ───────────────
        if isinstance(s.target, ast.Tuple):
            self._for_unpack(s)
            return
        if not isinstance(s.target, ast.Name):
            raise Blocked("循环的循环变量只能是单个名字")
        var = self.check_name(s.target.id, "循环变量")
        head = None
        # ── map / filter 内联：for y in map(f, xs) ≡ for x in xs: y = f(x)
        #    filter 同理包一层 如果 —— 单次消费时与惰性迭代器完全等价，
        #    副作用顺序也一致，无需语义警告。
        if (isinstance(iter_node, ast.Call)
                and isinstance(iter_node.func, ast.Name)
                and iter_node.func.id in ("map", "filter")
                and not iter_node.keywords):
            self._for_map_filter(s, var, iter_node)
            return
        # `for i in range(a, b, c)`：AGL 没有 range，展开成 while 计数循环。
        # step 为常负数时倒着走；step 不是常量就拒绝（语义没法静态确定）。
        if (isinstance(iter_node, ast.Call)
                and isinstance(iter_node.func, ast.Name)
                and iter_node.func.id == "range"
                and not iter_node.keywords):
            args = iter_node.args
            if len(args) == 1:
                lo, hi, step = ast.Constant(value=0), args[0], ast.Constant(value=1)
            elif len(args) == 2:
                lo, hi, step = args[0], args[1], ast.Constant(value=1)
            elif len(args) == 3:
                lo, hi, step = args[0], args[1], args[2]
            else:
                raise Blocked("`range()` 的参数个数不对")
            if not (isinstance(step, ast.Constant) and isinstance(step.value, int)):
                raise Blocked("循环的步长不是固定整数时不能安全展开")
            step_v = step.value
            if step_v == 0:
                raise Blocked("`range()` 的步长不能是 0")
            if s.orelse:
                self.warnings.append(
                    "`for ... else ...` 直译成展开式 `当` 循环的 `否则` 块："
                    "循环没被 `中断` 打断才执行，与 Python 一致")
            # 正数步长往上数、负数步长往下数，只是比较方向不同
            cmp_word = self.op_of("<" if step_v > 0 else ">")
            let_word = self.kw_of("let") if s.target.id not in self.declared else ""
            self.declared.add(s.target.id)
            self.put(f"{let_word} {var} = {self.expr(lo)}".strip())
            self.put(f"{self.kw_of('while')} ({var} {cmp_word} {self.expr(hi)}) {{")
            self.block(s.body)
            op = "+" if step_v > 0 else "-"
            self.put(f"  {var} = ({var} {op} {abs(step_v)})")
            self.put("}")
            if s.orelse:
                self.put(f"{self.kw_of('else')} {{")
                self.block(s.orelse)
                self.put("}")
            self.warnings.append(
                "`for ... in range(...)` 被展开成计数式 `当` 循环："
                "range 序列 Python 会一次算好，展开后每次都比较，"
                "所以循环体里再改上下界，两边的次数会不一样")
            return
        # v0.42：自定义对象迭代 for x in obj（有 __iter__）→ while + __next__ + 空值哨兵
        iter_cls = self._expr_instance_cls(iter_node)
        if iter_cls is not None and self._resolve_dunder_owner(iter_cls, "__iter__") is not None:
            owner_iter = self._resolve_dunder_owner(iter_cls, "__iter__")
            owner_next = self._resolve_dunder_owner(iter_cls, "__next__")
            it_var = self._fresh_name("迭代器")
            iter_fn = self.check_name(f"{owner_iter}___iter__", "迭代器方法")
            next_fn = self.check_name(f"{owner_next}___next__", "取下一个方法")
            self.put(f"{self.kw_of('let')} {it_var} = {iter_fn}({self.expr(iter_node)})")
            self.put(f"{self.kw_of('while')} {self.literal(ast.Constant(value=True))} {{")
            self.put(f"{self.kw_of('let')} {var} = {next_fn}({it_var})")
            self.put(f"{self.kw_of('if')} ({var} == {self.literal(ast.Constant(value=None))}) {{ {self.kw_of('break')} }}")
            self.block(s.body)
            self.put("}")
            return
        if isinstance(iter_node, (ast.Name, ast.Attribute, ast.List, ast.Subscript, ast.Call)):
            head = f"{self.kw_of('for')} {var} {self.kw_of('in')} {self.expr(iter_node)} {{"
        else:
            raise Blocked("这种 `for ... in ...` 的写法暂时不能导")
        self.declared.add(s.target.id)
        self.put(head)
        self.block(s.body)
        self.put("}")
        if s.orelse:
            self.warnings.append(
                "`for ... else ...` 直译成 `遍历` 循环的 `否则` 块："
                "循环没被 `中断` 打断才执行，与 Python 一致")
            self.put(f"{self.kw_of('else')} {{")
            self.block(s.orelse)
            self.put("}")

    def _for_unpack(self, s: ast.For) -> None:
        """`for a, b in ...` 解包循环：enumerate / zip / 元素是元组的列表。

        AGL 的循环只能遍历单个值，解包一律展开成计数式 `当` 循环 + 下标，
        语义与 Python 完全等价（enumerate 的索引变量就是循环计数变量，
        zip 取最短长度，普通列表逐元素取第 j 个成员）。
        """
        targets = s.target.elts
        if not targets or not all(isinstance(t, ast.Name) for t in targets):
            raise Blocked("解包循环的循环变量必须都是单个名字")
        names = [self.check_name(t.id, "循环变量") for t in targets]
        it = s.iter
        # enumerate(xs[, start=N]) → 索引 + 取元素
        if (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                and it.func.id == "enumerate"):
            if len(names) != 2:
                raise Blocked("`enumerate(...)` 解包只给两个变量：索引和元素")
            if any(k.arg is None for k in it.keywords):
                raise Blocked("`enumerate(...)` 带 `**` 展开，暂时不能导")
            kw = {k.arg: k.value for k in it.keywords}
            if kw and set(kw) != {"start"}:
                raise Blocked(
                    "`enumerate(xs)` / `enumerate(xs, start=N)` 之外的写法暂时不能导")
            idx, val = names[0], names[1]
            if len(it.args) == 1:
                src = self.expr(it.args[0])
                start = "0"
                elem = f"{src}[{idx}]"
            elif len(it.args) == 2:
                # enumerate(xs, start) 位置写法：与 start= 关键字等价
                st = it.args[1]
                if not (isinstance(st, ast.Constant)
                        and isinstance(st.value, int)):
                    raise Blocked(
                        "`enumerate(xs, start)` 的 start 必须是常量整数："
                        "展开成计数循环时 start 要参与下标换算，非常量没法静态对齐")
                src = self.expr(it.args[0])
                start = self.expr(st)
                elem = f"{src}[{idx} - {start}]"
                if kw:
                    raise Blocked(
                        "`enumerate(xs, start)` 位置与关键字不能同时给 start")
            else:
                raise Blocked("`enumerate(...)` 只接受一个序列参数（外加可选 start）")
            if "start" in kw:
                st = kw["start"]
                if not (isinstance(st, ast.Constant)
                        and isinstance(st.value, int)):
                    raise Blocked(
                        "`enumerate(xs, start=...)` 的 start 必须是常量整数："
                        "展开成计数循环时 start 要参与下标换算，非常量没法静态对齐")
                start = self.expr(st)
                # enumerate(xs, start=s) 的第 n 个元素是 (s+n, xs[n])：
                # 计数变量从 s 起，元素必须回退 start 再取下标
                elem = f"{src}[{idx} - {start}]"
            self._unpack_tail(s, idx,
                              f"({start} + {src}.{self.attr_of('length')})",
                              [f"{val} = {elem}"], start=start)
            return
        # zip(a, b, ...) → 最短长度，逐个取元素
        if (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                and it.func.id == "zip"):
            if it.keywords:
                raise Blocked("`zip(...)` 带关键字参数，暂时不能导")
            if not it.args:
                raise Blocked("`zip()` 至少要有一个序列")
            if len(it.args) != len(names):
                raise Blocked("`zip(...)` 的序列个数必须和解包变量个数一样")
            srcs = [self.expr(a) for a in it.args]
            if len(srcs) == 1:
                limit = f"{srcs[0]}.{self.attr_of('length')}"
            else:
                limit = (f"{self.builtin_of('min')}({srcs[0]}.{self.attr_of('length')}, "
                         f"{srcs[1]}.{self.attr_of('length')})")
                for extra in srcs[2:]:
                    limit = (f"{self.builtin_of('min')}({limit}, "
                             f"{extra}.{self.attr_of('length')})")
            i_var = self._fresh_name("_i")
            assigns = [f"{n} = {srcs[j]}[{i_var}]" for j, n in enumerate(names)]
            self._unpack_tail(s, i_var, limit, assigns)
            return
        # 普通列表：元素是元组/列表 → 下标再取成员
        if isinstance(it, (ast.Name, ast.Attribute, ast.List, ast.Subscript)):
            src = self.expr(it)
            i_var = self._fresh_name("_i")
            assigns = [f"{n} = {src}[{i_var}][{j}]" for j, n in enumerate(names)]
            self._unpack_tail(s, i_var, f"{src}.{self.attr_of('length')}", assigns)
            return
        # d.items()：for k, v in d.items() → 遍历 键值对列表(d) 的下标解包
        # （键值对列表每项是 [键, 值]，与「元素是元组的列表」同构）
        if (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                and it.func.attr == "items"
                and not it.args and not it.keywords):
            recv = self.expr(it.func.value)
            pairs = f"{self.builtin_of('items')}({recv})"
            i_var = self._fresh_name("_i")
            assigns = [f"{n} = {pairs}[{i_var}][{j}]" for j, n in enumerate(names)]
            self._unpack_tail(s, i_var, f"{pairs}.{self.attr_of('length')}", assigns)
            return
        raise Blocked(
            "解包循环（`for a, b in ...`）目前支持 `enumerate` / `zip` / "
            "`d.items()` / 元素是元组的列表；其他写法暂时不能导，请先用普通循环 + 下标")

    def _unpack_tail(self, s: ast.For, i_var: str, limit: str,
                     assigns: list[str], start: str | None = None) -> None:
        """解包循环公共尾部：变量 i = start；当 (i < limit) { 赋值…; body; i += 1 }"""
        let_word = self.kw_of("let") if i_var not in self.declared else ""
        self.declared.add(i_var)
        self.put(f"{let_word} {i_var} = {start if start is not None else '0'}".strip())
        self.put(f"{self.kw_of('while')} ({i_var} < {limit}) {{")
        self.depth += 1
        for a in assigns:
            self.put(a)
        self.block(s.body)
        self.put(f"{i_var} = ({i_var} + 1)")
        self.depth -= 1
        self.put("}")
        if s.orelse:
            self.warnings.append(
                "`for ... else ...` 直译成解包展开式 `当` 循环的 `否则` 块："
                "循环没被 `中断` 打断才执行，与 Python 一致")
            self.put(f"{self.kw_of('else')} {{")
            self.block(s.orelse)
            self.put("}")

    def _for_map_filter(self, s: ast.For, var: str, it: ast.Call) -> None:
        """`for y in map(f, xs)` / `for y in filter(p, xs)` 内联展开。

        map → `对 xs 中的 x { y = f(x) ... }`；filter → `对 xs 中的 y {
        如果 (p(y)) { ... } }`。单次消费时与惰性迭代器完全等价，
        副作用顺序也一致，所以不产生语义警告。
        """
        if s.orelse:
            raise Blocked(
                "`for ... else ...` 暂时不能导：AGL 的 `遍历` 循环没有 "
                "「循环没被 `跳出` 打断才执行」的 `否则` 分支。"
                "把 else 块的条件反过来写进循环体里。")
        kind = it.func.id
        if len(it.args) != 2:
            raise Blocked(
                f"`{kind}(...)` 用在 循环 里时只支持 `{kind}(函数, 序列)` 两个参数")
        fn_node, src_node = it.args
        if isinstance(fn_node, ast.Lambda):
            fn = self._fresh_name("_键")
            self._lift_lambda(fn, fn_node)
            fn = self.check_name(fn, "这里")
        elif isinstance(fn_node, ast.Name):
            fn = self.check_name(fn_node.id, "这里")
        else:
            raise Blocked(
                f"`{kind}()` 的第一个参数只支持函数名或 lambda："
                "AGL 没有函数值，方法/复杂表达式传不进来")
        if not isinstance(src_node, (ast.Name, ast.Attribute, ast.List, ast.Subscript)):
            raise Blocked(
                f"`{kind}()` 的序列部分只支持变量、成员、列表字面量或下标")
        src = self.expr(src_node)
        self.declared.add(s.target.id)
        if kind == "filter":
            # 循环变量就是原元素（= 过滤后的目标值）
            self.put(f"{self.kw_of('for')} {var} {self.kw_of('in')} {src} {{")
            self.depth += 1
            self.put(f"{self.kw_of('if')} ({fn}({var})) {{")
            self.depth += 1
            self.block(s.body)
            self.depth -= 1
            self.put("}")
            self.depth -= 1
            self.put("}")
        else:
            xv = self._fresh_name("_源")
            self.put(f"{self.kw_of('for')} {xv} {self.kw_of('in')} {src} {{")
            self.depth += 1
            self.put(f"{var} = {fn}({xv})")
            self.block(s.body)
            self.depth -= 1
            self.put("}")
        if s.orelse:
            self.warnings.append(
                "`for ... else ...` 直译成内联展开循环的 `否则` 块："
                "循环没被 `中断` 打断才执行，与 Python 一致")
            self.put(f"{self.kw_of('else')} {{")
            self.block(s.orelse)
            self.put("}")

    def _raise_stmt(self, s: ast.Raise) -> None:
        """`raise` 的失败语义表达。

        `raise X("消息")` → `取到(失败("消息"))`：AGL 的 `失败(...)` 造一个
        失败结果，`取到(...)` 把失败抛成可被 `捕获` 接住、且显示原因的运行时
        错误 —— 这正是 Python `raise` 在该有的位置上做的事。
        `except` 块里的裸 `raise`（重抛当前异常）翻译成
        `取到(失败(失败原因(捕获名)))`，与 Python 的重抛行为一致；
        块外的裸 raise 没有活动异常可重抛，精确拒绝。
        `raise ... from ...` 的「由哪个错误引起」没有对应语义，
        把原因并入失败消息（近似等价，挂警告）。
        """
        if s.cause is not None:
            if not isinstance(s.exc, ast.Call):
                raise Blocked(
                    "`raise ... from ...` 的抛出对象不是 `异常类(\"消息\")` 的形式，"
                    "暂时不能导；请先写成 `raise 异常类(\"消息\") from 原因`")
            if not s.exc.args:
                raise Blocked(
                    "`raise 异常类() from ...` 没有消息文本，没法把原因并入消息；"
                    "请给异常写一句消息")
            cause = s.exc.args[0]
            if isinstance(cause, ast.Constant) and isinstance(cause.value, str):
                msg = self.expr(cause)
            else:
                msg = f"{self.builtin_of('to_text')}({self.expr(cause)})"
            from_expr = s.cause
            if isinstance(from_expr, ast.Call):
                # `from TypeError('y')`：Python 显示的是这个异常的实例；
                # 这里只取它的消息文本并入失败原因（近似）
                if not from_expr.args:
                    raise Blocked(
                        "`raise ... from 异常类()`（不调用）暂时不能导："
                        "没有可并入消息的文本")
                a = from_expr.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    reason = self.expr(a)
                else:
                    reason = f"{self.builtin_of('to_text')}({self.expr(a)})"
                self.warnings.append(
                    "`raise ... from 异常类(...)` 的原因只取消息文本："
                    "Python 的异常链会保留整个异常对象，这里只能并入文本")
            elif isinstance(from_expr, (ast.Name, ast.Attribute, ast.Constant,
                                        ast.Subscript)):
                reason = self.expr(from_expr)
            else:
                raise Blocked(
                    "`raise ... from ...` 的「原因」只支持变量、成员、字面量、"
                    "下标或 `异常类(消息)`：其它写法暂时不能导")
            tname = (self._dotted(s.exc.func)
                     if isinstance(s.exc.func, (ast.Name, ast.Attribute)) else "异常")
            self.warnings.append(
                f"`raise {tname} ... from ...` 的原因被并入失败消息："
                "Python 的异常链会同时保留两个异常、可分别捕获，"
                "这里只把它写进消息文本，两边行为有差异请确认")
            self.put(
                f"{self.builtin_of('unwrap')}({self.builtin_of('Err')}("
                f"{self.builtin_of('join')}({msg}, "
                f"\"（由 \", {reason}, \" 引起）\")))")
            return
        if s.exc is None:
            if self._except_stack:
                cname = self._except_stack[-1]
                self.warnings.append(
                    "`except` 块里的裸 `raise`（重抛当前异常）翻译成 "
                    "`取到(失败(失败原因(捕获名)))`："
                    "Python 重抛的是原异常对象（保留类型和堆栈），"
                    "这里只有错误描述文本，行为有差异请确认")
                self.put(
                    f"{self.builtin_of('unwrap')}({self.builtin_of('Err')}("
                    f"{self.builtin_of('fail_reason')}({cname})))")
                return
            self.warnings.append(
                "块外的裸 `raise` 翻译成 `取到(失败(\"没有活动异常可重抛\"))`："
                "Python 运行到这里会抛 RuntimeError（没有活动异常可重抛），"
                "这里抛的是同位置的失败，`尝试/捕获` 接住的行为一致；"
                "类型信息不一样（Python 是 RuntimeError，这里是通用失败）请确认")
            self.put(
                f"{self.builtin_of('unwrap')}({self.builtin_of('Err')}("
                f"\"没有活动异常可重抛\"))")
            return
        exc = s.exc
        if not isinstance(exc, ast.Call):
            raise Blocked(
                "`raise 异常类`（不调用）暂时不能导：请写成 "
                "`raise 异常类(\"消息\")` 的形式，AGL 翻译成 `取到(失败(\"消息\"))`")
        if not exc.args:
            msg = '""'
        elif len(exc.args) == 1:
            a = exc.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                msg = self.expr(a)
            else:
                msg = f"{self.builtin_of('to_text')}({self.expr(a)})"
        else:
            parts = [self.expr(a) for a in exc.args]
            msg = f"{self.builtin_of('join')}({', '.join(parts)})"
            self.warnings.append(
                "`raise 异常(多个参数)` 的消息被 `拼接` 成一段文本："
                "Python 实际显示的是参数的元组形式，两边文本略有差异")
        tname = (self._dotted(exc.func)
                 if isinstance(exc.func, (ast.Name, ast.Attribute)) else "异常")
        self.warnings.append(
            f"`raise {tname} ...` 被翻译成 `取到(失败(...))`："
            "AGL 不区分异常类型，`捕获` 一律接住；"
            "原代码里按类型捕获的行为请人工确认")
        self.put(f"{self.builtin_of('unwrap')}({self.builtin_of('Err')}({msg}))")

    def try_block(self, s: ast.Try) -> None:
        # 直译：AGL 现在原生支持 `最后`/finally（尝试/捕获/最后），
        # try 体 / except 体里的 return/break/continue 不会跳过 finally，
        # finally 自己的 return/break/continue 也会正确覆盖结果，与 Python 一致。
        if not s.handlers and not s.finalbody:
            raise Blocked("一段 `try` 既没有 `except` 也没有 `finally`，没法表达")
        h = s.handlers[0] if len(s.handlers) == 1 else None
        multi_handlers = len(s.handlers) > 1
        # 类型化捕获：`except ValueError:` → `捕获 (错: 值错误)`（AGL v0.23 原生支持）。
        # 只精确映射"同层"异常类；子类（FileNotFoundError）与未知类型
        # 降级成抓所有 + 警告（原代码只抓那一类，行为有差异，必须让人确认）。
        _EXC_AGL = {
            "ValueError": "值错误", "TypeError": "类型错误", "KeyError": "键错误",
            "IndexError": "下标越界", "ZeroDivisionError": "零除",
            "OSError": "文件错误", "RuntimeError": "运行时错误",
            "AssertionError": "断言错误",
        }
        catch_type = None
        if h is not None and h.type is not None:
            indicated = self._dotted(h.type)
            if indicated == "BaseException":
                self.warnings.append(
                    "`except BaseException` 被当成「抓所有」处理："
                    "AGL 的 捕获 也是一网打尽，两边一致；"
                    "但这也意味着键盘中断这类也会被吃掉，建议确认这是你要的")
            elif indicated in _EXC_AGL:
                catch_type = _EXC_AGL[indicated]
                self.warnings.append(
                    f"`except {indicated}:` 直译成 `捕获 (错: {catch_type})`："
                    "只捕获这一类异常，其它异常继续往上冒，与 Python 一致")
            else:
                self.warnings.append(
                    f"`except {indicated}:` 被统一翻译成「捕获所有异常」："
                    f"原代码只捕获 {indicated}，其它异常会继续往上冒；"
                    f"AGL 的类型化捕获暂不覆盖 {indicated}，"
                    f"两边行为有差异，请确认这是你要的")
        if h is not None and h.type is None:
            self.warnings.append(
                "这里写的是裸 `except:`（不写类型）："
                "它会把一切异常都吃掉，包括你想不到的（比如按 Ctrl+C），"
                "AGL 里已经按「抓所有」处理，建议改成 `except Exception` 之类的明确写法")
        if s.finalbody:
            self.warnings.append(
                "`finally` 直译成 AGL 的 `最后` 块（原生 try/except/finally）："
                "try 体 / except 体里的 return/break/continue 不会跳过它，"
                "finally 自己里的 return/break/continue 也会正确覆盖结果，与 Python 一致")
        if s.orelse:
            self.warnings.append(
                "`try ... except ... else ...` 直译成 AGL 的 `否则` 块："
                "try 没出错才执行，与 Python 一致（try 体里的 return 会跳过它，"
                "finally 仍会执行）")
        if multi_handlers:
            self.warnings.append(
                f"`try` 带了 {len(s.handlers)} 个 `except` 分支："
                "统一翻译成「一个捕获所有 + 内部 if/elif 判断异常类型」，"
                "没匹配到的异常会重新抛出，与 Python 语义一致")
        self.put(f"{self.kw_of('try')} {{")
        self.block(s.body)
        if multi_handlers:
            # 多个 except：一个捕获(错) + 内部 if/elif 链
            self.put(f"}} {self.kw_of('catch')} (错) {{")
            self.declared.add("错")
            self._except_stack.append("错")
            try:
                # 收集所有 handler 用到的变量名（去重），预声明
                handler_names = []
                for hh in s.handlers:
                    name = hh.name or "错"
                    if name not in handler_names and name != "错":
                        handler_names.append(name)
                for vname in handler_names:
                    self.put(f"{self.kw_of('let')} {vname}: {self.kw_of('Any')} = {self.kw_of('Null')}")
                # 逐个 handler 翻译成 if/elif 链
                for i, hh in enumerate(s.handlers):
                    name = hh.name or "错"
                    # 异常类型判断
                    if hh.type is not None:
                        indicated = self._dotted(hh.type)
                        if indicated in _EXC_AGL:
                            type_word = _EXC_AGL[indicated]
                            cond = f"{self.builtin_of('isinstance')}(错, \"{type_word}\")"
                        else:
                            # 未知类型，降级成"总是匹配"（warning 已经加过了）
                            cond = "真"
                    else:
                        # 裸 except（没写类型）→ 总是匹配
                        cond = "真"
                    # if / elif
                    if i == 0:
                        self.put(f"{self.kw_of('if')} {cond} {{")
                    else:
                        self.put(f"}} {self.kw_of('else')} {self.kw_of('if')} {cond} {{")
                    # 把捕获的异常赋值给用户的变量名
                    if name != "错":
                        self.put(f"{name} = 错")
                    # 翻译 handler body
                    saved_declared = self.declared
                    self.declared = set(saved_declared)
                    self.declared.add(name)
                    try:
                        self.block(hh.body)
                    finally:
                        self.declared = saved_declared
                # 最后 else：没匹配到的异常重新抛出
                self.put(f"}} {self.kw_of('else')} {{")
                self.put(f"{self.builtin_of('unwrap')}({self.builtin_of('Err')}({self.builtin_of('fail_reason')}(错)))")
                self.put("}")
            finally:
                self._except_stack.pop()
        elif h is not None:
            catch_name = h.name or "错"
            self.check_name(catch_name, "捕获到的错误名")
            if catch_type:
                self.put(f"}} {self.kw_of('catch')} ({catch_name}: {catch_type}) {{")
            else:
                self.put(f"}} {self.kw_of('catch')} ({catch_name}) {{")
            self.declared.add(catch_name)
            self._except_stack.append(catch_name)
            try:
                self.block(h.body)
            finally:
                self._except_stack.pop()
            if len(h.body) == 1 and isinstance(h.body[0], ast.Pass):
                self.warnings.append(
                    "这里有个什么都没做的 `except`（吞异常）："
                    "Python 里它会把错悄悄吃掉，AGL 不许静默吞错，请补上处理")
        if s.orelse:
            self.put(f"}} {self.kw_of('else')} {{")
            self.block(s.orelse)
        if s.finalbody:
            self.put(f"}} {self.kw_of('finally')} {{")
            self.block(s.finalbody)
        self.put("}")

    # ── 函数 ───────────────────────────────────────────────────────
    def signature(self, fn: ast.FunctionDef | ast.AsyncFunctionDef,
                  effects: set[str] | None = None,
                  has_await: bool = False,
                  self_class: str | None = None,
                  drop_first: bool = False,
                  fname_override: str | None = None,
                  ret_override: str | None = None,
                  prefix_args: list[str] | None = None,
                  suffix_args: list[str] | None = None) -> str:
        a = fn.args
        parts: list[str] = []
        # 嵌套函数提升：被捕获的外层变量排在参数最前面（调用处按同序补传）
        if prefix_args:
            parts.extend(f"{self.check_name(c, '捕获变量')}: {self.kw_of('Any')}"
                         for c in prefix_args)
        plain = list(a.posonlyargs) + list(a.args)
        if drop_first and plain:
            plain = plain[1:]  # 构造函数：self 是实例本身，不是参数
        if a.kwonlyargs:
            raise Blocked(
                f"函数 `{fn.name}` 带了仅限关键字的参数（`def f(*, x)` 这种），"
                "暂时不能导：AGL 的调用都是按位置传参")
        for i, arg in enumerate(plain):
            if self_class is not None and arg.arg == "self" and i == 0:
                # 方法里的 self：实例被翻译成映射表
                parts.append(f"{self.check_name(arg.arg, '参数')}: "
                             f"{self.kw_of('Map')}<{self.kw_of('Any')}, {self.kw_of('Any')}>")
                continue
            if arg.annotation is None:
                self.warnings.append(
                    f"参数 `{arg.arg}` 没有类型注解，按「任意」类型导入："
                    "AGL 的函数签名是给人看的契约，建议补上明确类型")
                parts.append(f"{self.check_name(arg.arg, '参数')}: {self.kw_of('Any')}")
                continue
            parts.append(f"{self.check_name(arg.arg, '参数')}: "
                         f"{self.type_of(arg.annotation, '参数 ' + arg.arg)}")
        if a.vararg:
            # *nums → 列表<任意>：AGL 没有变参，调用处会自动把多余实参包成列表
            self.warnings.append(
                f"可变参数 `*{a.vararg.arg}` 被翻译成列表参数 `{a.vararg.arg}: 列表<任意>`："
                "AGL 没有变参，调用处会自动把多余的实参包成一个列表传进来")
            parts.append(f"{self.check_name(a.vararg.arg, '参数')}: "
                         f"{self.kw_of('List')}<{self.kw_of('Any')}>")
        if a.kwarg:
            # **kw → 映射表<文本, 任意>：AGL 没有关键字参数，
            # 调用处会把关键字实参打包成一个映射表传进来
            self.warnings.append(
                f"关键字参数 `**{a.kwarg.arg}` 被翻译成映射表参数 "
                f"`{a.kwarg.arg}: 映射表<文本, 任意>`：AGL 没有关键字参数，"
                "调用处会把关键字实参打包成一个映射表传进来")
            parts.append(f"{self.check_name(a.kwarg.arg, '参数')}: "
                         f"{self.kw_of('Map')}<{self.kw_of('String')}, {self.kw_of('Any')}>")
        # 默认参数：放行但加 warning —— AGL 函数所有参数必传，
        # 调用方需要自己补上默认值。签名里不体现默认值（AGL 没这个概念）。
        if a.defaults:
            n = len(a.defaults)
            for arg, default in zip(plain[-n:], a.defaults):
                try:
                    default_repr = self.expr(default)
                except Blocked:
                    default_repr = "复杂表达式"
                self.warnings.append(
                    f"参数 `{arg.arg}` 在原 Python 里有默认值 `{default_repr}`；"
                    "导入后 AGL 函数所有参数必传，请在调用处显式传入")
        fname = fname_override or fn.name
        # 可变全局共享存储（v0.38）：所有非入口函数在参数末尾收 _agl_globals
        # （入口函数自己初始化它，不接收；调用处按同序补传）
        if suffix_args:
            parts.extend(f"{self.check_name(c, '参数')}: "
                         f"{self.kw_of('Map')}<{self.kw_of('String')}, {self.kw_of('Any')}>"
                         for c in suffix_args)
        if ret_override is not None:
            ret = ret_override
        elif fn.returns:
            ret = self.type_of(fn.returns, f"函数 `{fname}` 的返回值")
        else:
            self.warnings.append(
                f"函数 `{fname}` 没有返回值类型注解，按「任意」类型导入："
                "AGL 的函数签名是给人看的契约，建议补上明确类型")
            ret = self.kw_of("Any")
        head = self.kw_of("fn")
        if isinstance(fn, ast.AsyncFunctionDef) or has_await:
            head = f"{self.kw_of('async')} {head}"
        sig = f"{head} {self.check_name(fname, '函数名', allow_entry=True)}" \
              f"({', '.join(parts)}) -> {ret}"
        if effects:
            sig += f" {self.kw_of('effect')} {' '.join(self.kw_of(e) for e in sorted(effects))}"
        return sig

    def _scan_effects(self, body: list[ast.stmt]) -> tuple[set[str], bool]:
        """预扫描函数体，收集需要的效应和是否有 await。"""
        effects: set[str] = set()
        has_await = False
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Call):
                func = node.func
                # 直接调用 sleep(x)
                if isinstance(func, ast.Name) and func.id == "sleep":
                    effects.add("Time")
                    has_await = True
                # time.sleep(x) 方法调用形式
                if (isinstance(func, ast.Attribute)
                        and func.attr == "sleep"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "time"):
                    effects.add("Time")
                    has_await = True
                # asyncio.sleep(s) 方法调用形式（v0.39）→ 同样翻成 等待 睡眠
                if (isinstance(func, ast.Attribute)
                        and func.attr == "sleep"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "asyncio"):
                    effects.add("Time")
                    has_await = True
                # time.time() / perf_counter() / monotonic() 方法调用形式
                if (isinstance(func, ast.Attribute)
                        and func.attr in ("time", "perf_counter", "monotonic")
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "time"):
                    effects.add("Time")
                # time.strftime(fmt) 方法调用形式
                if (isinstance(func, ast.Attribute)
                        and func.attr == "strftime"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "time"):
                    effects.add("Time")
                # 网络效应：requests.get / urllib.request.urlopen / urlopen（from-import）
                if (isinstance(func, ast.Attribute)
                        and func.attr == "get"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "requests"):
                    effects.add("Net")
                if (isinstance(func, ast.Attribute)
                        and func.attr == "urlopen"
                        and isinstance(func.value, ast.Attribute)
                        and isinstance(func.value.value, ast.Name)
                        and func.value.value.id == "urllib"
                        and func.value.attr == "request"):
                    effects.add("Net")
                if isinstance(func, ast.Name) and func.id == "urlopen":
                    effects.add("Net")
                # 读写效应：input() → 读入
                if isinstance(func, ast.Name) and func.id == "input":
                    effects.add("IO")
        return effects, has_await

    def _collect_fn_effects(self) -> dict[str, set[str]]:
        """全函数效应闭包（v0.39）：每函数的「直接内建效应」∪ 它调用的
        用户自定义函数的效应闭包，不动点迭代。

        背景：AGL 规则是「调用带效应的函数，调用方必须自己声明该效应」。
        异步嵌套时代第一次出现 `主入口 → producer()（producer 里 sleep）`
        这类跨函数调用链——单函数静态扫描看不到被调函数的效应，会漏报。
        嵌套函数提升后名字变 闭包N，但这里按原始 Python 函数名分析，
        发射签名时按原名合并即可。
        """
        fn_nodes: dict[str, ast.AST] = {}
        for f in self.funcs:
            fn_nodes[f.name] = f
        for f in self.funcs:
            for sub in ast.walk(f):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_nodes[sub.name] = sub
        direct: dict[str, set[str]] = {}
        for name, node in fn_nodes.items():
            eff, _ = self._scan_effects(node.body)
            direct[name] = set(eff)
        callees: dict[str, set[str]] = {}
        for name, node in fn_nodes.items():
            cs: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id in fn_nodes:
                    cs.add(sub.func.id)
            callees[name] = cs
        closure = {k: set(v) for k, v in direct.items()}
        changed = True
        while changed:
            changed = False
            for name in fn_nodes:
                for c in callees[name]:
                    if not closure[c] <= closure[name]:
                        closure[name] |= closure[c]
                        changed = True
        return closure

    def _collect_ctor_types(self) -> tuple[dict, dict]:
        """v0.40：预扫描「哪里能静态推出一个实例的类」。

        旧 __str__/__repr__ 分发只认 `Name` 直接落在 self.instances 里
        （即 `c = C()` 后裸用 c）。实际代码里常见 `o.inner`（属性取出
        另一个实例）、`make()`（函数 return C()）、子类实例继承父类
        __str__——三种形态全漏。这里静态收集：
          · attr_types[cls][attr] = C：__init__ 里 `self.attr = C()`
          · fn_returns[fname] = C：函数体里 `return C()`
        沿 MRO 从祖先到后代收集，子类同名属性覆盖父类。
        """
        attr_types: dict[str, dict[str, str]] = {}
        for cls in self.classes:
            per: dict[str, str] = {}
            for c in self._mro(cls):
                init = self.classes[c]["methods"].get("__init__")
                if init is None:
                    continue
                for node in ast.walk(init):
                    if isinstance(node, ast.Assign) and len(node.targets) == 1:
                        t = node.targets[0]
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == "self"
                                and isinstance(node.value, ast.Call)
                                and isinstance(node.value.func, ast.Name)
                                and node.value.func.id in self.classes):
                            per[t.attr] = node.value.func.id
            attr_types[cls] = per
        fn_nodes: dict[str, ast.AST] = {}
        for f in self.funcs:
            fn_nodes[f.name] = f
        for f in self.funcs:
            for sub in ast.walk(f):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_nodes[sub.name] = sub
        fn_returns: dict[str, str] = {}
        for name, node in fn_nodes.items():
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Return)
                        and isinstance(sub.value, ast.Call)
                        and isinstance(sub.value.func, ast.Name)
                        and sub.value.func.id in self.classes):
                    fn_returns[name] = sub.value.func.id
                    break
        return attr_types, fn_returns

    def _expr_instance_cls(self, e: ast.AST) -> str | None:
        """静态推断表达式 e 是不是某个用户类的实例；是则返回类名。

        只认四种形态（v0.40，属性链 v0.40 起递归穿透）：
          · 裸变量（`c`，已在 self.instances 登记）
          · 属性取出（`o.inner`、`a.b.c`，递归推 receiver 的类再查 attr_types）
          · 函数返回（`make()`，make 直接 return C()）
          · 直接构造（`C()`）
        推不出（两层以上属性链里某层不是实例、函数返回变量、条件分支不同类型）
        一律 None，调用方回退到默认显示协议——宁可少对齐，不能错调 __str__。
        """
        if isinstance(e, ast.Name):
            return self.instances.get(e.id)
        if isinstance(e, ast.Attribute):
            # 递归推 receiver 的类：a.b.c → 先推 a.b，再查 a.b 这个类的 c 属性
            owner = self._expr_instance_cls(e.value)
            if owner is not None:
                return self._attr_types.get(owner, {}).get(e.attr)
            # self 直接作为 receiver（方法体内 self.attr）
            if isinstance(e.value, ast.Name) and e.value.id == "self":
                return self._attr_types.get(self.cur_class or "", {}).get(e.attr)
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Name):
            fn = e.func.id
            if fn in self.classes:
                return fn
            return self._fn_returns.get(fn)
        # v0.41：a + b / a - b / a * b 走 __add__/__sub__/__mul__，约定返回同类型
        if isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Add, ast.Sub, ast.Mult)):
            left_cls = self._expr_instance_cls(e.left)
            if left_cls is not None:
                dunder = {ast.Add: "__add__", ast.Sub: "__sub__",
                          ast.Mult: "__mul__"}[type(e.op)]
                if self._resolve_dunder_owner(left_cls, dunder) is not None:
                    return left_cls
        return None

    def _resolve_dunder_owner(self, cls: str, dunder: str) -> str | None:
        """沿 MRO 找第一个定义了 dunder 的类名（子类没定义时继承父类）。"""
        for c in self._mro(cls):
            if dunder in self.classes[c]["methods"]:
                return c
        return None

    def _fmt_instance_dunder(self, e: ast.AST, dunder: str) -> str | None:
        """把表达式 e 包成 `<owner>___<dunder>(e)`；推不出实例类或该类
        （含父类）没定义 dunder 时返回 None，调用方回退默认显示。"""
        cls = self._expr_instance_cls(e)
        if cls is None:
            return None
        owner = self._resolve_dunder_owner(cls, dunder)
        if owner is None:
            return None
        recv = self.expr(e)
        return (f"{self.check_name(f'{owner}_{dunder}', dunder + ' 方法')}"
                f"({recv})")

    def function(self, fn: ast.FunctionDef | ast.AsyncFunctionDef,
                 self_class: str | None = None,
                 fname_override: str | None = None,
                 deco_ok: bool = False,
                 drop_first: bool = False) -> None:
        if fn.decorator_list and not deco_ok:
            # @property 已在 _collect_class 登记并翻译成同名 getter 方法：
            # 这里把 property 的装饰器当作已消化（不影响函数体翻译）
            property_only = (len(fn.decorator_list) == 1
                             and isinstance(fn.decorator_list[0], ast.Name)
                             and fn.decorator_list[0].id == "property")
            if not property_only:
                # v0.47：自定义装饰器支持——@decorator 翻译成 f = decorator(f)
                # （模块级函数当值传已经做了，decorator(f) 调用时 f 自动翻译成函数引用列表）
                for deco in fn.decorator_list:
                    if isinstance(deco, ast.Name):
                        self.warnings.append(
                            f"函数 `{fn.name}` 带了装饰器 `@{deco.id}`："
                            f"翻译成函数定义后自动加一行 `{fn.name} = {deco.id}({fn.name})`"
                            "（装饰器接收函数当参数，返回新函数）")
                    elif isinstance(deco, ast.Call):
                        # @decorator(args) 形式——翻译成 f = decorator(args)(f)
                        deco_call_code = self.expr(deco)
                        self.warnings.append(
                            f"函数 `{fn.name}` 带了带参数的装饰器 `@{self.expr(deco)}`："
                            f"翻译成函数定义后自动加一行 `{fn.name} = {deco_call_code}({fn.name})`"
                            "（先调用装饰器工厂，返回真正的装饰器，再包装函数）")
                    else:
                        raise Blocked(
                            f"函数 `{fn.name}` 带了不认识的装饰器形式 `{ast.dump(deco)}`："
                            "暂时只支持 @decorator 和 @decorator(args) 形式")
        # v0.48：生成器函数支持——预扫描函数体里有没有 yield
        # 如果有，把生成器翻译成普通函数：开头建结果列表，每个 yield 追加元素，最后 return 列表
        has_yield = any(isinstance(n, ast.Yield) for n in ast.walk(fn))
        self._is_generator = has_yield
        if has_yield:
            self.warnings.append(
                f"函数 `{fn.name}` 带了 `yield`：被翻译成普通函数——"
                "所有 yield 的值收集到一个列表里，最后返回这个列表。"
                "不是真的生成器（不能暂停/恢复），但 for 循环遍历效果一致")
            self._gen_var = "_gen_result"
        # v0.47：nonlocal 完整支持——预扫描这个函数里所有嵌套函数，
        # 找出哪些外层变量会被内层修改，把这些变量在外层函数里包装成单元素列表（box），
        # 这样内层函数修改 box[0]，外层也能看到（共享同一个可变容器）。
        self._nonlocal_boxes: dict[str, str] = {}  # key=原变量名, value=box变量名
        nested_fns = [n for n in ast.walk(fn)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn]
        if nested_fns:
            # 收集本函数的局部变量名（参数 + 函数体内的赋值目标）
            local_names = set()
            for a in fn.args.posonlyargs + fn.args.args:
                local_names.add(a.arg)
            for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    local_names.add(n.id)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local_names.add(n.name)
            # v0.47：nonlocal 完整支持——收集所有内层函数里 nonlocal 声明的变量
            modified_outer_vars = set()
            for inner in nested_fns:
                # 找内层函数里的 Nonlocal 语句，收集声明的变量名
                for n in ast.walk(ast.Module(body=inner.body, type_ignores=[])):
                    if isinstance(n, ast.Nonlocal):
                        for v in n.names:
                            if v in local_names:  # 确实是外层函数的局部变量
                                modified_outer_vars.add(v)
            # 给每个要 box 的变量生成 box 名
            if modified_outer_vars:
                for v in sorted(modified_outer_vars):
                    box_name = f"{v}盒"
                    self._nonlocal_boxes[v] = box_name
                # 用 AST 转换器把外层函数体里所有用到这些变量的地方替换成 box[0]
                transformer = self._BoxVarsTransformer(self._nonlocal_boxes)
                fn.body = list(transformer.visit(ast.Module(body=fn.body, type_ignores=[])).body)
                self.warnings.append(
                    f"函数 `{fn.name}` 里有嵌套函数修改外层变量："
                    f"变量 `{', '.join(sorted(modified_outer_vars))}` 被包装成可变容器（box），"
                    "内层修改外层可见（nonlocal 语义完整支持）")
        self.declared = set()
        self._promoted = set()
        self.instances = {}
        # 编译期已确认是集合的变量（`s = {1, 2}` / `s = set(...)` 的赋值目标）：
        # 集合运算符 `|`/`&`/`-` 只有两侧都能确认是集合才直译成 并集/交集/差集
        self.set_vars: set[str] = set()
        # v0.30：编译期已确认是列表 / 映射的变量（字面量或 list()/dict() 构造的
        # 赋值目标）。`x.pop(参数)` 1 参时列表是 `xs.pop(i)`、映射是 `d.pop(k)`，
        # 按这份登记分发；两者都没登记过的名字精确拒绝（宁可不翻，不替你猜）。
        self.list_vars: set[str] = set()
        self.dict_vars: set[str] = set()
        # v0.43：编译期已确认是函数引用的变量（右边是嵌套函数名 → 函数引用列表）。
        # 调用时 `f(args)` 自动分发到 `调用(f, args...)`，不用用户显式写。
        # dict: key=变量名, value=是不是异步函数引用（异步的话要自动加等待）
        self.fn_ref_vars: dict[str, bool] = {}
        self.cur_fn = fn.name
        self.cur_fn_is_async = isinstance(fn, ast.AsyncFunctionDef)
        self._except_stack: list[str] = []
        self._fresh_used: set[str] = set()
        # v0.37：嵌套函数提升的映射与「本函数体内会出现赋值」的预扫描
        # （块内声明提升后调用点可见，捕获可见性检查用它兜底）
        self.nested_map = {}
        self._pending_locals = self._store_names(fn)
        if self_class is not None:
            # 方法体里 self 就是这个类的实例：self.属性 → self["属性"]
            self.instances["self"] = self_class
            # super() 按方法所属类的 MRO 解析（super_base 存"当前类"）
            self.super_base = self_class
            # v0.41：二元 dunder 方法的 other 参数也是同类型实例
            # （__eq__/__add__ 等里 other.attr 才能正确翻译成 other["attr"]）
            _binary_dunders = {
                "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
                "__add__", "__sub__", "__mul__",
            }
            if fn.name in _binary_dunders and len(fn.args.args) >= 2:
                self.instances[fn.args.args[1].arg] = self_class
        # v0.40：当前方法所属类（用于 __str__ 分发里 self.attr 的类型推断）
        self.cur_class = self_class
        # v0.41：classmethod 的第一个参数（cls）→ 类属性访问别名
        self._cls_alias = None
        if drop_first and self_class is not None:
            all_args = list(fn.args.posonlyargs) + list(fn.args.args)
            if all_args:
                self._cls_alias = all_args[0].arg
        for arg in list(fn.args.posonlyargs) + list(fn.args.args):
            self.declared.add(arg.arg)
        # 预扫描函数体，收集效应和 await
        effects, has_await = self._scan_effects(fn.body)
        # v0.39：并入被调用户函数的效应闭包（跨函数调用链的效应传播）
        effects |= self._fn_effects_closure.get(fn.name, set())
        # 可变全局（v0.38）：非入口函数签名末尾收 _agl_globals，调用处补传
        final_name = fname_override or self.rename_map.get(fn.name, fn.name)
        is_entry = final_name in self.entry_names
        suffix_args = ([self.globals_store] if (self.globals_active and not is_entry)
                       else None)
        sig = self.signature(fn, effects, has_await,
                             self_class=self_class, fname_override=fname_override,
                             drop_first=drop_first, suffix_args=suffix_args)
        self.put(f"{sig} {{")
        # 可变全局（v0.38）：入口函数初始化共享存储（非入口函数由调用处补传）
        if self.globals_active and is_entry and self.globals_init_line:
            self.put(_IND + self.globals_init_line)
        # v0.47：nonlocal 完整支持——box 变量初始化（空列表，后面第一次赋值会填上）
        for orig_name, box_name in sorted(self._nonlocal_boxes.items()):
            box_name_checked = self.check_name(box_name, "nonlocal 容器")
            self.put(_IND + f"{self.kw_of('let')} {box_name_checked} = [空]")
        # Python 没有块作用域：先做一遍「块内首次声明提升」，让
        # 「块内声明 + 块后引用」的代码翻译过去不会撞上 0302。
        # 提升行统一发在函数体顶层（循环之外），只执行一次。
        self._promote_block_decls(fn.body)
        # v0.48：生成器函数——开头建结果列表
        if self._is_generator:
            self.put(f"{self.kw_of('let')} {self._gen_var}: {self.kw_of('List')}<{self.kw_of('Any')}> = []")
        self.block(fn.body)
        # v0.48：生成器函数——最后返回结果列表
        if self._is_generator:
            self.put(f"{self.kw_of('return')} {self._gen_var}")
            self._is_generator = False  # 嵌套函数里重置标志
        self.put("}")
        # v0.47：自定义装饰器——函数定义完后，逐个加装饰器赋值
        # @decorator → f = decorator(f)
        # @decorator(args) → f = decorator(args)(f)
        if fn.decorator_list and not deco_ok:
            property_only = (len(fn.decorator_list) == 1
                             and isinstance(fn.decorator_list[0], ast.Name)
                             and fn.decorator_list[0].id == "property")
            if not property_only:
                fn_name = self.check_name(fn.name, "函数")
                for deco in fn.decorator_list:
                    if isinstance(deco, ast.Name):
                        deco_name = self.check_name(deco.id, "装饰器")
                        self.put(f"{fn_name} = {deco_name}({fn_name})")
                    elif isinstance(deco, ast.Call):
                        deco_call_code = self.expr(deco)
                        self.put(f"{fn_name} = {deco_call_code}({fn_name})")
        self.put("")

    # ── 入口 ───────────────────────────────────────────────────────
    def run(self) -> ImportResult:
        try:
            tree = ast.parse(self.src)
        except SyntaxError as e:
            return ImportResult(
                ok=False,
                blockers=[f"这不是能读的 Python：第 {e.lineno} 行 {e.msg}"],
            )

        # 从这里往后的每一步都可能撞上"翻不过去的写法"，
        # Blocked 是统一的刹车：不管埋在多深的表达式里，都在这里变成一条人话。
        try:
            return self._build(tree)
        except Blocked as e:
            return ImportResult(ok=False, blockers=[str(e)],
                                warnings=list(self.warnings))

    def _build(self, tree: ast.Module) -> ImportResult:
        # 顶层只认函数定义 / 模块级只读常量 / 类，以及那句最常见的入口哨兵
        entry_call = None
        entry_wrapper = None
        body: list[ast.stmt] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.append(node)
                continue
            if isinstance(node, ast.If) and self._is_main_guard(node):
                # 入口块整段保留：直接 `main()` 走改名入口；
                # `print(main())` 这类包裹调用走 wrapper 主入口（原语句原样翻译）
                entry_wrapper = node.body
                for s in node.body:
                    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                        entry_call = self._dotted(s.value.func)
                        # v0.39：`asyncio.run(main())` 是 Python 跑异步程序的
                        # 惯用法——语义就是"跑 main"，等价于裸 `main()` 入口；
                        # AGL 运行时对异步入口自动驱动事件循环，不需要 wrapper。
                        if (entry_call == "asyncio.run"
                                and len(s.value.args) == 1
                                and isinstance(s.value.args[0], ast.Call)
                                and isinstance(s.value.args[0].func, ast.Name)):
                            entry_call = s.value.args[0].func.id
                            entry_wrapper = None
                continue
            if isinstance(node, ast.Import):
                what = ".".join(a.name for a in node.names)
                # time 模块的 sleep / time / perf_counter / monotonic 已被特殊处理
                if what == "time":
                    self.warnings.append(
                        "`import time` 被忽略：只有 `time.sleep()` / `time.time()` / "
                        "`time.perf_counter()` / `time.monotonic()` 被翻译成 AGL 内建，"
                        "time 模块的其他功能请走 AGL 的 FFI 声明")
                    continue
                # json / requests / urllib.request：内建直译（解析JSON/生成JSON/获取网页）
                if what in ("json", "requests", "urllib.request", "urllib"):
                    self._note_json_net(what)
                    continue
                # math / random：常用函数直译成 AGL 内建，其余走 FFI 声明
                if what == "math":
                    self.warnings.append(
                        "`import math` 被忽略：`math.ceil()` / `math.floor()` 等已翻译成 "
                        "AGL 内建（上取整 / 下取整），math 的其他函数请走 AGL 的 FFI 声明")
                    continue
                if what == "random":
                    self.warnings.append(
                        "`import random` 被忽略：`random.randint()` / `random.choice()` / "
                        "`random.seed()` 已翻译成 AGL 内建（随机整数 / 随机选择 / 随机种子），"
                        "random 的其他功能请走 AGL 的 FFI 声明")
                    continue
                # asyncio（v0.39）：run / sleep 有专属翻译，其余走 FFI
                if what == "asyncio":
                    self.warnings.append(
                        "`import asyncio` 被忽略：`asyncio.run(x)` 被直译成调用 `x`"
                        "（AGL 运行时自动驱动异步入口），`asyncio.sleep(s)` 被翻译成"
                        " `等待 睡眠(取整(s * 1000))`；asyncio 的其他功能请走 FFI 声明")
                    continue
                raise Blocked(
                    f"`import {what}` 暂时不能导：外部库要走 AGL 的 FFI 声明"
                    f"（`导入 外部 {what}`）并补上效应，不能由导入器替你决定")
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # `from time import ...` 与 `import time` 同一条路放行
                if mod == "time":
                    self.warnings.append(
                        "`from time import ...` 被忽略：只有 time.sleep() / time.time() / "
                        "time.perf_counter() / time.monotonic() / time.strftime() 被翻译成 AGL 内建，"
                        "time 模块的其他功能请走 AGL 的 FFI 声明")
                    continue
                if mod in ("json", "requests", "urllib.request", "urllib"):
                    self._note_json_net(mod)
                    continue
                # math / random 的 from-import：ceil/floor/randint/choice/seed 已在
                # 调用处翻译成 AGL 内建，导入语句本身放行即可
                if mod in ("math", "random"):
                    self.warnings.append(
                        f"`from {mod} import ...` 被忽略："
                        f"{'`ceil()` / `floor()` 已翻译成 AGL 内建（上取整 / 下取整）' if mod == 'math' else '`randint()` / `choice()` / `seed()` 已翻译成 AGL 内建（随机整数 / 随机选择 / 随机种子）'}，"
                        "其余功能请走 AGL 的 FFI 声明")
                    continue
                # v0.48：collections 的 from-import——警告一下，放行
                if mod == "collections":
                    self.warnings.append(
                        f"`from {mod} import ...` 被忽略："
                        "OrderedDict / defaultdict 等请走 AGL 的 FFI 声明，"
                        "日常用普通字典就行")
                    continue
                raise Blocked(
                    f"`from {mod} import ...` 暂时不能导：外部库要走 AGL 的 FFI 声明"
                    f"（`导入 外部 {mod}`）并补上效应，不能由导入器替你决定")
            if isinstance(node, ast.Assign):
                self._collect_global_const(node)
                continue
            if isinstance(node, ast.ClassDef):
                self._collect_class(node)
                continue
            body.append(node)

        if body:
            kind = type(body[0]).__name__
            raise Blocked(
                f"顶层的 `{kind}` 暂时不能导：AGL 文件里只放函数定义")

        # 可变参数信息：调用处按这个把多余的实参包成列表、关键字实参包成映射
        for f in self.funcs:
            plain = list(f.args.posonlyargs) + list(f.args.args)
            self.callee_info[f.name] = {
                "vararg": f.args.vararg is not None,
                "fixed": len(plain),
                "kwarg": f.args.kwarg is not None,
            }

        # 函数体里写模块级常量 / global 声明 / 类属性赋值 → 标为可变（v0.38）：
        # 这类状态走模块级共享存储 _agl_globals，不再当只读常量函数。
        # 必须先于 _check_global_const_usage（那里的"只读"检查要放行可变项）。
        self._scan_mutable_state()

        # 模块级常量翻译成常量函数后是只读的：函数里不许重新赋值或原地修改
        self._check_global_const_usage()

        # 类生成的函数名（构建X / X_方法）不许和已有函数名撞车
        self._check_name_collisions()

        # 决定谁当入口
        main_word = self.kw_of("main")
        candidates = {f.name for f in self.funcs}
        renamed = None
        use_wrapper = False
        if entry_call and entry_call in candidates:
            renamed = entry_call
        elif entry_wrapper:
            # 入口块是包裹调用（print(main()) 之类）：main 保持原语义，
            # 生成一个 主入口() { 原语句 } 包装函数，入口返回值的打印不丢。
            # 但 `main` 本身是 AGL 保留字（主入口的英文名），必须改名避开；
            # 入口块里任何位置都可能引用它（直接调用 / try 包裹），统一改名。
            use_wrapper = True
            renamed = None
            wrapper_target = None
            if "main" in candidates:
                renamed = "main"
                wrapper_target = "入口函数"
            self.wrapper_target = wrapper_target
        elif "main" in candidates:
            renamed = "main"
            # 没有 main guard 却认了 main 当入口：两边行为会不一样，必须讲明。
            # Python 直接跑这种文件只会定义函数、什么也不输出，
            # AGL 因为有入口会自动跑一次。
            if not entry_call:
                self.warnings.append(
                    "原文件没有 `if __name__ == '__main__'`，Python 直接跑它只会定义函数、"
                    "什么都不输出；这里按 `main` 就是入口来处理，"
                    "所以导入后这一份会真的跑起来 —— "
                    "如果它是给别人 import 用的，请留意这个区别")
        elif main_word in candidates:
            renamed = None

        # 可变全局 / 可变类属性（v0.38）激活时：入口函数初始化共享存储，
        # 所有非入口函数签名末尾收 _agl_globals、调用处补传（读/写同源）。
        self.globals_active = bool(self.mutable_globals or self.mutable_class_attrs)
        self.entry_names: set[str] = {main_word}
        if self.globals_active:
            # 常量函数体引用共享存储（可变全局/可变类属性）的才收 _agl_globals 参数；
            # 初值行 / 读点按这份表决定补不补传（避免 `变量 _agl_globals = {...}` 右侧自引用）。
            self.const_needs_globals: set[str] = set()
            for cn in self.global_consts:
                if cn in self.mutable_globals:
                    continue
                body = [ast.Expr(value=self.global_consts[cn])]
                for node in ast.walk(ast.Module(body=body, type_ignores=[])):
                    if isinstance(node, ast.Name) and node.id in self.mutable_globals:
                        self.const_needs_globals.add(cn)
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id in self.classes
                            and (node.value.id, node.attr) in self.mutable_class_attrs):
                        self.const_needs_globals.add(cn)
            # 可变全局 / 类属性的初值禁止引用共享存储（会自引用 `变量 _agl_globals = {...}`）
            for n in self.global_consts:
                if n in self.mutable_globals:
                    for node in ast.walk(ast.Module(
                            body=[ast.Expr(value=self.global_consts[n])], type_ignores=[])):
                        if (isinstance(node, ast.Name)
                                and (node.id in self.mutable_globals
                                     or node.id in self.const_needs_globals)):
                            raise Blocked(
                                f"可变全局 `{n}` 的初值引用了其他可变全局："
                                "AGL 的共享存储按模块级常量初始化，不能引用自己；"
                                "请把初值拆成纯字面量/只读表达式")
            init_items: list[str] = []
            for n in self.global_consts:
                if n in self.mutable_globals:
                    init_items.append(f"\"{n}\": {self.expr(self.global_consts[n])}")
            for (cls_name, aname) in sorted(self.mutable_class_attr_values):
                init_items.append(f"\"{cls_name}.{aname}\": "
                                  f"{self.expr(self.mutable_class_attr_values[(cls_name, aname)])}")
            self.globals_init_line = (
                f"{self.kw_of('let')} {self.globals_store} = "
                f"{{ {', '.join(init_items)} }}")
        else:
            self.globals_init_line = None
            self.const_needs_globals = set()

        # v0.39：发射函数签名前，先算好全函数效应闭包（跨函数调用链传播）
        self._fn_effects_closure = self._collect_fn_effects()
        # v0.40：预扫描实例类型（属性取出 / 函数返回 / 直接构造），
        # 供 __str__/__repr__ 分发从「只认裸变量」扩到这些形态。
        self._attr_types, self._fn_returns = self._collect_ctor_types()

        head = "#语言=中文" if self.lang == LANG_ZH else "#lang=English"
        self.lines.append(head)
        self.lines.append("")
        for f in self.funcs:
            name = f.name
            if renamed is not None and name == renamed and name != main_word:
                target = (self.wrapper_target if use_wrapper
                          else main_word)
                self.warnings.append(
                    f"入口函数 `{name}` 已改名为 `{target}`："
                    f"AGL 的入口名字是固定的，所有调用点都跟着改了")
                self.rename_map[name] = target
            self.function(f)

        # 模块级只读常量 → 同名常量函数
        for cname in self.global_consts:
            self._emit_global_const(cname)

        # 可变全局 / 可变类属性（v0.38）→ 入口函数初始化共享存储（_agl_globals），
        # 非入口函数签名收参数、调用处补传（读/写同源）。AGL 模块级只有函数声明，
        # 可变状态必须经入口初始化后按引用传递，才能跨函数共享同一个映射表。

        # 类 → 构造函数（构建X）+ 方法（X_方法）
        for cls_name in self.classes:
            self._emit_class(cls_name)

        # v0.48：元类支持——生成一个"初始化类"函数，在入口里调用，
        # 这样元类的 __new__ 只调用一次（创建类的时候），不是每次创建实例都调用
        has_metaclass = any(info.get("metaclass") for info in self.classes.values())
        if has_metaclass:
            self.put(f"{self.kw_of('fn')} 初始化类() {{")
            for cls_name, info in self.classes.items():
                meta_name = info.get("metaclass")
                if meta_name:
                    bases = info.get("bases", [])
                    bases_list = ", ".join(f'"{b}"' for b in bases)
                    self.put(_IND + f"// 调用元类 {meta_name} 的 __new__ 创建类 {cls_name}")
                    self.put(_IND + f"变量 {cls_name}_类 = {meta_name}___new__({meta_name}, \"{cls_name}\", [{bases_list}], {{}})")
            self.put("}")
            self.put("")

        # 被提升的 lambda → 命名函数（_lift_lambda 已把签名行 + 函数体存成完整块）
        for _sig, block_lines in self.lifted_fns:
            self.lines.extend(block_lines)
            self.lines.append("")

        # 提升 lambda 的调用点必须在定义函数内，且不能作为值传出
        # （lambda_captures 在 function() 翻译时才填充，必须放在所有函数之后查）
        self._check_lambda_usage()

        # 入口块是包裹调用（print(main())）：生成 主入口() { 原语句 } 包装函数
        if use_wrapper and entry_wrapper:
            self.lines.append(f"{self.kw_of('fn')} {main_word}() {{")
            # v0.48：元类支持——入口开头调用初始化类函数
            if has_metaclass:
                self.lines.append(f"{_IND}初始化类()")
            old_declared = self.declared
            self.declared = set()
            for s in entry_wrapper:
                self.stmt(s)
            self.declared = old_declared
            self.lines.append("}")
            self.lines.append("")

        # 没有入口函数：生成一个空的，让人一眼知道从哪开始读
        if not any((renamed is None and f.name == main_word)
                   or (renamed is not None and f.name == renamed)
                   for f in self.funcs):
            self.lines.append(f"{self.kw_of('fn')} {main_word}() {{")
            # v0.48：元类支持——入口开头调用初始化类函数
            if has_metaclass:
                self.lines.append(f"{_IND}初始化类()")
            self.lines.append(f"{_IND}// 原文件里没有明确的入口函数，这里是留给你填的")
            self.lines.append("}")
            self.warnings.append(
                f"原文件没有 `main` 也没有 `if __name__ == '__main__'`，"
                f"补了一个空的 `{main_word}` —— AGL 程序必须有入口")

        # v0.48：元类支持——在正常入口函数的开头插入一行调用 初始化类()
        if has_metaclass:
            for i, line in enumerate(self.lines):
                if line.startswith(f"{self.kw_of('fn')} {main_word}()"):
                    # 找到入口函数了，在它的函数体开头插入一行
                    self.lines.insert(i + 1, f"{_IND}初始化类()")
                    break

        text = "\n".join(self.lines).rstrip() + "\n"
        n_classes = len(self.classes)
        n_methods = sum(len(info["methods"])
                        - (1 if "__init__" in info["methods"] else 0)
                        for info in self.classes.values())
        return ImportResult(
            ok=True, agl=text, warnings=list(self.warnings),
            stats={"functions": len(self.funcs) + n_classes + n_methods + len(self.lifted_fns),
                   "lines": text.count("\n")},
        )

    @staticmethod
    def _is_main_guard(node: ast.If) -> bool:
        t = node.test
        if not isinstance(t, ast.Compare) or len(t.ops) != 1:
            return False
        if not isinstance(t.ops[0], ast.Eq):
            return False
        left = t.left
        right = t.comparators[0]
        if not isinstance(left, ast.Name) or not isinstance(right, ast.Constant):
            return False
        return left.id == "__name__" and right.value == "__main__"

    # ── 模块级只读常量 ─────────────────────────────────────────────
    def _collect_global_const(self, node: ast.Assign) -> None:
        if len(node.targets) != 1:
            raise Blocked(
                "模块级变量暂时只能有一个目标（`X = ...`）：AGL 没有全局变量，"
                "只能把只读常量翻译成同名常量函数")
        target = node.targets[0]
        # v0.48：动态修改类方法/类属性——`MyClass.my_method = func`
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            cls_name = target.value.id
            attr_name = target.attr
            if cls_name in self.classes:
                # 是类的属性赋值——警告一下，然后当成可变类属性处理
                self.warnings.append(
                    f"模块级语句 `{cls_name}.{attr_name} = ...`：动态修改类属性/方法，"
                    "被翻译成运行时往类字典里加键值对")
                # 把它加到可变类属性里，后面会处理
                self.mutable_class_attrs.add((cls_name, attr_name))
                # 如果右边是一个函数名，记录下来，后面发射函数的时候改名
                if isinstance(node.value, ast.Name):
                    orig_fn_name = node.value.id
                    new_fn_name = f"{cls_name}_{attr_name}"
                    self.dynamic_method_map[orig_fn_name] = new_fn_name
                    self.warnings.append(
                        f"函数 `{orig_fn_name}` 是动态加到类 `{cls_name}` 上的方法——"
                        f"发射时会改名为 `{new_fn_name}`")
                return
        if not isinstance(target, ast.Name):
            raise Blocked(
                "模块级变量暂时只能是一个名字（`X = ...`）：AGL 没有全局变量，"
                "只能把只读常量翻译成同名常量函数")
        name = target.id
        if name in self.global_consts:
            raise Blocked(
                f"模块级变量 `{name}` 被赋值了两次：AGL 没有全局变量，"
                "翻译成常量函数后第二次赋值没法表达")
        if isinstance(node.value, ast.Lambda):
            raise Blocked("模块级变量不能是 lambda：AGL 没有函数值，lambda 只能写在函数里的赋值上")
        if isinstance(node.value, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            raise Blocked(
                f"模块级变量 `{name}` 的值暂时不能是推导式："
                "AGL 没有全局变量，请先把推导式写进函数里")
        self.global_consts[name] = node.value

    def _scan_mutable_state(self) -> None:
        """函数体里的 global 声明 / 对模块级常量的赋值 / 类属性赋值 → 标为可变。

        可变项从「只读常量函数」转为模块级共享存储 `_agl_globals`，读写同源。
        必须在 _check_global_const_usage（只读检查）之前调用；AugAssign / For /
        原地方法调用（append 等）没有等价翻译，仍留在只读检查里被拒绝。
        """
        def scan_body(body: list[ast.stmt], where: str) -> None:
            for node in ast.walk(ast.Module(body=body, type_ignores=[])):
                if isinstance(node, ast.Global):
                    for n in node.names:
                        if n not in self.global_consts:
                            raise Blocked(
                                f"`global {n}` 指向的模块级变量没有定义"
                                "（或不是只读常量）：AGL 没有全局变量，"
                                "只能把文件顶层的 `名字 = ...` 翻译成同名只读常量函数，"
                                "函数内 `global` 声明的名字必须对应这样的常量")
                        self.mutable_globals.add(n)
                elif isinstance(node, ast.Assign):
                    # 函数内给模块级变量赋值（含下标/成员改写基对象）→ 可变全局
                    if len(node.targets) == 1:
                        hit = self._mutated_global(node.targets[0])
                        if hit and hit in self.global_consts:
                            self.mutable_globals.add(hit)
                    # 类属性赋值 C.x = v → 可变类属性（初值从类收集表里拿）
                    for t in node.targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                            owner = t.value.id
                            # v0.41：classmethod 里 cls.x → 当前类.x
                            if (self._cls_alias is not None
                                    and owner == self._cls_alias):
                                owner = cls_name if (cls_name := self._cls_scan_class) else owner
                            if owner in self.classes:
                                cinfo = self.classes[owner]
                                if t.attr in cinfo.get("attrs", {}):
                                    self.mutable_class_attrs.add((owner, t.attr))
                                    if (owner, t.attr) not in self.mutable_class_attr_values:
                                        self.mutable_class_attr_values[(owner, t.attr)] = \
                                            cinfo["attrs"][t.attr]
                elif isinstance(node, ast.AugAssign):
                    # v0.41：cls.count += 1 / C.count += 1 → 可变类属性
                    t = node.target
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                        owner = t.value.id
                        if (self._cls_alias is not None
                                and owner == self._cls_alias):
                            owner = self._cls_scan_class or owner
                        if owner in self.classes:
                            cinfo = self.classes[owner]
                            if t.attr in cinfo.get("attrs", {}):
                                self.mutable_class_attrs.add((owner, t.attr))
                                if (owner, t.attr) not in self.mutable_class_attr_values:
                                    self.mutable_class_attr_values[(owner, t.attr)] = \
                                        cinfo["attrs"][t.attr]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    hit = self._mutated_global(node.target)
                    if hit and hit in self.global_consts:
                        self.mutable_globals.add(hit)

        for f in self.funcs:
            scan_body(f.body, f.name)
        # 类方法体同样可能 global 改写 / 给类属性赋值（v0.38 边缘：方法里写全局）
        for cls_name in self.classes:
            cinfo = self.classes[cls_name]
            mk = cinfo.get("method_kinds", {})
            self._cls_scan_class = cls_name
            for mname, mnode in cinfo.get("methods", {}).items():
                # v0.41：classmethod 的 cls 参数 → 类名（用于预扫描类属性赋值）
                saved_cls_alias = self._cls_alias
                if mk.get(mname) == "class":
                    args = list(mnode.args.posonlyargs) + list(mnode.args.args)
                    if args:
                        self._cls_alias = args[0].arg
                scan_body(mnode.body, f"{cls_name}.{mname}")
                self._cls_alias = saved_cls_alias
            self._cls_scan_class = None
            for mname, mnode in cinfo.get("setters", {}).items():
                scan_body(mnode.body, f"{cls_name}.{mname}(setter)")
            for prop in cinfo.get("properties", ()):
                scan_body(cinfo["methods"][prop].body, f"{cls_name}.{prop}(property)")

    def _check_global_const_usage(self) -> None:
        """常量函数是只读的：函数里重新赋值或原地修改都会让翻译失真，必须拒绝。

        可变全局（_scan_mutable_state 标出）的赋值已由 _assign_one 走共享存储，
        这里放行；AugAssign / For / 原地方法调用没有等价翻译，仍拒绝。
        """
        for f in self.funcs:
            for node in ast.walk(ast.Module(body=f.body, type_ignores=[])):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        hit = self._mutated_global(t)
                        if hit:
                            # 可变全局（v0.38）：赋值走共享存储，只读检查放行
                            if hit in self.mutable_globals:
                                continue
                            raise Blocked(
                                f"函数 `{f.name}` 里给模块级变量 `{hit}` 赋值："
                                "AGL 没有全局变量，导入后它是只读常量函数，没法表达这种改动")
                elif isinstance(node, ast.AnnAssign):
                    hit = self._mutated_global(node.target)
                    if hit:
                        if hit in self.mutable_globals:
                            continue
                        raise Blocked(
                            f"函数 `{f.name}` 里给模块级变量 `{hit}` 赋值："
                            "AGL 没有全局变量，导入后它是只读常量函数，没法表达这种改动")
                elif isinstance(node, ast.AugAssign):
                    hit = self._mutated_global(node.target)
                    if hit:
                        raise Blocked(
                            f"函数 `{f.name}` 里对模块级变量 `{hit}` 做了复合赋值："
                            "AGL 没有全局变量，导入后它是只读常量函数，没法表达这种改动")
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    hit = self._mutated_global(node.target)
                    if hit:
                        raise Blocked(
                            f"函数 `{f.name}` 里把模块级变量 `{hit}` 当循环变量："
                            "AGL 没有全局变量，导入后它是只读常量函数，没法表达这种改动")
                # 原地修改：xs.append(x) 这类方法调用会改到全局列表
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("append", "pop", "remove", "insert",
                                               "clear", "extend", "sort", "reverse")
                        and isinstance(node.func.value, (ast.Name, ast.Subscript, ast.Attribute))):
                    hit = self._mutated_global(node.func.value)
                    if hit:
                        raise Blocked(
                            f"函数 `{f.name}` 里对模块级变量 `{hit}` 调用了 `.{(node.func.attr)}()`："
                            "AGL 没有全局变量，导入后它是只读常量函数，没法表达原地修改")

    def _mutated_global(self, t: ast.expr) -> str | None:
        """赋值目标里用到的模块级常量名（下标/成员取基对象）。"""
        if isinstance(t, ast.Name):
            return t.id if t.id in self.global_consts else None
        if isinstance(t, (ast.Subscript, ast.Attribute)):
            return self._mutated_global(t.value)
        if isinstance(t, ast.Tuple):
            for elt in t.elts:
                hit = self._mutated_global(elt)
                if hit:
                    return hit
        return None

    def _check_name_collisions(self) -> None:
        """生成的函数名（构建X / X_方法 / 常量函数名）不许和已有函数名撞车。"""
        seen: list[str] = [f.name for f in self.funcs]
        seen += list(self.global_consts)
        for cls_name in self.classes:
            seen.append(f"构建{cls_name}")
            seen += [f"{cls_name}_{m}" for m in self.classes[cls_name]["methods"]]
        if len(seen) != len(set(seen)):
            raise Blocked(
                "生成的函数名和已有的函数名撞了（比如 `构建X` / `X_方法` 与现有函数重名）："
                "请先在 Python 里改个类名或函数名")

    def _mro(self, cls_name: str) -> list[str]:
        """C3 线性化：方法解析顺序（与 Python 一致）。C3 失败 → Blocked（Python 也报错）。"""
        cache = getattr(self, "_mro_cache", {})
        if cls_name in cache:
            return cache[cls_name]

        def c3(cls: str, stack: list[str]) -> list[str]:
            info = self.classes[cls]
            bases = info["bases"]
            if not bases:
                return [cls]
            if cls in stack:
                raise Blocked(f"类 `{cls}` 的继承关系成环了（Python 里也过不了）")
            seqs: list[list[str]] = [c3(b, stack + [cls]) for b in bases] + [list(bases)]
            out = [cls]
            while True:
                chosen = None
                for s in seqs:
                    if not s:
                        continue
                    head = s[0]
                    if not any(head in tail[1:] for tail in seqs):
                        chosen = head
                        break
                if chosen is None:
                    raise Blocked(
                        f"类 `{cls}` 的继承顺序无法线性化（C3 冲突），"
                        "Python 里也会在定义时直接报错")
                out.append(chosen)
                for s in seqs:
                    if s and s[0] == chosen:
                        del s[0]
                if all(not s for s in seqs):
                    break
            return out

        mro = c3(cls_name, [])
        cache[cls_name] = mro
        self._mro_cache = cache
        return mro

    def _resolve_method(self, cls_name: str, meth: str):
        """沿 MRO 找方法：返回 (所属类名, 方法节点)，找不到返回 (None, None)。"""
        for c in self._mro(cls_name):
            info = self.classes.get(c)
            if info and meth in info["methods"]:
                return c, info["methods"][meth]
        return None, None

    def _method_kind(self, cls_name: str, meth: str) -> str | None:
        """沿 MRO 找方法所属类，返回它的装饰器种类：None / "static" / "class"。"""
        for c in self._mro(cls_name):
            info = self.classes.get(c)
            if info and meth in info["methods"]:
                return info.get("method_kinds", {}).get(meth)
        return None

    def _bind_call_args(self, params: list[str], args: list[ast.expr],
                        keywords: list[ast.keyword],
                        what: str) -> list[ast.expr]:
        """把位置实参 + 关键字实参按形参顺序绑定，返回每个形参的表达式（AST）。

        与 Python 的实参绑定规则一致：位置实参从左占形参，关键字实参按名填剩余；
        位置超界 / 同名重复 / 未知关键字 / 漏填都是 Python 的 TypeError，
        这里给出精确拒绝，不让不等价的翻译溜过去。
        """
        bound: dict[str, ast.expr] = {}
        if len(args) > len(params):
            raise Blocked(
                f"`{what}` 给了 {len(args)} 个位置实参，形参只有 {len(params)} 个"
                "（Python 会直接抛 TypeError）")
        for p, a in zip(params, args):
            bound[p] = a
        for k in keywords:
            if k.arg is None:
                raise Blocked(
                    f"`{what}` 用了 `**d` 动态展开，但目标没有 `**kw` 形参"
                    "（Python 会直接抛 TypeError）")
            if k.arg not in params:
                raise Blocked(
                    f"`{what}` 的关键字实参 `{k.arg}` 不是它的形参"
                    "（Python 会直接抛 TypeError）")
            if k.arg in bound:
                raise Blocked(
                    f"`{what}` 的参数 `{k.arg}` 既按位置又按关键字传了"
                    "（Python 会直接抛 TypeError）")
            bound[k.arg] = k.value
        missing = [p for p in params if p not in bound]
        if missing:
            raise Blocked(
                f"`{what}` 漏了实参：{', '.join(missing)}"
                "（Python 会直接抛 TypeError）")
        return [bound[p] for p in params]

    # ── 类：翻译成构造函数 + 实例显式传参的普通函数 ─────────────────
    @staticmethod
    def _is_super_call(e: ast.expr) -> bool:
        """`super().__xxx(...)` 调用？"""
        return (isinstance(e, ast.Call)
                and isinstance(e.func, ast.Attribute)
                and isinstance(e.func.value, ast.Call)
                and isinstance(e.func.value.func, ast.Name)
                and e.func.value.func.id == "super")

    def _super_search_tail(self, super_call: ast.Call, mro: list[str],
                           default_tail: list[str]) -> list[str]:
        """给出 super(...) 方法解析的「从哪开始找」MRO 切片。

        · 裸 `super()`：用 default_tail（表达式/语句语境是「当前类之后」，
          构造链展开语境是「当前正在展开的这个类之后」）—— 现有行为不变；
        · 显式 `super(B, self)`：按 Python 语义，在 mro 里找到 B 的位置，
          从 **B 的下一位** 开始找方法（而不是从当前类的下一位）。

        显式写法翻不过去的地方（类名不是写死的、第二个实参不是 self、
        类不在继承链上）一律精确拒绝 —— Python 运行期也会抛 TypeError，
        不能凭猜给一个看似能跑的翻译。
        """
        if not super_call.args:
            return default_tail
        if super_call.keywords:
            raise Blocked(
                "super(...) 带了关键字参数：Python 里 super() 的两个实参"
                "只能按位置写成 `super(类名, self)`，暂时不能导")
        if len(super_call.args) != 2:
            raise Blocked(
                "显式 super() 必须写成 `super(类名, self)`："
                "要同时给「从哪个类之后开始找」和「在哪个实例上找」两个实参")
        cls_arg, inst_arg = super_call.args
        if not isinstance(cls_arg, ast.Name):
            raise Blocked(
                "super(...) 的第一个实参必须是写死的类名（`super(类名, self)`）："
                "运行期才算出来的类名静态定不了它在继承链上的位置，暂时不能导")
        if not (isinstance(inst_arg, ast.Name) and inst_arg.id == "self"):
            raise Blocked(
                "super(类名, ...) 的第二个实参必须是 `self`："
                "AGL 把方法翻成「实例显式传参」的普通函数，super 只能作用在当前实例上；"
                "请改成 `super(类名, self)`")
        cls_name = cls_arg.id
        if cls_name not in self.classes:
            raise Blocked(
                f"super(...) 写的类名 `{cls_name}` 在本文件里没有定义："
                "AGL 只支持继承同文件的类，外部类请走 FFI 声明")
        if cls_name not in mro:
            raise Blocked(
                f"super(...) 写的类名 `{cls_name}` 不在当前类的继承链上："
                "Python 运行到这里会抛 TypeError，请确认类名写对了")
        return mro[mro.index(cls_name) + 1:]

    def _expand_init_chain(self, cls_name: str) -> tuple[list[str], list[ast.stmt]]:
        """返回 (签名参数名, 展开后的初始化语句)：把 __init__ 协作链静态展开。

        Python 里 `super().__init__(...)` 按**实例真实类**的 MRO 继续初始化。
        导入产物没有运行时 super 对象，所以把整条链内联进构建函数：
        遇到 `super().__init__(args)` 就递归展开「MRO 中下一个有 __init__ 的类」的
        初始化体（实参绑定成临时变量，避免重名），直到链底。
        `super().方法(args)`（非 init）静态解析成 `所属类_方法(self, args)` 调用。
        等价性：链的执行顺序与 MRO 一致，字段都写在同一个 self 映射表上。
        """
        mro = self._mro(cls_name)
        start = None
        for c in mro:
            if "__init__" in self.classes[c]["methods"]:
                start = c
                break
        if start is None:
            return [], []
        start_init = self.classes[start]["methods"]["__init__"]
        params = [a.arg for a in list(start_init.args.posonlyargs) + list(start_init.args.args)][1:]
        body: list[ast.stmt] = []
        self._expand_init_body(start, mro, body, [])
        return params, body

    def _expand_init_body(self, cur: str, mro: list[str],
                          out: list[ast.stmt], seen: list[str]) -> None:
        """把 cur 的 __init__ 体（含 super 链展开）追加进 out。"""
        init = self.classes[cur]["methods"]["__init__"]
        for st in init.body:
            self._expand_init_stmt(cur, mro, st, out, seen)

    def _expand_init_stmt(self, cur: str, mro: list[str],
                          st: ast.stmt, out: list[ast.stmt],
                          seen: list[str]) -> None:
        i = mro.index(cur)

        # `super().__init__(args)` → 递归展开下一个有 __init__ 的类
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                and self._is_super_call(st.value)
                and st.value.func.attr == "__init__"):
            tail = self._super_search_tail(st.value.func.value, mro, mro[i + 1:])
            y = next((c for c in tail
                      if "__init__" in self.classes[c]["methods"]), None)
            if y is None:
                # Python 会调 object.__init__（无操作）；展开版同样什么都不做
                return
            if y in seen:
                raise Blocked(
                    f"`__init__` 的 super 链在 `{y}` 上转圈了：Python 里也过不了")
            y_init = self.classes[y]["methods"]["__init__"]
            y_params = [a.arg for a in list(y_init.args.posonlyargs) + list(y_init.args.args)][1:]
            arg_exprs = self._bind_call_args(
                y_params, st.value.args, st.value.keywords,
                f"super().__init__() → `{y}.__init__`")
            binds: dict[str, str] = {}
            prefix: list[ast.stmt] = []
            for idx, (p, arg) in enumerate(zip(y_params, arg_exprs)):
                bn = f"__链{len(seen)}_{idx}"
                binds[p] = bn
                prefix.append(ast.Assign(targets=[ast.Name(id=bn)], value=arg))
            renamed = _RenameParams(binds).visit(
                ast.Module(body=copy.deepcopy(y_init.body), type_ignores=[]))
            out.extend(prefix)
            # 展开的是「改名后的体」，不是 y 的原始体：
            # 原始体里的形参名此刻并不存在于展开后的作用域，不改名会解析到
            # 外层碰巧同名的签名参数（参数名全一致时结果碰巧对，错一个就 NameError）。
            for s2 in renamed.body:
                self._expand_init_stmt(y, mro, s2, out, seen + [y])
            return

        # `super().方法(args)`（非 init）→ 静态解析成 所属类_方法(self, args)
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                and self._is_super_call(st.value)
                and st.value.func.attr != "__init__"):
            meth = st.value.func.attr
            tail = self._super_search_tail(st.value.func.value, mro, mro[i + 1:])
            owner = next((c for c in tail
                          if meth in self.classes[c]["methods"]), None)
            if owner is None:
                raise Blocked(
                    f"`super().{meth}()` 在继承链上找不到 `{meth}` 的实现"
                    "（Python 运行到这里会抛 AttributeError）")
            owner_method = self.classes[owner]["methods"][meth]
            m_params = [a.arg for a in list(owner_method.args.posonlyargs)
                        + list(owner_method.args.args)][1:]  # 去 self
            arg_exprs = self._bind_call_args(
                m_params, st.value.args, st.value.keywords, f"super().{meth}()")
            out.append(ast.Expr(value=ast.Call(
                func=ast.Name(id=f"{owner}_{meth}"),
                args=[ast.Name(id="self")] + arg_exprs,
                keywords=[])))
            return

        # 其它语句：递归处理嵌套块里的 super 调用
        new = copy.copy(st)
        if isinstance(st, ast.If):
            new.body = self._exp_list(cur, mro, st.body, seen)
            new.orelse = self._exp_list(cur, mro, st.orelse, seen)
        elif isinstance(st, ast.While):
            new.body = self._exp_list(cur, mro, st.body, seen)
            new.orelse = self._exp_list(cur, mro, st.orelse, seen)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            new.body = self._exp_list(cur, mro, st.body, seen)
            new.orelse = self._exp_list(cur, mro, st.orelse, seen)
        elif isinstance(st, ast.Try):
            new.body = self._exp_list(cur, mro, st.body, seen)
            new.orelse = self._exp_list(cur, mro, st.orelse, seen)
            new.finalbody = self._exp_list(cur, mro, st.finalbody, seen)
            new.handlers = []
            for h in st.handlers:
                h2 = copy.copy(h)
                h2.body = self._exp_list(cur, mro, h.body, seen)
                new.handlers.append(h2)
        elif isinstance(st, ast.With):
            new.body = self._exp_list(cur, mro, st.body, seen)
        out.append(new)

    def _exp_list(self, cur: str, mro: list[str],
                  stmts: list[ast.stmt], seen: list[str]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for s in stmts or []:
            self._expand_init_stmt(cur, mro, s, out, seen)
        return out
    def _collect_class(self, node: ast.ClassDef) -> None:
        bases: list[str] = []
        if node.bases:
            # 单继承与多重继承都支持：方法解析按 C3 线性化（与 Python 一致），
            # 构造链按 MRO 顺序静态展开到同一个实例映射表上。
            for b in node.bases:
                if not isinstance(b, ast.Name):
                    raise Blocked(
                        f"类 `{node.name}` 的基类写法暂时不能导："
                        "只支持继承本文件里已定义的类")
                # v0.48：元类支持——继承 type 的类当成元类
                if b.id == "type":
                    # 不要把 type 加到真正的 bases 里——MRO 计算会找不到它
                    self.warnings.append(
                        f"类 `{node.name}` 继承了 type——当成元类处理（简化版支持）")
                    continue
                if b.id not in self.classes:
                    raise Blocked(
                        f"类 `{node.name}` 继承了 `{b.id}`，但它在文件里没有先定义："
                        "AGL 只支持继承同文件已定义的类；"
                        "继承外部库的类请走 FFI 声明，不能由导入器替你决定")
                if b.id == node.name:
                    raise Blocked(f"类 `{node.name}` 不能继承它自己")
                if b.id in bases:
                    raise Blocked(
                        f"类 `{node.name}` 重复继承了 `{b.id}`：Python 里这也是错的")
                bases.append(b.id)
        if node.decorator_list:
            # v0.47：类装饰器——简化版支持 @decorator 形式
            # 注意：AGL 的类是编译期静态结构（构造函数 + 方法），没有运行时类对象；
            # 这里只翻译装饰器调用本身，类名到构造函数的映射还是编译期硬编码的。
            self._pending_class_decorators[node.name] = list(node.decorator_list)
            for deco in node.decorator_list:
                if isinstance(deco, ast.Name):
                    self.warnings.append(
                        f"类 `{node.name}` 带了装饰器 `@{deco.id}`："
                        f"翻译成类定义后自动加一行 `构建{node.name} = {deco.id}(构建{node.name})`"
                        "（注意：AGL 的类是编译期静态结构，没有运行时类对象；"
                        "类装饰器只支持最简单的形式，复杂的类装饰器请手写重审）")
                elif isinstance(deco, ast.Call):
                    deco_call_code = self.expr(deco)
                    self.warnings.append(
                        f"类 `{node.name}` 带了带参数的装饰器 `@{self.expr(deco)}`："
                        f"翻译成类定义后自动加一行 `构建{node.name} = {deco_call_code}(构建{node.name})`"
                        "（注意：AGL 的类是编译期静态结构，没有运行时类对象）")
                else:
                    raise Blocked(
                        f"类 `{node.name}` 带了不认识的装饰器形式 `{ast.dump(deco)}`："
                        "暂时只支持 @decorator 和 @decorator(args) 形式")
        # v0.48：元类支持——把 metaclass 的名字存下来
        metaclass_name = None
        if node.keywords:
            for kw in node.keywords:
                if kw.arg == "metaclass":
                    if isinstance(kw.value, ast.Name):
                        metaclass_name = kw.value.id
                        self.warnings.append(
                            f"类 `{node.name}` 带了 metaclass={metaclass_name}——"
                            "创建类时会调用元类的 __new__")
                    else:
                        raise Blocked(
                            f"类 `{node.name}` 的 metaclass 参数不是简单的类名："
                            "暂时只支持 metaclass=类名 这种简单形式")
                else:
                    raise Blocked(
                        f"类 `{node.name}` 带了不认识的关键字参数 `{kw.arg}`："
                        "暂时只支持 metaclass=")
        methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        method_kinds: dict[str, str] = {}   # 方法名 → "static" / "class"（其余不记录）
        class_attrs: dict[str, ast.expr] = {}  # 类属性名 → 默认值表达式
        setter_methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # @x.setter 的 def 与 @property 的 def 同名（都叫 x）：
                # methods 按名字收会互相覆盖，setter 单独收一份。
                if any(isinstance(d, ast.Attribute) and d.attr == "setter"
                       for d in item.decorator_list):
                    setter_methods[item.name] = item
                else:
                    methods[item.name] = item
            elif isinstance(item, ast.Pass):
                continue
            elif isinstance(item, ast.Assign) and len(item.targets) == 1 \
                    and isinstance(item.targets[0], ast.Name):
                # 类属性：`class A: count = 0` → 翻译成模块级变量 A_count（跨实例共享）。
                # Python 类体在类定义时求值一次；AGL 模块级变量在加载时求值，时机一致。
                class_attrs[item.targets[0].id] = item.value
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) \
                    and item.value is not None:
                class_attrs[item.target.name] = item.value
            elif isinstance(item, ast.ClassDef):
                # v0.48：嵌套类——当成独立的类来收集，名字改成 Outer_Inner
                self.warnings.append(
                    f"类 `{node.name}` 里有嵌套类 `{item.name}`——"
                    f"当成独立的类 `{node.name}_{item.name}` 处理")
                # 递归收集嵌套类，把类名改成 Outer_Inner
                nested_node = item
                nested_node.name = f"{node.name}_{item.name}"
                self._collect_class(nested_node)
            else:
                raise Blocked(
                    f"类 `{node.name}` 里有暂时不能导的成员 `{type(item).__name__}`")
        for mname, mnode in methods.items():
            plain = list(mnode.args.posonlyargs) + list(mnode.args.args)
            kind = None
            if mnode.decorator_list:
                for d in mnode.decorator_list:
                    if isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod"):
                        if kind is None:
                            kind = "static" if d.id == "staticmethod" else "class"
                    elif isinstance(d, ast.Name) and d.id == "property":
                        # @property：只读属性 → 翻译成同名方法 <类>_x(实例)，
                        # 访问 `a.x` 时调用它（getter 语义与 Python 一致）
                        if kind is None:
                            kind = "property"
                        else:
                            raise Blocked(
                                f"方法 `{mname}` 同时带 @property 和其他装饰器，暂时不能导")
                    else:
                        name = d.id if isinstance(d, ast.Name) else type(d).__name__
                        raise Blocked(
                            f"方法 `{mname}` 的装饰器 `@{name}` 暂时不能导："
                            "AGL 只支持 @staticmethod（方法不绑定实例）、"
                            "@classmethod（去掉 cls 参数）和 @property（只读属性），"
                            "其余装饰器需要函数值，AGL 没有")
            if kind == "static":
                # static 方法没有 self：翻译成普通函数，与类无关
                if mname == "__init__":
                    raise Blocked("`__init__` 不能带 @staticmethod / @classmethod")
                method_kinds[mname] = kind
                if mname.startswith("__") and mname != "__init__":
                    raise Blocked(
                        f"特殊方法 `{mname}` 暂时不能导（AGL 没有运算符重载这一层）")
                if mnode.args.vararg or mnode.args.kwarg or mnode.args.kwonlyargs:
                    raise Blocked(
                        f"方法 `{mname}` 带了可变参数或关键字参数，暂时不能导")
                continue
            if kind == "property":
                # @property 只读属性：getter 方法（只收 self），访问 a.x → A_x(a)
                if mname == "__init__":
                    raise Blocked("`__init__` 不能带 @property")
                if not plain or plain[0].arg != "self":
                    raise Blocked(
                        "@property 方法的第一个参数必须是 self")
                if len(plain) != 1:
                    raise Blocked(
                        "@property 方法不能带额外参数（getter 只读实例）")
                if mnode.args.vararg or mnode.args.kwarg or mnode.args.kwonlyargs:
                    raise Blocked(
                        f"方法 `{mname}` 带了可变参数或关键字参数，暂时不能导")
                method_kinds[mname] = kind
                continue
            if kind == "class":
                # classmethod 的第一个参数（惯例 cls）不参与调用：翻译时去掉
                if mname == "__init__":
                    raise Blocked("`__init__` 不能带 @staticmethod / @classmethod")
                if not plain:
                    raise Blocked(
                        f"@classmethod 方法 `{mname}` 至少要有一个参数（惯例叫 cls）")
                method_kinds[mname] = kind
                if mname.startswith("__") and mname != "__init__":
                    raise Blocked(
                        f"特殊方法 `{mname}` 暂时不能导（AGL 没有运算符重载这一层）")
                if mnode.args.vararg or mnode.args.kwarg or mnode.args.kwonlyargs:
                    raise Blocked(
                        f"方法 `{mname}` 带了可变参数或关键字参数，暂时不能导")
                continue
            if not plain or (plain[0].arg != "self" and plain[0].arg != "cls"):
                raise Blocked(
                    f"方法 `{mname}` 的第一个参数不是 self，暂时不能导："
                    "AGL 没有对象方法，方法被翻译成「实例显式传参」的普通函数")
            if mname.startswith("__") and mname not in (
                    "__init__", "__new__", "__str__", "__repr__",
                    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
                    "__add__", "__sub__", "__mul__",
                    "__getitem__", "__setitem__", "__len__",
                    "__call__", "__iter__", "__next__",
                    "__enter__", "__exit__",
                    "__init_subclass__", "__set_name__", "__prepare__"):
                raise Blocked(
                    f"特殊方法 `{mname}` 暂时不能导（AGL 没有运算符重载这一层）")
            if mnode.args.vararg or mnode.args.kwarg or mnode.args.kwonlyargs:
                # v0.48：允许一些特殊方法带可变参数——完美版支持
                if mname in ("__call__", "__init_subclass__", "__prepare__", "__new__"):
                    self.warnings.append(
                        f"方法 `{mname}` 带了可变参数或关键字参数——完美版支持，"
                        "可变参数会被翻译成列表参数")
                    # 不报错，继续往下走，让正常的参数翻译逻辑处理
                else:
                    raise Blocked(
                        f"方法 `{mname}` 带了可变参数或关键字参数，暂时不能导")
        # ── @x.setter 第二遍（setter 方法与 getter 同名，单独收集；这里校验并配对）──
        # Python 语义：@x.setter 只能挂在本类已有的 @property x 上（赋值形态）。
        setters: dict[str, ast.FunctionDef] = {}
        for mname, mnode in setter_methods.items():
            prop = None
            for d in mnode.decorator_list:
                if isinstance(d, ast.Attribute) and d.attr == "setter" \
                        and isinstance(d.value, ast.Name):
                    prop = d.value.id
            if prop is None:
                raise Blocked(
                    f"setter 方法 `{mname}` 的装饰器写法不认识，暂时不能导")
            if prop not in method_kinds or method_kinds[prop] != "property":
                raise Blocked(
                    f"`@{prop}.setter` 对应的 `{prop}` 必须先有 @property 定义，暂时不能导")
            plain = list(mnode.args.posonlyargs) + list(mnode.args.args)
            if not plain or plain[0].arg != "self":
                raise Blocked(
                    f"setter 方法 `{mname}` 的第一个参数必须是 self")
            if len(plain) != 2:
                raise Blocked(
                    f"setter 方法 `{mname}` 必须正好收两个参数（self, 新值）")
            if mnode.args.vararg or mnode.args.kwarg or mnode.args.kwonlyargs:
                raise Blocked(
                    f"setter 方法 `{mname}` 带了可变参数或关键字参数，暂时不能导")
            setters[prop] = mnode
        self.classes[node.name] = {
            "node": node, "methods": methods, "bases": bases,
            "method_kinds": method_kinds, "attrs": class_attrs,
            "properties": {m for m, k in method_kinds.items() if k == "property"},
            "setters": setters,
            "str_methods": "__str__" if "__str__" in methods else None,
            "repr_methods": "__repr__" if "__repr__" in methods else None,
            "metaclass": metaclass_name,
        }

    def _emit_class(self, cls_name: str) -> None:
        info = self.classes[cls_name]
        methods = info["methods"]
        bases = info.get("bases", [])
        base = bases[0] if bases else None
        base_info = self.classes.get(base) if base else None
        init = methods.get("__init__")
        factory_name = f"构建{cls_name}"
        map_type = f"{self.kw_of('Map')}<{self.kw_of('Any')}, {self.kw_of('Any')}>"

        # v0.48：元类支持——创建类时调用元类的 __new__
        meta_name = info.get("metaclass")
        if meta_name:
            # 简化版：调用 元类___new__(元类, "类名", [父类...], {属性字典})
            bases_list = ", ".join(f'"{b}"' for b in bases)
            attrs_dict = ", ".join(f'"{k}": {v}' for k, v in info.get("attrs", {}).items())
            self.warnings.append(
                f"类 `{cls_name}` 创建时会调用元类 `{meta_name}` 的 __new__ 方法")

        # ── 构造函数：按 MRO 展开 __init__ 协作链 → 构建<类名>()，返回同一实例 ──
        # 单继承 / 多重继承走同一条路：链上的每一个 __init__ 都作用在同一个
        # `let self = {}` 上（super().__init__ 已由 _expand_init_chain 静态展开）。
        self.declared = set()
        self.instances = {}
        # ── 类属性（只读）：翻译成同名常量函数（跨实例共享的只读值）。
        # Python 类体在类定义时求值一次；AGL 常量函数每次调用求值同一字面量，
        # 语义一致。可变类属性（`A.count = A.count + 1`）在赋值点精确拒绝。
        attrs = info.get("attrs", {})
        for aname, avalue in attrs.items():
            # 可变类属性（v0.38）：初值进模块级共享存储（_agl_globals），不发常量函数；
            # 读/写都走共享存储（与 Python 跨实例共享可变类属性语义一致）
            if (cls_name, aname) in self.mutable_class_attrs:
                continue
            afname = self.check_name(f"{cls_name}_{aname}", "类属性")
            aret = self._infer_const_type(avalue)
            self.put(f"{self.kw_of('fn')} {afname}() -> {aret} {{")
            self.depth += 1
            self.put(f"{self.kw_of('return')} {self.expr(avalue)}")
            self.depth -= 1
            self.put("}")
        if attrs:
            self.put("")
        chain_params, chain_stmts = self._expand_init_chain(cls_name)
        if chain_stmts or chain_params:
            # 起点类的 __init__ 决定签名；展开后的语句体直接翻译
            start_init = None
            for c in self._mro(cls_name):
                if "__init__" in self.classes[c]["methods"]:
                    start_init = self.classes[c]["methods"]["__init__"]
                    break
            for arg in list(start_init.args.posonlyargs) + list(start_init.args.args):
                self.declared.add(arg.arg)
            self.instances["self"] = cls_name
            for node in ast.walk(ast.Module(body=start_init.body, type_ignores=[])):
                if isinstance(node, ast.Return):
                    raise Blocked(
                        f"`__init__` 里有 `return`，暂时不能导："
                        "它会被翻译成构造函数，构造函数里只能有一条收尾的 `返回 self`")
            effects, has_await = self._scan_effects(start_init.body)
            if has_await or isinstance(start_init, ast.AsyncFunctionDef):
                raise Blocked("`__init__` 不能是异步的，暂时不能导")
            sig = self.signature(start_init, effects, has_await=False,
                                 drop_first=True, fname_override=factory_name,
                                 ret_override=map_type,
                                 suffix_args=([self.globals_store]
                                              if self.globals_active else None))
            self.put(f"{sig} {{")
            self.put(_IND + f"{self.kw_of('let')} {self.check_name('self', '实例变量')} = {{}}")
            self.block(chain_stmts)
            self.put(_IND + f"{self.kw_of('return')} {self.check_name('self', '实例变量')}")
            self.put("}")
            self.put("")
        else:
            # 继承链上没有任何 __init__：空构建
            fname = self.check_name(factory_name, "类构造函数")
            params = ""
            if self.globals_active:
                params = (f"{self.check_name(self.globals_store, '参数')}: "
                          f"{self.kw_of('Map')}<{self.kw_of('String')}, {self.kw_of('Any')}>")
            self.put(f"{self.kw_of('fn')} {fname}({params}) -> {map_type} {{")
            self.put(_IND + f"{self.kw_of('let')} {self.check_name('self', '实例变量')} = {{}}")
            self.put(_IND + f"{self.kw_of('return')} {self.check_name('self', '实例变量')}")
            self.put("}")
            self.put("")

        # ── 方法：bar(self, ...) → <类名>_bar(self, ...) ──
        # @staticmethod → <类名>_s(参数...)（无实例、无 self）
        # @classmethod → <类名>_c(参数...)（去掉 cls 参数）
        method_kinds = info.get("method_kinds", {})
        for mname, mnode in methods.items():
            if mname == "__init__":
                continue
            kind = method_kinds.get(mname)
            if kind == "static":
                self.function(mnode, self_class=None, deco_ok=True,
                              fname_override=f"{cls_name}_{mname}")
            elif kind == "class":
                # classmethod：去掉 cls 参数，但 self_class 仍传类名——
                # 方法体里 cls.xxx 是类属性访问，不是实例访问
                self.function(mnode, self_class=cls_name, deco_ok=True,
                              drop_first=True,
                              fname_override=f"{cls_name}_{mname}")
            else:
                self.function(mnode, self_class=cls_name,
                              fname_override=f"{cls_name}_{mname}")

        # ── @x.setter（v0.38）：翻译成 <类>_<属性>_赋值(实例, 新值) ──
        # 与 getter 同名但单独收集；`c.x = v` 在赋值目标处分发调用。
        # 发射前清掉装饰器（function() 只认 @property/@staticmethod/@classmethod，
        # setter 的 @x.setter 已在本类收集阶段消费掉）。
        for prop, smnode in info.get("setters", {}).items():
            sm2 = copy.copy(smnode)
            sm2.decorator_list = []
            self.function(sm2, self_class=cls_name,
                          fname_override=f"{cls_name}_{prop}_赋值")

        # ── 继承：子类没覆盖的祖先方法 → 转发函数 <子类>_m(实例, ...) ──
        # 沿 MRO 顺序（除自己外）收一遍，同名方法只转发给最先定义它的类
        # （MRO 中更靠前的类，与 Python 的方法解析一致）。
        if bases:
            forwarded: set[str] = set()
            for anc in self._mro(cls_name)[1:]:
                anc_info = self.classes.get(anc)
                if not anc_info:
                    continue
                anc_kinds = anc_info.get("method_kinds", {})
                for mname, mnode in anc_info["methods"].items():
                    if mname == "__init__" or mname in methods or mname in forwarded:
                        continue  # 已覆盖（或构造）不转发；MRO 更靠前的已转发
                    if mname in anc_kinds:
                        # @staticmethod / @classmethod 不转发：它们与实例无关，
                        # 调用点沿 MRO 直接解析到定义类（Python 里子类名调用
                        # 拿到的就是父类的函数对象，行为一致）
                        forwarded.add(mname)
                        continue
                    forwarded.add(mname)
                    saved = (self.declared, self.instances)
                    self.declared = set()
                    self.instances = {"self": cls_name}
                    for arg in list(mnode.args.posonlyargs) + list(mnode.args.args):
                        self.declared.add(arg.arg)
                    base_effects, base_await = self._scan_effects(mnode.body)
                    # 被转发的祖先方法是异步的（async def 声明，或体内有 await 调用）：
                    # 转发函数自身已带「异步」前缀，转发体内调用祖先方法处也必须补「等待」，
                    # 否则编译器报「异步函数调用要写 等待」。注意不能只看 base_await——
                    # `async def m: return x+1` 这种无 await 体的方法也是异步函数。
                    anc_async = isinstance(mnode, ast.AsyncFunctionDef) or base_await
                    sig = self.signature(mnode, base_effects,
                                         has_await=base_await,
                                         self_class=cls_name,
                                         fname_override=f"{cls_name}_{mname}",
                                         suffix_args=([self.globals_store]
                                                      if self.globals_active else None))
                    self.put(f"{sig} {{")
                    inner = ", ".join(
                        self.check_name(a.arg, "参数")
                        for a in list(mnode.args.posonlyargs) + list(mnode.args.args))
                    call = f"{self.check_name(f'{anc}_{mname}', '祖先类方法')}" \
                           f"({self._append_globals(inner)})"
                    if anc_async:
                        call = f"{self.kw_of('await')} {call}"
                    self.put(_IND + f"{self.kw_of('return')} {call}")
                    self.put("}")
                    self.put("")
                    self.declared, self.instances = saved
        # v0.47：类装饰器——整个类发射完后，加装饰器赋值
        if cls_name in self._pending_class_decorators:
            factory_name = f"构建{cls_name}"
            factory_name_checked = self.check_name(factory_name, "类构造函数")
            for deco in self._pending_class_decorators[cls_name]:
                if isinstance(deco, ast.Name):
                    deco_name = self.check_name(deco.id, "装饰器")
                    self.put(f"{factory_name_checked} = {deco_name}({factory_name_checked})")
                elif isinstance(deco, ast.Call):
                    deco_call_code = self.expr(deco)
                    self.put(f"{factory_name_checked} = {deco_call_code}({factory_name_checked})")
            self.put("")
            del self._pending_class_decorators[cls_name]

    # ── 模块级常量 → 同名常量函数 ─────────────────────────────────
    def _emit_global_const(self, name: str) -> None:
        node = self.global_consts[name]
        # 可变全局（v0.38）：不发常量函数，初值进模块级共享存储（发射循环统一处理）
        if name in self.mutable_globals:
            return
        ret = self._infer_const_type(node)
        fname = self.check_name(name, "模块级常量")
        # 常量函数体引用共享存储的才收 _agl_globals（其余保持无参）
        params = ""
        if name in self.const_needs_globals:
            params = (f"{self.check_name(self.globals_store, '参数')}: "
                      f"{self.kw_of('Map')}<{self.kw_of('String')}, {self.kw_of('Any')}>")
        self.put(f"{self.kw_of('fn')} {fname}({params}) -> {ret} {{")
        self.depth += 1
        self.put(f"{self.kw_of('return')} {self.expr(node)}")
        self.depth -= 1
        self.put("}")
        self.put("")
        self.warnings.append(
            f"模块级常量 `{name}` 被翻译成同名常量函数："
            "AGL 没有全局变量，只能把它变成每次调用都重算的纯函数；"
            "如果原代码里这个变量会被赋值或原地修改，请手动改写")

    def _infer_const_type(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return self.kw_of("Bool")
            if isinstance(v, int):
                return self.kw_of("Int")
            if isinstance(v, float):
                return self.kw_of("Float")
            if isinstance(v, str):
                return self.kw_of("String")
            if v is None:
                return self.kw_of("Null")
        if isinstance(node, ast.List):
            return f"{self.kw_of('List')}<{self.kw_of('Any')}>"
        if isinstance(node, ast.Dict):
            return f"{self.kw_of('Map')}<{self.kw_of('Any')}, {self.kw_of('Any')}>"
        return self.kw_of("Any")

    # ── lambda → 提升成命名函数 ────────────────────────────────────
    def _lift_lambda(self, name: str, node: ast.Lambda) -> None:
        params = [a.arg for a in node.args.posonlyargs] + [a.arg for a in node.args.args]
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            raise Blocked(
                f"lambda 带了可变参数 / 关键字参数，暂时不能导："
                "AGL 没有闭包也没有变参，请先改成普通函数")
        # 自由变量：AGL 没有闭包，捕获的变量变成显式参数，调用处自动补传。
        # 文件级名字（本文件函数 / 类 / 模块级常量）不是闭包捕获：
        # AGL 里它们都是可直接调用的全局函数，lambda 体内照用即可，
        # 不该被当成"没在当前函数里定义的捕获变量"而误拒。
        free = self._free_vars(node, set(params))
        file_level = ({f.name for f in self.funcs}
                      | set(self.classes)
                      | set(self.global_consts))
        captures = [f for f in sorted(free) if f not in file_level]
        for c in captures:
            if c not in self.declared:
                raise Blocked(
                    f"lambda 捕获了 `{c}`，但它在当前函数里没有定义："
                    "AGL 没有闭包，捕获变量必须是本函数内先赋值过的局部变量；"
                    "请先把它改成显式传参的普通函数")
        if node.args.defaults:
            self.warnings.append(
                f"lambda 带了默认参数：AGL 函数所有参数必传，请在调用处显式传入")
        # 在干净上下文里翻译成命名函数（保存并隔离当前函数的声明表）
        saved_lines, saved_depth = self.lines, self.depth
        saved_declared, saved_instances = self.declared, self.instances
        self.lines, self.depth = [], 0
        self.declared, self.instances = set(params), {}
        try:
            body_stmt = ast.Expr(value=node.body)
            effects, _ = self._scan_effects([body_stmt])
            parts = [f"{self.check_name(a, '参数')}: {self.kw_of('Any')}"
                     for a in params]
            for c in captures:
                parts.append(f"{self.check_name(c, '捕获变量')}: {self.kw_of('Any')}")
            if self.globals_active:
                parts.append(f"{self.check_name(self.globals_store, '参数')}: "
                             f"{self.kw_of('Map')}<{self.kw_of('String')}, {self.kw_of('Any')}>")
            fname = self.check_name(name, "lambda 提升的函数名")
            sig = f"{self.kw_of('fn')} {fname}({', '.join(parts)}) -> {self.kw_of('Any')}"
            if effects:
                sig += f" {self.kw_of('effect')} {' '.join(self.kw_of(e) for e in sorted(effects))}"
            self.put(f"{sig} {{")
            self.depth += 1
            self.put(f"{self.kw_of('return')} {self.expr(node.body)}")
            self.depth -= 1
            self.put("}")
            if captures:
                self.warnings.append(
                    f"lambda 捕获了外层变量 `{'、'.join(captures)}`："
                    "AGL 没有闭包，捕获变量被提升成显式参数，调用处会自动补传；"
                    "lambda 只能在定义它的函数里被调用，不能作为值传出去")
            else:
                self.warnings.append(
                    f"lambda 被提升成命名函数 `{name}`：AGL 没有函数值，"
                    "只能翻译成普通函数；它不能捕获外层变量")
        finally:
            body_lines = list(self.lines)
            self.lines, self.depth = saved_lines, saved_depth
            self.declared, self.instances = saved_declared, saved_instances
        self.lifted_fns.append((sig, body_lines))
        # 所有提升 lambda 都记下定义函数与捕获表：调用点限定义函数内、
        # 不能被当值转出（AGL 没有函数值）；捕获型额外在调用处补传捕获变量
        self.lambda_def_fn[name] = self.cur_fn
        self.lambda_captures[name] = {"fn": fname, "captures": captures}

    # ── 嵌套函数（v0.37）────────────────────────────────────────────
    def _lift_nested_fn(self, fn: ast.FunctionDef) -> None:
        """函数内的 `def inner(...)` → 提升成模块级函数 + 环境传参。

        与 lambda 提升同一套机制（lifted_fns 输出、捕获变量变显式参数、
        调用处补传）：嵌套函数体引用的外层变量按 AST 静态分析成捕获列表，
        提升函数的参数 = 捕获变量 + 原参数；只在定义它的外层函数里可调用。
        """
        params = [a.arg for a in fn.args.posonlyargs] + [a.arg for a in fn.args.args]
        # v0.48：嵌套函数带变参也支持——和模块级函数同款处理
        has_vararg = fn.args.vararg is not None
        has_kwarg = fn.args.kwarg is not None
        # 把 *args / **kwargs 的名字也加进参数列表
        if has_vararg:
            params.append(fn.args.vararg.arg)
        if has_kwarg:
            params.append(fn.args.kwarg.arg)
        if fn.decorator_list:
            raise Blocked(
                f"嵌套函数 `{fn.name}` 带了装饰器，暂时不能导："
                "请先把装饰逻辑搬进函数体")
        # 自由变量 = 子树里用到的名字 - 内建名 - 自身参数；再减「子树内绑定名」
        # （自己的局部、更深嵌套函数的参数与函数名），剩下的才是外层捕获
        free = self._free_vars(fn, set(params))
        locals_ = self._locals_of(fn)
        file_level = ({f.name for f in self.funcs}
                      | set(self.classes)
                      | set(self.global_consts)
                      | set(_ALLOWED_MODULES))
        captures = [c for c in sorted(free)
                    if c not in file_level and c not in locals_]
        # nonlocal 回写：内层给外层变量赋值（Python 改的是外层绑定；提升成
        # 传值后改的是局部参数，调用方不受影响 → v0.47 改成 warning 不拒绝）
        store_names = self._store_names(fn)
        for c in captures:
            if c in store_names:
                self.warnings.append(
                    f"嵌套函数 `{fn.name}` 给外层变量 `{c}` 赋值（nonlocal 语义）："
                    "AGL 是值传递，传值后改的是局部参数，外层变量不会变；"
                    "如果原代码依赖这个回写行为，请手动改成返回新值的形式")
        # 捕获变量必须在当前（外层）函数作用域可见：本函数参数 / 已声明 /
        # 本函数体内任何位置会赋值的名字（块内声明提升后调用点可见）
        # v0.47：nonlocal box 变量也算已声明（外层函数里会在开头自动初始化）
        box_var_names = set(self._nonlocal_boxes.values()) if hasattr(self, '_nonlocal_boxes') else set()
        for c in captures:
            if c not in self.declared and c not in self._pending_locals and c not in box_var_names:
                raise Blocked(
                    f"嵌套函数 `{fn.name}` 捕获了 `{c}`，但它在当前函数里没有定义："
                    "AGL 没有闭包，捕获变量必须是本函数内先声明过的参数或局部变量；"
                    "请先把它改成显式传参的普通函数")
        if fn.args.defaults:
            self.warnings.append(
                f"嵌套函数 `{fn.name}` 的参数带了默认值：AGL 函数所有参数必传，"
                "请在调用处显式传入")
        fname = self._fresh_name("闭包")
        self._nested_lifted_names.add(fname)
        # 在干净上下文里翻译函数体（隔离当前函数的声明表，防嵌套 def 的
        # 局部变量污染外层翻译；块内首次声明提升照常做）
        saved_lines, saved_depth = self.lines, self.depth
        saved_declared, saved_instances = self.declared, self.instances
        saved_promoted = self._promoted
        saved_set_vars, saved_list_vars, saved_dict_vars, saved_fn_ref_vars = (
            self.set_vars, self.list_vars, self.dict_vars, self.fn_ref_vars)
        saved_except, saved_fresh, saved_cur = (self._except_stack,
                                                self._fresh_used,
                                                self.cur_fn)
        saved_async = self.cur_fn_is_async
        saved_nested = self.nested_map
        self.lines, self.depth = [], 0
        self.declared, self.instances = set(params) | set(captures), {}
        self._promoted = set()
        self.set_vars, self.list_vars, self.dict_vars, self.fn_ref_vars = (
            set(), set(), set(), {})
        self._except_stack = []
        self.cur_fn = fn.name
        # v0.39：翻译 inner 体期间，「当前函数」就是 inner 自己——它体内再
        # 嵌套更深的 async 函数时，判定依据是 inner 是不是 async
        # （AsyncFunctionDef 直接标记；普通函数体里 has_await 不可能为真）
        self.cur_fn_is_async = isinstance(fn, ast.AsyncFunctionDef)
        try:
            effects, has_await = self._scan_effects(fn.body)
            sig = self.signature(fn, effects, has_await,
                                 fname_override=fname, prefix_args=captures,
                                 suffix_args=([self.globals_store]
                                              if self.globals_active else None))
            self.put(f"{sig} {{")
            self.depth += 1
            self._promote_block_decls(fn.body)
            self.block(fn.body)
            self.depth -= 1
            self.put("}")
        finally:
            body_lines = list(self.lines)
            self.lines, self.depth = saved_lines, saved_depth
            self.declared, self.instances = saved_declared, saved_instances
            self._promoted = saved_promoted
            self.set_vars, self.list_vars, self.dict_vars, self.fn_ref_vars = (
                saved_set_vars, saved_list_vars, saved_dict_vars, saved_fn_ref_vars)
            self._except_stack, self._fresh_used, self.cur_fn = (saved_except,
                                                                 saved_fresh,
                                                                 saved_cur)
            self.cur_fn_is_async = saved_async
            # 更深层嵌套函数（c 定义在 b 提升函数体内）的登记只在翻译 b 体
            # 期间有意义，翻译完即丢：外层函数作用域里没有 c 这个绑定
            self.nested_map = saved_nested
        self.lifted_fns.append((sig, body_lines))
        self.nested_map[fn.name] = {
            "fn": fname,
            "captures": captures,
            "is_async": isinstance(fn, ast.AsyncFunctionDef),
        }
        if captures:
            self.warnings.append(
                f"嵌套函数 `{fn.name}` 被提升成模块级函数：AGL 没有闭包，"
                f"它捕获的外层变量 `{'、'.join(captures)}` 变成显式参数，"
                "调用处会自动补传；嵌套函数只能在定义它的函数里被调用")
        else:
            self.warnings.append(
                f"嵌套函数 `{fn.name}` 被提升成模块级函数：AGL 没有函数值，"
                "只能翻译成普通函数；它只能在定义它的函数里被调用")

    def _locals_of(self, fn: ast.FunctionDef) -> set[str]:
        """fn 子树内所有「绑定」的名字：内层赋值目标、更深嵌套函数的
        参数与函数名。这些名字不是外层捕获（外层只能捕获更外层作用域的）。"""
        names: set[str] = set()
        for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, ast.arg):
                names.add(n.arg)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(n.name)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
        return names

    def _store_names(self, fn: ast.FunctionDef) -> set[str]:
        """fn 子树内所有被赋值的名字（nonlocal 回写检测、块内声明预扫描用）。"""
        names: set[str] = set()
        for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
        return names

    def _locals_top_level(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        """fn 自己的局部变量名（参数 + 函数体内的赋值目标，不含嵌套函数的）。
        手动遍历 body，遇到嵌套函数就停止（不深入）。"""
        names: set[str] = set()
        for a in fn.args.posonlyargs + fn.args.args:
            names.add(a.arg)

        class _TopLevelStoreFinder(ast.NodeVisitor):
            """只收集当前函数顶层的 Store 变量名，遇到嵌套函数就停。"""
            def __init__(self):
                self.names: set[str] = set()

            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Store):
                    self.names.add(node.id)

            def visit_FunctionDef(self, node):
                pass  # 不深入嵌套函数

            def visit_AsyncFunctionDef(self, node):
                pass

            def visit_Lambda(self, node):
                pass

        finder = _TopLevelStoreFinder()
        finder.visit(ast.Module(body=fn.body, type_ignores=[]))
        names.update(finder.names)

        return names

    class _BoxVarsTransformer(ast.NodeTransformer):
        """把函数体里所有用到指定变量的地方替换成 box[0] 形式。
        嵌套函数作用域自动隔离——嵌套函数自己的参数和局部变量不替换。"""
        def __init__(self, var_to_box: dict[str, str]):
            # key=原变量名, value=box 变量名
            self.var_to_box = var_to_box
            # 当前作用域里的局部变量名（这些不替换）
            self.scope_locals: list[set[str]] = [set()]

        def _locals_of_func(self, node) -> set[str]:
            locals_ = set()
            for a in node.args.posonlyargs + node.args.args:
                locals_.add(a.arg)
            for n in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    locals_.add(n.id)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    locals_.add(n.name)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    locals_.add(n.name)
            # nonlocal 声明的变量不是内层自己的局部变量——是外层的，要排除掉
            for n in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(n, ast.Nonlocal):
                    for v in n.names:
                        locals_.discard(v)
            return locals_

        def visit_Name(self, node):
            # 如果在当前作用域的局部变量里，不替换
            for scope in self.scope_locals:
                if node.id in scope:
                    return node
            if node.id in self.var_to_box:
                box_name = self.var_to_box[node.id]
                # 替换成 box[0]，保留原来的 ctx（Load/Store/Del）
                return ast.Subscript(
                    value=ast.Name(id=box_name, ctx=ast.Load()),
                    slice=ast.Constant(value=0),
                    ctx=node.ctx,
                )
            return node

        def visit_FunctionDef(self, node):
            # 进入嵌套函数：把它的局部变量压栈，翻译完再弹栈
            self.scope_locals.append(self._locals_of_func(node))
            node = self.generic_visit(node)
            self.scope_locals.pop()
            return node

        def visit_AsyncFunctionDef(self, node):
            self.scope_locals.append(self._locals_of_func(node))
            node = self.generic_visit(node)
            self.scope_locals.pop()
            return node

        def visit_Lambda(self, node):
            locals_ = set(a.arg for a in node.args.posonlyargs + node.args.args)
            self.scope_locals.append(locals_)
            node = self.generic_visit(node)
            self.scope_locals.pop()
            return node

    def _check_lambda_usage(self) -> None:
        """提升 lambda 的调用点必须限定在定义函数内，且不能作为值传出。"""
        for lname, info in self.lambda_captures.items():
            lifted = info["fn"]
            def_fn = self.lambda_def_fn.get(lname)
            for f in self.funcs:
                self._scan_lambda_uses(f.body, lname, lifted,
                                       own_fn=(f.name == def_fn))

    def _scan_lambda_uses(self, body: list[ast.stmt], lname: str,
                          lifted: str, own_fn: bool) -> None:
        targets = (lname, lifted)

        class _V(ast.NodeVisitor):
            def __init__(self):
                self.bad: str | None = None

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id in targets:
                    # 调用本身 OK；但实参位置出现它（g(f)）就是当值用，要查
                    for a in node.args:
                        self.visit(a)
                    for kw in node.keywords:
                        self.visit(kw.value)
                    return
                self.generic_visit(node)

            def visit_Assign(self, node):
                # 赋值目标里的它是定义处（f = lambda），不算使用；其余照查
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in targets:
                        continue
                    self.visit(t)
                self.visit(node.value)

            def visit_Name(self, node):
                if node.id in targets:
                    self.bad = node.id

            def visit_Attribute(self, node):
                self.visit(node.value)

        v = _V()
        for n in body:
            v.visit(n)
        if v.bad:
            where = "定义它的函数里被当成值使用" if own_fn else "定义它的函数之外被使用"
            raise Blocked(
                f"lambda `{lname}` 在{where}（AGL 没有函数值，"
                "lambda 只能被翻译成命名函数，不能转出去；捕获变量的 lambda "
                "调用点也必须在定义函数内）：请把它改成显式传参的普通函数")

    @staticmethod
    def _free_vars(node: ast.Lambda, params: set[str]) -> set[str]:
        """lambda 体里用到、又不是参数的变量名（闭包依赖）。"""
        import builtins
        known = set(dir(builtins))
        free: set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id not in params and n.id not in known:
                free.add(n.id)
        return free


def import_python(src: str, lang: str = LANG_ZH) -> ImportResult:
    return PyImporter(src, lang).run()
