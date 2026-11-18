# -*- coding: utf-8 -*-
"""AGB v0 的栈式解释器（M1 骨架 + M2 部分）。

已执行：字面量 / 变量 / 算术 / 比较 / 逻辑 / 分支 / while / for /
函数调用与递归 / 列表、映射表、集合 / 下标 / `.长度` / 基础内建 /
本地与跨模块结构体 / try-catch-finally（含类型化捕获、`否则`）/ match / 模块加载 /
`使用`（结果对象）/ 读文件、写文件、追加写文件 / FFI /
`任务` / `等待` / `当任务完成`（按创建顺序推进的惰性任务队列，不是真并发）。

**不执行**：AGL 源语法里没有闭包 / lambda / 函数值。导入器把 Python 的
嵌套函数和 lambda 提升成「模块级函数 + 捕获变量变显式参数」，所以 AGB 里
不会出现需要捕获环境的节点；这不是待补的 VM 缺口。
FFI 只允许显式 `导入 外部` 的库，运行期复核 `External` 效应。
默认后端仍是 py，`aglc vm` / `aglc run --backend vm` 显式启用。
"""
from __future__ import annotations

import importlib
import json
import time
from urllib.parse import quote, unquote

RT_GENERIC = "AGL-RT-0001"
RT_DIV_ZERO = "AGL-RT-0010"
RT_INDEX = "AGL-RT-0020"
RT_KEY = "AGL-RT-0021"
RT_LIMIT = "AGL-RT-0091"
RT_EFFECT = "AGL-RT-0092"
RT_CALLS = "AGL-RT-0093"
RT_TOOLS = "AGL-RT-0094"
RT_TIMEOUT = "AGL-RT-0095"


def _html_escape(value):
    """转义 HTML 文本节点/属性里必须转义的五个字符。"""
    text = str(value)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))


def _url_encode(value):
    """按 URL 组件编码：保留 RFC 3986 的非保留字符，空格编码为 %20。"""
    return quote(str(value), safe="")


def _url_decode(value):
    """解码 URL 百分比转义；不把 `+` 当空格（那是表单的规则）。"""
    return unquote(str(value))


def _load_builtin_effects() -> dict[str, str]:
    """从关键字表读「内建 → 必需效应」，不在 VM 里维护第二份手写表。

    之前只硬编码了 IO / 计时四个，导致 `读入`、`当前时间戳`、`存在`、
    读写数据库、抓网页等带效应的内建在 VM 里能绕过效应声明。这里按
    `spec/keywords.toml` 的 `effect` 字段统一收口；表读不到时退回最小集，
    保证 VM 仍能启动。
    """
    fallback = {
        "read_file": "IO", "write_file": "IO", "append_file": "IO",
        "input": "IO", "exists": "IO", "sleep": "Time", "now": "Time",
    }
    try:
        from compiler.lexer import KeywordTable

        return {b.canonical: b.effect
                for b in KeywordTable.load().builtins if b.effect}
    except Exception:
        return fallback


# 有副作用的内建 → 需要函数声明对应效应；VM 在执行点再拦一道。
_BUILTIN_EFFECT = _load_builtin_effects()

_EXC_CODES = {
    "零除": {RT_DIV_ZERO}, "除零": {RT_DIV_ZERO}, "ZeroDivisionError": {RT_DIV_ZERO},
    "下标越界": {RT_INDEX}, "IndexError": {RT_INDEX},
    "键错误": {RT_KEY}, "KeyError": {RT_KEY},
    "值错误": {RT_GENERIC}, "类型错误": {RT_GENERIC}, "ValueError": {RT_GENERIC},
    "TypeError": {RT_GENERIC},
    "运行时错误": set(), "RuntimeError": set(),
}


def _catch_matches(catch_type: str, err: "VmError") -> bool:
    """类型化捕获：空集合表示抓所有；`全部` 也抓所有。"""
    name = catch_type.strip()
    if name in ("全部", "Exception", "BaseException"):
        return True
    codes = _EXC_CODES.get(name)
    if codes is None:
        return True
    return not codes or err.code in codes


class VmError(Exception):
    def __init__(self, code: str, message: str, line: int = 1, fn: str = ""):
        self.code = code
        self.message = message
        self.line = line
        self.fn = fn
        super().__init__(message)

    def render(self, filename: str) -> str:
        where = f"{filename} 第 {self.line} 行"
        if self.fn:
            where += f"（函数 {self.fn}）"
        return f"✗ {self.code}  {where}\n{self.message}"


class ResultBox:
    __slots__ = ("ok", "value")

    def __init__(self, ok: bool, value):
        self.ok = ok
        self.value = value


class StructValue:
    """结构体运行时值：模块 + 类型名 + 字段（字段顺序取声明顺序）。"""

    __slots__ = ("type_name", "fields", "order", "module")

    def __init__(self, type_name: str, values: dict, order: list[str], module: str = ""):
        self.type_name = type_name
        self.fields = values
        self.order = order
        self.module = module


class FnRef:
    """函数引用：当前只在 `按键排序` 的键函数位置使用。"""

    __slots__ = ("module", "name")

    def __init__(self, module: str, name: str):
        self.module = module
        self.name = name


class TaskHandle:
    """`任务(...)` 的句柄：先记下待跑的函数与实参，到 `等待` 或主入口结束时再跑。"""

    __slots__ = ("kind", "module", "name", "args", "status", "result",
                 "error", "handled", "cancelled", "callbacks", "line", "fn")

    def __init__(self, kind: str, module: str, name: str, args: list,
                 line: int = 1, fn: str = ""):
        self.kind = kind
        self.module = module
        self.name = name
        self.args = args
        self.status = "pending"
        self.result = None
        self.error = None
        # 失败已被 `当任务失败` 接管：`等待` 不再重抛，和后端一致。
        self.handled = False
        self.cancelled = False
        self.callbacks = []
        self.line = line
        self.fn = fn


