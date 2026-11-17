# -*- coding: utf-8 -*-
"""M5 后端 A：把 IR 转译成 Python。

这一层最重要的不是"能跑"，而是**出错了能指回 AGL 源码**。

做法：
  · 按 AGL 语句的 span.line 把生成的 Python 写到**同一物理行号**上，
    正常情况下第 N 行 AGL 就对应第 N 行 Python，无需查表；
  · 遇到"一行 AGL 展开成多行 Python"（如 否则 分支）时自动顺延，
    并把真实落点记进 source map；
  · source map 是 {python_line: agl_line}，运行期回译**只认它**。

阶段 1 明确的限制（写进文档，不藏着）：
  · 属性访问原样转译（`x.状态` → `x.状态`），由宿主对象自行保证成员存在；
  · 不生成类型注解；
  · `异步` 按同步处理，并已由 M4 告警。
"""
from __future__ import annotations

import json

from compiler.ir import _EFFECT_ZH
from compiler.lexer import KeywordTable

INDENT = "  "


def host_module_name(module: str) -> str:
    """AGL 模块名 → 生成的 Python 模块名。

    必须加前缀：AGL 的模块名是用户起的（英文分支里 `math`、`json`、`time`
    都是很自然的名字），直接落成 `import math` 会拿到宿主语言自带的同名模块，
    用户自己的模块被顶掉，运行时报"模块没有这个属性"。
    前缀是固定规则，不是按冲突临时决定 —— 否则同一份代码在不同
    Python 版本上跑出不同结果。
    """
    return f"_agl_{module}"


# IR 运算符 → Python 运算符
_BINOP_PY = {
    "&&": "and", "||": "or",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "==": "==", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<=",
    # 新增运算符
    "//": "//", "**": "**",
}

# 语言层面的只读属性名（中英两种写法都认）。
# 它们不是对象的真实成员，转译时要换成宿主语言的对应操作。
# 对应关系写在 spec/keywords.toml 的 [[attribute]] 里，这里不硬编码。

