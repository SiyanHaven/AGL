# -*- coding: utf-8 -*-
"""agl-mcp：把编译器包成 MCP 工具，供任何支持 MCP 的 AI 客户端直接调用。

为什么要它：AGL 是给 AI 用的语言，AI 不该去背命令行，而应该**调用工具**。
这里只暴露固定的一组工具、固定参数，参数 schema 本身就是生成约束。

传输层：stdio，一行一条 JSON-RPC 2.0 消息（MCP 的规定）。
同时兼容带 `Content-Length` 头的帧写法，两种都能收。

设计纪律：
  · 单独跑得起来（`python -m compiler.mcp`），不依赖 CLI 进程；
  · 所有工具的返回都是结构化 JSON，和 `aglc --json` 完全同构；
  · 不往 stdout 打任何非协议内容 —— 混一行日志就废掉整条流。
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

from compiler.cli import pipeline
from compiler.diagnostics import Diagnostic
from compiler.ir import dumps, summarize
from compiler.lexer import KeywordTable

SERVER_NAME = "agl-mcp"
# 服务器版本跟随语言版本（spec/keywords.toml meta.version），
# 发布、更新、MCP 握手全看这一个数字，不许再出现「MCP 0.3.0 vs 语言 0.25.0」的脱节。
def _server_version() -> str:
    return KeywordTable.load().version


SERVER_VERSION = _server_version()
PROTOCOL_VERSION = "2024-11-05"
# GitHub 发布源：代码上传 GitHub 后把 releases 主页填在这里，agl_version 的
# 更新检查就指向它；留空则检查功能自动跳过（返回「未配置」）。
GITHUB_RELEASES_URL = ""

# 客户端连上后第一眼看到的话：告诉它先读速查卡，别靠猜
INSTRUCTIONS = (
    "这是 AGL（Agent Grounded Language）编译器工具。写 AGL 代码之前，"
    "先调用 agl_card 拿到关键字速查卡（唯一正确的写法来源），再写；"
    "写完先用 agl_check_source 直接传源码字符串预检，按返回的 fix_hints 改到 ok=true；"
    "满意了再写文件到磁盘，最后用 agl_run 运行。"
    "不要发明速查卡里没有的语法。"
)

# ── 工具清单（参数固定，不许自由变体）─────────────────────────────────
_FILE = {"type": "string", "description": "AGL 源文件路径（绝对路径最稳）"}

TOOLS: list[dict] = [
    {
        "name": "agl_card",
        "description": "关键字速查卡：写 AGL 代码之前先读这一页。返回 Markdown，"
                       "含全部关键字的中英对照、运算符、内置属性、可跑的最小程序和常见错误。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lang": {"type": "string", "enum": ["zh", "en", "both"],
                         "description": "要哪个分支的卡（默认 both）"},
            },
        },
    },
    {
        "name": "agl_check",
        "description": "检查 AGL 源码（词法 / 语法 / 语义 / 效应 / 模块），不产出文件。"
                       "返回结构化诊断：错误码、文件行号、中文原因、fix_hints。",
        "inputSchema": {
            "type": "object",
            "properties": {"file": _FILE},
            "required": ["file"],
        },
    },
    {
        "name": "agl_check_source",
        "description": "直接传 AGL 源码字符串做预检（不写文件）。"
                       "AI 生成代码后先调这个，按返回的 fix_hints 改到 ok=true，再落盘。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "AGL 源码全文"},
                "filename": {"type": "string", "description": "虚拟文件名（默认 main.agl），仅用于报错里显示"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "agl_build",
        "description": "编译 AGL 源码，产出 xxx.agb（AGB 字节码）和 xxx.agl.ir；不再产出 .py。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "out_dir": {"type": "string", "description": "输出目录（默认与源文件同目录）"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "agl_run",
        "description": "编译并运行 AGL 程序（默认 VM；py 仅显式兜底）；也接受 .agb 字节码文件。"
                       "运行期出错时返回 AGL 的文件行号、中文原因和 AGL 级调用栈（不暴露宿主语言术语）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "backend": {"type": "string", "enum": ["vm", "py", "auto"],
                             "description": "默认 vm；py 显式兜底；auto 超出 AGB 子集才退回 py"},
                "allow_effects": {"type": "string",
                                  "description": "允许的效应，逗号分隔；默认全禁，写 all 显式全量放行"},
                "allow_tools": {"type": "string",
                                "description": "允许的外部工具，逗号分隔；默认全禁，写 all 显式全量放行"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "agl_run_source",
        "description": "直接传 AGL 源码字符串编译并运行（不写文件）。"
                       "预检通过后用这个快速验证效果，满意了再落盘。默认 VM。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "AGL 源码全文"},
                "filename": {"type": "string", "description": "虚拟文件名（默认 main.agl），仅用于报错里显示"},
                "backend": {"type": "string", "enum": ["vm", "py", "auto"],
                             "description": "默认 vm；py 显式兜底；auto 超出 AGB 子集才退回 py"},
                "allow_effects": {"type": "string",
                                  "description": "允许的效应，逗号分隔；默认全禁，写 all 显式全量放行"},
                "allow_tools": {"type": "string",
                                "description": "允许的外部工具，逗号分隔；默认全禁，写 all 显式全量放行"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "agl_convert",
        "description": "中英双分支转换：只换关键字 / 运算符 / 内置属性名，"
                       "标识符、字符串、注释原样保留（可逆）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "to": {"type": "string", "enum": ["zh", "en"], "description": "目标分支"},
                "write": {"type": "boolean",
                          "description": "true 则写入同目录（文件名加 _zh/_en），否则只返回内容"},
            },
            "required": ["file", "to"],
        },
    },
    {
        "name": "agl_summary",
        "description": "从中间表示生成中文摘要：这个程序由哪些函数组成、各自做什么。"
                       "给不看代码的人复述逻辑用。",
        "inputSchema": {
            "type": "object",
            "properties": {"file": _FILE},
            "required": ["file"],
        },
    },
    {
        "name": "agl_version",
        "description": "查询 AGL / MCP 版本，并可选检查 GitHub 是否有新版本。"
                       "checkUpdate=true 且配置了发布地址时才访问 GitHub Releases（超时 5 秒，"
                       "网络失败不报错）；未配置发布地址时返回 not_configured，不联网。"
                       "更新方法见 docs/安装与更新.md：换新版本源码后重启 MCP 客户端即生效"
                       "（stdio 长连接，重连即加载新代码）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "checkUpdate": {"type": "boolean",
                                "description": "是否检查 GitHub 最新版本（默认 false；未配置发布地址时不联网）"},
            },
        },
    },
    {
        "name": "agl_doctor",
        "description": "编译器自检（29 项验收：规格文件、关键字表、黄金用例、双分支等价、"
                       "错误码、行号对齐、模块、速查卡、MCP、安全默认、标准库、优化对拍、编辑器高亮、LSP、"
                       "异步、FFI、VM 效应/配额白名单与 AGB 产物）。改完 AGL 工具链后跑它。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _diag(d: Diagnostic) -> dict:
    return d.to_json()


def _result(res) -> dict:
    payload = {
        "ok": res.ok,
        "file": res.path.name,
        "lang": res.lang,
        "diagnostics": [_diag(d) for d in res.diagnostics],
    }
    if res.errors:
        payload["first_error"] = res.errors[0].code
    return payload


# ── 各工具的实现 ──────────────────────────────────────────────────────
def tool_card(args: dict) -> dict:
    from compiler import card

    lang = args.get("lang") or "both"
    langs = ["zh", "en"] if lang == "both" else [lang]
    if lang not in ("zh", "en", "both"):
        raise ValueError("lang 只能是 zh / en / both")
    return {"ok": True, "version": KeywordTable.load().version,
            "cards": {x: card.build(x) for x in langs}}


def tool_check(args: dict) -> dict:
    res = pipeline.compile_file(_file(args))
    return _result(res)


def tool_check_source(args: dict) -> dict:
    source = args["source"]
    filename = args.get("filename") or "main.agl"
    res = pipeline.compile_source(source, filename)
    return _result(res)


def tool_build(args: dict) -> dict:
    from compiler import bytecode

    res = pipeline.compile_file(_file(args))
    payload = _result(res)
    if not res.ok:
        return payload
    outdir = _inside_workspace(Path(args["out_dir"])) if args.get("out_dir") else res.path.parent
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        agb = bytecode.compile_program(res.ir)
    except bytecode.AgbUnsupported as e:
        payload["ok"] = False
        payload["first_error"] = "AGB_UNSUPPORTED"
        payload["message"] = f"该程序超出 AGB 子集，不再生成 .py：{e}"
        return payload

    ir_path = outdir / (res.path.name + ".ir")
    agb_path = outdir / (res.path.stem + ".agb")
    ir_path.write_text(dumps(res.ir), encoding="utf-8", newline="\n")
    agb_path.write_text(bytecode.encode(agb), encoding="utf-8", newline="\n")

    dep_files = []
    seen = set()

    def _write_dep(name, dep):
        try:
            dep_agb = bytecode.compile_program(dep.ir)
        except bytecode.AgbUnsupported as e:
            payload["ok"] = False
            payload["first_error"] = "AGB_UNSUPPORTED"
            payload["message"] = f"依赖模块 `{name}` 超出 AGB 子集：{e}"
            return False
        if dep_agb["module"] in seen:
            return True
        seen.add(dep_agb["module"])
        p = outdir / (dep_agb["module"].replace("/", "_").replace("\\", "_") + ".agb")
        p.write_text(bytecode.encode(dep_agb), encoding="utf-8", newline="\n")
        dep_files.append(str(p))
        (outdir / (dep.path.name + ".ir")).write_text(dumps(dep.ir), encoding="utf-8",
                                                       newline="\n")
        for child_name, child in (dep.deps or {}).items():
            if not _write_dep(child_name, child):
                return False
        return True

    for name, dep in (res.deps or {}).items():
        if not _write_dep(name, dep):
            return payload
    payload["agb"] = str(agb_path)
    payload["ir"] = str(ir_path)
    payload["deps"] = dep_files
    return payload


def _allow_set(args: dict, key: str) -> set[str] | None:
    """MCP 运行授权：未传参默认空集（全禁）；显式 all 才返回 None（全量）。"""
    raw = args.get(key)
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw]
    else:
        items = [x.strip() for x in str(raw).replace("，", ",").split(",")]
    values = {x for x in items if x}
    if any(x.lower() == "all" for x in values):
        return None
    return values


def _run_payload(res, backend: str = "vm",
                 allow_effects: set[str] | None = set(),
                 allow_tools: set[str] | None = set()) -> dict:
    """按后端跑一份已编译结果；默认 VM，超出子集不静默退回 Python。"""
    from compiler import bytecode, vm as vm_backend

    payload = _result(res)
    payload["stdout"] = ""
    if not res.ok:
        return payload

    if backend in ("vm", "auto"):
        try:
            agb = bytecode.compile_program(res.ir)
            modules = {}

            def _collect(dep):
                dep_agb = bytecode.compile_program(dep.ir)
                if dep_agb["module"] in modules:
                    return
                modules[dep_agb["module"]] = dep_agb
                for d in (dep.deps or {}).values():
                    _collect(d)

            for dep in (res.deps or {}).values():
                _collect(dep)
        except bytecode.AgbUnsupported as e:
            if backend == "auto":
                payload["warning"] = f"该程序超出 AGB 子集，已退回 Python：{e}"
            else:
                payload["ok"] = False
                payload["first_error"] = "AGB_UNSUPPORTED"
                payload["message"] = (f"该能力还不支持（{e}）；"
                                      "VM 不会静默退回 Python，要兜底请显式传 backend=py")
                return payload
        else:
            try:
                payload["stdout"] = vm_backend.run(
                    agb, res.lang, modules,
                    allowed_effects=allow_effects, allowed_tools=allow_tools)
            except vm_backend.VmError as e:
                payload["ok"] = False
                payload["first_error"] = e.code
                payload["message"] = e.message
                payload["failed_at"] = {"file": res.path.name, "line": e.line,
                                        "function": e.fn}
            return payload

    ok, diag, out = pipeline.run_result(res)
    payload["stdout"] = out
    if not ok and diag is not None:
        payload["ok"] = False
        payload["diagnostics"].append(_diag(diag))
        payload["first_error"] = diag.code
        payload["failed_at"] = {"file": diag.file, "line": diag.line,
                                "function": diag.stack[-1]["fn"] if diag.stack else None}
    return payload


def tool_run(args: dict) -> dict:
    from compiler import bytecode, vm as vm_backend

    path = Path(_file(args))
    if path.suffix.lower() == ".agb":
        payload = {"ok": True, "stdout": ""}
        try:
            agb = bytecode.decode(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            payload.update(ok=False, first_error="AGB_BAD_FILE",
                           message=f"读不了 AGB 文件：{e}")
            return payload
        modules = {}
        for p in sorted(path.parent.glob("*.agb")):
            if p.resolve() == path.resolve():
                continue
            try:
                m = bytecode.decode(p.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if m.get("module") != agb.get("module"):
                modules[m["module"]] = m
        try:
            payload["stdout"] = vm_backend.run(
                agb, agb.get("lang") or "zh", modules,
                allowed_effects=_allow_set(args, "allow_effects"),
                allowed_tools=_allow_set(args, "allow_tools"))
        except vm_backend.VmError as e:
            payload.update(ok=False, first_error=e.code, message=e.message,
                           failed_at={"file": path.name, "line": e.line,
                                      "function": e.fn})
        return payload

    res = pipeline.compile_file(_file(args))
    return _run_payload(res, args.get("backend") or "vm",
                        _allow_set(args, "allow_effects"),
                        _allow_set(args, "allow_tools"))


def tool_run_source(args: dict) -> dict:
    source = args["source"]
    filename = args.get("filename") or "main.agl"
    res = pipeline.compile_source(source, filename)
    return _run_payload(res, args.get("backend") or "vm",
                        _allow_set(args, "allow_effects"),
                        _allow_set(args, "allow_tools"))


def tool_convert(args: dict) -> dict:
    from compiler import convert as convert_mod

    path = _file(args)
    to = args.get("to")
    if to not in ("zh", "en"):
        raise ValueError("to 只能是 zh / en")
    src = path.read_text(encoding="utf-8-sig")
    try:
        out = convert_mod.convert(src, path.name, to)
    except Exception as e:  # AglError 带诊断
        diag = getattr(e, "diag", None)
        return {"ok": False, "file": path.name,
                "diagnostics": [_diag(diag)] if diag else [],
                "error": str(diag.message_zh) if diag else str(e)}
    payload = {"ok": True, "file": path.name, "to": to, "source": out}
    if args.get("write"):
        suffix = "_zh" if to == "zh" else "_en"
        target = path.with_name(path.stem + suffix + path.suffix)
        target.write_text(out, encoding="utf-8")
        payload["written"] = str(target)
    return payload


def tool_summary(args: dict) -> dict:
    res = pipeline.compile_file(_file(args))
    payload = _result(res)
    if res.ok:
        payload["summary_zh"] = summarize(res.ir, res.lang)
    return payload


def tool_doctor(args: dict) -> dict:
    from compiler.cli import doctor

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = doctor.run(verbose=False, as_json=True)
    try:
        report = json.loads(buf.getvalue())
    except json.JSONDecodeError:
        report = {"ok": code == 0, "raw": buf.getvalue()}
    report["exit_code"] = code
    return report


def tool_version(args: dict) -> dict:
    """版本查询 + 可选 GitHub 更新检查。

    热更新的现实：MCP stdio 服务器是客户端拉起的子进程，长连接里代码文件
    变了也不会自动重载（Python 已 import 的模块不会换血）。真正的「更新」
    是：换源码/装新版 → 重启 MCP 客户端 → 重连即加载新代码，配置不用改。
    这里只负责告诉你「现在什么版本、GitHub 有没有更新的」，不替客户端做
    半新半旧的静默热替换。
    """
    payload = {
        "ok": True,
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "languageVersion": SERVER_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "updateGuide": "见 docs/安装与更新.md：换新版源码后重启 MCP 客户端即生效"
                       "（stdio 长连接，重连加载新代码，客户端配置不用改）",
    }
    if not args.get("checkUpdate"):
        payload["updateCheck"] = "skipped"
        return payload
    if not GITHUB_RELEASES_URL:
        payload["updateCheck"] = "not_configured"
        payload["latest"] = None
        return payload
    import urllib.request

    try:
        with urllib.request.urlopen(GITHUB_RELEASES_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        latest = (data or [{}])[0].get("tag_name") or "unknown"
        payload["updateCheck"] = "ok"
        payload["latest"] = latest
        payload["updateAvailable"] = latest != SERVER_VERSION
    except Exception:
        # 网络失败/非 GitHub API 形态都静默降级：不把「检查不了」当成错误
        payload["updateCheck"] = "unavailable"
        payload["latest"] = None
    return payload


_HANDLERS = {
    "agl_card": tool_card,
    "agl_check": tool_check,
    "agl_check_source": tool_check_source,
    "agl_build": tool_build,
    "agl_run": tool_run,
    "agl_run_source": tool_run_source,
    "agl_convert": tool_convert,
    "agl_summary": tool_summary,
    "agl_version": tool_version,
    "agl_doctor": tool_doctor,
}


def _workspace_root() -> Path:
    """MCP 工具可访问的工作区根目录。

    默认是启动服务器时的当前目录；需要显式扩大范围时设置
    `AGL_MCP_WORKSPACE`，而不是让任意绝对路径直接穿透到宿主文件系统。
    """
    import os

    raw = os.environ.get("AGL_MCP_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path.cwd()
    return root.resolve()


def _inside_workspace(path: Path) -> Path:
    root = _workspace_root()
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = root / resolved
    resolved = resolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(
            f"路径不在 MCP 工作区 {root} 内：{resolved}；"
            "需要更大范围时设置 AGL_MCP_WORKSPACE"
        )
    return resolved


def _file(args: dict) -> Path:
    raw = args.get("file")
    if not raw:
        raise ValueError("缺少参数 file（AGL 源文件路径）")
    return _inside_workspace(Path(raw))


# ── JSON-RPC 分发 ─────────────────────────────────────────────────────
class Server:
    """不碰 stdin/stdout 的纯逻辑层，方便体检直接调用。"""

    def handle(self, req: dict) -> dict | None:
        """处理一条 JSON-RPC 消息。通知（没有 id）返回 None。"""
        method = req.get("method")
        rid = req.get("id")
        is_notification = "id" not in req or rid is None

        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "languageVersion": SERVER_VERSION,
                "instructions": INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "initialized",
                      "notifications/cancelled"):
            return None
        if method == "ping":
            return self._ok(rid, {})
        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            return self._call(rid, req.get("params") or {})
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"没有这个方法：{method}"},
        }

    def _call(self, rid, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32602, "message": f"没有这个工具：{name}",
                          "data": {"tools": sorted(_HANDLERS)}},
            }
        try:
            payload = handler(args)
        except ValueError as e:
            return self._tool_result(rid, {"ok": False, "error": str(e)}, is_error=True)
        except Exception as e:  # 工具内部炸了也要按协议回，不能让客户端等
            return self._tool_result(
                rid, {"ok": False, "error": f"{type(e).__name__}: {e}"}, is_error=True)

        is_error = payload.get("ok") is False
        return self._tool_result(rid, payload, is_error=is_error)

    @staticmethod
    def _tool_result(rid, payload: dict, is_error: bool) -> dict:
        return Server._ok(rid, {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        })

    @staticmethod
    def _ok(rid, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}


# ── stdio 传输 ────────────────────────────────────────────────────────
def _read_message(stream) -> dict | None:
    """读一条消息。支持「一行一条 JSON」和「Content-Length 头 + 正文」两种。"""
    while True:
        line = stream.readline()
        if not line:
            return None
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        if text.lower().startswith("content-length:"):
            try:
                n = int(text.split(":", 1)[1].strip())
            except ValueError:
                continue
            while True:                      # 吃掉剩余头部直到空行
                h = stream.readline()
                if not h or not h.strip():
                    break
            body = stream.read(n)
            return json.loads(body.decode("utf-8", "replace"))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 不是 JSON 就跳过，绝不把垃圾当消息回出去
            continue


def _write_message(stream, msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False)
    stream.write(data.encode("utf-8") + b"\n")
    stream.flush()


def serve() -> int:
    server = Server()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        req = _read_message(stdin)
        if req is None:
            return 0
        resp = server.handle(req)
        if resp is not None:
            _write_message(stdout, resp)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if argv and argv[0] in ("--version", "-V"):
        print(f"{SERVER_NAME} {SERVER_VERSION}（协议 {PROTOCOL_VERSION}）")
        return 0
    if argv and argv[0] in ("--list", "list"):
        for t in TOOLS:
            print(f"{t['name']:<14} {t['description'].splitlines()[0]}")
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
