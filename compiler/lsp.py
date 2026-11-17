# -*- coding: utf-8 -*-
"""agl-lsp：AGL 语言服务器（LSP 3.17 子集，零依赖）。

为什么自己写而不用 pygls：这个仓库的硬规则是零第三方依赖 ——
别的 AI 拿到就能跑，不该先让它装一堆包。LSP 的传输层只有一条规矩
（`Content-Length` 头 + JSON-RPC），几百行就够，没必要引框架。

它做什么：
  · 诊断：边打字边编译缓冲区，错误按 AGL 行号标在编辑器里；
  · 悬停：关键字的中英对照、函数的完整签名与效应；
  · 补全：当前分支的关键字、内建、本文件的函数、`.` 后的内置属性；
  · 大纲：函数列表；
  · 跳转：从调用处跳到函数定义。

不做什么（有意）：
  · 不做格式化、不做重命名 —— 那些要在 IR 上做，属阶段 2 的后续；
  · 不碰磁盘上的文件，缓冲区内容永远优先（用户没保存的代码也要报错）。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from compiler import diagnostics as diag_mod
from compiler.cli import pipeline
from compiler.diagnostics import Diagnostic
from compiler.lexer import (
    LANG_EN, LANG_ZH, IDENT, KEYWORD, OP, KeywordTable, detect_lang, tokenize,
)

SERVER_NAME = "agl-lsp"
SERVER_VERSION = "0.4.0"

# LSP 的 DiagnosticSeverity
_SEV = {"error": 1, "warning": 2, "info": 3, "hint": 4}

# LSP 的 CompletionItemKind
_KW_ITEM = 14
_FN_ITEM = 3
_PROP_ITEM = 10

# 文档同步方式：1 = 全量替换（文件都很小，全量最不容易出错）
_SYNC_FULL = 1


# ══════════════════════════════════════════════════════════════════════
# 位置换算：LSP 的 character 是 UTF-16 码元，Python 的列是字符
# 汉字在 BMP 里占 1 个码元，所以平时两者相等；emoji 之类会差 2，
# 不换算的话光标会跑偏 —— 这类偏移极难发现，索性一次换算干净。
# ══════════════════════════════════════════════════════════════════════
def _u16_len(s: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


def _char_index(line_text: str, u16_col: int) -> int:
    n = 0
    for i, ch in enumerate(line_text):
        if n >= u16_col:
            return i
        n += 2 if ord(ch) > 0xFFFF else 1
    return len(line_text)


@dataclass
class Doc:
    uri: str
    path: Path
    text: str
    version: int = 0
    lang: str = LANG_ZH
    result: pipeline.Result | None = None
    tokens: list = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    def offset_at(self, line: int, character: int) -> int:
        """LSP 位置（0 基）→ 源码字符偏移。"""
        lines = self.lines
        if line >= len(lines):
            return len(self.text)
        idx = _char_index(lines[line], character)
        return sum(len(x) + 1 for x in lines[:line]) + idx

    def position_of(self, line1: int, char_col0: int) -> dict:
        """AGL 的 (1 基行, 0 基列) → LSP 位置。"""
        lines = self.lines
        i = line1 - 1
        if i < 0 or i >= len(lines):
            return {"line": max(0, i), "character": 0}
        col = min(char_col0, len(lines[i]))
        return {"line": i, "character": _u16_len(lines[i][:col])}


def path_to_uri(p: Path) -> str:
    from urllib.parse import quote

    return "file:///" + quote(p.resolve().as_posix(), safe="/:")


def uri_to_path(uri: str) -> Path:
    from urllib.parse import unquote, urlparse

    u = urlparse(uri)
    raw = unquote(u.path)
    if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]          # Windows: /D:/x → D:/x
    return Path(raw)


# ══════════════════════════════════════════════════════════════════════
# 编译与诊断
# ══════════════════════════════════════════════════════════════════════
def _compile(doc: Doc) -> pipeline.Result:
    """编译缓冲区内容。磁盘上的模块关系照旧解析，但入口用内存里的文本。"""
    try:
        if doc.path.exists():
            return pipeline.compile_project(doc.path, override_source=doc.text)
        return pipeline.compile_source(doc.text, doc.path.name)
    except Exception as e:  # 编译器自身炸了，也不能让编辑器跟着崩
        return pipeline.Result(path=doc.path, source=doc.text, diagnostics=[
            Diagnostic(code="AGL-E-0503", message_zh="编译器内部出错，这段代码没能检查完",
                       file=doc.path.name, line=1, col=0, end_col=1,
                       cause=f"{type(e).__name__}: {e}",
                       fix_hints=[diag_mod.FixHint("report_bug", doc.path.name,
                                                   "把这段代码和这条消息一起上报")])])


def _tokens(doc: Doc) -> list:
    if doc.tokens:
        return doc.tokens
    try:
        doc.tokens = tokenize(doc.text, doc.path.name)
    except diag_mod.AglError as e:
        # 词法没走完也要给出已识别的前半段，悬停才有东西可看
        doc.tokens = []
    return doc.tokens


def _lsp_diags(lines: list[str], diags: list[Diagnostic]) -> list[dict]:
    """把诊断转成 LSP 形态。lines 是该诊断所属文件的源码行（用来换算 UTF-16 列）。"""
    out: list[dict] = []
    for d in diags:
        msg = d.message_zh
        if d.fix_hints:
            msg += "\n建议：" + d.fix_hints[0].suggest
        if d.cause and d.severity != "error":
            msg += f"\n原因：{d.cause}"
        out.append({
            "range": {"start": _pos(lines, d.line, d.col),
                      "end": _pos(lines, d.line, max(d.col + 1, d.end_col))},
            "severity": _SEV.get(d.severity, 1),
            "code": d.code,
            "source": "agl",
            "message": msg,
        })
    return out


def _pos(lines: list[str], line1: int, col0: int) -> dict:
    i = line1 - 1
    if i < 0 or i >= len(lines):
        return {"line": max(0, i), "character": 0}
    return {"line": i, "character": _u16_len(lines[i][:min(col0, len(lines[i]))])}


# ══════════════════════════════════════════════════════════════════════
# 悬停 / 补全 / 大纲 / 跳转
# ══════════════════════════════════════════════════════════════════════
def _token_at(doc: Doc, offset: int):
    for t in _tokens(doc):
        if t.offset <= offset < t.end_offset:
            return t
    return None


def _functions(doc: Doc) -> list[dict]:
    res = doc.result
    if res is None or not res.ir:
        return []
    try:
        return res.ir["program"]["modules"][0]["functions"]
    except (KeyError, IndexError):
        return []


_TYPE_ZH = {
    "Int": "整数", "Float": "浮点数", "String": "文本", "Bool": "真假值",
    "List": "列表", "Map": "映射表", "Any": "任意", "Void": "无", "Result": "结果",
}
_EFFECT_ZH = {
    "IO": "读写外部", "Net": "网络", "model": "模型调用",
    "Time": "时间", "External": "外部进程",
}
# 关键字类别 → 给人看的说法（悬停里不许出现表里的英文 kind）
_KIND_ZH = {
    "decl": "声明", "modifier": "修饰符", "stmt": "语句", "type": "类型",
    "effect": "效应标签", "literal": "真假值", "builtin": "入口与内建",
}


def _type_text(t: dict, lang: str) -> str:
    name = (t or {}).get("type", "Any")
    if name == "List":
        return f"{_type_text(t.get('elem') or {}, lang)} 的列表" if lang == LANG_ZH \
            else f"List<{_type_text(t.get('elem') or {}, lang)}>"
    if name == "Map":
        k = _type_text(t.get("key") or {}, lang)
        v = _type_text(t.get("value") or {}, lang)
        return f"从{k}到{v}的映射表" if lang == LANG_ZH else f"Map<{k}, {v}>"
    if name == "Result":
        ok = _type_text(t.get("ok") or {}, lang)
        err = _type_text(t.get("err") or {}, lang)
        return f"结果（成功时{ok}，失败时{err}）" if lang == LANG_ZH else f"Result<{ok}, {err}>"
    if lang == LANG_ZH:
        return _TYPE_ZH.get(name, name)
    return {"String": "String", "Void": "Void"}.get(name, name)


def _signature(fn: dict, lang: str) -> str:
    tbl = KeywordTable.load()
    kw_fn = tbl.text_for("fn", lang)
    kw_let = tbl.text_for("let", lang)
    params = ", ".join(f"{p['name']}: {_type_text(p['type'], lang)}" for p in fn["params"])
    ret = "" if (fn.get("return") or {}).get("type") in (None, "Void") \
        else f" -> {_type_text(fn['return'], lang)}"
    prefix = ("异步 " if lang == LANG_ZH else "async ") if fn.get("is_async") else ""
    return f"{prefix}{kw_fn} {fn['name']}({params}){ret}"


def hover(doc: Doc, line: int, character: int) -> dict | None:
    tbl = KeywordTable.load()
    off = doc.offset_at(line, character)
    tok = _token_at(doc, off)
    if tok is None:
        return None

    rng = {"start": doc.position_of(tok.line, tok.col),
           "end": doc.position_of(tok.line, tok.col + len(tok.text))}

    if tok.kind == KEYWORD:
        zh = tbl.text_for(tok.canonical, LANG_ZH)
        en = tbl.text_for(tok.canonical, LANG_EN)
        kind = _KIND_ZH.get(tbl.kind_of(tok.canonical) or "", "")
        stage = tbl.stage_of(zh, LANG_ZH)
        body = (f"**`{zh}`** / `{en}`　—　{kind}　·　阶段 {stage} 已启用"
                if doc.lang == LANG_ZH else
                f"**`{en}`** / `{zh}`　—　{kind}　·　available in stage {stage}")
        return {"contents": {"kind": "markdown", "value": body}, "range": rng}

    if tok.kind == OP:
        word = tbl.op_text_for(tok.canonical, LANG_ZH)
        if word and word != tok.canonical:
            body = f"**`{word}`** / `{tok.canonical}`　—　运算符"
            return {"contents": {"kind": "markdown", "value": body}, "range": rng}
        return None

    # `.长度` 这类：看它前面是不是紧挨着 `.`
    prev = doc.text[:tok.offset].rstrip()
    if prev.endswith(".") and tok.kind == IDENT:
        a = tbl.attr_entry(tok.text)
        if a is not None:
            applies = ("文本 / 列表 / 映射表" if doc.lang == LANG_ZH
                       else "text / list / map")
            if doc.lang == LANG_ZH:
                body = f"**`.{a.zh}`** / `.{a.en}`　—　只读属性 · 适用于{applies}"
            else:
                body = f"**`.{a.en}`** / `.{a.zh}`　—　read-only · {applies}"
            return {"contents": {"kind": "markdown", "value": body}, "range": rng}

    if tok.kind == IDENT:
        for fn in _functions(doc):
            if fn["name"] == tok.text:
                sig = _signature(fn, doc.lang)
                eff = fn.get("effects") or []
                if doc.lang == LANG_ZH:
                    eff_text = "、".join(_EFFECT_ZH.get(e, e) for e in eff) if eff else "纯函数（不碰外部）"
                    body = f"**{sig}**\n\n效应：{eff_text}"
                else:
                    eff_text = ", ".join(_EFFECT_ZH.get(e, e) for e in eff) if eff else "pure (no external effects)"
                    body = f"**{sig}**\n\nEffects: {eff_text}"
                return {"contents": {"kind": "markdown", "value": body}, "range": rng}
        # 内建函数：用户没定义它，但它确实存在。悬停里必须说清"它会失败时怎么返回"。
        bi = tbl.builtin_entry(tok.text)
        if bi is not None and not prev.endswith("."):
            params = "、".join(bi.params + (("…",) if bi.rest else ()))
            if doc.lang == LANG_ZH:
                fails = ("转不了返回「失败」，不抛异常" if bi.may_fail
                         else "出问题直接报错（越界不静默截断）")
                body = (f"**`{bi.zh}`** / `{bi.en}`　—　内建函数（不用导入）\n\n"
                        f"参数：{params}　→　{bi.ret}\n\n{bi.summary_zh}\n\n{fails}")
            else:
                fails = ("returns Err instead of throwing" if bi.may_fail
                         else "raises on bad input (never truncates silently)")
                body = (f"**`{bi.en}`** / `{bi.zh}`　—　built-in (no import needed)\n\n"
                        f"params: {params}　→　{bi.ret}\n\n{bi.summary_en}\n\n{fails}")
            return {"contents": {"kind": "markdown", "value": body}, "range": rng}
    return None


def completion(doc: Doc, line: int, character: int) -> dict:
    tbl = KeywordTable.load()
    lines = doc.lines
    text = lines[line] if 0 <= line < len(lines) else ""
    before = text[: _char_index(text, character)]
    lang = doc.lang

    # `.` 后面只可能是内置只读属性：这时候把整张关键字表倒出来是噪音。
    # 已经敲了一半（`.长`）就按前缀过滤，客户端不必再二次筛。
    m = re.search(r"\.([^.()\[\]{}\s]*)$", before)
    if m:
        prefix = m.group(1)
        items = []
        for a in tbl.attrs:
            word = a.zh if lang == LANG_ZH else a.en
            other = a.en if lang == LANG_ZH else a.zh
            if prefix and not word.startswith(prefix):
                continue
            items.append({
                "label": word, "kind": _PROP_ITEM, "insertText": word,
                "detail": f"只读属性 / .{other}" if lang == LANG_ZH else f"read-only / .{other}",
                "documentation": "文本、列表、映射表都有长度" if lang == LANG_ZH
                                 else "text, list and map all have a length",
            })
        return {"isIncomplete": False, "items": items}

    items: list[dict] = []
    seen: set[str] = set()

    def add(label: str, kind: int, detail: str, sort: str) -> None:
        if label in seen:      # 内建（打印）既是关键字又是函数，只留一条
            return
        seen.add(label)
        items.append({"label": label, "kind": kind, "insertText": label,
                      "detail": detail, "sortText": sort})

    for fn in _functions(doc):
        add(fn["name"], _FN_ITEM, _signature(fn, lang), "0" + fn["name"])
    # 内建函数：不用定义就能调，排在用户函数之后、关键字之前
    for bi in tbl.builtins:
        word = bi.zh if lang == LANG_ZH else bi.en
        other = bi.en if lang == LANG_ZH else bi.zh
        desc = bi.summary_zh if lang == LANG_ZH else bi.summary_en
        add(word, _FN_ITEM, f"内建 · {desc} · {other}" if lang == LANG_ZH
            else f"built-in · {desc} · {other}", "0b" + bi.canonical)
    for k in tbl.keywords:
        if k.stage > 1:
            continue
        word = k.zh if lang == LANG_ZH else k.en
        other = k.en if lang == LANG_ZH else k.zh
        add(word, _KW_ITEM, f"{_KIND_ZH.get(k.kind, k.kind)} · {other}", f"1{k.kind}")
    return {"isIncomplete": False, "items": items}


def document_symbols(doc: Doc) -> list[dict]:
    out: list[dict] = []
    for fn in _functions(doc):
        sp = fn.get("span") or {}
        if not sp:
            continue
        rng = {"start": {"line": sp["line"] - 1, "character": sp["col"]},
               "end": {"line": sp.get("endLine", sp["line"]) - 1,
                       "character": sp.get("endCol", sp["col"] + 1)}}
        out.append({
            "name": fn["name"],
            "detail": _signature(fn, doc.lang),
            "kind": 12,                      # Function
            "range": rng,
            "selectionRange": rng,
        })
    return out


def definition(doc: Doc, line: int, character: int) -> list[dict]:
    off = doc.offset_at(line, character)
    tok = _token_at(doc, off)
    if tok is None or tok.kind != IDENT:
        return []
    for fn in _functions(doc):
        if fn["name"] != tok.text:
            continue
        sp = fn.get("span") or {}
        if not sp:
            continue
        return [{
            "uri": doc.uri,
            "range": {"start": {"line": sp["line"] - 1, "character": sp["col"]},
                      "end": {"line": sp.get("endLine", sp["line"]) - 1,
                              "character": sp.get("endCol", sp["col"] + 1)}},
        }]
    return []


# ══════════════════════════════════════════════════════════════════════
# 服务器
# ══════════════════════════════════════════════════════════════════════
class Server:
    """不碰 stdin/stdout 的纯逻辑层：输入一条消息，返回要发出去的所有消息。"""

    def __init__(self) -> None:
        self.docs: dict[str, Doc] = {}
        self.shutdown_requested = False

    # ── 入口 ────────────────────────────────────────────────────────
    def handle(self, msg: dict) -> list[dict]:
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg or rid is None

        try:
            handler = getattr(self, "_m_" + method.replace("/", "_"), None) if method else None
            if handler is not None:
                out = handler(params)
                if is_notification or method.startswith(("$/", "textDocument/did",
                                                          "textDocument/will", "initialized",
                                                          "exit", "workspace/did")):
                    return out or []
                return self._result(rid, out)
            if method == "initialize":
                return self._result(rid, self._initialize())
            if is_notification:
                return []
            return [self._error(rid, -32601, f"不支持的方法：{method}")]
        except Exception as e:  # 任何一格出错都要按协议回，不能卡死编辑器
            if is_notification:
                return []
            return [self._error(rid, -32603, f"{type(e).__name__}: {e}")]

    # ── 生命周期 ────────────────────────────────────────────────────
    def _initialize(self) -> dict:
        return {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": _SYNC_FULL,
                                     "save": {"includeText": False}},
                "hoverProvider": True,
                "completionProvider": {"triggerCharacters": [".", " "],
                                       "resolveProvider": False},
                "documentSymbolProvider": True,
                "definitionProvider": True,
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _m_initialized(self, params: dict) -> list[dict]:
        return []

    def _m_shutdown(self, params: dict):
        self.shutdown_requested = True
        return None

    def _m_exit(self, params: dict) -> list[dict]:
        raise SystemExit(0)

    # ── 文档同步 ────────────────────────────────────────────────────
    def _m_textDocument_didOpen(self, params: dict) -> list[dict]:
        td = params.get("textDocument") or {}
        doc = Doc(uri=td.get("uri", ""), path=uri_to_path(td.get("uri", "")),
                  text=td.get("text", ""), version=td.get("version", 0))
        doc.lang = detect_lang(doc.text) or LANG_ZH
        self.docs[doc.uri] = doc
        return self._refresh(doc)

    def _m_textDocument_didChange(self, params: dict) -> list[dict]:
        uri = (params.get("textDocument") or {}).get("uri", "")
        doc = self.docs.get(uri)
        if doc is None:
            return []
        changes = params.get("contentChanges") or []
        if changes:
            doc.text = changes[-1].get("text", doc.text)
        doc.version = (params.get("textDocument") or {}).get("version", doc.version + 1)
        doc.lang = detect_lang(doc.text) or doc.lang
        doc.tokens = []                    # 内容变了，缓存的 token 必须作废
        return self._refresh(doc)

    def _m_textDocument_didSave(self, params: dict) -> list[dict]:
        doc = self.docs.get((params.get("textDocument") or {}).get("uri", ""))
        return self._refresh(doc) if doc else []

    def _m_textDocument_didClose(self, params: dict) -> list[dict]:
        uri = (params.get("textDocument") or {}).get("uri", "")
        self.docs.pop(uri, None)
        return [{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                 "params": {"uri": uri, "diagnostics": []}}]

    # ── 查询 ────────────────────────────────────────────────────────
    def _m_textDocument_hover(self, params: dict) -> dict | None:
        doc, line, ch = self._at(params)
        return hover(doc, line, ch) if doc else None

    def _m_textDocument_completion(self, params: dict):
        doc, line, ch = self._at(params)
        return completion(doc, line, ch) if doc else {"isIncomplete": False, "items": []}

    def _m_textDocument_documentSymbol(self, params: dict):
        doc = self.docs.get((params.get("textDocument") or {}).get("uri", ""))
        return document_symbols(doc) if doc else []

    def _m_textDocument_definition(self, params: dict):
        doc, line, ch = self._at(params)
        return definition(doc, line, ch) if doc else []

    # ── 内部 ────────────────────────────────────────────────────────
    def _at(self, params: dict):
        uri = (params.get("textDocument") or {}).get("uri", "")
        doc = self.docs.get(uri)
        pos = params.get("position") or {}
        return doc, int(pos.get("line", 0)), int(pos.get("character", 0))

    def _refresh(self, doc: Doc) -> list[dict]:
        doc.result = _compile(doc)
        doc.tokens = []
        res = doc.result
        if res is None:
            return []
        out: list[dict] = []
        mine = [d for d in res.diagnostics if d.file == doc.path.name]
        out.append({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                    "params": {"uri": doc.uri,
                               "diagnostics": _lsp_diags(doc.lines, mine),
                               "version": doc.version}})
        # 依赖文件里的错误也要报出去，否则改了库文件、入口却说"没问题"。
        # 但只有它没在编辑器里打开时才替它报 —— 打开着的话它自己会报，
        # 两边都推同一个 URI 会让诊断闪一下又消失。
        for name, dep in (res.deps or {}).items():
            ds = [d for d in res.diagnostics if d.file == dep.path.name]
            if not ds:
                continue
            u = path_to_uri(dep.path)
            if u in self.docs:
                continue
            out.append({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                        "params": {"uri": u,
                                   "diagnostics": _lsp_diags(dep.source.splitlines(), ds)}})
        return out

    @staticmethod
    def _result(rid, result) -> list[dict]:
        return [{"jsonrpc": "2.0", "id": rid, "result": result}]

    @staticmethod
    def _error(rid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


# ══════════════════════════════════════════════════════════════════════
# stdio 传输（LSP 规定：Content-Length 头 + 空行 + JSON 正文）
# ══════════════════════════════════════════════════════════════════════
def _read_message(stream) -> dict | None:
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        text = line.decode("utf-8", "replace").strip()
        if not text:
            if length is not None:
                break
            continue
        if text.lower().startswith("content-length:"):
            try:
                length = int(text.split(":", 1)[1].strip())
            except ValueError:
                length = None
            continue
        # 容错：有的客户端不带头，直接发一行 JSON
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    if length is None:
        return None
    body = stream.read(length)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def _write_message(stream, msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
    stream.flush()


def serve() -> int:
    server = Server()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = _read_message(stdin)
        if msg is None:
            return 0
        try:
            out = server.handle(msg)
        except SystemExit:
            return 0
        for m in out or []:
            _write_message(stdout, m)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        return 0
    if argv and argv[0] in ("--selftest", "selftest"):
        return _selftest()
    return serve()


def _selftest() -> int:
    """不依赖编辑器的最小自检：握手 → 打开一份坏代码 → 必须报诊断。"""
    s = Server()
    s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    bad = "#语言=中文\n函数 主入口() {\n  打印(没定义的名字)\n}\n"
    uri = "file:///tmp/agl_selftest.agl"
    s.handle({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": uri, "languageId": "agl", "version": 1, "text": bad}}})
    doc = s.docs[uri]
    codes = [d.code for d in (doc.result.diagnostics if doc.result else [])]
    ok = bool(codes)
    print(f"{'✓' if ok else '✗'} LSP 自检：诊断 {codes}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
