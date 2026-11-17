# -*- coding: utf-8 -*-
"""AGL 可编程网页服务：把每个 HTTP 请求交给 AGL 处理函数。

设计目标很小，但必须是真实可用的：
  · 请求 → 一个映射表（方法 / 路径 / 查询 / Cookie / 请求体 / 表单 / 头）；
  · 处理函数 → 一个映射表（状态 / 类型 / 内容 / 文件 / 头），也允许直接返回文本；
  · JSON、表单、CORS 这些网页应用最常碰到的事，在运行时统一处理；
  · 不引入第三方依赖，单进程、单线程，和 AGL VM 的状态模型一致。
"""
from __future__ import annotations

import json
import mimetypes
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


def _flat_params(query: str) -> dict:
    """同名参数取最后一个值；同时保留完整列表，避免丢信息。"""
    parsed = parse_qs(query, keep_blank_values=True)
    flat = {}
    for key, values in parsed.items():
        flat[key] = values[-1] if values else ""
    return flat


def _cookies(value: str) -> dict:
    """把 Cookie 请求头解析成普通映射表；坏值按空表处理。"""
    jar = SimpleCookie()
    try:
        jar.load(value or "")
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def _response_parts(result):
    """兼容中文响应键和英文响应键，默认返回一段 HTML。"""
    if isinstance(result, str):
        return 200, "text/html; charset=utf-8", result, {}, None
    if not isinstance(result, dict):
        return 500, "text/plain; charset=utf-8", "处理函数没有返回映射表或文本", {}, None
    status = result.get("状态", result.get("status", 200))
    content_type = result.get(
        "类型", result.get("content_type", "text/html; charset=utf-8"))
    body = result.get("内容", result.get("body", ""))
    file_path = result.get("文件", result.get("file"))
    headers = result.get("头", result.get("headers", {}))
    explicit_type = "类型" in result or "content_type" in result
    # 返回映射表 / 列表时，默认按 JSON 响应；显式写了「类型」就尊重处理函数。
    if not explicit_type and isinstance(body, (dict, list)):
        content_type = "application/json; charset=utf-8"
    if file_path:
        file_path = str(file_path)
        if not explicit_type:
            guessed = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            if guessed.startswith("text/") and "charset=" not in guessed.lower():
                guessed += "; charset=utf-8"
            content_type = guessed
    else:
        file_path = None
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        headers = {}
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 500
    if not isinstance(content_type, str) or not content_type:
        content_type = "text/html; charset=utf-8"
    return status, content_type, body, headers, file_path


def serve_web_app(port: int, handler, lang: str = "zh",
                  host: str = "127.0.0.1") -> None:
    """启动可编程 HTTP 服务。handler(request_map) 返回响应。

    默认只监听回环地址；确实要局域网访问时，在 AGL 里显式传第三个参数
    `"0.0.0.0"`，不要让“本机预览”悄悄变成对全网开放。
    """
    zh = lang == "zh"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _body_text(self):
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return ""
            return self.rfile.read(length).decode("utf-8", "replace")

        def _request(self, method: str):
            parsed = urlsplit(self.path)
            body = self._body_text() if method not in ("GET", "HEAD") else ""
            content_type = self.headers.get("Content-Type", "")
            form = {}
            if "application/x-www-form-urlencoded" in content_type:
                form = _flat_params(body)
            return {
                "方法": method,
                "路径": unquote(parsed.path) or "/",
                "原始路径": self.path,
                "查询": _flat_params(parsed.query),
                "查询串": parsed.query,
                "Cookie": _cookies(self.headers.get("Cookie", "")),
                "请求体": body,
                "表单": form,
                "头": {k: v for k, v in self.headers.items()},
                "内容类型": content_type,
            }

        def _send(self, status, content_type, body, extra_headers=None, file_path=None):
            if file_path:
                data = Path(file_path).read_bytes()
            else:
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, ensure_ascii=False)
                elif body is None:
                    body = ""
                elif not isinstance(body, str):
                    body = str(body)
                data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for key, value in extra_headers.items():
                    if key.lower() in ("content-type", "content-length"):
                        continue
                    self.send_header(str(key), str(value))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _dispatch(self, method: str):
            if method == "OPTIONS":
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                result = handler(self._request(method))
                status, content_type, body, headers, file_path = _response_parts(result)
                self._send(status, content_type, body, headers, file_path)
            except Exception as exc:  # 不让一个坏请求带崩整个服务
                message = "网页处理函数出错：" + str(exc) if zh else "web handler failed: " + str(exc)
                self._send(500, "text/plain; charset=utf-8", message, {})

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def log_message(self, *args):
            pass

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((host, int(port)), Handler)
    actual_port = server.server_address[1]
    print(("网页应用已启动：http://localhost:" if zh else "web app started: http://localhost:") + str(actual_port))
    server.serve_forever()
