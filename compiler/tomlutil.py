# -*- coding: utf-8 -*-
"""兼容 Python 3.10 的最小 TOML 读取入口。

3.11 以后直接转发标准库 tomllib；3.10 只实现本仓库实际用到的 TOML 子集
（表、数组表、双引号字符串、整数、布尔、内联字符串/整数数组、行注释）。
不为了兼容旧 Python 引入第三方运行时依赖。
"""
from __future__ import annotations


try:  # pragma: no cover - 取决于宿主 Python 版本
    import tomllib

    def load(fp):
        return tomllib.load(fp)

    def loads(text):
        return tomllib.loads(text)

    NATIVE = True
except ModuleNotFoundError:  # Python 3.10
    import re as _re

    NATIVE = False

    def _split_dotted(name: str):
        return [p.strip().strip('"') for p in name.split(".")]

    def _make_table(root, name: str):
        cur = root
        for part in _split_dotted(name):
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        return cur

    def _parse_string(token: str):
        token = token.strip()
        if token.startswith("'") and token.endswith("'"):
            return token[1:-1]
        if token.startswith('"') and token.endswith('"'):
            body = token[1:-1]
            out = []
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == "\\" and i + 1 < len(body):
                    nxt = body[i + 1]
                    if nxt == "n":
                        out.append("\n"); i += 2; continue
                    if nxt == "t":
                        out.append("\t"); i += 2; continue
                    if nxt == "r":
                        out.append("\r"); i += 2; continue
                    if nxt in {'"', "\\"}:
                        out.append(nxt); i += 2; continue
                out.append(ch)
                i += 1
            return "".join(out)
        raise ValueError(f"不支持的 TOML 字面量：{token}")

    def _strip_comment(value: str):
        out = []
        in_str = False
        quote = ""
        prev = ""
        for ch in value:
            if in_str:
                out.append(ch)
                if ch == quote and prev != "\\":
                    in_str = False
            elif ch in {'"', "'"}:
                in_str = True
                quote = ch
                out.append(ch)
            elif ch == "#":
                break
            else:
                out.append(ch)
            prev = ch
        return "".join(out).strip()

    def _parse_top_level(text: str):
        parts = []
        depth = 0
        in_str = False
        quote = ""
        cur = []
        prev = ""
        for ch in text:
            if in_str:
                cur.append(ch)
                if ch == quote and prev != "\\":
                    in_str = False
            elif ch in {'"', "'"}:
                in_str = True
                quote = ch
                cur.append(ch)
            elif ch in "[{":
                depth += 1
                cur.append(ch)
            elif ch in "]}":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
            prev = ch
        if "".join(cur).strip():
            parts.append("".join(cur).strip())
        return parts

    def _parse_value(token: str):
        token = _strip_comment(token)
        if token.startswith("{"):
            inner = token.rstrip("}")[1:]
            result = {}
            for part in _parse_top_level(inner):
                key, value = part.split("=", 1)
                result[key.strip().strip('"')] = _parse_value(value)
            return result
        if token.startswith("["):
            inner = token.rstrip("]")[1:]
            if not inner.strip():
                return []
            return [_parse_value(x) for x in _parse_top_level(inner) if x.strip()]
        if token in {"true", "false"}:
            return token == "true"
        if token.startswith(('"', "'")):
            return _parse_string(token)
        if _re.fullmatch(r"[+-]?\d+", token):
            return int(token)
        raise ValueError(f"不支持的 TOML 值：{token}")

    def loads(text: str):
        root: dict = {}
        current = root
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            lineno = i + 1
            raw = lines[i]
            line = raw.strip()
            i += 1
            if not line or line.startswith("#"):
                continue
            if line.startswith("[["):
                end = line.rindex("]]")
                parts = _split_dotted(line[2:end])
                cur = root
                for part in parts[:-1]:
                    cur = cur.setdefault(part, {})
                last = parts[-1]
                arr = cur.get(last)
                if not isinstance(arr, list):
                    arr = []
                    cur[last] = arr
                entry: dict = {}
                arr.append(entry)
                current = entry
            elif line.startswith("["):
                end = line.rindex("]")
                current = _make_table(root, line[1:end])
            elif "=" in line:
                key, value = line.split("=", 1)
                key = key.strip().strip('"')
                value = value.strip()
                if value.startswith('"""') or value.startswith("'''"):
                    quote = value[:3]
                    body = value[3:]
                    if quote not in body:
                        block = [body]
                        while i < len(lines):
                            nxt = lines[i]
                            i += 1
                            if quote in nxt:
                                block.append(nxt.split(quote, 1)[0])
                                break
                            block.append(nxt)
                        body = "\n".join(block)
                        if body.startswith("\n"):
                            body = body[1:]
                    else:
                        body = body.split(quote, 1)[0]
                    current[key] = body
                else:
                    current[key] = _parse_value(value)
            else:
                raise ValueError(f"第 {lineno} 行不是合法 TOML：{line}")
        return root

    def load(fp):
        return loads(fp.read().decode("utf-8") if isinstance(fp.read(0), bytes) else fp.read())
