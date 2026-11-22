# -*- coding: utf-8 -*-
"""假的 MCP stdio 服务器：给自检用，按「一行一条 JSON-RPC」应答。

支持 initialize / notifications/initialized / tools/call 三件事，
不实现 tools/list —— 运行时只用到前三个。
"""
import json
import sys


def reply(payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            reply({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stdio", "version": "0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "天气":
                city = args.get("城市") or "未知"
                reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"{city}：晴，23度"}]}})
            else:
                reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "没有这个工具"}],
                    "isError": True}})
        else:
            if rid is not None:
                reply({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32601, "message": "unknown method"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
