#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖 IR 校验器 —— 只实现 spec/ir-schema.json 用到的那部分 JSON Schema 子集。

为什么不装 jsonschema：并行开发的环境不一定有外网/依赖。
本文件只用标准库，任何人拿到仓库即可验证自己产出的 IR 是否合法。

用法：
    python spec/validate_ir.py <xxx.agl.ir>
    python spec/validate_ir.py <目录>        # 校验目录下所有 .agl.ir

退出码：0 = 全部合法，1 = 有非法项
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("ir-schema.json")


class IrInvalid(Exception):
    pass


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise IrInvalid(f"不支持的 $ref：{ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _type_ok(inst: Any, t: str) -> bool:
    if t == "object":
        return isinstance(inst, dict)
    if t == "array":
        return isinstance(inst, list)
    if t == "string":
        return isinstance(inst, str)
    if t == "integer":
        return isinstance(inst, int) and not isinstance(inst, bool)
    if t == "number":
        return isinstance(inst, (int, float)) and not isinstance(inst, bool)
    if t == "boolean":
        return isinstance(inst, bool)
    if t == "null":
        return inst is None
    return True


def validate(inst: Any, schema: dict, root: dict, path: str = "$") -> None:
    """递归校验；不通过则抛出 IrInvalid（含精确路径）。"""
    if "$ref" in schema:
        validate(inst, _resolve(schema["$ref"], root), root, path)
        return

    if "oneOf" in schema:
        errs: list[str] = []
        for sub in schema["oneOf"]:
            try:
                validate(inst, sub, root, path)
                return
            except IrInvalid as e:
                errs.append(str(e))
        raise IrInvalid(f"{path}: 不匹配任何允许的节点类型（尝试 {len(errs)} 种均失败）")

    if "allOf" in schema:
        for sub in schema["allOf"]:
            validate(inst, sub, root, path)

    if "const" in schema and inst != schema["const"]:
        raise IrInvalid(f"{path}: 期望 {schema['const']!r}，实际 {inst!r}")

    if "enum" in schema and inst not in schema["enum"]:
        raise IrInvalid(f"{path}: 值 {inst!r} 不在允许范围 {schema['enum']}")

    t = schema.get("type")
    if t and not _type_ok(inst, t):
        raise IrInvalid(f"{path}: 期望类型 {t}，实际 {type(inst).__name__}")

    if t == "object" or isinstance(inst, dict):
        if not isinstance(inst, dict):
            return
        for key in schema.get("required", []):
            if key not in inst:
                raise IrInvalid(f"{path}: 缺少必填字段 `{key}`")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in inst:
                validate(inst[key], sub, root, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = set(inst) - set(props)
            if extra:
                raise IrInvalid(f"{path}: 出现未定义字段 {sorted(extra)}")

    if t == "array" and "items" in schema:
        for i, item in enumerate(inst):
            validate(item, schema["items"], root, f"{path}[{i}]")


def check_file(path: Path, schema: dict) -> list[str]:
    """返回错误列表（空 = 合法）。"""
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"JSON 解析失败：第 {e.lineno} 行 {e.msg}"]
    try:
        validate(data, schema, schema)
    except IrInvalid as e:
        problems.append(str(e))

    # 所有 span 必须五字段齐全
    def walk(node: Any, where: str = "$") -> None:
        if isinstance(node, dict):
            if "span" in node:
                sp = node["span"]
                need = {"file", "line", "col", "endLine", "endCol"}
                miss = need - set(sp) if isinstance(sp, dict) else need
                if miss:
                    problems.append(f"{where}.span 缺字段 {sorted(miss)}")
            for k, v in node.items():
                walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]")

    walk(data)
    return problems


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python spec/validate_ir.py <文件或目录>", file=sys.stderr)
        sys.exit(1)

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    target = Path(sys.argv[1])
    files = [target] if target.is_file() else sorted(target.rglob("*.agl.ir"))

    if not files:
        print(f"× 没找到 .agl.ir 文件：{target}", file=sys.stderr)
        sys.exit(1)

    bad = 0
    for f in files:
        problems = check_file(f, schema)
        if problems:
            bad += 1
            print(f"× {f}")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"✓ {f}")

    print()
    print(f"共 {len(files)} 个文件，{len(files) - bad} 个合法，{bad} 个非法")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