def _equal(a, b) -> bool:
    if isinstance(a, StructValue) and isinstance(b, StructValue):
        if a.type_name != b.type_name or a.module != b.module:
            return False
        return all(_equal(a.fields.get(f), b.fields.get(f)) for f in a.order)
    return a == b


def _truth(value, lang: str = "zh") -> bool:
    if isinstance(value, ResultBox):
        return value.ok
    if type(value).__name__ == "_Result" and hasattr(value, "ok"):
        return bool(value.ok)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, (str, list, dict, set)):
        return len(value) > 0
    return True


def _str_repr(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _show(value, lang: str = "zh", seen=None) -> str:
    if isinstance(value, bool):
        return ("真" if value else "假") if lang == "zh" else ("true" if value else "false")
    if value is None:
        return "空" if lang == "zh" else "Null"
    if isinstance(value, ResultBox):
        head = ("成功" if value.ok else "失败") if lang == "zh" else ("Ok" if value.ok else "Err")
        return f"{head}({_show(value.value, lang, seen)})"
    if type(value).__name__ == "_Result" and hasattr(value, "ok") and hasattr(value, "value"):
        # 纯计算内建直接复用 backend_py 的 `_Result`，打印口径必须一致。
        head = ("成功" if value.ok else "失败") if lang == "zh" else ("Ok" if value.ok else "Err")
        return f"{head}({_show(value.value, lang, seen)})"
    if isinstance(value, StructValue):
        body = ", ".join(
            f"{f}={_show(value.fields.get(f), lang, seen)}" for f in value.order
        )
        return f"{value.type_name}({body})"
    if isinstance(value, TaskHandle):
        head = "任务" if lang == "zh" else "Task"
        return f"{head}({value.name})"
    if isinstance(value, VmError):
        return value.message
    if isinstance(value, list):
        seen = seen or set()
        if id(value) in seen:
            return "[…]"
        seen.add(id(value))
        out = "[" + ", ".join(
            _str_repr(v) if isinstance(v, str) else _show(v, lang, seen) for v in value
        ) + "]"
        seen.discard(id(value))
        return out
    if isinstance(value, dict):
        seen = seen or set()
        if id(value) in seen:
            return "{…}"
        seen.add(id(value))
        out = "{" + ", ".join(
            f"{_str_repr(k) if isinstance(k, str) else _show(k, lang, seen)}: "
            f"{_str_repr(v) if isinstance(v, str) else _show(v, lang, seen)}"
            for k, v in value.items()
        ) + "}"
        seen.discard(id(value))
        return out
    if isinstance(value, set):
        return "{" + ", ".join(
            _str_repr(v) if isinstance(v, str) else _show(v, lang, seen) for v in sorted(value, key=str)
        ) + "}"
    return str(value)


def _io_reason(exc: OSError) -> str:
    """把宿主文件错误翻成中文；VM 的报错不许直接漏 Python 异常文本。"""
    if isinstance(exc, FileNotFoundError):
        return "文件不存在"
    if isinstance(exc, PermissionError):
        return "没有权限访问文件"
    if isinstance(exc, IsADirectoryError):
        return "路径是一个目录"
    return str(exc) or "文件操作失败"


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return ResultBox(True, f.read())
    except OSError as e:
        return ResultBox(False, _io_reason(e))
    except UnicodeDecodeError:
        return ResultBox(False, "文件不是 UTF-8 文本")


def _write_file(path, content):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return ResultBox(True, None)
    except OSError as e:
        return ResultBox(False, _io_reason(e))


def _append_file(path, content):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return ResultBox(True, None)
    except OSError as e:
        return ResultBox(False, _io_reason(e))


_BACKEND_RT: dict[str, dict] = {}


def _backend_runtime(lang: str = "zh") -> dict:
    """把 backend_py 的纯内建实现装进一个命名空间，供 VM 直接复用。

    这些函数本来只存在于生成代码的 RUNTIME 文本里；VM 复制一份会漂移，
    所以执行同一段运行时定义，拿到 `_agl_join` / `_agl_split` 等原函数。
    中英各一份命名空间，报错文案才跟当前分支一致。
    """
    ns = _BACKEND_RT.get(lang)
    if ns is None:
        from compiler import backend_py

        ns = {
            "__AGL_ZH__": lang == "zh",
            "__AGL_FILE__": "<vm>",
            "__AGL_SOURCE__": "",
        }
        exec(backend_py.RUNTIME, ns)
        _BACKEND_RT[lang] = ns
    return ns


def _agl_fail_reason(value):
    """`失败原因` 的 VM 版：结果对象取 value，捕获到 VmError 时取中文原因。

    种子 Python 后端的捕获变量是宿主异常对象，`_agl_fail_reason` 已处理；
    VM 捕获时压栈的是 VmError（不是 ResultBox），此前只认 `.value` 会 AttributeError。
    """
    if isinstance(value, VmError):
        return value.message
    if isinstance(value, ResultBox):
        return value.value
    # 种子后端运行时返回的是它自己的 _Result（VM 复用 backend_py 的纯计算内建），
    # 形状同为 .ok/.value，不能只认 VM 自己的 ResultBox。
    if hasattr(value, "ok") and hasattr(value, "value"):
        return value.value
    return getattr(value, "message", str(value))


def _agl_http_serve(port, text):
    """常驻简单 HTTP 服务：所有 GET 返回同一段文本。"""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = str(text).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", int(port)), Handler)
    print(f"HTTP服务已启动：http://localhost:{port}")
    server.serve_forever()


def _agl_web_serve(port, handler_name, vm, host="127.0.0.1"):
    """可编程网页服务：把请求映射交给用户指定的 AGL 处理函数。"""
    from compiler.webapp import serve_web_app

    name = str(handler_name or "").strip()
    if not name:
        _raise(RT_GENERIC, "启动网页应用需要一个处理函数名")

    def call_handler(request):
        return vm._call(vm.root, name, [request])

    serve_web_app(int(port), call_handler, vm.lang, host=str(host))


def _agl_todo_web_serve(port, db_path):
    """常驻待办 Web 服务：GET 返回 JSON 列表，POST /add 新增。"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json
    import sqlite3
    import urllib.parse

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS todo "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, 内容 TEXT NOT NULL, 完成 INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    class Handler(BaseHTTPRequestHandler):
        def _rows(self):
            c = sqlite3.connect(db_path)
            try:
                return c.execute(
                    "SELECT id, 内容, 完成 FROM todo ORDER BY id DESC"
                ).fetchall()
            finally:
                c.close()

        def do_GET(self):
            data = json.dumps(
                [{"id": r[0], "内容": r[1], "完成": r[2]} for r in self._rows()],
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path == "/add":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                content = urllib.parse.parse_qs(body).get("内容", [""])[0]
                if content:
                    c = sqlite3.connect(db_path)
                    c.execute("INSERT INTO todo (内容) VALUES (?)", (content,))
                    c.commit()
                    c.close()
                self.send_response(201)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", int(port)), Handler)
    print(f"待办Web服务已启动：http://localhost:{port}")
    server.serve_forever()


def _state_encode(value):
    """P8-2：VM 侧状态编码，格式与 backend_py 的 _agl_state_encode 一致。"""
    if isinstance(value, ResultBox):
        return {"__agl_type__": "result", "ok": value.ok,
                "value": _state_encode(value.value)}
    if isinstance(value, StructValue):
        return {"__agl_type__": "struct", "type": value.type_name,
                "module": value.module, "order": list(value.order),
                "fields": {k: _state_encode(v) for k, v in value.fields.items()}}
    if isinstance(value, (set, frozenset)):
        return {"__agl_type__": "set",
                "items": [_state_encode(x) for x in sorted(value, key=repr)]}
    if isinstance(value, dict):
        return {"__agl_type__": "map",
                "items": [[_state_encode(k), _state_encode(v)]
                          for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [_state_encode(x) for x in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("快照不支持这种值：" + type(value).__name__)


def _state_decode(value):
    if isinstance(value, list):
        return [_state_decode(x) for x in value]
    if not isinstance(value, dict) or "__agl_type__" not in value:
        return value
    kind = value.get("__agl_type__")
    if kind == "result":
        return ResultBox(bool(value.get("ok")), _state_decode(value.get("value")))
    if kind == "struct":
        return StructValue(
            value.get("type") or "", {k: _state_decode(v)
            for k, v in (value.get("fields") or {}).items()},
            list(value.get("order") or []), value.get("module") or "")
    if kind == "set":
        return set(_state_decode(x) for x in (value.get("items") or []))
    if kind == "map":
        return {_state_decode(k): _state_decode(v)
                for k, v in (value.get("items") or [])}
    raise ValueError("快照里有无法识别的值类型：" + str(kind))


def _builtins(vm) -> dict:
    """VM 内建：纯计算 + 结果对象 + 文本文件读写（文件操作由效应声明把关）。"""
    import math

    def divmod_agl(a, b):
        return [a // b, a % b]

    def show(x):
        return _show(x, vm.lang)

    def save_state(path):
        try:
            mod = vm.modules.get(vm._active_module or vm.root)
            if mod is None:
                return ResultBox(False, "当前模块不存在")
            table = {k: _state_encode(v) for k, v in mod.globals.items()
                     if not k.startswith("_")}
            data = {"format": "agl.state", "version": 1,
                    "lang": vm.lang, "globals": table}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            return ResultBox(True, None)
        except OSError as e:
            return ResultBox(False, str(e))
        except (TypeError, ValueError) as e:
            return ResultBox(False, str(e) or "快照编码失败")

    def load_state(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("format") != "agl.state":
                return ResultBox(False, "不是 AGL 状态快照")
            table = data.get("globals")
            if not isinstance(table, dict):
                return ResultBox(False, "状态快照缺少 globals")
            mod = vm.modules.get(vm._active_module or vm.root)
            if mod is None:
                return ResultBox(False, "当前模块不存在")
            # 先整体解码，成功后再落表，避免半恢复。
            restored = {k: _state_decode(v) for k, v in table.items()
                        if not k.startswith("_")}
            mod.globals.update(restored)
            return ResultBox(True, len(restored))
        except OSError as e:
            return ResultBox(False, str(e))
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            return ResultBox(False, "状态快照解析失败：" + str(e))

    base = {
        "print": lambda *v: vm._emit(" ".join(show(x) for x in v), True),        "print_no_newline": lambda *v: vm._emit(" ".join(show(x) for x in v), False),
        "to_text": show,
        "repr": show,
        "format_number": lambda x, n=0: f"{x:.{n}f}",
        "abs": abs, "min": min, "max": max, "sum": sum,
        "round": round, "pow": pow, "floordiv": lambda a, b: a // b,
        "divmod": divmod_agl,
        "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil,
        "trunc": math.trunc, "sign": lambda x: (x > 0) - (x < 0),
        "split": lambda s, sep=None, maxsplit=-1: (
            list(str(s)) if sep == "" else str(s).split(str(sep))
            if maxsplit is None or int(maxsplit) < 0 else str(s).split(str(sep), int(maxsplit))),
        "join": lambda sep, items: sep.join(show(x) for x in items),
        "replace": lambda s, a, b: s.replace(a, b),
        "html_escape": _html_escape,
        "url_encode": _url_encode,
        "url_decode": _url_decode,
        "strip": lambda s: s.strip(), "lstrip": lambda s: s.lstrip(),
        "rstrip": lambda s: s.rstrip(),
        "upper": lambda s: s.upper(), "lower": lambda s: s.lower(),
        "contains": lambda s, sub: sub in s,
        "find": lambda s, sub: s.find(sub), "rfind": lambda s, sub: s.rfind(sub),
        "startswith": lambda s, p: s.startswith(p),
        "endswith": lambda s, p: s.endswith(p),
        "isdigit": lambda s: s.isdigit(), "isalpha": lambda s: s.isalpha(),
        "splitlines": lambda s: s.splitlines(),
        "format": lambda s, *a: s.format(*a),
        "range": lambda *a: list(range(*a)),
        "sorted": lambda xs: sorted(xs),
        "sort_by": lambda xs, keyf: sorted(
            xs, key=lambda x: vm._call(keyf.module, keyf.name, [x])
        ),
        "call_fn": lambda ref, *a: vm._call_ref(ref, *a),
        "sleep": lambda ms: time.sleep(float(ms) / 1000.0),
        "http_serve": _agl_http_serve,
        "web_serve": lambda port, handler, host="127.0.0.1": _agl_web_serve(port, handler, vm, host),
        "todo_web_serve": _agl_todo_web_serve,
        "cancel": lambda h: vm._cancel_task(h),
        "report_progress": lambda text: vm._report_progress(text),
        "reverse": lambda xs: list(reversed(xs)),
        "count": lambda xs, x: xs.count(x),
        "keys": lambda d: list(d.keys()),
        "values": lambda d: list(d.values()),
        "items": lambda d: [[k, v] for k, v in d.items()],
        "get": lambda d, k, default=None: d.get(k, default),
        "append": lambda xs, x: xs.append(x) or xs,
        "append_list": lambda xs, ys: xs.extend(ys) or xs,
        "sublist": lambda xs, a, b=None: xs[a:b],
        "slice": lambda xs, a, b=None: xs[a:b],
        "to_int": lambda x: int(x), "to_float": lambda x: float(x),
        "to_bool": lambda x: _truth(x), "to_list": lambda x: list(x),
        "make_set": lambda *xs: set(xs),
        "add": lambda s, x: s.add(x) or s,
        "union": lambda a, b: a | b,
        "intersection": lambda a, b: a & b,
        "difference": lambda a, b: a - b,
        "is_same": lambda a, b: a == b,
        "is_not_same": lambda a, b: a != b,
        "Ok": lambda v: ResultBox(True, v),
        "Err": lambda v: ResultBox(False, v),
        "unwrap": lambda r: r.value if r.ok else (_raise(RT_GENERIC, show(r.value))),
        "fail_reason": _agl_fail_reason,
        "read_file": _read_file,
        "write_file": _write_file,
        "append_file": _append_file,
        "save_state": save_state,
        "load_state": load_state,
        "存快照": save_state,
        "取快照": load_state,
    }
    # 能直接复用 backend_py 的纯计算内建就复用：两边共享同一套边界行为
    # （空分隔符、格式化宽度、越界文案……），VM 不再维护第二份实现。
    vm_specific = {
        "print", "print_no_newline", "to_text", "sort_by", "call_fn",
        "sleep", "cancel", "report_progress", "read_file", "write_file",
        "append_file", "save_state", "load_state", "Ok", "Err", "unwrap", "fail_reason", "task",
        "http_serve", "web_serve", "todo_web_serve",
    }
    try:
        from compiler.lexer import KeywordTable

        rt = _backend_runtime(vm.lang)
        for b in KeywordTable.load().builtins:
            if b.canonical in vm_specific:
                continue
            fn = rt.get(b.lower)
            if callable(fn):
                base[b.canonical] = fn
        if callable(base.get("call_tool")):
            real_call_tool = base["call_tool"]

            def _call_tool_whitelisted(name, args, config, _real=real_call_tool):
                if vm.allowed_tools is not None and name not in vm.allowed_tools:
                    raise VmError(
                        RT_TOOLS,
                        f"工具 `{name}` 不在运行时工具白名单里，"
                        f"用 --allow-tools 显式放行",
                        0, "<tool>")
                return _real(name, args, config)

            base["call_tool"] = _call_tool_whitelisted
    except Exception:
        # 共享纯内建只是加速与一致性优化；拿不到时仍保留 VM 本地实现。
        pass
    return base


def _raise(code, message):
    raise VmError(code, str(message))


class _Module:
    __slots__ = ("name", "agb", "consts", "names", "types", "funcs", "globals", "ready")

    def __init__(self, name: str, agb: dict):
        self.name = name
        self.agb = agb
        self.consts = agb.get("consts") or []
        self.names = agb.get("names") or []
        self.types = {t["name"]: t.get("fields") or [] for t in agb.get("types") or []}
        self.funcs = {f["name"]: f for f in agb.get("functions") or []}
        self.globals: dict = {}
        self.ready = False


class Frame:
    __slots__ = ("fn", "locals", "stack", "pc", "line", "handlers", "module")

    def __init__(self, fn: dict, module: str):
        self.fn = fn
        self.locals = {}
        self.stack = []
        self.pc = 0
        self.line = 1
        self.handlers = []
        self.module = module


class VM:
    def __init__(self, agb: dict, lang: str = "zh", modules: dict | None = None,
                 max_steps: int | None = None,
                 allowed_effects: set[str] | None = None,
                 max_calls: int | None = None,
                 allowed_tools: set[str] | None = None,
                 timeout_ms: int | None = None):
        self.agb = agb
        self.lang = lang
        self.max_steps = 5_000_000 if max_steps is None else max_steps
        # 墙钟超时：步数上限管不住「单步很慢」的宿主调用，进程级超时需要单独一条。
        self.timeout_ms = timeout_ms
        self.deadline: float | None = None
        # 权限白名单：None = 不做额外限制（保持既有行为）；空集合 = 一个效应都不授权。
        # 这是 M3 的第一层沙箱：函数自己声明了效应还不够，还得宿主点头。
        self.allowed_effects = None if allowed_effects is None else set(allowed_effects)
        # P8-1：调用次数配额（带效应的内建 + FFI）与外部库白名单。
        self.max_calls = max_calls
        self.allowed_tools = None if allowed_tools is None else set(allowed_tools)
        self.steps = 0
        self.calls = 0
        self.modules: dict[str, _Module] = {}
        root = agb.get("module") or "main"
        self.root = root
        self.modules[root] = _Module(root, agb)
        for name, m in (modules or {}).items():
            key = m.get("module") or name
            self.modules[key] = _Module(key, m)
        self.out: list[str] = []
        self.builtins = _builtins(self)
        self.depth = 0
        self.call_modules: list[str] = []
        self.pending: list[TaskHandle] = []
        self.current_task: TaskHandle | None = None

    def _emit(self, text: str, newline: bool) -> None:
        if newline or not self.out:
            self.out.append(text)
        else:
            self.out[-1] += text

    def run(self) -> str:
        self._ensure(self.root)
        entry = self.agb["entry"]
        if entry not in self.modules[self.root].funcs:
            raise VmError(RT_GENERIC, f"入口函数 `{entry}` 不存在")
        if self.timeout_ms is not None:
            self.deadline = time.monotonic() + max(0.0, self.timeout_ms / 1000.0)
        try:
            self._call(self.root, entry, [])
        except VmError:
            self._drain_tasks()
            raise
        self._drain_tasks()
        return "\n".join(self.out)

    def _ensure(self, name: str) -> _Module:
        mod = self.modules.get(name)
        if mod is None:
            raise VmError(RT_GENERIC, f"模块 `{name}` 没有加载")
        if not mod.ready:
            mod.ready = True
            init = mod.agb.get("init")
            if init:
                init_frame = Frame(init, name)
                init_frame.locals = mod.globals
                self._execute(init_frame)
        return mod

    def _count_call(self, ctx, what: str) -> None:
        """P8-1 调用次数配额：带效应的内建 + FFI 调用共享一个计数器。

        ctx 是 Frame（fn 是函数 dict）或 TaskHandle（fn 是名字字符串）。
        """
        if self.max_calls is None:
            return
        self.calls += 1
        if self.calls > self.max_calls:
            fn_name = ctx.fn["name"] if isinstance(ctx.fn, dict) else ctx.fn
            raise VmError(
                RT_CALLS,
                f"外部调用次数超过上限 {self.max_calls}，不能调用 {what}"
                f"（用 --max-calls 放宽）",
                ctx.line, fn_name,
            )

    def _check_effects(self, frame: Frame, needed, what: str) -> None:
        """运行期效应复核：先查调用方声明，再查宿主权限白名单。"""
        missing = set(needed or []) - set(frame.fn.get("effects") or [])
        if missing:
            tags = "、".join(sorted(missing))
            raise VmError(
                RT_EFFECT,
                f"函数 `{frame.fn['name']}` 没有声明效应 `{tags}`，不能调用 {what}",
                frame.line, frame.fn["name"],
            )
        if self.allowed_effects is not None:
            denied = sorted(set(needed or []) - self.allowed_effects)
            if denied:
                tags = "、".join(denied)
                raise VmError(
                    RT_EFFECT,
                    f"当前运行时没有授权效应 `{tags}`，不能调用 {what}"
                    f"（用 --allow-effects 放行）",
                    frame.line, frame.fn["name"],
                )

    def _await_task(self, handle: TaskHandle):
        """`等待 句柄`：按创建顺序把排在它前面的任务也推进完，和后端事件循环一致。"""
        while handle.status == "pending" and not handle.cancelled:
            if self.pending and self.pending[0] is not handle:
                nxt = self.pending.pop(0)
                if nxt.status == "pending" and not nxt.cancelled:
                    self._run_task(nxt)
            else:
                if handle in self.pending:
                    self.pending.remove(handle)
                if not handle.cancelled:
                    self._run_task(handle)
        return self._run_task(handle)

    def _fire_event(self, handle: TaskHandle, event: str, value) -> None:
        for ev, ref in list(handle.callbacks):
            if ev == event:
                self._call(ref.module, ref.name, [value])

    def _run_task(self, handle: TaskHandle):
        if handle.cancelled:
            return None
        if handle.status == "done":
            if handle.error is not None and not handle.handled:
                raise handle.error
            return handle.result
        handle.status = "running"
        prev = self.current_task
        self.current_task = handle
        try:
            if handle.kind == "user":
                handle.result = self._call(handle.module, handle.name, handle.args)
            elif handle.kind == "module":
                handle.result = self._call(handle.module, handle.name, handle.args)
            else:
                fnb = self.builtins.get(handle.name)
                if fnb is None:
                    raise VmError(RT_GENERIC, f"任务内建 `{handle.name}` 不存在",
                                  handle.line, handle.fn)
                need = _BUILTIN_EFFECT.get(handle.name)
                if need:
                    self._count_call(handle, f"任务 `{handle.name}`")
                res = fnb(*handle.args)
                if isinstance(res, tuple) and len(res) == 2 and res[0] == "out":
                    self.out.append(res[1])
                    res = None
                handle.result = res
        except VmError as e:
            handle.error = e
            handle.result = None
        finally:
            self.current_task = prev
            handle.status = "done"
        if handle.error is not None:
            fail = [cb for cb in handle.callbacks if cb[0] == "fail"]
            self._fire_event(handle, "fail", handle.error)
            if not fail:
                raise handle.error
            # 有 `当任务失败` 接管：错误已交给回调，`等待` 拿到空值并继续。
            handle.handled = True
        else:
            self._fire_event(handle, "finish", handle.result)
        return handle.result

    def _register_event(self, handle, event: str, ref: FnRef) -> None:
        if not isinstance(handle, TaskHandle) or handle.cancelled:
            return
        handle.callbacks.append((event, ref))
        if handle.status == "done":
            # 注册时任务已经结束：补发一次，和后端行为一致。
            if event == "finish" and handle.error is None:
                self._call(ref.module, ref.name, [handle.result])
            elif event == "fail" and handle.error is not None:
                self._call(ref.module, ref.name, [handle.error])

    def _report_progress(self, text) -> None:
        if self.current_task is not None:
            self._fire_event(self.current_task, "progress", text)

    def _cancel_task(self, handle) -> None:
        if isinstance(handle, TaskHandle):
            handle.cancelled = True
            handle.callbacks = []
            handle.result = None

    def _drain_tasks(self) -> None:
        while self.pending:
            handle = self.pending.pop(0)
            if handle.status == "pending" and not handle.cancelled:
                self._run_task(handle)

    def _call_ffi(self, frame: Frame, lib: str, method: str, args: list):
        """FFI：只允许显式 `导入 外部` 的库，且调用方必须声明 `External` 效应。"""
        if self.allowed_tools is not None and lib not in self.allowed_tools:
            raise VmError(
                RT_TOOLS,
                f"外部库 `{lib}` 不在运行时工具白名单里，不能调用 `{lib}.{method}`"
                f"（用 --allow-tools 放行）",
                frame.line, frame.fn["name"],
            )
        self._check_effects(frame, ["External"], f"外部库 `{lib}.{method}`")
        self._count_call(frame, f"外部库 `{lib}.{method}`")
        try:
            mod = importlib.import_module(lib)
        except Exception as e:
            raise VmError("AGL-RT-0070",
                          f"找不到外部库 `{lib}`：它需要先在环境里装好才能用",
                          frame.line, frame.fn["name"]) from e
        fn = getattr(mod, method, None)
        if not callable(fn):
            raise VmError("AGL-RT-0071",
                          f"外部库 `{lib}` 里没有 `{method}` 这个方法",
                          frame.line, frame.fn["name"])
        try:
            return fn(*args)
        except Exception as e:
            raise VmError("AGL-RT-0072",
                          f"调用外部库 `{lib}.{method}` 时出错：{_show(e, self.lang)}",
                          frame.line, frame.fn["name"]) from e

    def _call_ref(self, ref, *args):
        """`调用(fn_ref, args...)`：fn_ref 为函数引用或 `[函数名, 捕获1, …]`。"""
        if isinstance(ref, FnRef):
            module, name, captures = ref.module, ref.name, []
        elif isinstance(ref, list) and ref and isinstance(ref[0], str):
            module = self._active_module or self.root["module"]
            name, captures = ref[0], ref[1:]
        else:
            raise VmError(RT_GENERIC, "`调用` 要一个函数引用列表 `[函数名, 捕获1, …]`")
        return self._call(module, name, list(captures) + list(args))

    def _call(self, module: str, name: str, args: list):
        if self.depth > 200:
            raise VmError(RT_GENERIC, "调用层级过深（可能无限递归）")
        mod = self.modules.get(module)
        if mod is None:
            raise VmError(RT_GENERIC, f"模块 `{module}` 没有加载")
        fn = mod.funcs.get(name)
        if fn is None:
            raise VmError(RT_GENERIC, f"函数 `{name}` 不存在")
        frame = Frame(fn, module)
        for pname, value in zip(fn.get("params") or [], args):
            frame.locals[pname] = value
        self.depth += 1
        prev = getattr(self, "_active_module", None)
        self._active_module = module
        try:
            return self._execute(frame)
        finally:
            self._active_module = prev
            self.depth -= 1

    def _execute(self, frame: Frame):
        fn = frame.fn
        mod = self.modules[frame.module]
        code = fn["code"]
        lines = fn["lines"]
        while frame.pc < len(code):
            self.steps += 1
            if self.max_steps and self.steps > self.max_steps:
                raise VmError(
                    RT_LIMIT,
                    f"执行步数超过上限 {self.max_steps}（可能是死循环）",
                    frame.line, fn["name"],
                )
            # 每 1024 步查一次墙钟，避免为每次指令调用计时器；
            # 超时判断放在取指令前，错误行号取上一条已执行指令的行。
            if (self.deadline is not None and (self.steps & 1023) == 0
                    and time.monotonic() > self.deadline):
                line_no = lines[frame.pc] if frame.pc < len(lines) else 1
                raise VmError(
                    RT_TIMEOUT,
                    f"执行时间超过上限 {self.timeout_ms} 毫秒"
                    "（可能是死循环或外部调用阻塞）",
                    line_no, fn["name"],
                )
            ins = code[frame.pc]
            op = ins["op"]
            arg = ins.get("arg")
            frame.line = lines[frame.pc] if frame.pc < len(lines) else 1
            frame.pc += 1
            try:
                if op == "LOAD_CONST":
                    frame.stack.append(mod.consts[arg])
                elif op == "LOAD_FUNC":
                    frame.stack.append(FnRef(frame.module, mod.names[arg]))
                elif op == "LOAD_NAME":
                    name = mod.names[arg]
                    if name in frame.locals:
                        frame.stack.append(frame.locals[name])
                    elif name in mod.globals:
                        frame.stack.append(mod.globals[name])
                    else:
                        raise VmError(RT_GENERIC, f"名字 `{name}` 未定义", frame.line, fn["name"])
                elif op == "STORE_NAME":
                    frame.locals[mod.names[arg]] = frame.stack.pop()
                elif op in ("ADD", "SUB", "MUL", "DIV", "MOD"):
                    b = frame.stack.pop()
                    a = frame.stack.pop()
                    try:
                        if op == "ADD":
                            frame.stack.append(a + b)
                        elif op == "SUB":
                            frame.stack.append(a - b)
                        elif op == "MUL":
                            frame.stack.append(a * b)
                        elif op == "DIV":
                            frame.stack.append(a / b)
                        else:
                            frame.stack.append(a % b)
                    except ZeroDivisionError:
                        raise VmError(RT_DIV_ZERO, "不能除以零", frame.line, fn["name"])
                elif op == "NEG":
                    frame.stack.append(-frame.stack.pop())
                elif op in ("EQ", "NE", "GT", "LT", "GE", "LE"):
                    b = frame.stack.pop()
                    a = frame.stack.pop()
                    if op == "EQ":
                        result = _equal(a, b)
                    elif op == "NE":
                        result = not _equal(a, b)
                    elif op == "GT":
                        result = a > b
                    elif op == "LT":
                        result = a < b
                    elif op == "GE":
                        result = a >= b
                    else:
                        result = a <= b
                    frame.stack.append(result)
                elif op in ("AND", "OR"):
                    b = _truth(frame.stack.pop(), self.lang)
                    a = _truth(frame.stack.pop(), self.lang)
                    frame.stack.append(a and b if op == "AND" else a or b)
                elif op == "NOT":
                    frame.stack.append(not _truth(frame.stack.pop(), self.lang))
                elif op == "JUMP":
                    frame.pc = arg
                elif op == "JUMP_IF_FALSE":
                    if not _truth(frame.stack.pop(), self.lang):
                        frame.pc = arg
                elif op == "CALL_USER":
                    argc = arg["argc"]
                    args = [frame.stack.pop() for _ in range(argc)][::-1]
                    callee = mod.funcs[mod.names[arg["name"]]]
                    self._check_effects(frame, callee.get("effects"), f"函数 `{callee['name']}`")
                    frame.stack.append(self._call(frame.module, callee["name"], args))
                elif op == "CALL_MODULE":
                    argc = arg["argc"]
                    args = [frame.stack.pop() for _ in range(argc)][::-1]
                    target = mod.names[arg["module"]]
                    self._ensure(target)
                    callee = self.modules[target].funcs[mod.names[arg["name"]]]
                    self._check_effects(frame, callee.get("effects"), f"函数 `{callee['name']}`")
                    frame.stack.append(self._call(target, callee["name"], args))
                elif op == "CALL_FFI":
                    argc = arg["argc"]
                    args = [frame.stack.pop() for _ in range(argc)][::-1]
                    frame.stack.append(self._call_ffi(
                        frame, mod.names[arg["lib"]], mod.names[arg["name"]], args))
                elif op == "TASK":
                    argc = arg["argc"]
                    args = [frame.stack.pop() for _ in range(argc)][::-1]
                    kind = arg["kind"]
                    name = mod.names[arg["name"]]
                    target_module = frame.module
                    if kind == "module":
                        target_module = mod.names[arg["module"]]
                        self._ensure(target_module)
                        callee = self.modules[target_module].funcs[name]
                        need = callee.get("effects") or []
                    elif kind == "user":
                        callee = mod.funcs[name]
                        need = callee.get("effects") or []
                    else:
                        need = [_BUILTIN_EFFECT[name]] if name in _BUILTIN_EFFECT else []
                    self._check_effects(frame, need, f"任务 `{name}`")
                    handle = TaskHandle(kind, target_module, name, args,
                                        frame.line, fn["name"])
                    self.pending.append(handle)
                    frame.stack.append(handle)
                elif op == "AWAIT":
                    value = frame.stack.pop()
                    if isinstance(value, TaskHandle):
                        value = self._await_task(value)
                    frame.stack.append(value)
                elif op == "REGISTER_EVENT":
                    handle = frame.stack.pop()
                    ref = FnRef(frame.module, mod.names[arg["name"]])
                    self._register_event(handle, arg["event"], ref)
                elif op == "CALL_BUILTIN":
                    argc = arg["argc"]
                    args = [frame.stack.pop() for _ in range(argc)][::-1]
                    name = mod.names[arg["name"]]
                    need = _BUILTIN_EFFECT.get(name)
                    self._check_effects(frame, [need] if need else [], f"内建 `{name}`")
                    if need:
                        self._count_call(frame, f"内建 `{name}`")
                    fnb = self.builtins.get(name)
                    if fnb is None:
                        raise VmError(
                            RT_GENERIC,
                            f"该能力还不支持：VM 暂未实现内建 `{name}`"
                            "（可用 `aglc run --backend py` 兜底）",
                            frame.line, fn["name"])
                    try:
                        result = fnb(*args)
                    except VmError:
                        raise
                    except Exception as e:
                        raise VmError(RT_GENERIC,
                                      f"调用内建 `{name}` 时出错：{e}",
                                      frame.line, fn["name"]) from None
                    if isinstance(result, tuple) and len(result) == 2 and result[0] == "out":
                        self.out.append(result[1])
                        frame.stack.append(None)
                    else:
                        frame.stack.append(result)
                elif op == "RETURN":
                    return frame.stack.pop()
                elif op == "POP":
                    frame.stack.pop()
                elif op == "WITH_ENTER":
                    box = frame.stack.pop()
                    if not isinstance(box, ResultBox):
                        raise VmError(RT_GENERIC, "`使用` 需要结果对象", frame.line, fn["name"])
                    if not box.ok:
                        raise VmError(RT_GENERIC, _show(box.value, self.lang),
                                      frame.line, fn["name"])
                    frame.stack.append(box.value)
                elif op == "BUILD_LIST":
                    items = [frame.stack.pop() for _ in range(arg)][::-1]
                    frame.stack.append(items)
                elif op == "BUILD_MAP":
                    items = [frame.stack.pop() for _ in range(arg * 2)][::-1]
                    frame.stack.append({items[i]: items[i + 1] for i in range(0, len(items), 2)})
                elif op == "BUILD_SET":
                    items = [frame.stack.pop() for _ in range(arg)][::-1]
                    frame.stack.append(set(items))
                elif op == "INDEX":
                    idx = frame.stack.pop()
                    target = frame.stack.pop()
                    try:
                        frame.stack.append(target[idx])
                    except IndexError:
                        raise VmError(RT_INDEX, "列表取值越界", frame.line, fn["name"])
                    except KeyError:
                        raise VmError(RT_KEY, "映射表里没有这个键", frame.line, fn["name"])
                    except TypeError:
                        raise VmError(RT_GENERIC, "任务运行出错", frame.line, fn["name"])
                elif op == "STORE_INDEX":
                    value = frame.stack.pop()
                    idx = frame.stack.pop()
                    target = frame.stack.pop()
                    try:
                        target[idx] = value
                    except IndexError:
                        raise VmError(RT_INDEX, "列表赋值越界", frame.line, fn["name"])
                    except KeyError:
                        raise VmError(RT_KEY, "映射表里没有这个键", frame.line, fn["name"])
                    except TypeError:
                        raise VmError(RT_GENERIC, "这个值不支持下标赋值", frame.line, fn["name"])
                elif op == "MATCH_LIST":
                    # 列表模式只接受真列表，并且长度必须精确匹配；
                    # 非列表 / 长度不符都只是"这一臂不匹配"，不是运行时错误。
                    value = frame.stack.pop()
                    frame.stack.append(isinstance(value, list) and len(value) == arg)
                elif op == "LENGTH":
                    frame.stack.append(len(frame.stack.pop()))
                elif op == "STRUCT_NEW":
                    type_name = mod.names[arg["type"]]
                    field_names = [mod.names[i] for i in arg["fields"]]
                    values = [frame.stack.pop() for _ in range(arg["argc"])][::-1]
                    owner = self.modules.get(mod.names[arg["module"]]) if "module" in arg else mod
                    order = owner.types.get(type_name) if owner else None
                    if order is None:
                        raise VmError(RT_GENERIC, f"结构体 `{type_name}` 未注册", frame.line, fn["name"])
                    frame.stack.append(StructValue(
                        type_name, dict(zip(field_names, values)), order,
                        owner.name if owner else mod.name))
                elif op == "GET_FIELD":
                    field = mod.names[arg]
                    target = frame.stack.pop()
                    if isinstance(target, StructValue) and field in target.fields:
                        frame.stack.append(target.fields[field])
                    elif isinstance(target, dict) and field in target:
                        frame.stack.append(target[field])
                    else:
                        raise VmError(RT_KEY, f"没有字段 `{field}`", frame.line, fn["name"])
                elif op == "GET_ITER":
                    frame.stack.append(iter(frame.stack.pop()))
                elif op == "UNPACK":
                    value = frame.stack.pop()
                    try:
                        items = list(value)
                    except TypeError:
                        raise VmError(RT_GENERIC, "解构赋值需要一个可展开的值",
                                      frame.line, fn["name"])
                    if len(items) != arg:
                        raise VmError(
                            RT_GENERIC,
                            f"解构需要 {arg} 个值，实际给出 {len(items)} 个",
                            frame.line, fn["name"])
                    frame.stack.extend(items)
                elif op == "ITER_NEXT":
                    it = frame.stack[-1]
                    try:
                        frame.stack.append(next(it))
                    except StopIteration:
                        frame.stack.pop()
                        frame.pc = arg
                elif op == "POP_ITER":
                    frame.stack.pop()
                elif op == "TRY_BEGIN":
                    frame.handlers.append({
                        "target": arg["target"],
                        "depth": len(frame.stack),
                        "type": arg.get("type"),
                    })
                elif op == "TRY_END":
                    if not frame.handlers:
                        raise VmError(RT_GENERIC, "TRY_END 没有对应的 TRY_BEGIN", frame.line, fn["name"])
                    frame.handlers.pop()
                elif op == "RETHROW":
                    if not frame.stack:
                        raise VmError(RT_GENERIC, "RETHROW 时栈为空", frame.line, fn["name"])
                    exc = frame.stack.pop()
                    if isinstance(exc, VmError):
                        raise exc
                    raise VmError(RT_GENERIC, "RETHROW 只接受运行期错误值", frame.line, fn["name"])
                else:
                    raise VmError(RT_GENERIC, f"未知指令 `{op}`", frame.line, fn["name"])
            except VmError as e:
                handler = None
                while frame.handlers:
                    candidate = frame.handlers.pop()
                    ctype = candidate.get("type")
                    if not ctype or _catch_matches(ctype, e):
                        handler = candidate
                        break
                if handler is None:
                    raise
                del frame.stack[handler["depth"]:]
                frame.stack.append(e)
                frame.pc = handler["target"]
                continue
        return None


def run(agb: dict, lang: str = "zh", modules: dict | None = None,
        max_steps: int | None = None,
        allowed_effects: set[str] | None = None,
        max_calls: int | None = None,
        allowed_tools: set[str] | None = None,
        timeout_ms: int | None = None) -> str:
    return VM(agb, lang, modules, max_steps=max_steps,
              allowed_effects=allowed_effects, max_calls=max_calls,
              allowed_tools=allowed_tools, timeout_ms=timeout_ms).run()