# 捕获 (错: 类型) 的异常类型名 → 宿主异常类（中英通用）
_EXC_ZH_TO_HOST = {
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

RUNTIME = '''
# ── 运行时支持（自动生成，勿手改）────────────────────────────────────
import asyncio
import contextvars
import json
import math
import random
import re
import sys

_AGL_ZH = __AGL_ZH__

# 运行时辅助函数用中文类型名做注解。Python 在 def 执行时就会求值注解，
# 因此必须先把这些类型名绑定到实际类型，否则产物一旦被直接执行或作为模块
# 导入，就会在定义第一个带注解的函数时抛 NameError（例如 `文本` 未定义）。
# 这些名字都是 AGL 关键字，不可能与用户标识符冲突。
文本 = str
整数 = int
浮数 = float
浮点数 = float
布尔 = bool
列表 = list
列表们 = list
映射表 = dict
集合 = set
结果 = object
错误 = Exception
任意 = object
任务 = object
空 = type(None)


def _agl_str_repr(s):
    """嵌套容器里的字符串按 Python repr 风格加单引号。

    顶层 `打印("abc")` 仍是裸 abc（AGL 人话显示），但列表/映射/集合里的
    字符串要带引号 —— 否则 `[a, b, , c]` 里的空串看不见，且无法与
    Python 的 `['a', 'b', '', 'c']` 逐字对拍。这里不用反斜杠转义写法，
    因为整个 RUNTIME 是三引号模板，反斜杠会被宿主解析吃掉。
    """
    bs = chr(92)
    q = chr(39)
    return q + s.replace(bs, bs + bs).replace(q, bs + q) + q


def _agl_exc_zh(exc):
    """宿主异常 → 一句中文说明。

    两处用它：
      · `当任务失败` 回调的「原因」参数 —— 之前直接传 str(exc)，
        接管失败的人拿到的是 `division by zero` 这种英文原文；
      · `打印(捕获变量)` —— `捕获 (错)` 绑定的是宿主异常对象，也要翻成中文。
    分类表与编译器侧 pipeline 的 `_EXC_MAP` 对齐；表上没有的异常，
    消息里已有中文就直接用，否则只给一句兜底的「任务运行出错」。
    """
    if isinstance(exc, ZeroDivisionError):
        return "不能除以零"
    if isinstance(exc, IndexError):
        return "列表取值越界"
    if isinstance(exc, KeyError):
        return "映射表里没有这个键"
    if isinstance(exc, FileNotFoundError):
        return "文件不存在"
    if isinstance(exc, PermissionError):
        return "没有权限访问"
    if isinstance(exc, AttributeError):
        return "这里读不到它的成员（值可能是空）"
    s = str(exc)
    if any("一" <= c <= "鿿" for c in s):
        return s
    # `取到(失败(…))` 抛的是 RuntimeError，消息就是失败文本本身。
    # 不能吞掉换成「任务运行出错」——否则 捕获 → 失败原因 会丢掉原原因。
    if isinstance(exc, RuntimeError) and s:
        return s
    return "任务运行出错" if _AGL_ZH else "task failed"


def _agl_err_message(v):
    """错误对象的 `.消息`：宿主异常取一句中文说明，其余值取显示文本。"""
    if isinstance(v, BaseException):
        return _agl_exc_zh(v)
    return str(v)


class _Result:
    __slots__ = ("ok", "value")

    def __init__(self, ok, value):
        self.ok = ok
        self.value = value

    def __repr__(self):
        return ("Ok(" if self.ok else "Err(") + repr(self.value) + ")"

    def __bool__(self):
        return self.ok

    # `使用 读文件(...) 作为 内容 { … }`：结果对象当上下文管理器用。
    # 成功进块拿到解包后的值；失败直接抛中文说明，不让块里拿着错误值继续跑。
    def __enter__(self):
        if not self.ok:
            raise ValueError(self.value if isinstance(self.value, str) else "结果失败")
        return self.value

    def __exit__(self, exc_type, exc, tb):
        v = self.value
        if hasattr(v, "close"):
            v.close()
        return False


def Ok(value):
    return _Result(True, value)


def Err(value):
    return _Result(False, value)


def _agl_struct_type(tname, fields):
    """结构体声明：造一个带 __slots__ 的小类，字段名保持 AGL 原文。

    用关键字参数构造（AGL 里构造结构体必须写 `人(名字: "甲")`），
    多一个字段、少一个字段都在构造时直接抛中文错误。
    """
    class _S:
        __slots__ = tuple(fields)

        def __init__(self, **kw):
            for f in fields:
                if f not in kw:
                    raise TypeError("结构体 " + tname + " 缺少字段：" + f)
            for k in kw:
                if k not in fields:
                    raise TypeError("结构体 " + tname + " 没有字段：" + k)
            for k, v in kw.items():
                setattr(self, k, v)

        def __repr__(self):
            body = ", ".join(f + "=" + _agl_show(getattr(self, f)) for f in fields)
            return tname + "(" + body + ")"

        def __eq__(self, other):
            # 结构体是数据记录：字段都相等就算相等（不按对象身份比）。
            # 否则 `等于` 在两个字段相同的结构体上静默给「假」，审计者会以为是 bug。
            if type(other) is not type(self):
                return NotImplemented
            return all(getattr(self, f) == getattr(other, f) for f in fields)

        def __ne__(self, other):
            eq = self.__eq__(other)
            if eq is NotImplemented:
                return NotImplemented
            return not eq

    _S.__name__ = "_agl_t_" + tname
    return _S


def _agl_show(value, _seen=None):
    """按当前分支的语言把值渲染成人话。

    这一步不是为了好看，是为了"普通人能看懂"：
    中文分支里 `打印(包含("甲乙", "乙"))` 应该出「真」，而不是 Python 的 True；
    Result 应该是「成功(42)」「失败(这不是一个整数：四十二)」。

    循环引用（`变量 m = {"甲": 1}; m["乙"] = m`）用 _seen 挡住：
    已渲染过的对象输出 `{…}` / `[…]`，绝不无限递归把程序弄崩。
    """
    if isinstance(value, bool):
        if _AGL_ZH:
            return "真" if value else "假"
        return "true" if value else "false"
    if value is None:
        return "空" if _AGL_ZH else "Null"
    if isinstance(value, BaseException):
        # `捕获 (错)` 绑定的就是宿主异常对象：给人看的必须是分类后的中文，
        # 不许把 `division by zero` 这种英文原文漏出去。
        return _agl_exc_zh(value)
    if isinstance(value, _Result):
        if _AGL_ZH:
            head = "成功" if value.ok else "失败"
        else:
            head = "Ok" if value.ok else "Err"
        return f"{head}({_agl_show(value.value, _seen)})"
    if isinstance(value, list):
        if _seen is None:
            _seen = set()
        oid = id(value)
        if oid in _seen:
            return "[…]"
        _seen.add(oid)
        out = "[" + ", ".join(
            _agl_str_repr(v) if isinstance(v, str) else _agl_show(v, _seen)
            for v in value) + "]"
        _seen.discard(oid)
        return out
    if isinstance(value, dict):
        if _seen is None:
            _seen = set()
        oid = id(value)
        if oid in _seen:
            return "{…}"
        _seen.add(oid)
        out = "{" + ", ".join(
            f"{_agl_str_repr(k) if isinstance(k, str) else _agl_show(k, _seen)}: "
            f"{_agl_str_repr(v) if isinstance(v, str) else _agl_show(v, _seen)}"
            for k, v in value.items()) + "}"
        _seen.discard(oid)
        return out
    if isinstance(value, set):
        # 集合渲染成 `{1, 2}`（与 Python 的 set repr 同构，但走本分支语言的字面量
        # 显示——集合元素不能是可变类型，不存在循环引用问题，无需 _seen）。
        return "{" + ", ".join(
            _agl_str_repr(v) if isinstance(v, str) else _agl_show(v, _seen)
            for v in sorted(value, key=_agl_show)) + "}"
    return str(value)


def _agl_print(*values):
    print(*[_agl_show(v) for v in values])


def _agl_print_no_newline(*values):
    print(*[_agl_show(v) for v in values], end="")


def _agl_memoryview(data):
    # v0.48：内存视图——直接透传 Python 的 memoryview
    return memoryview(data.encode("utf-8") if isinstance(data, str) else data)


def _agl_getattr(obj, name, default=None):
    # v0.48：动态取属性——直接用 Python 的 getattr
    return getattr(obj, name, default)


def _agl_hasattr(obj, name):
    # v0.48：判断有没有属性——直接用 Python 的 hasattr
    return hasattr(obj, name)


def _agl_setattr(obj, name, value):
    # v0.48：动态设属性——直接用 Python 的 setattr
    setattr(obj, name, value)


def _agl_delattr(obj, name):
    # v0.48：动态删属性——直接用 Python 的 delattr
    delattr(obj, name)


def _agl_type_name(obj):
    # v0.48：取对象的类名
    return type(obj).__name__


def _agl_sqrt(x):
    # v0.50：平方根
    import math
    return math.sqrt(x)


def _agl_read_text(path):
    # v0.51：读文本文件
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _agl_write_text(path, content):
    # v0.51：写文本文件
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _agl_append_text(path, content):
    # v0.51：追加写文本文件
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)


def _agl_to_json(obj):
    # v0.51：把对象转成 JSON 文本
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _agl_list_dir(path):
    # v0.51：列出目录下的文件和文件夹
    import os
    return os.listdir(path)


def _agl_exists(path):
    # v0.51：判断路径是否存在
    import os
    return os.path.exists(path)


def _agl_sleep(秒):
    # v0.51：睡眠延迟
    import time
    time.sleep(秒)


def _agl_args():
    # v0.51：获取命令行参数（去掉程序名本身）
    import sys
    return sys.argv[1:]


def _agl_delete(path):
    # v0.51：删除文件
    import os
    os.remove(path)


def _agl_mkdir(path):
    # v0.51：创建文件夹
    import os
    os.makedirs(path, exist_ok=True)


def _agl_now():
    # v0.51：获取当前时间戳（秒）
    import time
    return time.time()


def _agl_regex_match(模式, 文本):
    # v0.51：正则匹配
    import re
    m = re.search(模式, 文本)
    if m:
        return m.group(0)
    return ""


def _agl_输入(提示=""):
    # v0.51：命令行读一行输入
    return input(提示)


def _agl_路径拼接(*部分):
    # v0.51：拼接路径
    import os
    return os.path.join(*部分)


# ── v0.51 一次性补全的常用内建 ──────────────────────────────────────
def _agl_最大值(*数们):
    return max(数们)


def _agl_最小值(*数们):
    return min(数们)


def _agl_求和(数们):
    return sum(数们)


def _agl_绝对值(x):
    return abs(x)


def _agl_四舍五入(x, 位数=0):
    return round(x, 位数)


def _agl_重命名(旧路径, 新路径):
    import os
    os.rename(旧路径, 新路径)


def _agl_文件大小(路径):
    import os
    return os.path.getsize(路径)


def _agl_是文件夹(路径):
    import os
    return os.path.isdir(路径)


def _agl_是文件(路径):
    import os
    return os.path.isfile(路径)


# ── v0.51 继续补：类型转换/日期时间/递归遍历 ─────────────────────────
def _agl_转文本(x):
    # 数字转字符串
    return str(x)


def _agl_转整数(s):
    # 字符串转整数
    return int(s)


def _agl_转浮数(s):
    # 字符串转浮点数
    return float(s)


def _agl_现在时间():
    # 获取格式化的当前时间字符串
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _agl_遍历目录(路径):
    # 递归遍历目录，返回所有文件路径
    import os
    结果 = []
    for 根, 目录们, 文件们 in os.walk(路径):
        for 文件 in 文件们:
            结果.append(os.path.join(根, 文件))
    return 结果


# ── v0.51 一次性补完的核心常用内建 ─────────────────────────────────
def _agl_范围(起, 止=None, 步长=1):
    # range：生成数字序列
    if 止 is None:
        起, 止 = 0, 起
    return list(range(起, 止, 步长))


def _agl_排序(列表, 反转=False):
    # sorted：返回新排序列表
    return sorted(列表, reverse=反转)


def _agl_枚举(列表):
    # enumerate：带下标的遍历
    return list(enumerate(列表))


def _agl_配对(*列表们):
    # zip：把多个列表按位置配对
    return list(zip(*列表们))


def _agl_反转(列表):
    # reversed：反转列表
    return list(reversed(列表))


def _agl_获取网页(url):
    # v0.51：获取网页文本内容；只允许 http/https，避免 file:// 读本机文件。
    import urllib.request
    from urllib.parse import urlsplit
    地址 = str(url)
    协议 = urlsplit(地址).scheme.lower()
    if 协议 not in ("http", "https"):
        raise ValueError("获取网页只允许 http/https 地址" if _AGL_ZH
                         else "fetch only accepts http/https URLs")
    req = urllib.request.Request(地址, headers={"User-Agent": "AGL/0.5"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _agl_退出(码=0):
    # v0.51：退出程序
    import sys
    sys.exit(码)


def _agl_当前目录():
    # v0.51：获取当前工作目录
    import os
    return os.getcwd()


def _agl_环境变量(名, 默认=""):
    # v0.51：读环境变量。凭证类变量默认拒绝，必须在宿主侧显式点名放行
    # （AGL_ENV_ALLOW_SECRETS=NAME 或 all），避免一行 AGL 就把密钥读走。
    import os
    name = str(名)
    blocked = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
               "PRIVATE_KEY", "API_KEY", "ACCESS_KEY", "AUTH", "SESSION", "COOKIE")
    upper = name.upper()
    if any(k in upper for k in blocked):
        allow = os.environ.get("AGL_ENV_ALLOW_SECRETS", "")
        allowed = {x.strip() for x in allow.split(",") if x.strip()}
        if "all" not in allowed and name not in allowed:
            raise PermissionError(
                f"读取环境变量 `{name}` 被拒绝：名称像凭证；"
                f"确需读取时由宿主设置 AGL_ENV_ALLOW_SECRETS={name}")
    return os.environ.get(name, 默认)


def _agl_MD5(文本):
    # v0.51：计算文本的MD5哈希
    import hashlib
    return hashlib.md5(文本.encode("utf-8")).hexdigest()


def _agl_SHA256(文本):
    # v0.51：计算文本的SHA256哈希
    import hashlib
    return hashlib.sha256(文本.encode("utf-8")).hexdigest()


# ── v0.52：SQLite数据库基础能力 ─────────────────────────────────
def _agl_连接值(连接):
    # 打开数据库返回结果；执行函数同时接受「结果」和已取到的连接对象，
    # 这样 `取到(...)` / `如果` 两种写法都能对上同一份规格。
    if isinstance(连接, _Result):
        if not 连接.ok:
            raise RuntimeError(f"数据库连接失败：{连接.value}")
        return 连接.value
    if hasattr(连接, "ok") and hasattr(连接, "value"):
        if not 连接.ok:
            raise RuntimeError(f"数据库连接失败：{连接.value}")
        return 连接.value
    return 连接


def _agl_打开数据库(路径):
    # 打开SQLite数据库，返回 Result<连接>
    import sqlite3
    try:
        return _Result(True, sqlite3.connect(路径))
    except Exception as 错误:
        return _Result(False, str(错误))


def _agl_执行SQL(连接, sql):
    # 执行写操作SQL（增删改），自动提交
    连接 = _agl_连接值(连接)
    游标 = 连接.cursor()
    游标.execute(sql)
    连接.commit()
    return 游标.rowcount


def _agl_查询SQL(连接, sql):
    # 执行查询SQL，返回所有结果列表
    连接 = _agl_连接值(连接)
    游标 = 连接.cursor()
    游标.execute(sql)
    return 游标.fetchall()


def _agl_执行SQL带参(连接, sql, 参数们):
    # 参数化执行写操作，防注入
    连接 = _agl_连接值(连接)
    游标 = 连接.cursor()
    游标.execute(sql, 参数们)
    连接.commit()
    return 游标.rowcount


def _agl_查询SQL带参(连接, sql, 参数们):
    # 参数化查询，防注入
    连接 = _agl_连接值(连接)
    游标 = 连接.cursor()
    游标.execute(sql, 参数们)
    return 游标.fetchall()


def _agl_关闭数据库(连接):
    # 关闭数据库连接
    _agl_连接值(连接).close()


def _agl_启动HTTP(端口, 返回文本):
    # 常驻简单HTTP服务：所有 GET 请求返回同一段文本。
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class _处理器(BaseHTTPRequestHandler):
        def do_GET(self):
            数据 = str(返回文本).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(数据)))
            self.end_headers()
            self.wfile.write(数据)

        def log_message(self, *args):
            pass

    服务器 = HTTPServer(("127.0.0.1", int(端口)), _处理器)
    print(f"HTTP服务已启动：http://localhost:{端口}")
    服务器.serve_forever()


def _agl_启动网页应用(端口, 处理函数名, 绑定地址="127.0.0.1"):
    # 可编程网页服务：每个请求交给同模块中指定名称的处理函数。
    # 默认只监听回环地址；需要局域网访问时显式传 "0.0.0.0"。
    from compiler.webapp import serve_web_app

    def _调用处理函数(请求):
        函数 = globals().get(str(处理函数名))
        if not callable(函数):
            return {
                "状态": 500,
                "类型": "text/plain; charset=utf-8",
                "内容": f"找不到处理函数：{处理函数名}",
            }
        return 函数(请求)

    serve_web_app(int(端口), _调用处理函数, "zh" if __AGL_ZH__ else "en",
                  host=str(绑定地址))


def _agl_启动待办HTTP(端口, db路径):
    # 完整待办Web服务：支持列表展示、新增待办、标记完成
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import html as _html
    import sqlite3
    import urllib.parse
    
    连接 = sqlite3.connect(db路径)
    连接.execute("CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY AUTOINCREMENT, 内容 TEXT NOT NULL, 完成 INTEGER DEFAULT 0)")
    连接.commit()
    连接.close()
    
    class _处理器(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            conn = sqlite3.connect(db路径)
            cur = conn.cursor()
            cur.execute("SELECT id, 内容, 完成 FROM todo ORDER BY id DESC")
            rows = cur.fetchall()
            conn.close()
            html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>我的待办</title>
<style>
body { font-family: 微软雅黑; max-width: 800px; margin: 50px auto; padding: 0 20px; background: #f5f5f5; }
h1 { color: #333; text-align: center; }
.add-form { display: flex; gap: 10px; margin: 20px 0; }
.add-form input { flex: 1; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; }
.add-form button { padding: 12px 24px; background: #4CAF50; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; }
.todo-item { padding: 14px; background: white; border-radius: 8px; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
.done { color: #999; text-decoration: line-through; }
.badge { background: #4CAF50; color: white; padding: 3px 10px; border-radius: 12px; font-size: 12px; }
a { color: #2196F3; text-decoration: none; margin-left: auto; }
</style></head><body>
<h1>📝 我的待办列表</h1>
<form class="add-form" method="POST" action="/add">
<input type="text" name="内容" placeholder="输入新待办..." required>
<button type="submit">添加</button>
</form>
"""
            for r in rows:
                状态 = 'class="done"' if r[2] == 1 else ''
                徽章 = '<span class="badge">已完成</span>' if r[2] == 1 else ''
                html += f'<div class="todo-item">{徽章}<span {状态}>{_html.escape(str(r[1]))}</span>'
                if r[2] == 0:
                    html += f'<a href="/完成/{r[0]}">标记完成</a>'
                html += '</div>'
            html += "</body></html>"
            self.wfile.write(html.encode("utf-8"))

        def do_POST(self):
            if self.path == "/add":
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode("utf-8")
                params = urllib.parse.parse_qs(body)
                内容 = params.get("内容", [""])[0]
                if 内容:
                    conn = sqlite3.connect(db路径)
                    conn.execute("INSERT INTO todo (内容) VALUES (?)", (内容,))
                    conn.commit()
                    conn.close()
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, *args):
            pass
    
    服务器 = HTTPServer(("127.0.0.1", 端口), _处理器)
    print(f"✅ 待办Web服务已启动：http://localhost:{端口}")
    服务器.serve_forever()


# ── v0.52 复杂数据处理能力 ─────────────────────────────────
def _agl_随机整数(最小, 最大):
    # 生成[最小, 最大]之间的随机整数
    import random
    return random.randint(最小, 最大)


def _agl_随机选(列表):
    # 从列表里随机选一个元素
    import random
    return random.choice(列表)


def _agl_当前时间():
    # 返回格式化的当前时间字符串：YYYY-MM-DD HH:MM:SS
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _agl_分割字符串(原文本: 文本, 分隔符: 文本) -> 列表:
    # 按分隔符把字符串切成列表
    return 原文本.split(分隔符)


def _agl_连接字符串(分隔符: 文本, 元素列表: 列表) -> 文本:
    # 用分隔符把列表里的元素拼成字符串
    return 分隔符.join([str(x) for x in 元素列表])


def _agl_排序列表(原列表: 列表) -> 列表:
    # 返回排序后的新列表
    return sorted(原列表)


def _agl_幂运算(底数: 整数, 指数: 整数) -> 整数:
    return pow(底数, 指数)


def _agl_转大写(原文本: 文本) -> 文本:
    return 原文本.upper()


def _agl_转小写(原文本: 文本) -> 文本:
    return 原文本.lower()


def _agl_去首尾空格(原文本: 文本) -> 文本:
    return 原文本.strip()


def _agl_替换字符串(原文本: 文本, 旧内容: 文本, 新内容: 文本) -> 文本:
    return 原文本.replace(旧内容, 新内容)


def _agl_睡眠(秒数: 浮点数):
    # 延迟指定秒数
    import time
    time.sleep(秒数)
    return None


def _agl_读CSV(路径):
    # 读CSV文件，返回二维列表
    import csv
    结果 = []
    with open(路径, "r", encoding="utf-8") as f:
        读 = csv.reader(f)
        for 行 in 读:
            结果.append(行)
    return 结果


def _agl_写CSV(路径, 数据):
    # 写二维列表到CSV文件
    import csv
    with open(路径, "w", encoding="utf-8", newline="") as f:
        写 = csv.writer(f)
        for 行 in 数据:
            写.writerow(行)


def _agl_正则匹配(模式, 文本):
    # 正则匹配，返回所有匹配到的子串
    import re
    return re.findall(模式, 文本)


def _agl_格式化(模板, 参数们):
    # 字符串模板格式化，类似"你好{name}".format(name="张三")
    return 模板.format(*参数们)


def _agl_过滤(列表, 判断函数):
    # 过滤列表，保留满足条件的元素
    return [x for x in 列表 if 判断函数(x)]


def _agl_映射(列表, 转换函数):
    # 对列表每个元素做转换，返回新列表
    return [转换函数(x) for x in 列表]


def _agl_log(x):
    # v0.50：自然对数
    import math
    return math.log(x)


def _agl_sin(x):
    # v0.50：正弦
    import math
    return math.sin(x)


def _agl_cos(x):
    # v0.50：余弦
    import math
    return math.cos(x)


def _agl_exp(x):
    # v0.50：指数
    import math
    return math.exp(x)


def _agl_tan(x):
    # v0.50：正切
    import math
    return math.tan(x)


def _agl_asin(x):
    # v0.50：反正弦
    import math
    return math.asin(x)


def _agl_acos(x):
    # v0.50：反余弦
    import math
    return math.acos(x)


def _agl_atan(x):
    # v0.50：反正切
    import math
    return math.atan(x)


def _agl_radians(x):
    # v0.50：度转弧度
    import math
    return math.radians(x)


def _agl_degrees(x):
    # v0.50：弧度转度
    import math
    return math.degrees(x)


def _agl_sinh(x):
    # v0.50：双曲正弦
    import math
    return math.sinh(x)


def _agl_cosh(x):
    # v0.50：双曲余弦
    import math
    return math.cosh(x)


def _agl_tanh(x):
    # v0.50：双曲正切
    import math
    return math.tanh(x)


def _agl_pi():
    # v0.50：圆周率
    import math
    return math.pi


def _agl_sign(x):
    # v0.50：取符号
    if x > 0:
        return 1
    elif x < 0:
        return -1
    else:
        return 0


def _agl_callable(obj):
    # v0.48：判断对象是不是可调用的
    return callable(obj)


def _agl_create_class(name, bases, attrs):
    # v0.48：动态创建类
    if isinstance(bases, (list, tuple)):
        base_classes = tuple(bases)
    else:
        base_classes = (bases,) if bases else ()
    attrs_dict = dict(attrs) if attrs else {}
    return type(name, base_classes, attrs_dict)


def _agl_vars(obj):
    # v0.48：取对象的属性字典
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    else:
        return {}


def _agl_issubclass(cls, parent):
    # v0.48：判断是不是子类——直接用 Python 的 issubclass
    return issubclass(cls, parent)


def _agl_type(name_or_obj, bases=None, attrs=None):
    # v0.48：动态创建类 / 取对象类型
    if bases is None and attrs is None:
        # 只有一个参数——取对象的类型
        return type(name_or_obj).__name__
    else:
        # 三个参数——动态创建类
        name = name_or_obj
        if isinstance(bases, (list, tuple)):
            base_classes = tuple(bases)
        else:
            base_classes = (bases,) if bases else ()
        attrs_dict = dict(attrs) if attrs else {}
        return type(name, base_classes, attrs_dict)


# Python 类型名（str/int/...）→ AGL 类型词；isinstance 第二参按词查类型
_AGL_TYPE_WORDS = {
    "int": int, "integer": int, "整数": int,
    "float": float, "floatnumber": float, "浮数": float,
    "str": str, "string": str, "文本": str,
    "bool": bool, "boolean": bool, "布尔": bool,
    "list": list, "列表": list,
    "dict": dict, "map": dict, "映射表": dict,
    "set": set, "集合": set,
    "null": type(None), "none": type(None), "空": type(None),
}


def _agl_isinstance(value, types):
    if isinstance(types, list):
        for t in types:
            cls = _AGL_TYPE_WORDS.get(str(t).lower())
            if cls is not None and isinstance(value, cls):
                return True
        return False
    cls = _AGL_TYPE_WORDS.get(str(types).lower())
    if cls is None:
        raise ValueError("isinstance 不认识这个类型词：" + str(types))
    return isinstance(value, cls)


def _agl_assert(cond, msg="断言失败"):
    if not cond:
        raise AssertionError(str(msg) if msg else "断言失败")


def _agl_del_key(mapping, key):
    newmap = dict(mapping)
    if key in newmap:
        del newmap[key]
    return newmap


def _agl_ceil(x):
    return math.ceil(x)


def _agl_floor(x):
    return math.floor(x)


def _agl_choice(xs):
    return random.choice(xs)


def _agl_seed(n):
    random.seed(n)
    return None


def _agl_max_default(xs, default):
    return max(xs) if xs else default


def _agl_min_default(xs, default):
    return min(xs) if xs else default


# ── 异步任务运行时 ─────────────────────────────────────────────────────
# 两条硬原则：
#   1. **任务只在自己跑完时触发回调，取消后一个都不再触发**；
#   2. **回调是任务自身的一部分** —— 任务结束前先 `gather` 收尾所有派生的回调任务，
#      这样「任务完成 == 回调已跑完」，主入口 `等待 句柄` 拿到的一定是处理完毕的结果，
#      不会被 asyncio.run 在退出时把还没排上队的孤儿回调取消掉。
class _AglTask:
    __slots__ = ("fut", "handlers", "cancelled", "result", "exc",
                 "callbacks", "inner", "started", "has_fail")

    def __init__(self, fut):
        self.fut = fut
        self.handlers = {"progress": [], "finish": [], "fail": []}
        self.cancelled = False
        self.result = None
        self.exc = None
        # 本任务派生的回调任务（进度 / 完成 / 失败）。统一收尾，杜绝孤儿任务。
        self.callbacks = []
        # 是否有人注册了 `当任务失败`：没有的话，任务内部的异常要重新抛出去，
        # 让行号照常回译到 AGL 源码，而不是被静默吞掉（越界一律报错，不静默）。
        self.has_fail = False

    def __repr__(self):
        return "<任务>" if _AGL_ZH else "<task>"


_current_task = contextvars.ContextVar("agl_task", default=None)

# 任务结束后才「迟到注册」的回调会走这条名单：它们是在任务收尾之后
# 才被补发的，任务自己的 _agl_drain 永远收不到它们。裸 ensure_future
# 不登记 = 回调里出错就是「退出码 0、stderr 甩一屏 never retrieved」。
# 统一由 _agl_main_wrap 在主入口收尾前 gather，异常照主链路抛出回译。
_LATE = []


async def _agl_wait(value):
    """`等待 x`：x 是任务句柄就等它跑完（含它的回调都跑完），是协程就直接等。

    任务已被 `取消` 时，fut 处于取消态，`await` 会抛 CancelledError；这里接住它，
    返回任务的既有结果（取消时恒为 空），让 `等待 句柄` 优雅落地而不是把整个程序带崩。
    """
    if isinstance(value, _AglTask):
        try:
            return await value.fut
        except asyncio.CancelledError:
            return value.result
    return await value


def _agl_fire_into(task, event, value):
    """把某事件的全部回调排成任务并记录；真正跑完由任务统一收尾（gather）。"""
    for fn in list(task.handlers.get(event) or []):
        if task.cancelled:
            return
        task.callbacks.append(asyncio.ensure_future(fn(value)))


async def _agl_drain(task):
    """跑完派发出去的回调，回调里出了错就地抛出（按注册顺序，取第一个）。

    gather 的 return_exceptions 只是不让它因为一个回调出错就把其余的都掐掉，
    收集到的异常不能顺手丢掉 —— 丢了就等于「越界不报错」，
    而且错误再也指不回源码的那一行。
    """
    pending = task.callbacks
    task.callbacks = []
    if not pending:
        return
    results = await asyncio.gather(*pending, return_exceptions=True)
    for r in results:
        # 取消是"取消后不再触发任何回调"语义的一部分，不是错误，不参与抛
        if isinstance(r, asyncio.CancelledError):
            continue
        if isinstance(r, BaseException):
            raise r


async def _agl_settle(task):
    if task.cancelled or task.fut.cancelled():
        return
    # 先把之前派发的进度回调都跑完，再触发完成 / 失败（它们也会被记录进 callbacks）。
    await _agl_drain(task)
    if task.exc is None:
        _agl_fire_into(task, "finish", task.result)
    else:
        if task.has_fail:
            # 有人接管了失败：把错误说明交给 `当任务失败` 回调，不再外抛。
            # 说明用分类后的中文（与无人接管时人看到的「不能除以零」一致），
            # 而不是 str(task.exc) 甩一句 `division by zero`。
            _agl_fire_into(task, "fail", _agl_exc_zh(task.exc))
        else:
            # 没人接管：把任务内部的原异常原样重新抛出，让行号回译照常工作。
            # 静默吞掉等于「越界不报错」，违反语言硬原则。
            raise task.exc
    await _agl_drain(task)


def _agl_task(value):
    task = _AglTask(None)
    # 保留内部协程引用：当任务在尚未真正启动前就被 `取消` 时，
    # 需要显式 close 它，否则会留下 "coroutine never awaited" 的告警。
    task.inner = value
    task.started = False

    async def runner():
        task.started = True
        _current_task.set(task)
        try:
            task.result = await task.inner
            task.exc = None
        except asyncio.CancelledError:
            # 被 `取消` 主动取消：清掉回调后安静收场，不再触发任何回调。
            task.cancelled = True
            task.handlers = {"progress": [], "finish": [], "fail": []}
            return
        except Exception as e:
            task.exc = e
            task.result = None
        await _agl_settle(task)
        # 把任务结果透传出去：`等待 句柄` 拿到的是处理完毕的结果，
        # 而不是 None（否则 `等待 甲` 会拿到「空」）。
        return task.result

    task.fut = asyncio.ensure_future(runner())
    return task


def _agl_on(handle, event, fn):
    if not isinstance(handle, _AglTask) or handle.cancelled:
        return
    if event == "fail":
        handle.has_fail = True
    if handle.fut.done() and not handle.fut.cancelled():
        # 注册时任务已经结束：补发一次，别让注册方永远等不到。
        # 补发的 future 必须收进 _LATE：裸 ensure_future 不登记，
        # 回调里一旦出错就是「退出码 0 + stderr 甩 never retrieved traceback」。
        if event == "finish" and handle.exc is None:
            _LATE.append(asyncio.ensure_future(fn(handle.result)))
        elif event == "fail" and handle.exc is not None:
            _LATE.append(asyncio.ensure_future(fn(_agl_exc_zh(handle.exc))))
        return
    handle.handlers.setdefault(event, []).append(fn)


async def _agl_main_wrap(coro):
    """包住主入口：它跑完后，把迟到注册的回调统一跑完、有照抛。

    与 _agl_drain 同一条纪律：收集到的回调异常不能顺手丢掉 ——
    丢了就等于「出错不报错」，而且再也指不回源码的那一行。
    注意：finally 里绝不能写 return —— 那会把主入口正在传播的异常吞掉。
    """
    try:
        return await coro
    finally:
        pending = [t for t in _LATE if not t.done()]
        del _LATE[:]
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            for r in results:
                # 取消不是错误，不参与抛（与 _agl_drain 一致）
                if isinstance(r, asyncio.CancelledError):
                    continue
                if isinstance(r, BaseException):
                    raise r


def _agl_cancel(handle):
    if not isinstance(handle, _AglTask):
        return
    # 先断掉回调再取消：顺序反了会有一次事件在取消之后漏出来
    handle.cancelled = True
    handle.handlers = {"progress": [], "finish": [], "fail": []}
    if not handle.fut.done():
        # 任务还没真正跑起来就被取消：内部协程还没被 await，显式 close 避免泄漏告警
        if not getattr(handle, "started", False) and getattr(handle, "inner", None) is not None:
            inner = handle.inner
            handle.inner = None
            try:
                inner.close()
            except Exception:
                pass
        handle.fut.cancel()


async def _agl_sleep(ms):
    await asyncio.sleep(ms / 1000.0)


def _agl_report_progress(text):
    task = _current_task.get()
    if task is not None:
        _agl_fire_into(task, "progress", text)


# ── FFI 黑盒调用 ───────────────────────────────────────────────────────
# 外部 Python 库的一切都在宿主侧发生，编译器看不见它的签名与内部行为。
# 所以这里只做三件事：
#   1. 懒导入：库没装就在调用那一刻报「找不到外部库」，而不是让整个程序起不来；
#   2. 把外部抛出的异常包成 AGL 自己的异常 —— 报错时给 AGL 的行号和中文原因，
#      不把宿主的 traceback 甩给人；
#   3. 方法不存在要说清楚是「哪个库的哪个方法」。
class _AglFfiMissing(Exception):
    """外部库没装 / 导入不了。"""

    def __init__(self, lib):
        self.lib = lib
        super().__init__(lib)


class _AglFfiNoMember(Exception):
    """外部库里没有这个方法。"""

    def __init__(self, lib, method):
        self.lib = lib
        self.method = method
        super().__init__(f"{lib}.{method}")


class _AglFfiCallError(Exception):
    """外部库调用时抛了异常。"""

    def __init__(self, lib, method, reason, orig=None):
        self.lib = lib
        self.method = method
        self.reason = reason
        # 原始异常留一份：给人看的消息用中文，排查时还能看到宿主那边真正的类型。
        self.orig = orig
        super().__init__(reason)


def _agl_ffi_call(lib, method, *args):
    """调用外部库的某个方法（黑盒：签名不检查，异常翻译成 AGL 说法）。"""
    import importlib
    try:
        mod = importlib.import_module(lib)
    except ImportError:
        raise _AglFfiMissing(lib) from None
    fn = getattr(mod, method, None)
    if fn is None or not callable(fn):
        raise _AglFfiNoMember(lib, method)
    try:
        return fn(*args)
    except Exception as e:
        raise _AglFfiCallError(lib, method, str(e), orig=e) from None


_AGL_TOOL_BREAKER = {"fingerprints": {}, "tools": {}, "blocked_fp": {}, "blocked_tool": {}}
_AGL_TOOL_FAIL_LIMIT = 3


def _agl_tool_fingerprint(name, arguments):
    """同一工具 + 规范化实参的指纹；键顺序和空白不影响指纹。"""
    import hashlib as _hashlib
    import json as _json
    raw = _json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=repr)
    return _hashlib.sha256((name + "\\n" + raw).encode("utf-8")).hexdigest()[:12]


def _agl_tool_blocked_reason(name, fp):
    state = _AGL_TOOL_BREAKER
    if name in state["blocked_tool"]:
        return (f"工具 `{name}` 已熔断：本任务内累计失败达到 {_AGL_TOOL_FAIL_LIMIT} 次；"
                "换工具、改参数，或走任务代码里写好的降级路径")
    if fp in state["blocked_fp"]:
        return (f"工具调用熔断：`{name}` 的同一组实参已失败过；"
                "换工具、改参数，或走任务代码里写好的降级路径")
    return None


def _agl_tool_note_failure(name, fp):
    """记一次失败，返回给调用方看的补充说明。"""
    state = _AGL_TOOL_BREAKER
    count = state["fingerprints"].get(fp, 0) + 1
    state["fingerprints"][fp] = count
    state["blocked_fp"][fp] = True
    total = state["tools"].get(name, 0) + 1
    state["tools"][name] = total
    if total >= _AGL_TOOL_FAIL_LIMIT:
        state["blocked_tool"][name] = True
        return f"同一工具已累计失败 {total} 次，达到熔断阈值"
    return f"同一工具已累计失败 {total} 次"


def _agl_tool_note_success(name, fp):
    state = _AGL_TOOL_BREAKER
    state["fingerprints"].pop(fp, None)
    state["blocked_fp"].pop(fp, None)
    state["tools"].pop(name, None)
    state["blocked_tool"].pop(name, None)


def _agl_call_tool(name, arguments, config):
    """调用声明的工具。目前支持 MCP Streamable HTTP（JSON-RPC over POST）。"""
    import json as _json
    import time as _time
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    if not isinstance(name, str) or not name.strip():
        raise ValueError("工具名必须是非空文本")
    if not isinstance(arguments, dict):
        raise TypeError("工具实参必须是映射表")
    if not isinstance(config, dict):
        config = {}
    protocol = config.get("protocol") or config.get("协议")
    endpoint = config.get("endpoint") or config.get("地址")
    timeout = config.get("timeout_sec") or config.get("超时秒数") or 30
    retries = config.get("max_retry")
    if retries is None:
        retries = config.get("最大重试次数")
    if retries is None:
        retries = 2
    if protocol != "mcp":
        raise ValueError("工具运行时目前只支持 `协议 = 'mcp'`；"
                         f"收到的是 `{protocol}`")
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        raise ValueError("MCP 工具的 `地址` 必须是 http:// 或 https:// 开头的网址")
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 30
    try:
        retries = max(0, int(retries))
    except Exception:
        retries = 0

    fp = _agl_tool_fingerprint(name, arguments)
    blocked = _agl_tool_blocked_reason(name, fp)
    if blocked:
        raise RuntimeError(blocked)

    def rpc(method, params, session=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            payload["id"] = 1
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        req = _urlreq.Request(endpoint, data=_json.dumps(payload).encode("utf-8"),
                              headers=headers, method="POST")
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
        if notify:
            return None, sid
        if not raw:
            raise RuntimeError(f"MCP 服务器对 `{method}` 返回了空响应")
        text = raw.decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            data = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    data.append(line[5:].strip())
            text = data[-1] if data else ""
        try:
            obj = _json.loads(text)
        except Exception:
            raise RuntimeError(f"MCP 服务器返回的不是 JSON：{text[:200]}")
        if obj.get("error"):
            err = obj["error"]
            raise RuntimeError(f"MCP 工具 `{name}` 出错：{err.get('message', err)}")
        return obj.get("result") or {}, sid

    def call_once():
        init, sid = rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agl"},
        })
        rpc("notifications/initialized", {}, sid, notify=True)
        result, _ = rpc("tools/call", {"name": name, "arguments": arguments}, sid)
        if result.get("isError"):
            detail = []
            for c in result.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    detail.append(c.get("text") or "")
            tail = ("：" + "；".join(x for x in detail if x)) if detail else ""
            raise RuntimeError(f"工具 `{name}` 返回失败结果{tail}")
        content = result.get("content") or []
        texts = [c.get("text") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\\n".join(texts) if texts else _json.dumps(result, ensure_ascii=False)

    last = None
    for attempt in range(retries + 1):
        try:
            out = call_once()
            _agl_tool_note_success(name, fp)
            return out
        except _urlerr.HTTPError as e:
            # 5xx / 429 可能是瞬断，按最大重试次数退避；4xx 是确定性失败，不重试。
            transient = 500 <= e.code < 600 or e.code == 429
            last = e
            if transient and attempt < retries:
                _time.sleep(min(2 ** attempt, 5))
                continue
            note = _agl_tool_note_failure(name, fp)
            raise RuntimeError(f"工具 `{name}` 调用失败：HTTP {e.code}；{note}") from None
        except (_urlerr.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                _time.sleep(min(2 ** attempt, 5))
                continue
            note = _agl_tool_note_failure(name, fp)
            raise RuntimeError(f"连接 MCP 工具 `{name}` 失败：{e}；{note}") from None
        except Exception as e:
            note = _agl_tool_note_failure(name, fp)
            raise RuntimeError(f"工具 `{name}` 调用失败：{e}；{note}") from None
    note = _agl_tool_note_failure(name, fp)
    raise RuntimeError(f"连接 MCP 工具 `{name}` 失败：{last}；{note}")


# ── 内建函数的宿主实现 ─────────────────────────────────────────────────
# 名字与签名由 spec/keywords.toml 的 [[builtin]] 表定义，这里的函数只是它的落地。
# 两条纪律：
#   1. 越界一律报错，不静默截断 —— 静默截断是"看起来对"里最坏的一种；
#   2. 会失败的转换返回 Err，不抛异常 —— 让调用方必须显式处理。
def _agl_brief(v, limit=40):
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


def _agl_slice(value, start, stop, step=1):
    # Python 原生切片语义：负索引、步长（负步长反向）、越界自动裁剪全与 Python 一致
    return value[start:stop:step]


def _agl_pop(xs, i=None):
    """`xs.pop()` / `xs.pop(i)` → [值, 新列表]；`d.pop(k)` → [值, 新映射]。

    按实参类型自动分发（v0.31 泛型化）：列表下标缺省取尾部、支持负数下标、
    越界抛 IndexError；映射表必须带键，键缺失抛 KeyError。值语义：原容器不动。
    """
    if isinstance(xs, list):
        if i is None:
            i = len(xs) - 1
        try:
            v = xs[i]
        except IndexError:
            raise IndexError(
                "列表取值越界" if _AGL_ZH else "list index out of range")
        return [v, xs[:i] + xs[i + 1:]]
    if isinstance(xs, dict):
        if i is None:
            raise TypeError(
                "映射表的弹出必须带键：`弹出(d, k)`"
                if _AGL_ZH else "dict.pop() needs a key")
        if i not in xs:
            raise KeyError(
                "映射表里没有这个键" if _AGL_ZH else "key not in mapping")
        v = xs[i]
        return [v, {kk: vv for kk, vv in xs.items() if kk != i}]
    raise TypeError(
        "弹出 要一个列表或映射表" if _AGL_ZH else "pop expects a list or dict")


def _agl_clear(x):
    """`x.clear()` → 按类型返回空容器：列表 → []、映射表 → 建映射()、集合 → 建集合()。"""
    if isinstance(x, list):
        return []
    if isinstance(x, dict):
        return {}
    if isinstance(x, set):
        return set()
    raise TypeError(
        "清空 要一个列表、映射表或集合"
        if _AGL_ZH else "clear expects a list, dict or set")


def _agl_format_value(x, spec):
    """`format(x, spec)` 原生透传：千分位/科学计数/符号/自定义填充/`=` 对齐等。"""
    if not isinstance(spec, str):
        raise TypeError(
            "按格式 的格式说明要文本" if _AGL_ZH else "format spec must be a string")
    try:
        return format(x, spec)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"格式说明 `{spec}` 不合法：{exc}"
            if _AGL_ZH else f"invalid format spec: {exc}") from exc


def _agl_repr(x):
    """`repr(x)`：文本带引号、列表带方括号（f-string `!r`）。"""
    return repr(x)


def _agl_remove(xs, x):
    """`xs.remove(x)` → 移除第一个等于 x 的元素，返回新列表；没有则抛（Python 语义）。"""
    if not isinstance(xs, list):
        raise TypeError("移除元素 要一个列表" if _AGL_ZH else "remove expects a list")
    try:
        i = xs.index(x)
    except ValueError:
        raise ValueError(
            "列表里没有这个元素" if _AGL_ZH else "element not in list")
    return xs[:i] + xs[i + 1:]


def _agl_insert(xs, i, x):
    """`xs.insert(i, x)` → 插入后返回新列表；越界按 Python 裁剪。"""
    if not isinstance(xs, list):
        raise TypeError("插入 要一个列表" if _AGL_ZH else "insert expects a list")
    return xs[:i] + [x] + xs[i:]


def _agl_index(xs, x):
    """`xs.index(x)` / `s.index(x)` → 第一个等于 x 的下标；找不到给失败结果（Python 抛 ValueError）。"""
    if isinstance(xs, str):
        try:
            return Ok(xs.index(str(x)))
        except ValueError:
            return Err("文本里没有这个内容" if _AGL_ZH else "substring not found")
    if isinstance(xs, list):
        try:
            return Ok(xs.index(x))
        except ValueError:
            return Err("列表里没有这个元素" if _AGL_ZH else "element not in list")
    raise TypeError("元素位置 要一个文本或列表" if _AGL_ZH
                    else "index expects a string or list")


_MISSING = object()


def _agl_pop_key(d, k, default=_MISSING):
    """`d.pop(k)` / `d.pop(k, 默认)` → [值, 新映射]。

    键不存在且无默认 → 失败结果（对齐 Python KeyError，用 取到 消费抛成
    可捕获的中文错误）；有默认 → Ok([默认, 原映射])。值语义不修改原映射。
    """
    if not isinstance(d, dict):
        raise TypeError("弹出键 要一个映射表" if _AGL_ZH else "pop_key expects a dict")
    if k in d:
        v = d[k]
        return [v, {kk: vv for kk, vv in d.items() if kk != k}]
    if default is _MISSING:
        return Err("映射表里没有这个键" if _AGL_ZH else "key not in mapping")
    return [default, {kk: vv for kk, vv in d.items()}]


def _agl_round(x, ndigits=None):
    return round(x) if ndigits is None else round(x, int(ndigits))


def _agl_pow(x, y, mod=None):
    return pow(x, y) if mod is None else pow(x, y, int(mod))


def _agl_divmod(a, b):
    return [a // b, a % b]


def _agl_ord(c):
    return ord(str(c))


def _agl_chr(n):
    return chr(int(n))


def _agl_all(xs):
    return all(xs)


def _agl_any(xs):
    return any(xs)


def _agl_items(mapping):
    return [[k, v] for k, v in mapping.items()]


def _agl_values(mapping):
    return list(mapping.values())


def _agl_hex(n):
    return hex(int(n))


def _agl_oct(n):
    return oct(int(n))


def _agl_bin(n):
    return bin(int(n))


def _agl_split(text, sep, maxsplit=-1):
    s = str(text)
    t = str(sep)
    if t == "":
        return list(s)
    if maxsplit is None or maxsplit < 0:
        return s.split(t)
    return s.split(t, int(maxsplit))


def _agl_splitlines(text):
    # Python str.splitlines()：CRLF / CR / LF 都算换行，行尾换行符不保留。
    # splitlines() 与 split(换行) 在末尾换行/混合换行符上有差异，这里直接用原生。
    return str(text).splitlines()


def _agl_partition(text, sep):
    # Python str.partition(sep)：切成 [前, 分隔符, 后]；找不到分隔符给
    # (原文, "", "")——与 split 不同，分隔符本身保留在中间。
    s = str(text)
    t = str(sep)
    if t == "":
        raise ValueError("分隔符不能是空文本" if _AGL_ZH
                         else "separator must not be empty")
    head, found, tail = s.partition(t)
    return [head, found, tail]


def _agl_rpartition(text, sep):
    # Python str.rpartition(sep)：从右往左切 [前, 分隔符, 后]；找不到给
    # ("", "", 原文)——注意与 partition 的占位方向相反。
    s = str(text)
    t = str(sep)
    if t == "":
        raise ValueError("分隔符不能是空文本" if _AGL_ZH
                         else "separator must not be empty")
    head, found, tail = s.rpartition(t)
    return [head, found, tail]


def _agl_casefold(text):
    return str(text).casefold()


def _agl_expandtabs(text, size=8):
    return str(text).expandtabs(int(size))


def _agl_join(*parts):
    return "".join(str(p) for p in parts)


def _agl_replace(text, old, new, count=-1):
    o = str(old)
    if o == "":
        raise ValueError("替换的「原文」不能是空文本" if _AGL_ZH
                         else "the text to replace must not be empty")
    if count is None or count < 0:
        return str(text).replace(o, str(new))
    return str(text).replace(o, str(new), int(count))


def _agl_html_escape(text):
    # HTML 文本节点 / 属性里必须转义的五个字符。
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def _agl_url_encode(text):
    # URL 组件编码：空格编码为 %20，斜杠不保留。
    from urllib.parse import quote
    return quote(str(text), safe="")


def _agl_url_decode(text):
    # URL 百分比解码；`+` 留给表单解析，不在这里当空格。
    from urllib.parse import unquote
    return unquote(str(text))


def _agl_strip(text, chars=None):
    return str(text).strip(chars)


def _agl_upper(text):
    return str(text).upper()


def _agl_lower(text):
    return str(text).lower()


def _agl_contains(text, sub):
    # 文本看子串；列表 / 映射表 / 元组（元组导入时已转成列表）/ 集合看成员。
    # 映射表按键判断，和 Python 的 `k in d` 一致。
    if isinstance(text, (list, dict, tuple, set, frozenset)):
        return sub in text
    return str(sub) in str(text)


def _agl_count(container, target):
    # 文本 → 子串计数（Python s.count(sub)）；列表 / 元组 → 元素计数；
    # 映射表 → 键计数；集合 → 0/1。其他类型给 TypeError（Python 同样报错）。
    if isinstance(container, str):
        return container.count(str(target))
    if isinstance(container, (list, tuple)):
        return container.count(target)
    if isinstance(container, dict):
        return 1 if target in container else 0
    if isinstance(container, (set, frozenset)):
        return 1 if target in container else 0
    raise TypeError(
        "计数 只支持 文本 / 列表 / 映射表 / 集合"
        if _AGL_ZH
        else "count supports String / List / Map / Set")


def _agl_range_list(*args):
    return list(range(*args))


def _agl_repeat(text, n):
    k = int(n)
    if isinstance(text, list):
        return text * k
    if isinstance(text, str):
        return text * k
    raise TypeError("重复 要一个文本或列表" if _AGL_ZH
                    else "repeat expects a string or list")


def _agl_rfind(text, target):
    return str(text).rfind(str(target))


def _agl_setdefault(d, k, v=None):
    if not isinstance(d, dict):
        raise TypeError("默认键 的第一个实参要一个映射表" if _AGL_ZH
                        else "setdefault expects a mapping")
    return d.setdefault(k, v)


def _agl_floordiv(a, b):
    return int(a) // int(b)


def _agl_fromkeys(ks, v=None):
    return dict.fromkeys(ks, v)


def _agl_title(text):
    return str(text).title()


def _agl_zfill(value, width):
    return str(int(value)).zfill(int(width))


def _agl_lstrip(text, chars=None):
    return str(text).lstrip(chars)


def _agl_rstrip(text, chars=None):
    return str(text).rstrip(chars)


def _agl_ljust(text, width, fill=" "):
    return str(text).ljust(int(width), str(fill)[:1])


def _agl_rjust(text, width, fill=" "):
    return str(text).rjust(int(width), str(fill)[:1])


def _agl_center(text, width, fill=" "):
    return str(text).center(int(width), str(fill)[:1])


def _agl_removeprefix(text, prefix):
    return str(text).removeprefix(str(prefix))


def _agl_removesuffix(text, suffix):
    return str(text).removesuffix(str(suffix))


def _agl_swapcase(text):
    return str(text).swapcase()


def _agl_capitalize(text):
    return str(text).capitalize()


def _agl_islower(text):
    return str(text).islower()


def _agl_isupper(text):
    return str(text).isupper()


def _agl_isspace(text):
    return str(text).isspace()


def _agl_istitle(text):
    return str(text).istitle()


def _agl_isdecimal(text):
    return str(text).isdecimal()


def _agl_isnumeric(text):
    return str(text).isnumeric()


def _agl_to_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, list, dict, set, tuple)):
        return len(value) > 0
    raise TypeError("转布尔 只支持 布尔/数字/文本/列表/映射表/集合")


def _agl_to_list(value):
    if isinstance(value, str):
        return list(value)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError("转列表 只支持 文本/列表/映射表/集合")


def _agl_keys(mapping):
    return list(mapping.keys())


def _agl_make_map(pairs):
    out = {}
    for p in pairs:
        out[p[0]] = p[1]
    return out


def _agl_to_text(value):
    # 与打印同一条渲染管线：真/假/空/成功(x)/失败(x)/[1, 2]/{键: 1} 都按当前分支
    # 的语言显示，而不是把宿主的 None/True/Err(...) 漏给用户。
    return _agl_show(value)


def _agl_unwrap(value):
    if isinstance(value, _Result):
        if value.ok:
            return value.value
        # 抛的原因就是失败文本本身，不包框架文案 —— 否则「捕获 → 失败原因 →
        # 重抛」路径会把「取到(失败(…))：拿不到值，原因是「…」」层层累积。
        raise RuntimeError(str(value.value))
    raise TypeError(
        "取到 要一个「结果」，这里给的不是结果"
        if _AGL_ZH
        else "unwrap expects a Result")


def _agl_fail_reason(value):
    if isinstance(value, _Result):
        return "" if value.ok else str(value.value)
    if isinstance(value, BaseException):
        # 捕获变量（`捕获 (错)`）绑定的就是宿主异常对象：取它的原因
        # 必须是分类后的中文，不许把英文原文漏出去 —— 与 _agl_show 同口径。
        return _agl_exc_zh(value)
    raise TypeError(
        "失败原因 要一个「结果」，这里给的不是结果"
        if _AGL_ZH
        else "fail_reason expects a Result")


def _agl_call_fn(fn_ref, *args):
    """动态调用函数引用：fn_ref = [函数名, 捕获1, 捕获2, ...]。"""
    if not isinstance(fn_ref, list) or len(fn_ref) < 1:
        raise RuntimeError(
            "调用 要一个函数引用列表 `[函数名, 捕获1, ...]`"
            if _AGL_ZH
            else "call_fn expects a function reference list")
    fn_name = fn_ref[0]
    captures = fn_ref[1:]
    # 从调用者的全局命名空间里找函数（生成的代码是被 exec 到一个命名空间里的，
    # 不是作为模块运行的，所以不能用 sys.modules[__name__]）
    import sys
    caller_globals = sys._getframe(1).f_globals
    fn = caller_globals.get(fn_name)
    if fn is None or not callable(fn):
        raise RuntimeError(
            f"调用 找不到函数 `{fn_name}`"
            if _AGL_ZH
            else f"call_fn: function `{fn_name}` not found")
    return fn(*captures, *args)


def _agl_format_number(value, digits, *extra):
    out = f"{float(value):.{int(digits)}f}"
    if extra:
        width = int(extra[0])
        if width <= 0:
            raise ValueError("格式化数字的宽度要是正数" if _AGL_ZH
                             else "format_number width must be positive")
        pad_zero = bool(extra[1]) if len(extra) > 1 else False
        # 补零用 zfill：Python 的 `f"{x:05.2f}"` 正是这种语义（负数时零补在符号后）
        out = out.zfill(width) if pad_zero else out.rjust(width)
    return out


def _agl_trunc(value):
    if isinstance(value, bool):
        raise TypeError("取整要一个浮数，不是真假值" if _AGL_ZH
                        else "trunc expects a number, not a boolean")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TypeError("取整要一个浮数" if _AGL_ZH else "trunc expects a number")


def _agl_append(items, value):
    return list(items) + [value]


def _agl_sublist(items, start, stop):
    seq = list(items)
    n = len(seq)
    for v in (start, stop):
        if not isinstance(v, int):
            raise TypeError("子列表的位置要用整数" if _AGL_ZH
                            else "sublist positions must be integers")
    if start < 0 or stop < 0 or start > n or stop > n or start > stop:
        if _AGL_ZH:
            raise IndexError(
                f"取子列表越界：这个列表只有 {n} 项，却要取第 {start} 到第 {stop} 项")
        raise IndexError(
            f"sublist out of range: list has {n} items, asked for {start}..{stop}")
    return seq[start:stop]


def _agl_to_int(text, base=None):
    try:
        if isinstance(text, (int, float)) and base is None:
            # 数字直接截断转整（与 Python int(3.7) == 3 一致；bool 是 int 子类）
            return Ok(int(text))
        s = str(text).strip()
        if base is None:
            return Ok(int(s))
        return Ok(int(s, base))
    except (TypeError, ValueError):
        head = "这不是一个整数：" if _AGL_ZH else "not an integer: "
        return Err(head + _agl_brief(text))


def _agl_to_float(text):
    try:
        return Ok(float(str(text).strip()))
    except (TypeError, ValueError):
        head = "这不是一个数：" if _AGL_ZH else "not a number: "
        return Err(head + _agl_brief(text))


def _agl_abs(value):
    return abs(float(value))


def _agl_min(*args):
    # 单参数且是列表：聚合（空列表报「列表取值越界」，与 stdlib 设计一致）；
    # 多参数：取较小的那个。
    if len(args) == 1 and isinstance(args[0], list):
        if not args[0]:
            raise IndexError("min of empty list")
        return min(args[0])
    return min(args)


def _agl_max(*args):
    if len(args) == 1 and isinstance(args[0], list):
        if not args[0]:
            raise IndexError("max of empty list")
        return max(args[0])
    return max(args)


def _agl_fill(tpl, *args):
    # `{}` 顺序占位；值用 AGL 自己的显示（与打印一致），不是宿主 repr。
    parts = str(tpl).split("{}")
    out = [parts[0]]
    for i, piece in enumerate(parts[1:]):
        if i < len(args):
            out.append(_agl_show(args[i]))
        out.append(piece)
    return "".join(out)


def _agl_randint(lo, hi):
    return random.randint(lo, hi)


def _agl_matches(text, pattern):
    try:
        return re.search(pattern, str(text)) is not None
    except re.error:
        raise ValueError(
            "正则表达式不合法" if _AGL_ZH else "invalid regex") from None


def _agl_regex_sub(text, pattern, repl):
    try:
        return re.sub(pattern, str(repl), str(text))
    except re.error:
        raise ValueError(
            "正则表达式不合法" if _AGL_ZH else "invalid regex") from None


def _agl_sum(items):
    try:
        return sum(items)
    except TypeError:
        raise TypeError("合计要一个全是数的列表" if _AGL_ZH
                        else "sum expects a list of numbers")


def _agl_strftime(fmt):
    # 与 Python 的 time.strftime 同源：用宿主的 datetime 按格式串渲染当前时间
    import datetime
    return datetime.datetime.now().strftime(fmt)


def _agl_merge_map(*maps):
    # 与 Python 的 `{**a, **b}` 一致：键冲突时后者覆盖。
    # 静态类型追不到的路径（实参来自返回「任意」的函数）在这里兜底：
    # 不是映射表就抛一句中文，让回译链路把它变成指向 AGL 行号的诊断，
    # 而不是甩宿主的 TypeError 原文。
    out = {}
    for i, m in enumerate(maps, 1):
        if not isinstance(m, dict):
            raise TypeError(
                f"合并映射的第 {i} 个参数要映射表" if _AGL_ZH
                else f"merge_map: argument {i} must be a mapping")
        out.update(m)
    return out


def _agl_join_list(sep, xs):
    # 与 Python 的 `sep.join(xs)` 一致：元素必须是文本，否则 TypeError
    return sep.join(xs)


def _agl_sorted(xs):
    # 与 Python 的 `sorted(xs)` 一致：返回升序新列表，不动原列表
    return sorted(xs)


def _agl_sort_by(xs, keyf):
    # 与 Python 的 `sorted(xs, key=f)` 一致：键函数就是产物里同名的 Python 函数
    return sorted(xs, key=keyf)


def _agl_reverse(xs):
    # 与 Python 的 `xs[::-1]` 一致：返回倒序新列表，不动原列表
    return xs[::-1]


def _agl_is_same(a, b):
    # 与 Python 的 `a is b` 一致：比的是不是同一个对象（引用同一性）
    return a is b


def _agl_is_not_same(a, b):
    # 与 Python 的 `a is not b` 一致
    return a is not b


def _agl_read_file(path):
    # 按 UTF-8 读整个文本文件；出错不抛异常，以失败结果回报（中文原因）。
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Ok(f.read())
    except OSError as exc:
        return Err(_agl_exc_zh(exc))
    except UnicodeDecodeError:
        return Err("文件不是 UTF-8 文本" if _AGL_ZH else "file is not UTF-8 text")


def _agl_write_file(path, content):
    # 按 UTF-8 覆盖写；出错不抛异常，以失败结果回报（中文原因）。
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return Ok(None)
    except OSError as exc:
        return Err(_agl_exc_zh(exc))


def _agl_append_file(path, content):
    # 按 UTF-8 追加写；出错不抛异常，以失败结果回报（中文原因）。
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return Ok(None)
    except OSError as exc:
        return Err(_agl_exc_zh(exc))


# P8-2：状态快照。把模块全局变量表编码成可逆 JSON；第一层只收
# 可序列化的值（基本类型 / 列表 / 映射表 / 集合 / 结果对象），
# 函数、模块、结构体实例明确拒绝，绝不静默丢字段。
def _agl_state_encode(value):
    if isinstance(value, _Result):
        return {"__agl_type__": "result", "ok": value.ok,
                "value": _agl_state_encode(value.value)}
    if isinstance(value, (set, frozenset)):
        return {"__agl_type__": "set",
                "items": [_agl_state_encode(x) for x in sorted(value, key=repr)]}
    if isinstance(value, dict):
        return {"__agl_type__": "map",
                "items": [[_agl_state_encode(k), _agl_state_encode(v)]
                          for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [_agl_state_encode(x) for x in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("快照不支持这种值：" + type(value).__name__)


def _agl_state_decode(value):
    if isinstance(value, list):
        return [_agl_state_decode(x) for x in value]
    if not isinstance(value, dict) or "__agl_type__" not in value:
        return value
    kind = value.get("__agl_type__")
    if kind == "result":
        return Ok(_agl_state_decode(value.get("value"))) if value.get("ok") else Err(
            _agl_state_decode(value.get("value")))
    if kind == "set":
        return set(_agl_state_decode(x) for x in (value.get("items") or []))
    if kind == "map":
        return {_agl_state_decode(k): _agl_state_decode(v)
                for k, v in (value.get("items") or [])}
    raise ValueError("快照里有无法识别的值类型：" + str(kind))


def _agl_state_saveable(name, value):
    import types
    # 下划线开头的是运行时内部名（_AGL_* / _agl_* / _current_task 等），
    # 不能进快照，否则会把 ContextVar、哨兵对象甚至内建表本身写进去。
    if name.startswith("_") or name == "_AGL_ZH":
        return False
    if isinstance(value, types.ModuleType) or callable(value):
        return False
    return True


def _agl_save_state(path):
    # 只保存当前模块命名空间里的变量；函数/模块/宿主对象不落盘。
    try:
        frame = sys._getframe(1)
        table = {k: _agl_state_encode(v)
                 for k, v in frame.f_globals.items()
                 if _agl_state_saveable(k, v)}
        data = {"format": "agl.state", "version": 1,
                "lang": "zh" if _AGL_ZH else "en", "globals": table}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        return Ok(None)
    except OSError as exc:
        return Err(_agl_exc_zh(exc))
    except (TypeError, ValueError) as exc:
        return Err(str(exc) if str(exc) else "快照编码失败")


def _agl_load_state(path):
    # 恢复当前模块命名空间里的变量；不认识的类型整份拒绝，不做半恢复。
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("format") != "agl.state":
            return Err("不是 AGL 状态快照")
        table = data.get("globals")
        if not isinstance(table, dict):
            return Err("状态快照缺少 globals")
        frame = sys._getframe(1)
        restored = 0
        for name, encoded in table.items():
            if name.startswith("_"):
                continue
            if name in frame.f_globals and (
                    callable(frame.f_globals[name])
                    or type(frame.f_globals[name]).__name__ == "module"):
                continue
            frame.f_globals[name] = _agl_state_decode(encoded)
            restored += 1
        return Ok(restored)
    except OSError as exc:
        return Err(_agl_exc_zh(exc))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return Err("状态快照解析失败：" + str(exc))


def _agl_input(prompt=None):
    # 打印提示后读一行用户输入（去掉结尾换行）；读到文件尾给空文本。
    # 提示语缺省时与 Python `input()` 一致，不打印任何提示。
    try:
        line = input(prompt) if prompt is not None else input()
        return str(line)
    except EOFError:
        return ""


def _agl_isdigit(text):
    return str(text).isdigit()


def _agl_isalpha(text):
    return str(text).isalpha()


def _agl_find(text, target):
    return str(text).find(str(target))


def _agl_startswith(text, prefix):
    if isinstance(prefix, (tuple, list)):
        return str(text).startswith(tuple(prefix))
    return str(text).startswith(str(prefix))


def _agl_endswith(text, suffix):
    if isinstance(suffix, (tuple, list)):
        return str(text).endswith(tuple(suffix))
    return str(text).endswith(str(suffix))


def _agl_get(mapping, key, default=None):
    # 默认值缺省时返回 空：与 Python `d.get(k)` 一致
    if default is None:
        return mapping.get(key)
    return mapping.get(key, default)


# ── 集合 ──────────────────────────────────────────────────────────────
# AGL 集合 = 宿主 set。字面量 `{1, 2}`、显示走 _agl_show（排序保证确定性）；
# 内建都返回「新集合」，不修改原集合（与 `追加` 对列表的约定一致）。
def _agl_make_set(*items):
    # 参数里若混入列表则展开成元素（`建集合(xs)` 与 `set(xs)` 对齐）：
    # 单个列表参数是「从列表建集合」，多个参数是「把每个参数当元素」。
    if len(items) == 1 and isinstance(items[0], list):
        return set(items[0])
    return set(items)


def _agl_add(s, x):
    if not isinstance(s, set):
        raise TypeError(
            "添加 的第一个参数必须是集合"
            if _AGL_ZH else "add expects a set as its first argument")
    return set(s) | {x}


def _agl_union(*sets):
    if not all(isinstance(s, set) for s in sets):
        raise TypeError(
            "并集 的参数必须都是集合"
            if _AGL_ZH else "union expects set arguments")
    out: set = set()
    for s in sets:
        out |= s
    return out


def _agl_intersection(*sets):
    if not all(isinstance(s, set) for s in sets):
        raise TypeError(
            "交集 的参数必须都是集合"
            if _AGL_ZH else "intersection expects set arguments")
    if not sets:
        return set()
    out = set(sets[0])
    for s in sets[1:]:
        out &= s
    return out


def _agl_difference(*sets):
    if not all(isinstance(s, set) for s in sets):
        raise TypeError(
            "差集 的参数必须都是集合"
            if _AGL_ZH else "difference expects set arguments")
    if not sets:
        return set()
    out = set(sets[0])
    for s in sets[1:]:
        out -= s
    return out


def _agl_symmetric_difference(*sets):
    if not all(isinstance(s, set) for s in sets):
        raise TypeError(
            "对称差 的参数必须都是集合"
            if _AGL_ZH else "symmetric_difference expects set arguments")
    if not sets:
        return set()
    out = set(sets[0])
    for s in sets[1:]:
        out ^= s
    return out


def _agl_is_subset(first, second):
    if not isinstance(first, set) or not isinstance(second, set):
        raise TypeError(
            "是子集 的参数必须都是集合"
            if _AGL_ZH else "is_subset expects set arguments")
    return first <= second


def _agl_is_superset(first, second):
    if not isinstance(first, set) or not isinstance(second, set):
        raise TypeError(
            "是超集 的参数必须都是集合"
            if _AGL_ZH else "is_superset expects set arguments")
    return first >= second


# ── JSON ──────────────────────────────────────────────────────────────
# 解析 / 生成都走「结果」表达失败（中文原因），与 AGL 的失败约定一致；
# 中文原样保留（ensure_ascii=False），双分支输出一致。
def _agl_parse_json(text):
    try:
        return Ok(json.loads(text))
    except (ValueError, TypeError):
        # json.loads 解析失败抛 ValueError（含 JSONDecodeError 子类）——
        # 直接给用户能看懂的原因，不落「任务运行出错」兜底
        return Err("JSON 格式不合法" if _AGL_ZH else "malformed JSON")


def _agl_dump_json(value):
    try:
        return Ok(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        return Err(_agl_exc_zh(exc))


# ── 网络（效应 Net）──────────────────────────────────────────────────
# 零依赖：只用标准库 urllib.request。出错 / 非 2xx 都以失败结果回报。
def _agl_fetch(url):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            status = getattr(resp, "status", 200)
            if not (200 <= status < 300):
                return Err(
                    f"HTTP 状态码 {status}" if _AGL_ZH
                    else f"HTTP status {status}")
            body = resp.read()
            return Ok(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        # 网络层失败（连不上 / 超时 / DNS 解析失败……）给用户能看懂的原因，
        # 不落「任务运行出错」兜底
        return Err("网络请求失败" if _AGL_ZH else "network request failed")
# ──────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────
'''


class PyEmitter:
    def __init__(self, source_lines: list[str]):
        self.src_lines = source_lines
        self.out: list[str] = []
        self.smap: dict[int, int] = {}   # python_line(1-based) -> agl_line(1-based)

    # ── 行写入 ────────────────────────────────────────────────────────
    def put(self, agl_line: int, code: str) -> int:
        """把 code 尽量写到 agl_line 那一行；被占用则顺延。返回实际行号。"""
        idx = max(0, agl_line - 1)
        while len(self.out) < idx:
            self.out.append("")
        if idx < len(self.out) and not self.out[idx].strip():
            self.out[idx] = code
            self.smap[idx + 1] = agl_line
            return idx + 1
        if idx >= len(self.out):
            self.out.append(code)
            self.smap[len(self.out)] = agl_line
            return len(self.out)
        # 顺延：向后找空行
        j = idx
        while j < len(self.out):
            if not self.out[j].strip():
                self.out[j] = code
                self.smap[j + 1] = agl_line
                return j + 1
            j += 1
        self.out.append(code)
        self.smap[len(self.out)] = agl_line
        return len(self.out)

    def blank_to(self, agl_line: int) -> None:
        while len(self.out) < agl_line:
            self.out.append("")

    def out_len(self) -> int:
        return len(self.out)

    def code(self, lang: str = "zh") -> str:
        self.blank_to(len(self.src_lines))
        body = "\n".join(self.out).rstrip() + "\n"
        zh = "True" if lang == "zh" else "False"
        return body + RUNTIME.replace("__AGL_ZH__", zh)


class PyGenerator:
    def __init__(self, ir: dict, source_lines: list[str]):
        self.ir = ir
        self.em = PyEmitter(source_lines)
        self.depth = 0
        # 攒着待付 Sally 的"补注语句"：回调注册本来要独占一行，会把后面所有
        # 语句的行号挤走。这里先记下来，等到下一个简单语句时用 `;` 挂在同一行，
        # 一个物理行都不多占。详见 stmt() 里的消费逻辑。
        self.pending: list[str] = []
        self.tmp = 0
        mod = ir["program"]["modules"][0]
        # 本模块导入的模块名：引用它们时要换成宿主侧的模块名
        imps = mod.get("imports") or []
        # 模块别名：`导入 工具 作为 T`，后端把 `T` 也当模块名解析到真实模块。
        self.aliases: dict[str, str] = {
            imp["alias"]: imp["module"] for imp in imps if imp.get("alias")
        }
        self.imported: set[str] = {imp["module"] for imp in imps} | set(self.aliases)
        # 本模块声明过的用户名字（参数 / 变量 / 循环变量）：用户完全可以把
        # 变量叫成 `json`、`time` 这种导入模块名。此时名字优先是变量，
        # 不能换成 `_agl_json` —— 换了读取侧就对不上声明侧，运行期 NameError。
        self.user_vars: set[str] = set()
        for fn in mod["functions"]:
            self.user_vars |= self._scan_user_vars(fn)
        # 自定义结构体：构造时发射 `_agl_t_<名字>(…)`，类在声明行注册。
        self.structs: dict[str, dict] = {
            t["name"]: t for t in (mod.get("types") or [])
        }

    def ind(self) -> str:
        return INDENT * self.depth

    # ── 用户变量扫描 ──────────────────────────────────────────────────
    def _scan_user_vars(self, fn: dict) -> set[str]:
        """收集一个函数里声明的所有名字：参数、变量、循环变量、赋值目标。

        供 expr 的 name 分支做「变量名优先于导入模块名」的判定。
        """
        out: set[str] = set()
        for p in fn.get("params") or []:
            out.add(p["name"])
        stack = [fn.get("body") or []]
        while stack:
            stmts = stack.pop()
            for s in stmts:
                k = s.get("stmt")
                if k == "let":
                    out.add(s["name"])
                elif k == "let_multi":
                    out.update(s["names"])
                elif k == "for":
                    out.add(s["item"])
                elif k == "assign":
                    t = s.get("target") or {}
                    if t.get("expr") == "name":
                        out.add(t["name"])
                elif k == "aug_assign":
                    t = s.get("target") or {}
                    if t.get("expr") == "name":
                        out.add(t["name"])
                for key in ("body", "then", "else", "catch", "finally"):
                    sub = s.get(key)
                    if isinstance(sub, list):
                        stack.append(sub)
        return out

    # ── 表达式 ────────────────────────────────────────────────────────
    def call_args(self, args: list[dict]) -> str:
        """调用实参：位置实参在前、命名实参在后（Python 的硬性顺序）。"""
        pos = [a for a in args if a.get("expr") != "named"]
        named = [a for a in args if a.get("expr") == "named"]
        parts = [self.expr(a) for a in pos]
        parts += [f"{a['name']}={self.expr(a['value'])}" for a in named]
        return ", ".join(parts)

    def expr(self, e: dict) -> str:
        k = e.get("expr")
        if k == "string":
            return json.dumps(e["value"], ensure_ascii=False)
        if k == "int":
            return str(e["value"])
        if k == "float":
            return repr(e["value"])
        if k == "bool":
            return "True" if e["value"] else "False"
        if k == "null":
            return "None"
        if k == "name":
            # 模块名要换成本工程生成的模块名，否则会撞上宿主语言自带的同名模块；
            # 但用户自己声明的变量同名时，名字优先是变量，不能换。
            if e["name"] in self.imported and e["name"] not in self.user_vars:
                return host_module_name(self.aliases.get(e["name"], e["name"]))
            return e["name"]
        if k == "await":
            return f"(await _agl_wait({self.expr(e['value'])}))"
        if k == "list":
            return "[" + ", ".join(self.expr(i) for i in e["items"]) + "]"
        if k == "map":
            return "{" + ", ".join(f"{self.expr(x['key'])}: {self.expr(x['value'])}"
                                   for x in e["entries"]) + "}"
        if k == "set":
            return "{" + ", ".join(self.expr(i) for i in e["items"]) + "}"
        if k == "named":
            return f"{e['name']}={self.expr(e['value'])}"
        if k == "struct_call":
            # 跨模块结构体：`_agl_模块._agl_t_类型(字段=值, …)`
            return (f"{host_module_name(e['module'])}._agl_t_{e['name']}("
                    + self.call_args(e["args"]) + ")")
        if k == "call":
            # 内建函数由语义层打了标记，这里只认标记、不再按名字猜。
            # 名字会撞车（用户完全可以定义自己的 `切分`），标记不会。
            if "builtin" in e:
                bi = KeywordTable.load().builtin_by_canon(e["builtin"])
                lower = bi.lower if bi else e["name"]
                return f"{lower}(" + ", ".join(self.expr(a) for a in e["args"]) + ")"
            # FFI 黑盒调用：语义层打了 `ffi` 标记，标明这是外部库的方法。
            # 一律走 `_agl_ffi_call`，由它负责懒导入与异常翻译。
            if "ffi" in e:
                args = ", ".join(self.expr(a) for a in e["args"])
                return (f'_agl_ffi_call({json.dumps(e["ffi"])}, '
                        f'{json.dumps(e["name"])}'
                        + (f", {args}" if args else "") + ")")
            callee = e["name"]
            # 方法名映射：中文方法名 → Python 原生方法
            METHOD_MAP = {
                # 列表方法
                "追加": "append",
                "插入": "insert",
                "删除": "remove",
                "弹出": "pop",
                "排序": "sort",
                "反转": "reverse",
                "拼接": "extend",
                "索引": "index",
                "统计": "count",
                "复制": "copy",
                "清空": "clear",
                # 字符串方法
                "拆分": "split",
                "替换": "replace",
                "查找": "find",
                "大写": "upper",
                "小写": "lower",
                "去空格": "strip",
                "连接": "join",
                "开头是": "startswith",
                "结尾是": "endswith",
                "是数字": "isdigit",
                "是字母": "isalpha",
                "居中": "center",
                "左对齐": "ljust",
                "右对齐": "rjust",
                # 字典方法
                "获取": "get",
                "键们": "keys",
                "值们": "values",
                "项们": "items",
            }
            # 模块成员调用：`列表工具.排序(...)` 里的 `排序` 是模块导出的函数名，
            # 不是列表方法，绝不能走下面的中文方法名映射（否则变成 `.sort`）。
            tgt = e.get("target")
            is_module = (isinstance(tgt, dict) and tgt.get("expr") == "name"
                         and tgt.get("name") in self.imported
                         and tgt.get("name") not in self.user_vars)
            if "target" in e:
                # 只有「有接收者的调用」才可能是方法调用；裸调用 `插入(...)`
                # 是用户函数，不能被方法名映射改成宿主函数名。
                if not is_module:
                    callee = METHOD_MAP.get(callee, callee)
                callee = f"{self.expr(tgt)}.{callee}"
            elif e["name"] in self.structs:
                # 结构体构造：`用户(编号: 1, …)` → `_agl_t_用户(编号=1, …)`
                callee = f"_agl_t_{e['name']}"
            return f"{callee}(" + self.call_args(e["args"]) + ")"
        if k == "member":
            # 内置只读属性（`.长度` / `.length`）不是真实成员，
            # 必须换成宿主语言的等价操作；其余成员原样转译。
            if e["field"] in ("消息", "message"):
                return f"_agl_err_message({self.expr(e['target'])})"
            lower = KeywordTable.load().attr_lower_to(e["field"])
            if lower:
                return f"{lower}({self.expr(e['target'])})"
            # 常用容器方法名映射：中文方法名 → Python 原生方法
            METHOD_MAP = {
                # 列表方法
                "追加": "append",
                "插入": "insert",
                "删除": "remove",
                "弹出": "pop",
                "排序": "sort",
                "反转": "reverse",
                "拼接": "extend",
                # 字符串方法
                "拆分": "split",
                "替换": "replace",
                "查找": "find",
                "大写": "upper",
                "小写": "lower",
                "去空格": "strip",
            }
            field = METHOD_MAP.get(e["field"], e["field"])
            return f"{self.expr(e['target'])}.{field}"
        if k == "index":
            return f"{self.expr(e['target'])}[{self.expr(e['index'])}]"
        if k == "slice":
            # 切片：a[start:stop] / a[start:stop:step]
            目标 = self.expr(e['target'])
            起 = self.expr(e['start']) if 'start' in e else ""
            止 = self.expr(e['stop']) if 'stop' in e else ""
            步 = f":{self.expr(e['step'])}" if 'step' in e else ""
            return f"{目标}[{起}:{止}{步}]"
        if k == "unop":
            op = "not " if e["op"] == "!" else "-"
            return f"{op}{self.expr(e['operand'])}"
        if k == "binop":
            op = _BINOP_PY.get(e["op"], e["op"])
            return f"({self.expr(e['left'])} {op} {self.expr(e['right'])})"
        if k == "lambda":
            # lambda表达式：lambda 参数: 返回值
            参数 = ", ".join(self.expr(p) for p in e["params"])
            返回值 = self.expr(e["body"])
            return f"lambda {参数}: {返回值}"
        if k == "list_comp":
            # 列表推导：[表达式 for 变量 in 序列 if 条件]
            表达式 = self.expr(e["expr"])
            变量 = self.expr(e["var"])
            序列 = self.expr(e["iter"])
            条件 = f" if {self.expr(e['cond'])}" if "cond" in e else ""
            return f"[{表达式} for {变量} in {序列}{条件}]"
        if k == "match":
            # 表达式位置匹配：lambda + 条件表达式的单表达式实现。
            # 分支体只允许表达式，绑定用海象运算符落进 lambda 局部作用域。
            temp = f"__aglm{self.tmp}"
            self.tmp += 1
            result = "None"
            for arm in reversed(e["arms"]):
                pat = arm["pattern"]
                wildcard = pat.get("expr") == "name" and pat.get("name") == "_"
                list_pat = pat.get("expr") == "list_pattern"
                map_pat = pat.get("expr") == "map_pattern"
                parts: list[str] = []
                if map_pat:
                    parts.extend(self._map_pattern_parts(temp, pat.get("entries") or []))
                elif list_pat:
                    parts.extend(self._list_pattern_parts(temp, pat.get("items") or []))
                elif not wildcard:
                    parts.append(f"{temp} == {self.expr(pat)}")
                if arm.get("bind"):
                    parts.append(f"[{arm['bind']} := {temp}]")
                if arm.get("guard") is not None:
                    parts.append(self.expr(arm["guard"]))
                cond = " and ".join(parts) if parts else "True"
                body = self.expr(arm["body"])
                if cond == "True":
                    result = body
                else:
                    result = f"({body} if {cond} else {result})"
            return f"(lambda {temp}: {result})({self.expr(e['subject'])})"
        raise ValueError(f"后端不认识的表达式节点：{k}")

    # ── 语句 ──────────────────────────────────────────────────────────
    def simple_code(self, s: dict) -> str | None:
        """把"一句话就能说完"的语句渲染成**不带缩进**的单行 Python。

        返回 None 表示这是复合语句（if / while / for / try / on_event），
        在 Python 里必然自己带一个代码块，压不进一行。

        抽出来不是为了少写几行，是因为 `on_event` 必须先知道回调体能不能压成
        一行，才能决定要不要把注册语句挂到后面去 —— 先写进 emitter 再捞回来
        既绕又不安全。
        """
        k = s.get("stmt")
        if k == "let_multi":
            return ", ".join(s["names"]) + f" = {self.expr(s['value'])}"
        if k == "assign_multi":
            targets = ", ".join(self.expr(t) for t in s["targets"])
            return f"{targets} = {self.expr(s['value'])}"
        if k == "let":
            return f"{s['name']} = {self.expr(s['value'])}"
        if k == "assign":
            return f"{self.expr(s['target'])} = {self.expr(s['value'])}"
        if k == "aug_assign":
            return f"{self.expr(s['target'])} {s['op']}= {self.expr(s['value'])}"
        if k == "return":
            if "value" in s:
                return f"return {self.expr(s['value'])}"
            return "return"
        if k == "raise":
            if "value" in s:
                return f"raise Exception({self.expr(s['value'])})"
            return "raise Exception()"
        if k == "expr":
            return self.expr(s["value"])
        if k == "break":
            return "break"
        if k == "continue":
            return "continue"
        return None

    def take_pending(self) -> str:
        """取走积压的补注语句，接成 `<前一句>; ` 形式前置到本行。"""
        if not self.pending:
            return ""
        joined = "; ".join(self.pending)
        self.pending.clear()
        return joined + "; "

    def flush_pending(self, line: int) -> None:
        """下一个语句吃不下（复合语句）或块要结束时，把积压的吐出来。

        这里只能乖乖占行，行号会顺延 —— 但至少不会把语句弄丢。
        """
        while self.pending:
            self.em.put(line, f"{self.ind()}{self.pending.pop(0)}")

    def stmt(self, s: dict) -> None:
        k = s.get("stmt")
        line = s["span"]["line"]

        one = self.simple_code(s)
        if one is not None:
            self.em.put(line, f"{self.ind()}{self.take_pending()}{one}")
            return
        # 复合语句：`:` 后面不能跟分号，积压的只能另起一行（会顺延）
        self.flush_pending(line)

        if k == "if":
            self.emit_if(s)
            return
        if k == "while":
            self.em.put(line, f"{self.ind()}while {self.expr(s['cond'])}:")
            self.block(s["body"])
            if s.get("else") is not None:
                self.emit_loop_else(s, line)
            return
        if k == "for":
            self.em.put(line, f"{self.ind()}for {s['item']} in {self.expr(s['iter'])}:")
            self.block(s["body"])
            if s.get("else") is not None:
                self.emit_loop_else(s, line)
            return
        if k == "match":
            self.emit_match(s)
            return
        if k == "with":
            self.emit_with(s)
            return
        if k == "on_event":
            cb = f"_agl_cb_{line}"
            body = s["body"]
            # 单行回调块（AGL 里 `{ 语句 }` 全挤在一行）：Python 这边也压成一行
            # `async def cb(x): 语句`（Python 允许 def 后接简单语句），
            # 注册语句则交给 take_pending 挂到下一个语句行首 —— 两者都不额外占行，
            # 后面的 AGL 行号一个都不顺延。
            packed = None
            if len(body) == 1 and body[0]["span"]["line"] == line:
                packed = self.simple_code(body[0])
            if packed is not None:
                self.em.put(line, f"{self.ind()}async def {cb}({s['param']}): {packed}")
                self.pending.append(
                    f'_agl_on({self.expr(s["task"])}, "{s["event"]}", {cb})')
                return
            # 多行回调块：块首行 → def，块内语句各自按自己的行号落位，
            # 块结束后一行的位置写注册语句。
            self.em.put(line, f"{self.ind()}async def {cb}({s['param']}):")
            self.depth += 1
            if s["body"]:
                for x in s["body"]:
                    self.stmt(x)
            else:
                self.em.put(line + 1, f"{self.ind()}pass")
            self.depth -= 1
            last = s["body"][-1]["span"]["endLine"] if s["body"] else line
            self.em.put(last + 1,
                        f'{self.ind()}_agl_on({self.expr(s["task"])}, "{s["event"]}", {cb})')
            return
        if k == "try":
            self.em.put(line, f"{self.ind()}try:")
            self.block(s["body"])
            catch = s.get("catch")
            cname = s.get("catch_name")
            fin = s.get("finally")
            # except 行：取 try 块最后一行 + 1
            after = (s["body"][-1]["span"]["endLine"] + 1) if s["body"] else line + 1
            if catch:
                first_catch = catch[0]["span"]["line"]
                if first_catch - 1 > after:
                    after = first_catch - 1
            if catch is not None:
                head = f"{self.ind()}except {_EXC_ZH_TO_HOST.get(s.get('catch_type'), 'Exception')}"
                if cname:
                    head += f" as {cname}"
                self.em.put(after, head + ":")
                if catch:
                    self.block(catch)
                else:
                    self.em.put(after + 1, f"{self.ind()}{INDENT}pass")
                fin_line = (catch[-1]["span"]["endLine"] + 1) if catch else after + 2
                orelse = s.get("else")
                if orelse is not None:
                    # 否则块：try 没出错才执行。行号规则同循环 else——
                    # 块首行的上一行放 `else:`，与 catch 末行冲突则顺延。
                    cend = (catch[-1]["span"]["endLine"]) if catch else after + 1
                    eline = (orelse[0]["span"]["line"] - 1) if orelse else cend + 1
                    if eline <= cend:
                        eline = cend + 1
                    self.em.put(eline, f"{self.ind()}else:")
                    if orelse:
                        self.block(orelse)
                    else:
                        self.em.put(eline + 1, f"{self.ind()}{INDENT}pass")
                    fin_line = (orelse[-1]["span"]["endLine"] + 1) if orelse else eline + 2
            elif fin is not None:
                # 无捕获的 try…finally：异常继续向上冒（原生语义，不吃异常）。
                # 必须用 `is not None`：`最后 { }` 是空列表 []，真值判断会把
                # 整个 finally 丢掉 —— 生成只剩 `try:` 的非法 Python。
                fin_line = after
            else:
                fin_line = None
            if fin is not None:
                self.em.put(fin_line, f"{self.ind()}finally:")
                self.block(fin)
            return
        raise ValueError(f"后端不认识的语句节点：{k}")

    def _list_pattern_parts(self, subj: str, items: list[dict]) -> list[str]:
        """把列表模式（含嵌套）展开成宿主 if 条件里的布尔片段。"""
        parts = [f"isinstance({subj}, list)", f"len({subj}) == {len(items)}"]
        for j, it in enumerate(items):
            kind = it.get("kind")
            if kind == "literal":
                parts.append(f"{subj}[{j}] == {self.expr(it['value'])}")
            elif kind == "bind":
                parts.append(f"[{it['name']} := {subj}[{j}]]")
            elif kind == "list":
                parts.extend(self._list_pattern_parts(
                    f"{subj}[{j}]", it["pattern"].get("items") or []))
            elif kind == "map":
                parts.extend(self._map_pattern_parts(
                    f"{subj}[{j}]", it["pattern"].get("entries") or []))
        return parts

    def _map_pattern_parts(self, subj: str, entries: list[dict]) -> list[str]:
        """把映射模式（含嵌套）展开成宿主 if 条件里的布尔片段。"""
        parts = [f"isinstance({subj}, dict)"]
        for e in entries:
            key = self.expr(e["key"])
            parts.append(f"{key} in {subj}")
            val = e["value"]
            kind = val.get("kind")
            if kind == "literal":
                parts.append(f"{subj}[{key}] == {self.expr(val['value'])}")
            elif kind == "bind":
                parts.append(f"[{val['name']} := {subj}[{key}]]")
            elif kind == "list":
                parts.extend(self._list_pattern_parts(
                    f"{subj}[{key}]", val["pattern"].get("items") or []))
            elif kind == "map":
                parts.extend(self._map_pattern_parts(
                    f"{subj}[{key}]", val["pattern"].get("entries") or []))
        return parts

    def emit_match(self, s: dict) -> None:
        """`匹配 (表达式) { 模式 { … } … 否则 { … } }` → 宿主 if/elif 链。

        临时变量放在 AGL 源的行首（行号对齐），`==` 逐臂比较；
        `_` 通配臂发成 `else:`。AGL 无兜底且无匹配时什么都不执行，
        与 Python `match` 一致（无 else 的 if 链）。
        单行臂（`1 { x = "低" }` 整臂在一行）：if 头和体压成一行，
        与 on_event 的单行回调同款机制 —— 否则 pattern 行被 body 挤走，
        行号顺延错位，smap 对不上。
        """
        line = s["span"]["line"]
        tmp = f"_aglmatch_{line}"
        self.em.put(line, f"{self.ind()}{tmp} = {self.expr(s['subject'])}")
        for i, arm in enumerate(s["arms"]):
            pat = arm["pattern"]
            guard = arm.get("guard")
            bind = arm.get("bind")
            pline = pat.get("span", {}).get("line", line + i + 1)
            body = arm["body"]
            packed = None
            if len(body) == 1 and body[0]["span"]["line"] == pline:
                packed = self.simple_code(body[0])
            if pat.get("expr") == "map_pattern":
                parts = self._map_pattern_parts(tmp, pat.get("entries") or [])
                cond = " and ".join(parts)
                if guard is not None:
                    cond += f" and ({self.expr(guard)})"
                kw = "if" if i == 0 else "elif"
                head = f"{self.ind()}{kw} {cond}:"
                if packed is not None:
                    self.em.put(pline, f"{head} {packed}")
                else:
                    self.em.put(pline, head)
                    self.block(body)
                continue
            if pat.get("expr") == "list_pattern":
                parts = self._list_pattern_parts(tmp, pat.get("items") or [])
                cond = " and ".join(parts)
                if guard is not None:
                    cond += f" and ({self.expr(guard)})"
                kw = "if" if i == 0 else "elif"
                head = f"{self.ind()}{kw} {cond}:"
                if packed is not None:
                    self.em.put(pline, f"{head} {packed}")
                else:
                    self.em.put(pline, head)
                    self.block(body)
                continue
            if bind:
                # `[名 := 主体]` 是非空列表，恒真；绑定在守卫之前求值。
                # 不用 `else:`：绑定臂后面仍可能有别的分支，语义是"匹配任意"。
                kw = "if" if i == 0 else "elif"
                cond = f"[{bind} := {tmp}]"
                if guard is not None:
                    cond += f" and ({self.expr(guard)})"
                head = f"{self.ind()}{kw} {cond}:"
                if packed is not None:
                    self.em.put(pline, f"{head} {packed}")
                else:
                    self.em.put(pline, head)
                    self.block(body)
                continue
            if pat.get("expr") == "name" and pat.get("name") == "_":
                if guard is not None:
                    kw = "if" if i == 0 else "elif"
                    head = f"{self.ind()}{kw} {self.expr(guard)}:"
                    if packed is not None:
                        self.em.put(pline, f"{head} {packed}")
                    else:
                        self.em.put(pline, head)
                        self.block(body)
                elif packed is not None:
                    self.em.put(pline, f"{self.ind()}else: {packed}")
                else:
                    self.em.put(pline, f"{self.ind()}else:")
                    self.block(body)
            else:
                kw = "if" if i == 0 else "elif"
                cond = f"{tmp} == {self.expr(pat)}"
                if guard is not None:
                    cond += f" and ({self.expr(guard)})"
                head = f"{self.ind()}{kw} {cond}:"
                if packed is not None:
                    self.em.put(pline, f"{head} {packed}")
                else:
                    self.em.put(pline, head)
                    self.block(body)

    def emit_with(self, s: dict) -> None:
        """`使用 表达式 [作为 名] { … }` → 宿主 with 语句（原生上下文管理器）。"""
        line = s["span"]["line"]
        head = f"{self.ind()}with {self.expr(s['expr'])}"
        if s.get("name"):
            head += f" as {s['name']}"
        self.em.put(line, head + ":")
        self.block(s["body"])

    def emit_if(self, s: dict) -> None:
        line = s["span"]["line"]
        self.em.put(line, f"{self.ind()}if {self.expr(s['cond'])}:")
        self.block(s["then"])

        if "else" not in s:
            return
        els = s["else"]
        # `否则 如果` → elif，紧跟上一分支，不额外占行
        if len(els) == 1 and els[0].get("stmt") == "if":
            nested = els[0]
            # 把 if 换成 elif 就地生成
            self.em.put(nested["span"]["line"],
                        f"{self.ind()}elif {self.expr(nested['cond'])}:")
            self.block(nested["then"])
            if "else" in nested:
                self.emit_else_tail(nested, nested["span"]["line"])
            return

        self.emit_else_tail(s, line)

    def emit_loop_else(self, s: dict, fallback_line: int) -> None:
        """循环的否则块：Python 原生 while/for...else（未 中断 才执行）。

        行号规则与 emit_else_tail 一致：否则块首行的上一行放 `else:`，
        若与循环体末行冲突则顺延一行。
        """
        els = s["else"]
        body_end = s["body"][-1]["span"]["endLine"] if s["body"] else fallback_line
        eline = (els[0]["span"]["line"] - 1) if els else body_end + 1
        if eline <= body_end:
            eline = body_end + 1
        self.em.put(eline, f"{self.ind()}else:")
        if els:
            self.block(els)
        else:
            self.em.put(eline + 1, f"{self.ind()}{INDENT}pass")

    def emit_else_tail(self, s: dict, fallback_line: int) -> None:
        els = s["else"]
        then_end = s["then"][-1]["span"]["endLine"] if s["then"] else fallback_line
        eline = (els[0]["span"]["line"] - 1) if els else then_end + 1
        if eline <= then_end:
            eline = then_end + 1
        self.em.put(eline, f"{self.ind()}else:")
        if els:
            self.block(els)
        else:
            self.em.put(eline + 1, f"{self.ind()}{INDENT}pass")

    def block(self, stmts: list[dict]) -> None:
        self.depth += 1
        if not stmts:
            # 空块：在上一条之后补 pass（位置只能顺延）
            last = max(self.em.out_len(), 1)
            self.em.put(last + 1, f"{self.ind()}pass")
        for s in stmts:
            self.stmt(s)
        # 块里最后一条若刚好是单行回调块，注册语句还没落地 —— 补在本块收尾处
        self.flush_pending(self.em.out_len() + 1)
        self.depth -= 1

    # ── 函数 ──────────────────────────────────────────────────────────
    def function(self, fn: dict) -> None:
        sp = fn["span"]
        line = sp["line"]
        params = ", ".join(
            p["name"] + (f"={self.expr(p['default'])}" if "default" in p else "")
            for p in fn["params"]
        )

        # 校验块：默认强制开启的运行时断言。只要有一个条件不成立就立刻中断，
        # 并把原文（中文表达式）带进错误消息 —— 契约不是注释，是拦人的。
        for c in fn.get("checks", []):
            cl = c["span"]["line"]
            src = self.expr(c)
            msg = repr("校验失败：" + src)
            self.em.put(cl, f"{INDENT}if not ({src}): raise ValueError({msg})")

        eff = fn.get("effects", [])
        if eff:
            desc = "、".join(_EFFECT_ZH.get(e, e) for e in eff)
            eff_line = self.entry_line(fn, line)
            if eff_line:
                self.em.put(eff_line, f"{INDENT}# 效应: {desc}")

        prefix = "async " if fn.get("is_async") else ""
        self.em.put(line, f"{prefix}def {fn['name']}({params}):")
        self.depth = 1
        if not fn["body"]:
            self.em.put(line + 1, f"{self.ind()}pass")
        for s in fn["body"]:
            self.stmt(s)
        # 收尾：函数体最后一条若是单行回调块，注册语句补在 `}` 那一带
        self.flush_pending(self.em.out_len() + 1)
        self.depth = 0

    def entry_line(self, fn: dict, fn_line: int) -> int | None:
        """效应声明写在函数头与函数体之间，取第一个语句前一行。"""
        if fn["body"]:
            l = fn["body"][0]["span"]["line"] - 1
            return l if l > fn_line else None
        return None

    # ── 入口 ──────────────────────────────────────────────────────────
    def generate(self) -> tuple[str, dict]:
        mod = self.ir["program"]["modules"][0]

        # 导入语句按源码原行号落到 Python，这样它出错时也能指回 AGL 那一行
        for imp in mod.get("imports") or []:
            name = imp["module"]
            line = (imp.get("span") or {}).get("line")
            if not line:
                continue
            if imp.get("kind") == "ffi":
                # 外部库不在编译期导入（宿主侧懒导入），但这一行仍要占住位置，
                # 并把「用了哪个外部库、声明了什么效应」写进人能看懂的注释里。
                eff = imp.get("effect") or "External"
                self.em.put(line, f"# 外部库 {name}（黑盒调用，声明效应：{eff}）")
                continue
            self.em.put(line, f"import {host_module_name(name)}")

        # 顶层常量：语义上是模块级只读绑定，但**初始化必须晚于运行时模板**——
        # 常量初值可能调用内建（`拼接(...)`）或构造结构体，那些函数/类都定义在
        # 文件末尾的 RUNTIME 里。放在这里执行会在 import 阶段就 NameError。
        # 因此先记下行号与表达式，等拼完 RUNTIME 再落到尾部，见 generate()。
        self.const_decls: list[tuple[int, str]] = [
            (c["span"]["line"], f"{c['name']} = {self.expr(c['value'])}")
            for c in (mod.get("consts") or [])
        ]

        # 结构体声明：注册成一个带 __slots__ 的小类，字段名保持原文。
        # 注册必须放在运行时模板之后（模板里有 `_agl_struct_type`），
        # 所以这里先攒着，等拼完 RUNTIME 再落，见 generate() 末尾。
        self.type_regs: list[str] = [
            f'_agl_t_{t["name"]} = _agl_struct_type("{t["name"]}", '
            f'({", ".join(repr(f["name"]) for f in t["fields"])},))'
            for t in (mod.get("types") or [])
        ]

        for fn in mod["functions"]:
            self.function(fn)
        # 兜底：走到这里 pending 必须已经清空，否则说明有条路漏了 flush，
        # 回调会被静默丢掉 —— 宁可报出来，也不能让注册的回调凭空消失。
        self.flush_pending(self.em.out_len() + 1)
        code = self.em.code(self.ir.get("lang", "zh"))
        # 延迟段：结构体注册 + 顶层常量。顺序不能反——常量可能构造结构体。
        deferred: list[tuple[int, str]] = []
        for t in self.type_regs:
            deferred.append((0, t))
        deferred.extend(self.const_decls)
        if deferred:
            code += "\n"
            for agl_line, text in deferred:
                code += text + "\n"
                if agl_line:
                    # 记录真实行号，运行期错误仍能回译到常量那一行。
                    self.em.smap[code.count("\n")] = agl_line
        entry = self.ir["program"]["entry"]
        entry_fn = next((f for f in mod["functions"] if f["name"] == entry), None)
        if entry_fn is not None:
            # 异步入口必须交给事件循环启动；同步入口直接调用。
            # 异步入口套一层 _agl_main_wrap：主入口跑完后还要统一收走
            # 「任务完成后才迟到注册」的回调，出错照回译链路抛。
            call = (f"asyncio.run(_agl_main_wrap({entry}()))"
                    if entry_fn.get("is_async") else f"{entry}()")
            code += f'\n\nif __name__ == "__main__":\n    {call}\n'
        return code, self.em.smap


def generate(ir: dict, source_lines: list[str]) -> tuple[str, dict]:
    return PyGenerator(ir, source_lines).generate()
