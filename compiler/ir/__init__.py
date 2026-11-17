# -*- coding: utf-8 -*-
"""M4 IR 构建 / 校验 / 序列化 / 摘要。

职责边界：
  1. **不补 span**（span 只能由 M2 生成，这里只做搬运与搬移）；
  2. 阶段 1 **默认关闭优化**，保证 IR 与源码 1:1 对应 —— 人审时 IR 行号即源码行号；
  3. 降级：`异步` 标记保留，但下游按同步处理并**必须告警**，不允许静默；
  4. 序列化格式固定：UTF-8、2 空格缩进、ensure_ascii=False、结尾换行。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from compiler.diagnostics import Diagnostic

IR_VERSION = "0.2"
_REPO = Path(__file__).resolve().parents[2]


# ── 校验器（复用 spec/validate_ir.py，避免两套实现漂移）────────────────
def _load_validator():
    path = _REPO / "spec" / "validate_ir.py"
    spec = importlib.util.spec_from_file_location("agl_spec_validate_ir", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_validator = None


def _get_validator():
    global _validator
    if _validator is None:
        _validator = _load_validator()
    return _validator


def validate_ir(ir: dict) -> list[str]:
    """返回错误列表（空 = 合法）。路径精确到节点。"""
    v = _get_validator()
    schema = json.loads((_REPO / "spec" / "ir-schema.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    try:
        v.validate(ir, schema, schema)
    except v.IrInvalid as e:
        problems.append(str(e))
    # 凡是带 span 的节点，五个字段必须齐全
    # 注意：Type 节点与 Program 根节点按 schema 不带 span，不能要求它们有。
    def walk(node, where="$"):
        if isinstance(node, dict):
            if "span" in node:
                sp = node["span"]
                need = {"file", "line", "col", "endLine", "endCol"}
                miss = need - set(sp) if isinstance(sp, dict) else need
                if miss:
                    problems.append(f"{where}.span 缺字段 {sorted(miss)}")
            for k, val in node.items():
                walk(val, f"{where}.{k}")
        elif isinstance(node, list):
            for i, val in enumerate(node):
                walk(val, f"{where}[{i}]")
    walk(ir)
    return problems


# ── 构建 ──────────────────────────────────────────────────────────────
def build(program: dict, lang: str) -> dict:
    """把 M2 的 AST 包成完整 IR。AST 与 IR 同构，所以这里只加外壳。"""
    return {"ir_version": IR_VERSION, "lang": lang, "program": program}


def lower(ir: dict) -> tuple[dict, list[Diagnostic]]:
    """降级 + 收集告警。

    阶段 2 起 `异步` 是真的并发（单线程事件循环），不再降级成同步，
    所以这里没有告警可发 —— 以前那条「异步会被当成普通函数」的提醒
    现在反而是误导，已经删掉。
    """
    return ir, []


# ── 序列化 ────────────────────────────────────────────────────────────
def dumps(ir: dict) -> str:
    return json.dumps(ir, ensure_ascii=False, indent=2) + "\n"


def dump(ir: dict, path: Path) -> None:
    path.write_text(dumps(ir), encoding="utf-8")


# ── 中文摘要（给人看的，不是给 AI 看的）────────────────────────────────
_TYPE_ZH = {
    "Int": "整数", "Float": "浮点数", "Bool": "真假值", "String": "文本",
    "Null": "空", "List": "列表", "Map": "映射表",
    "Result": "结果", "Error": "错误",
}
_EFFECT_ZH = {
    "IO": "读写文件和磁盘", "Net": "访问网络", "model": "调用大模型",
    "Time": "取当前时间", "External": "调用外部程序",
}


def _type_text(t: dict | None, lang: str = "zh") -> str:
    if not t:
        return "未声明"
    base = t.get("type", "Null")
    name = t.get("declared") or _TYPE_ZH.get(base, base)
    args = t.get("args") or []
    if args:
        return f"{name}<{'、'.join(_type_text(a, lang) for a in args)}>"
    return name


def summarize(ir: dict, lang: str = "zh") -> str:
    """从 IR 直接生成中文自然语言摘要 —— 普通人审阅的主交付物。"""
    mod = ir["program"]["modules"][0]
    fns = mod["functions"]
    entry_name = ir["program"]["entry"]
    entry = next((f for f in fns if f["name"] == entry_name), fns[0] if fns else None)

    all_effects: list[str] = []
    for f in fns:
        for e in f.get("effects", []):
            if e not in all_effects:
                all_effects.append(e)

    out: list[str] = []
    out.append(f"这份程序包含 {len(fns)} 个函数" + (f"，入口是「{entry['name']}」" if entry else "") + "。")

    if all_effects:
        out.append("它会碰这些东西：" + "、".join(_EFFECT_ZH.get(e, e) for e in all_effects) + "。")
    else:
        out.append("它不碰网络、不读写文件、不调外部程序，只做计算。")

    with_checks = [f for f in fns if f.get("checks")]
    if with_checks:
        out.append("")
        out.append("必须满足的前提条件：")
        for f in with_checks:
            for c in f["checks"]:
                out.append(f"  ·「{f['name']}」要求 {_expr_text(c, lang)}")

    out.append("")
    out.append("逐个函数：")
    for f in fns:
        params = "、".join(f"{p['name']}（{_type_text(p['type'], lang)}）" for p in f["params"])
        ret = _type_text(f.get("return"), lang)
        eff = "、".join(_EFFECT_ZH.get(e, e) for e in f.get("effects", []))
        line = f"  ·「{f['name']}」"
        line += f"接受 {params}，" if params else "不接受参数，"
        line += "不返回任何值" if ret == "空" else f"给出 {ret}"
        if eff:
            line += f"；会{eff}"
        out.append(line)

    return "\n".join(out)


def _expr_text(e: dict, lang: str) -> str:
    """把表达式尽量还原成中文短语，用于校验条件的人话描述。"""
    k = e.get("expr")
    if k == "string":
        return "空文本" if e["value"] == "" else f"「{e['value']}」"
    if k == "int" or k == "float":
        return str(e["value"])
    if k == "bool":
        return "真" if e["value"] else "假"
    if k == "null":
        return "空"
    if k == "name":
        return e["name"]
    if k == "member":
        return f"{_expr_text(e['target'], lang)} 的 {e['field']}"
    if k == "index":
        return f"{_expr_text(e['target'], lang)} 的第 {_expr_text(e['index'], lang)} 项"
    if k == "call":
        return f"{e['name']}（{'、'.join(_expr_text(a, lang) for a in e['args'])}）"
    if k == "unop":
        return f"非 {_expr_text(e['operand'], lang)}" if e["op"] == "!" else f"负 {_expr_text(e['operand'], lang)}"
    if k == "binop":
        op_word = {"==": "等于", "!=": "不等于", ">": "大于", "<": "小于",
                   ">=": "不小于", "<=": "不大于", "&&": "并且", "||": "或者",
                   "+": "加", "-": "减", "*": "乘", "/": "除以", "%": "对…取余"}
        return f"{_expr_text(e['left'], lang)} {op_word.get(e['op'], e['op'])} {_expr_text(e['right'], lang)}"
    if k == "list":
        return "[" + "、".join(_expr_text(i, lang) for i in e["items"]) + "]"
    if k == "map":
        return "{" + "、".join(f"{_expr_text(x['key'], lang)}: {_expr_text(x['value'], lang)}" for x in e["entries"]) + "}"
    return "（无法描述）"
