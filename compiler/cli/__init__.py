# -*- coding: utf-8 -*-
"""aglc 命令行入口。

    python -m compiler.cli <命令> [参数]

命令一览（全部可用；`doctor` 是一键体检，别的 AI 拿到仓库先跑它）：
    check     只检查，不产出文件
    build     产出 .agl.ir 与 .py
    run       编译并执行（错误自动回译到 AGL 行号）
    summary   生成中文摘要（给不看代码的人）
    tokens    打印 Token 流（调试词法器）
    ir        打印 IR（调试语法分析）
    convert   中英双分支转换
    migrate   迁移旧示范的旧关键字/旧内建名
    compare   两份代码的结构等价性比对
    audit     契约层：扫描 Python 提取 AGL 契约
    card      关键字速查卡（写码前读的那一页）
    init      生成能直接跑的工程骨架
    mcp       启动 MCP 工具服务器（给 AI 客户端连）
    lsp       启动语言服务器（给编辑器连）
    grammar   输出编辑器语法高亮（由关键字表生成）
    ffi       列出项目用了哪些外部库、各自什么效应
    doctor    体检：规格文件 + 黄金用例 + 双分支一致性
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

from compiler import convert as convert_mod
from compiler import bytecode
from compiler import legacy as legacy_mod
from compiler import migrate as migrate_mod
from compiler import vm as vm_backend
from compiler import diagnostics
from compiler.cli import pipeline
from compiler.diagnostics import Diagnostic
from compiler.ir import dumps, summarize

REPO = Path(__file__).resolve().parents[2]


def language_version() -> str:
    """版本号只有一个权威来源：spec/keywords.toml 的 meta.version。"""
    try:
        with open(REPO / "spec" / "keywords.toml", "rb") as f:
            return tomllib.load(f).get("meta", {}).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════
def render_diag(d: Diagnostic, stream=sys.stderr) -> None:
    icon = "✗" if d.severity == "error" else "⚠"
    print(f"{icon} {d.code}  {d.file} 第 {d.line} 行", file=stream)
    if d.source_line is not None:
        num = str(d.line)
        print(f"    {num} | {d.source_line}", file=stream)
        width = max(1, (d.end_col or d.col + 1) - d.col)
        print(f"    {' ' * len(num)} | {' ' * d.col}{'^' * width}", file=stream)
    print(f"    问题  {d.message_zh}", file=stream)
    if d.cause:
        print(f"    原因  {d.cause}", file=stream)
    for h in d.fix_hints:
        print(f"    建议  {h.suggest}", file=stream)
    if d.stack:
        print("    AGL 调用栈", file=stream)
        for i, fr in enumerate(d.stack):
            lead = "      " if i else "      "
            print(f"{lead}{fr['fn']}()  {fr['file']}:{fr['line']}", file=stream)
    print(file=stream)


def emit_diags(diags: list[Diagnostic], as_json: bool) -> None:
    if as_json:
        print(json.dumps([d.to_json() for d in diags], ensure_ascii=False, indent=2))
        return
    for d in diags:
        render_diag(d)


def _is_json(args) -> bool:
    return bool(getattr(args, "json", False))


def _is_opt(args) -> bool:
    """优化通道默认关闭，只有显式 --opt 才开。"""
    return bool(getattr(args, "opt", False))


def note_opt(res) -> None:
    # 优化注记是编译期信息，走 stderr；stdout 必须保持纯净（run 的 stdout 是
    # 程序输出，用户重定向 `aglc run --opt f.agl > out.txt` 不该混进注记）。
    for n in getattr(res, "opt_notes", []) or []:
        print(f"· 优化：{n}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════
# 命令实现
# ══════════════════════════════════════════════════════════════════════
def cmd_check(args) -> int:
    res = pipeline.compile_file(args.file)
    # 旧写法是提醒，不是错误：即使当前语法也报错，用户至少能看到该怎么改。
    if res.source:
        lines = res.source.splitlines()
        for line, message in legacy_mod.scan(res.source):
            diag = Diagnostic(
                code="AGL-W-9001", severity="warning", message_zh=message,
                file=res.path.name, line=line, col=0, end_col=1,
            )
            if 1 <= line <= len(lines):
                diag.source_line = lines[line - 1]
            res.diagnostics.append(diag)
    if _is_json(args):
        emit_diags(res.diagnostics, True)
    else:
        if res.diagnostics:
            emit_diags(res.diagnostics, False)
        if res.ok:
            w = len(res.warnings)
            tail = f"（{w} 条提醒）" if w else ""
            print(f"✓ {res.path.name} 检查通过{tail}")
    return 0 if res.ok else 1


def _agb_file_name(module: str) -> str:
    """模块名 → 落盘文件名。只替换路径分隔符，模块名本身不翻译。"""
    return module.replace("/", "_").replace("\\", "_").replace(":", "_") + ".agb"


def _compile_agb(res, what: str = "程序"):
    """把编译结果转成 AGB；不在子集时给出可读的失败信息。"""
    try:
        return bytecode.compile_program(res.ir)
    except bytecode.AgbUnsupported as e:
        raise bytecode.AgbUnsupported(f"{what} `{res.path.name}`：{e}") from e


def cmd_build(args) -> int:
    # B7：默认用安装好的自举编译器产出 .agb（种子只负责启动 VM与静态检查）；
    # 需要回退时显式 `--compiler seed`。
    if getattr(args, "compiler", "agl") == "agl":
        from compiler.cli.bootstrap import BootstrapError, compile_with_selfhost

        try:
            agb_path, ir_path = compile_with_selfhost(
                Path(args.file).resolve(), Path(args.out or Path(args.file).parent))
        except BootstrapError as e:
            print(f"✗ {args.file}: {e}", file=sys.stderr)
            if getattr(e, "fallback", True):
                print("  种子编译器仍可用：加 `--compiler seed` 回退重试。",
                      file=sys.stderr)
            else:
                print("  先修正源码再重试（`aglc check <文件>` 可看完整诊断）。",
                      file=sys.stderr)
            return 1
        if _is_json(args):
            print(json.dumps({
                "ok": True, "compiler": "agl",
                "agb": str(agb_path),
                "ir": str(ir_path) if ir_path else None,
            }, ensure_ascii=False, indent=2))
        else:
            print(f"✓ 自举编译器已产出\n  AGB 字节码  {agb_path}"
                  + (f"\n  中间表示    {ir_path}" if ir_path else ""))
        return 0

    res = pipeline.compile_file(args.file, optimize=_is_opt(args))
    if not res.ok:
        emit_diags(res.diagnostics, _is_json(args))
        return 1
    outdir = Path(args.out or res.path.parent)
    outdir.mkdir(parents=True, exist_ok=True)

    # 先把主模块和所有依赖都编译成 AGB，再统一落盘；
    # 这样依赖不支持时不会留下一个半成品主文件。
    try:
        agb = _compile_agb(res, "主模块")
    except bytecode.AgbUnsupported as e:
        print(f"✗ {args.file}: {e}", file=sys.stderr)
        print("  该程序超出 AGB 子集，build 不再生成 .py；"
              "如需 Python 兜底请显式用 `aglc run --backend py`。", file=sys.stderr)
        return 1

    artifacts: list[tuple[Path, str]] = []
    artifacts.append((outdir / (res.path.stem + ".agb"), bytecode.encode(agb)))
    artifacts.append((outdir / (res.path.name + ".ir"), dumps(res.ir)))

    dep_files: list[str] = []
    seen: set[str] = set()

    def _collect(dep_res) -> None:
        try:
            dep_agb = _compile_agb(dep_res, "依赖模块")
        except bytecode.AgbUnsupported as e:
            raise bytecode.AgbUnsupported(str(e)) from e
        module = dep_agb["module"]
        if module in seen:
            return
        seen.add(module)
        p = outdir / _agb_file_name(module)
        artifacts.append((p, bytecode.encode(dep_agb)))
        artifacts.append((outdir / (dep_res.path.name + ".ir"), dumps(dep_res.ir)))
        dep_files.append(str(p))
        for d in (dep_res.deps or {}).values():
            _collect(d)

    try:
        for dep in (res.deps or {}).values():
            _collect(dep)
    except bytecode.AgbUnsupported as e:
        print(f"✗ {args.file}: {e}", file=sys.stderr)
        print("  依赖模块超出 AGB 子集，build 不再生成 .py。", file=sys.stderr)
        return 1

    for path, text in artifacts:
        path.write_text(text, encoding="utf-8", newline="\n")

    if _is_json(args):
        print(json.dumps({
            "ok": True,
            "agb": str(artifacts[0][0]),
            "ir": str(artifacts[1][0]),
            "deps": dep_files,
            "warnings": [d.to_json() for d in res.warnings],
        }, ensure_ascii=False, indent=2))
    else:
        emit_diags(res.warnings, False)
        note_opt(res)
        print(f"✓ 已产出\n  AGB 字节码  {artifacts[0][0]}\n  中间表示    {artifacts[1][0]}")
        for p in dep_files:
            print(f"  依赖模块    {p}")
    return 0


def _allowed_effects(args) -> set[str] | None:
    """宿主侧效应白名单。

    默认是空集（全禁），不再把“没写参数”解释成“什么都能用”。
    需要时显式传 `--allow-effects IO,Time`；写 `all` 才是显式全量放行。
    """
    raw = getattr(args, "allow_effects", None)
    if raw is None:
        return set()
    text = raw.strip()
    if text.lower() == "all":
        return None
    return {x.strip() for x in text.split(",")
            if x.strip() and x.strip().lower() != "none"}


def _allowed_tools(args) -> set[str] | None:
    """外部库白名单：默认空集（全禁）；`all` 才是显式全量放行。"""
    raw = getattr(args, "allow_tools", None)
    if raw is None:
        return set()
    text = raw.strip()
    if text.lower() == "all":
        return None
    return {x.strip() for x in text.split(",")
            if x.strip() and x.strip().lower() != "none"}


def _run_vm_agb(agb: dict, modules: dict, lang: str, file: str, args) -> int:
    try:
        out = vm_backend.run(agb, lang, modules,
                             max_steps=getattr(args, "max_steps", None),
                             allowed_effects=_allowed_effects(args),
                             max_calls=getattr(args, "max_calls", None),
                             allowed_tools=_allowed_tools(args),
                             timeout_ms=getattr(args, "timeout_ms", None))
    except vm_backend.VmError as e:
        if _is_json(args):
            print(json.dumps({
                "ok": False, "code": e.code, "line": e.line,
                "message": e.message, "function": e.fn,
            }, ensure_ascii=False, indent=2))
        else:
            print(e.render(file), file=sys.stderr)
        return 1
    if out:
        print(out)
    return 0


def _load_agb(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
        return bytecode.decode(text)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise bytecode.AgbUnsupported(f"读不了 AGB 文件 `{path}`：{e}") from e


def _agb_modules(path: Path, root: dict) -> dict:
    """加载同目录下其它 `.agb`，按模块名建表；坏文件跳过，缺模块由 VM 报。"""
    mods: dict = {}
    for p in sorted(path.parent.glob("*.agb")):
        if p.resolve() == path.resolve():
            continue
        try:
            m = _load_agb(p)
        except bytecode.AgbUnsupported:
            continue
        if m.get("module") == root.get("module"):
            continue
        mods[m["module"]] = m
    return mods


def cmd_run(args) -> int:
    path = Path(args.file)
    backend = getattr(args, "backend", "vm")

    # B6/B7：--compiler agl（默认）—— 先用自举编译器把源码编成 .agb，再用 VM 执行。
    # 显式 `--backend py` 是 Python 兜底通道，不能被自举分支截走。
    # 产物落在临时目录，不污染工程目录；执行语义与 `run .agb` 完全一致。
    if (backend != "py"
            and getattr(args, "compiler", "agl") == "agl"
            and path.suffix.lower() != ".agb"):
        from compiler.cli.bootstrap import BootstrapError, compile_with_selfhost

        tmp_out = Path(tempfile.mkdtemp(prefix="agl_selfhost_run_"))
        try:
            try:
                agb_path, _ = compile_with_selfhost(path.resolve(), tmp_out, want_ir=False)
            except BootstrapError as e:
                print(f"✗ {args.file}: {e}", file=sys.stderr)
                if getattr(e, "fallback", True):
                    print("  种子编译器仍可用：加 `--compiler seed` 回退重试。",
                          file=sys.stderr)
                else:
                    print("  先修正源码再重试（`aglc check <文件>` 可看完整诊断）。",
                          file=sys.stderr)
                return 1
            agb = _load_agb(agb_path)
        finally:
            shutil.rmtree(tmp_out, ignore_errors=True)
        modules = {}
        return _run_vm_agb(agb, modules, agb.get("lang") or "zh", str(path), args)

    # 直接跑字节码：不需要源码，也不需要 Python 后端。
    if path.suffix.lower() == ".agb":
        if backend == "py":
            print("✗ `.agb` 是 AGB 字节码，只能用 VM 执行；去掉 `--backend py`。",
                  file=sys.stderr)
            return 1
        try:
            agb = _load_agb(path)
        except bytecode.AgbUnsupported as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        modules = _agb_modules(path, agb)
        return _run_vm_agb(agb, modules, agb.get("lang") or "zh", str(path), args)

    res = pipeline.compile_file(args.file, optimize=_is_opt(args))
    if not res.ok:
        emit_diags(res.diagnostics, _is_json(args))
        return 1

    if backend in ("vm", "auto"):
        try:
            agb, modules = _vm_program(res)
        except bytecode.AgbUnsupported as e:
            if backend == "auto":
                print(f"⚠ 该程序超出 AGB 子集，已按 `--backend auto` 退回 Python：{e}",
                      file=sys.stderr)
            else:
                print(f"✗ {args.file}: 该能力还不支持（{e}）", file=sys.stderr)
                print("  VM 不会静默退回 Python；要兜底请显式加 `--backend py`。",
                      file=sys.stderr)
                return 1
        else:
            return _run_vm_program(res, args, agb, modules)

    if not _is_json(args):
        emit_diags(res.warnings, False)
        note_opt(res)
    ok, diag, out = pipeline.run_result(res)
    if out:
        print(out, end="")
    if not ok and diag is not None:
        if _is_json(args):
            print(json.dumps(diag.to_json(), ensure_ascii=False, indent=2))
        else:
            print(file=sys.stderr)
            print(f"程序在「{diag.stack[-1]['fn'] if diag.stack else res.path.stem}」"
                  f"停止：{diag.file} 第 {diag.line} 行", file=sys.stderr)
            print(file=sys.stderr)
            render_diag(diag, sys.stderr)
        return 1
    return 0


def cmd_summary(args) -> int:
    res = pipeline.compile_file(args.file)
    if not res.ok:
        emit_diags(res.diagnostics, _is_json(args))
        return 1
    text = summarize(res.ir, res.lang)
    if _is_json(args):
        print(json.dumps({"summary_zh": text}, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


def _ffi_calls(node, out: list) -> list:
    """递归找出 IR 里所有 FFI 调用（带 `ffi` 标记的 call 节点）。"""
    if isinstance(node, dict):
        if node.get("expr") == "call" and "ffi" in node:
            out.append(node)
        for v in node.values():
            _ffi_calls(v, out)
    elif isinstance(node, list):
        for v in node:
            _ffi_calls(v, out)
    return out


def _tool_schema(decl: dict, src_lines: list[str]) -> dict:
    """把一个 `工具` 声明导出成「函数调用三件套」（P7 定稿：name/description/inputSchema）。

    类型映射：文本→string、整数→integer、浮数→number、布尔→boolean、空→null；
    可空类型（`文本?`）导出为 ["string", "null"] 数组形态。
    description 取工具块上方紧邻的连续 `//` 注释。
    """
    _TYPE_MAP = {"String": "string", "Int": "integer", "Float": "number",
                 "Bool": "boolean", "Null": "null"}
    line0 = int((decl.get("span") or {}).get("line", 1))
    desc: list[str] = []
    i = line0 - 2
    while 0 <= i < len(src_lines) and src_lines[i].strip().startswith("//"):
        desc.append(src_lines[i].strip()[2:].strip())
        i -= 1
    props: dict = {}
    required: list[str] = []
    for p in decl.get("params") or []:
        t = p.get("type") or {}
        base = _TYPE_MAP.get(t.get("type", ""), "string")
        props[p["name"]] = ({"type": [base, "null"]} if t.get("nullable")
                            else {"type": base})
        required.append(p["name"])
    config = {}
    for k, v in (decl.get("config") or {}).items():
        config[k] = v.get("value") if isinstance(v, dict) else v
    return {
        "name": decl.get("name", ""),
        "description": " ".join(reversed(desc)).strip(),
        "inputSchema": {"type": "object", "properties": props,
                        "required": required, "additionalProperties": False},
        "config": config,
    }


def cmd_tools(args) -> int:
    """导出 `工具` 块的「函数调用三件套」，直接喂给 OpenAI / Anthropic 的函数调用。

    单文件（file）或整目录（--dir）扫描；工具是声明块，不参与运行，
    但它是 Agent 调用外部能力的入口 —— 出口必须结构化、可审、可直接用。
    """
    files: list[Path] = []
    if getattr(args, "dir", None):
        d = Path(args.dir)
        files = sorted(d.glob("*.agl"))
        if not files:
            print(f"✗ {args.dir} 里没有 .agl 文件", file=sys.stderr)
            return 1
    elif args.file:
        files = [Path(args.file)]
    else:
        print("✗ 给一份 .agl 文件或用 --dir 扫目录", file=sys.stderr)
        return 1

    tools_out: list[dict] = []
    for f in files:
        res = pipeline.compile_file(f)
        if not res.ok:
            print(f"✗ {f.name}: {res.errors[0].code} {res.errors[0].message_zh}",
                  file=sys.stderr)
            return 1
        decls = res.ir["program"]["modules"][0].get("declarations") or []
        src_lines = res.source.splitlines()
        for d in decls:
            if d.get("kind") == "tool":
                tools_out.append(_tool_schema(d, src_lines))

    payload = {"tools": tools_out}
    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if _is_json(args) or args.out:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for t in tools_out:
            params = ", ".join(
                f"{k}:{v['type'] if isinstance(v['type'], str) else '?'}"
                for k, v in t["inputSchema"]["properties"].items()) or "无"
            print(f"工具 {t['name']}({params})  {t['description']}")
            if t["config"]:
                print("  配置: " + json.dumps(t["config"], ensure_ascii=False))
    if tools_out:
        return 0
    print("（这份源码里没有 `工具` 声明块）")
    return 0


def cmd_ffi(args) -> int:
    """列出这个项目用了哪些外部库、各自声明了什么效应、都在哪儿调的。

    这是 FFI 的"可审"出口：外部库是黑盒，编译器看不见它内部干了什么，
    所以必须让人能一眼看出「这个项目把手伸到哪儿去了」。
    """
    from compiler.lexer import KeywordTable

    res = pipeline.compile_file(args.file)
    if not res.ok:
        emit_diags(res.diagnostics, _is_json(args))
        return 1

    table = KeywordTable.load()
    libs: dict[str, dict] = {}
    mods = [(res.path.name, res.ir)]
    for dep in (res.deps or {}).values():
        mods.append((dep.path.name, dep.ir))

    for fname, ir in mods:
        for imp in (ir["program"]["modules"][0].get("imports") or []):
            if imp.get("kind") == "ffi":
                libs.setdefault(imp["module"], {"effect": imp.get("effect") or "External",
                                                "sites": []})
        for call in _ffi_calls(ir["program"], []):
            sp = call.get("span") or {}
            entry = libs.setdefault(call["ffi"], {"effect": "External", "sites": []})
            entry["sites"].append((fname, sp.get("line", 0), call["name"]))

    items = []
    for name, info in sorted(libs.items()):
        items.append({
            "library": name,
            "effect": info["effect"],
            "effect_zh": table.text_for(info["effect"], res.lang),
            "sites": [{"file": f, "line": ln, "method": m} for f, ln, m in info["sites"]],
        })

    if _is_json(args):
        print(json.dumps({"ffi": items}, ensure_ascii=False, indent=2))
        return 0

    if not items:
        print("这个项目没有调用任何外部库。")
        return 0
    print(f"这个项目用了 {len(items)} 个外部库：\n")
    for it in items:
        print(f"· {it['library']} —— 声明效应：{it['effect_zh']}")
        for s in it["sites"]:
            print(f"    {s['file']}:{s['line']}  {it['library']}.{s['method']}")
        print()
    return 0


def cmd_import(args) -> int:
    """把一份 Python 翻译成 AGL 源码。

    导入≠无脑翻译：翻不了的地方会当场说清楚为什么，绝不猜一个看着像的映射。
    所以这个命令最常见的结果不是"成功"，而是"这一段翻不了，原因是……"。
    """
    from compiler.importer import import_python

    path = Path(args.file)
    try:
        src = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        print(f"读不了这个文件：{e}")
        return 1
    res = import_python(src, getattr(args, "lang", "zh") or "zh")

    if _is_json(args):
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return 0 if res.ok else 1

    if not res.ok:
        print(f"✗ 这份 Python 没能翻过去（{len(res.blockers)} 处挡路）：\n")
        for b in res.blockers:
            print(f"  · {b}")
        if res.warnings:
            print()
        for w in res.warnings:
            print(f"  ! {w}")
        print("\n改好这些地方再来 —— 导入器宁可不翻，也不替你猜。")
        return 1

    if res.warnings:
        print("注意这里的改动：\n")
        for w in res.warnings:
            print(f"  ! {w}")
        print()

    out = getattr(args, "out", None)
    if out:
        dest = Path(out)
        dest.write_text(res.agl, encoding="utf-8")
        print(f"✓ 已写出 {dest}（{res.stats['functions']} 个函数，{res.stats['lines']} 行）")
        return 0
    print(res.agl, end="")
    return 0


def cmd_tokens(args) -> int:
    res = pipeline.compile_file(args.file)
    toks = [t.to_json() for t in res.tokens]
    if _is_json(args):
        print(json.dumps(toks, ensure_ascii=False, indent=2))
    else:
        for t in res.tokens:
            if t.kind in ("NEWLINE", "COMMENT", "EOF"):
                continue
            print(f"{t.line:>4}:{t.col:<3} {t.kind:<8} {t.text!r:<20} → {t.canonical}")
    return 0 if res.ok else 1


def cmd_ir(args) -> int:
    res = pipeline.compile_file(args.file)
    if res.ir is None:
        emit_diags(res.diagnostics, _is_json(args))
        return 1
    print(dumps(res.ir), end="")
    return 0


def cmd_dis(args) -> int:
    """M0：IR → AGB 并反汇编；也接受现成的 `.agb`。只验证格式，不执行。"""
    path = Path(args.file)
    if path.suffix.lower() == ".agb":
        try:
            agb = _load_agb(path)
        except bytecode.AgbUnsupported as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        if _is_json(args):
            print(bytecode.encode(agb))
        else:
            print(bytecode.disassemble(agb), end="")
        return 0
    res = pipeline.compile_file(args.file)
    if not res.ok:
        emit_diags(res.diagnostics, _is_json(args))
        return 1
    try:
        agb = bytecode.compile_program(res.ir)
    except bytecode.AgbUnsupported as e:
        print(f"✗ {args.file}: 该能力还不支持（{e}）", file=sys.stderr)
        print("  `aglc dis` 只做 AGB 反汇编；完整执行走 `aglc vm`。", file=sys.stderr)
        return 1
    if _is_json(args):
        print(bytecode.encode(agb))
    else:
        print(bytecode.disassemble(agb), end="")
    return 0


def cmd_vm(args) -> int:
    """M1 骨架：用自研 AGB 解释器执行纯计算子集。支持 `.agl` 和 `.agb`。"""
    path = Path(args.file)
    if path.suffix.lower() == ".agb":
        try:
            agb = _load_agb(path)
        except bytecode.AgbUnsupported as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        return _run_vm_agb(agb, _agb_modules(path, agb),
                           agb.get("lang") or "zh", str(path), args)
    res = pipeline.compile_file(args.file)
    if not res.ok:
        emit_diags(res.diagnostics, _is_json(args))
        return 1
    return _run_vm_result(res, args)


def _vm_program(res):
    """编译主模块和依赖，返回 (agb, modules)；不在 AGB 子集时抛 AgbUnsupported。"""
    agb = bytecode.compile_program(res.ir)
    modules: dict = {}

    def _collect(dep_res) -> None:
        dep_agb = bytecode.compile_program(dep_res.ir)
        if dep_agb["module"] in modules:
            return
        modules[dep_agb["module"]] = dep_agb
        for d in (dep_res.deps or {}).values():
            _collect(d)

    for dep in (res.deps or {}).values():
        _collect(dep)
    return agb, modules


def _run_vm_program(res, args, agb, modules) -> int:
    return _run_vm_agb(agb, modules, res.lang, args.file, args)


def _run_vm_result(res, args) -> int:
    try:
        agb, modules = _vm_program(res)
    except bytecode.AgbUnsupported as e:
        print(f"✗ {args.file}: 该能力还不支持（{e}）", file=sys.stderr)
        print("  VM 不会静默退回 Python；要兜底请显式用 `aglc run --backend py`。",
              file=sys.stderr)
        return 1
    return _run_vm_program(res, args, agb, modules)


def cmd_convert(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"✗ 找不到文件：{path}", file=sys.stderr)
        return 1
    src = path.read_text(encoding="utf-8-sig")
    try:
        out = convert_mod.convert(src, path.name, args.to)
    except diagnostics.AglError as e:
        render_diag(e.diag)
        return 1
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"✓ 已写入 {args.out}")
    elif args.write:
        # 就地写回同目录，文件名加语言后缀
        suffix = "_zh" if args.to == "zh" else "_en"
        target = path.with_name(path.stem + suffix + path.suffix)
        target.write_text(out, encoding="utf-8")
        print(f"✓ 已写入 {target}")
    else:
        print(out, end="")
    return 0


def cmd_migrate(args) -> int:
    """旧示范迁移：默认只报告，`--write` 才落盘。支持单个文件或目录。"""
    target = Path(args.file)
    if not target.exists():
        print(f"✗ 找不到路径：{target}", file=sys.stderr)
        return 1
    files = sorted(target.rglob("*.agl")) if target.is_dir() else [target]
    if not files:
        print(f"✗ {target} 下没有 .agl 文件", file=sys.stderr)
        return 1
    results = []
    total_changes = 0
    for path in files:
        try:
            changes, warnings = migrate_mod.migrate_file(path, bool(args.write))
        except (OSError, UnicodeError) as exc:
            print(f"! {path}: 读/写失败：{exc}", file=sys.stderr)
            continue
        total_changes += len(changes)
        results.append((path, changes, warnings))
    if _is_json(args):
        print(json.dumps({
            "target": str(target),
            "written": bool(args.write),
            "files": [{
                "file": str(path),
                "changes": [{"line": c.line, "old": c.old, "new": c.new, "reason": c.reason}
                            for c in changes],
                "warnings": warnings,
            } for path, changes, warnings in results],
        }, ensure_ascii=False, indent=2))
    else:
        for path, changes, warnings in results:
            if not changes and not warnings:
                continue
            print(f"\n{path}:")
            for c in changes:
                print(f"  第 {c.line} 行：`{c.old}` → `{c.new}`（{c.reason}）")
            for w in warnings:
                print(f"  ! {w}")
        if total_changes == 0 and not any(w for _, _, w in results):
            print("✓ 没有发现旧写法")
        elif args.write:
            print(f"\n✓ 已写入 {len([1 for _, c, _ in results if c])} 个文件")
        else:
            print("\n（只报告，未写盘；确认后加 --write）")
    return 0


def cmd_compare(args) -> int:
    a = pipeline.compile_file(args.a)
    b = pipeline.compile_file(args.b)
    for r in (a, b):
        if not r.ok:
            emit_diags(r.diagnostics, _is_json(args))
            return 1
    ok, detail = convert_mod.compare(a.ir, b.ir)
    if _is_json(args):
        print(json.dumps({"equivalent": ok, "detail": detail}, ensure_ascii=False, indent=2))
    else:
        print(("✓ " if ok else "✗ ") + detail)
    return 0 if ok else 1


def cmd_roundtrip(args) -> int:
    """往返校验：转到另一分支，再转回原分支，必须逐字相同。"""
    from compiler.lexer import detect_lang

    path = Path(args.file)
    src = path.read_text(encoding="utf-8-sig")
    src_lang = detect_lang(src)
    if src_lang is None:
        print(f"✗ {path.name}：第一行缺少语言声明，无法往返校验", file=sys.stderr)
        return 1
    other = "zh" if src_lang == "en" else "en"
    target = args.to or other
    if target == src_lang:
        target = other
    once = convert_mod.convert(src, path.name, target)
    back = convert_mod.convert(once, path.name, src_lang)
    if back == src:
        print(f"✓ 往返校验通过：{path.name} → {target} → {src_lang} 逐字还原")
        return 0
    print(f"✗ 往返校验失败：{path.name}（{src_lang} → {target} → {src_lang}）", file=sys.stderr)
    import difflib
    for line in difflib.unified_diff(src.splitlines(), back.splitlines(),
                                     "orig", "roundtrip", lineterm="", n=1):
        print(line, file=sys.stderr)
    return 1


def cmd_audit(args) -> int:
    """转交契约层（aglc/audit.py，零依赖静态分析，0 token）。"""
    import subprocess
    script = REPO / "aglc" / "audit.py"
    if not script.exists():
        print("✗ 契约层脚本缺失：aglc/audit.py", file=sys.stderr)
        return 1
    cmd = [sys.executable, str(script), args.file]
    if getattr(args, "json", False):
        cmd.append("--json")
    return subprocess.call(cmd)


def cmd_mcp(args) -> int:
    """启动 MCP 工具服务器（stdio）。多余参数原样转给它（--list / --version）。"""
    from compiler import mcp
    return mcp.main(list(getattr(args, "passthrough", []) or []))


def cmd_lsp(args) -> int:
    """启动语言服务器（stdio）。多余参数原样转给它（--version / --selftest）。"""
    from compiler import lsp
    return lsp.main(list(getattr(args, "passthrough", []) or []))


def cmd_grammar(args) -> int:
    """输出/写入编辑器语法高亮文件（由关键字表生成，不手写）。"""
    from compiler import grammar

    if args.dir:
        made = grammar.write_into(args.dir)
        for n in made:
            print(f"✓ 已生成 {Path(args.dir) / n}")
        return 0
    print(grammar.dumps(), end="")
    return 0


def cmd_doctor(args) -> int:
    from compiler.cli import doctor
    if getattr(args, "update_golden", False):
        return doctor.update_golden()
    return doctor.run(verbose=not _is_json(args), as_json=_is_json(args))


def cmd_bootstrap(args) -> int:
    """自举编译器固定化安装：种子 → v1 → v2 → v3，安装不动点到 build/。"""
    from compiler.cli.bootstrap import install_bootstrap

    res = install_bootstrap(verbose=True)
    if not res.ok:
        for e in res.errors:
            print(f"✗ {e}")
        return 1
    print(f"✓ 安装产物：{res.installed}")
    return 0


def cmd_card(args) -> int:
    """输出关键字速查卡（写代码之前读的那一页）。"""
    from compiler import card
    from compiler.lexer import LANG_EN, LANG_ZH

    wanted = [LANG_ZH, LANG_EN] if args.lang == "both" else [args.lang]
    text = "\n\n---\n\n".join(card.build(x).rstrip() for x in wanted) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"✓ 已写入 {args.out}")
    else:
        print(text, end="")
    return 0


def cmd_init(args) -> int:
    """生成一个能直接跑的工程骨架。已存在的文件一律不覆盖。"""
    from compiler import scaffold

    root = Path(args.dir).resolve()
    made, skipped = scaffold.write(root, args.lang)
    for n in made:
        print(f"✓ 已生成 {n}")
    for n in skipped:
        print(f"· 已存在，未覆盖 {n}")
    print(f"\n下一步：aglc run {root / scaffold.ENTRY}")
    print("（还没装命令的话，先在 AGL 仓库里跑一次 `pip install -e .`）")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 参数
# ══════════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aglc",
        description="AGL（Agent Grounded Language）编译器 · 阶段 1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python -m compiler.cli run tests/golden/hello_zh.agl",
    )
    # v0.50：版本号从关键字表读，避免和 MCP / LSP 报的版本不一致
    p.add_argument("--version", "-v", action="version", version=f"AGL v{language_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_json(sp):
        sp.add_argument("--json", "--诊断", dest="json", action="store_true",
                        help="输出结构化 JSON（给 AI 用）")

    for name, fn, help_text, need_file in [
        ("check", cmd_check, "检查源码（词法/语法/语义/IR），不产出文件", True),
        ("build", cmd_build, "产出 .agb 字节码和 .agl.ir（不再生成 .py）", True),
        ("run", cmd_run, "编译并执行，错误回译到 AGL 行号（默认 VM）", True),
        ("summary", cmd_summary, "生成中文摘要（给不看代码的人）", True),
        ("tokens", cmd_tokens, "打印 Token 流", True),
        ("ir", cmd_ir, "打印中间表示", True),
        ("dis", cmd_dis, "IR → AGB 字节码并反汇编（M0，只格式不执行）", True),
        ("vm", cmd_vm, "用自研 AGB 解释器执行（M1 骨架，纯计算子集）", True),
        ("audit", cmd_audit, "契约层：扫描 Python 提取中文契约", True),
        ("doctor", cmd_doctor, "体检：规格 + 黄金用例 + 双分支一致性", False),
    ]:
        sp = sub.add_parser(name, help=help_text)
        if need_file:
            sp.add_argument("file", help="AGL 源文件")
        if name == "build":
            sp.add_argument("-o", "--out", help="输出目录（默认与源文件同目录）")
        if name in ("build", "run"):
            sp.add_argument("--opt", action="store_true",
                            help="开启 IR 优化（常量折叠 + 死代码消除，默认关闭）")
            sp.add_argument("--compiler", choices=["seed", "agl"], default="agl",
                            help="编译器：agl（默认，自举不动点编译器，需先 `aglc bootstrap` "
                                 "安装；编译前自动过种子静态检查）/ seed（Python 种子，显式回退）")
        if name == "run":
            sp.add_argument("--backend", choices=["py", "vm", "auto"], default="vm",
                            help="执行后端：vm（默认）/ py（显式 Python 兜底）/ "
                                 "auto（VM 能编译就用 VM，否则退回 py）")
        if name in ("run", "vm"):
            sp.add_argument("--max-steps", type=int, default=None,
                            help="VM 执行步数上限（默认 500 万步，防死循环）")
            sp.add_argument("--timeout-ms", type=int, default=None,
                            help="VM 墙钟超时（毫秒，正整数；超时报 AGL-RT-0095）")
            sp.add_argument("--allow-effects", default=None,
                            help="VM 权限白名单，逗号分隔（如 IO,Time,Net,External）；"
                                 "默认全禁，写 all 表示显式全量放行")
            sp.add_argument("--max-calls", type=int, default=None,
                            help="外部调用次数上限（带效应的内建 + FFI 调用总数），"
                                 "超限报 AGL-RT-0093")
            sp.add_argument("--allow-tools", default=None,
                            help="外部库白名单，逗号分隔（如 math,json）；"
                                 "默认全禁，写 all 表示显式全量放行，越库报 AGL-RT-0094")
        if name == "doctor":
            sp.add_argument("--update-golden", action="store_true",
                            help="刷新 tests/golden 下的期望产物（确认改动有意后再用）")
        add_json(sp)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("bootstrap", help="自举编译器固定化安装：种子→v1→v2→v3，验证 v2==v3 并安装不动点")
    add_json(sp)
    sp.set_defaults(func=cmd_bootstrap)

    sp = sub.add_parser("tools", help="导出 `工具` 块的函数调用三件套（name/description/inputSchema）")
    sp.add_argument("file", nargs="?", default=None, help="AGL 源文件")
    sp.add_argument("--dir", default=None, help="扫描整个目录的 .agl")
    sp.add_argument("-o", "--out", default=None, help="把三件套 JSON 写到这个文件")
    add_json(sp)
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("convert", help="中英双分支转换（只翻关键字，标识符原样）")
    sp.add_argument("file")
    sp.add_argument("--to", "--目标", dest="to", choices=["zh", "en"], required=True)
    sp.add_argument("-o", "--out", help="输出文件")
    sp.add_argument("--write", action="store_true", help="写入同目录（文件名加 _zh/_en）")
    add_json(sp)
    sp.set_defaults(func=cmd_convert)

    sp = sub.add_parser("migrate", help="迁移旧示范的旧关键字/旧内建名（默认只报告）")
    sp.add_argument("file")
    sp.add_argument("--write", action="store_true", help="确认后直接写回源文件")
    add_json(sp)
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("compare", help="比对两份代码的结构等价性（忽略标识符）")
    sp.add_argument("a")
    sp.add_argument("b")
    add_json(sp)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("roundtrip", help="往返校验：转换后必须能逐字还原")
    sp.add_argument("file")
    sp.add_argument("--to", dest="to", choices=["zh", "en"], default=None,
                    help="中间分支（默认转到相反分支）")
    add_json(sp)
    sp.set_defaults(func=cmd_roundtrip)

    sp = sub.add_parser("card", help="输出关键字速查卡（写码前读这一页）")
    sp.add_argument("--lang", "--分支", dest="lang", choices=["zh", "en", "both"],
                    default="both", help="输出哪个分支（默认两份都给）")
    sp.add_argument("-o", "--out", help="写入文件（默认打印到屏幕）")
    add_json(sp)
    sp.set_defaults(func=cmd_card)

    sp = sub.add_parser("init", help="生成能直接跑的工程骨架")
    sp.add_argument("dir", nargs="?", default=".", help="目标目录（默认当前目录）")
    sp.add_argument("--lang", "--分支", dest="lang", choices=["zh", "en"],
                    default="zh", help="项目默认分支（默认中文）")
    add_json(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("mcp", help="启动 MCP 工具服务器（stdio，给 AI 客户端连）")
    sp.add_argument("passthrough", nargs="*", help="转给服务器的参数（--list / --version）")
    add_json(sp)
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("lsp", help="启动语言服务器（stdio，给编辑器连）")
    sp.add_argument("passthrough", nargs="*", help="转给服务器的参数（--version / --selftest）")
    add_json(sp)
    sp.set_defaults(func=cmd_lsp)

    sp = sub.add_parser("grammar", help="输出编辑器语法高亮（由关键字表生成）")
    sp.add_argument("--dir", help="写入某个编辑器扩展目录（默认打印到屏幕）")
    add_json(sp)
    sp.set_defaults(func=cmd_grammar)

    sp = sub.add_parser("ffi", help="列出项目用了哪些外部库、各自什么效应")
    sp.add_argument("file", help="AGL 源文件")
    add_json(sp)
    sp.set_defaults(func=cmd_ffi)

    sp = sub.add_parser("import", help="把一份 Python 翻译成 AGL 源码（翻不了的会直说）")
    sp.add_argument("file", help="Python 源文件")
    sp.add_argument("-o", "--out", help="写到这个 .agl 文件；不给就打在屏幕上")
    sp.add_argument("--lang", choices=("zh", "en"), default="zh",
                    help="产出哪个分支的 AGL（默认 zh 中文）")
    add_json(sp)
    sp.set_defaults(func=cmd_import)

    # v0.50：格式化命令
    sp = sub.add_parser("format", help="自动格式化 AGL 代码（统一缩进、引号、空行）")
    sp.add_argument("file", help="要格式化的 .agl 文件")
    sp.add_argument("--check", action="store_true", help="只检查格式是否正确，不修改文件")
    add_json(sp)
    sp.set_defaults(func=cmd_format)

    # v0.50：Lint 命令
    sp = sub.add_parser("lint", help="检查代码问题（未使用的变量、命名不规范等）")
    sp.add_argument("file", help="要检查的 .agl 文件")
    add_json(sp)
    sp.set_defaults(func=cmd_lint)

    # v0.50：列出内建函数
    sp = sub.add_parser("list-builtins", help="列出所有内建函数")
    add_json(sp)
    sp.set_defaults(func=cmd_list_builtins)

    # v0.50：显示 AGL 信息
    sp = sub.add_parser("info", help="显示 AGL 的基本信息")
    add_json(sp)
    sp.set_defaults(func=cmd_info)

    # v0.50：清理临时文件
    sp = sub.add_parser("clean", help="清理编译生成的临时文件（.agl.ir、__pycache__）")
    sp.add_argument("--dry-run", action="store_true", help="只显示要删什么，不真删")
    add_json(sp)
    sp.set_defaults(func=cmd_clean)

    # v0.50：示例代码
    sp = sub.add_parser("examples", help="列出一些示例代码")
    add_json(sp)
    sp.set_defaults(func=cmd_examples)

    # v0.50：运行测试
    sp = sub.add_parser("test", help="运行所有测试用例（doctor 检查）")
    add_json(sp)
    sp.set_defaults(func=cmd_test)

    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    # mcp / lsp 后面的参数属于服务器自己（--version / --list / --selftest），
    # 这里不能拦；其它命令多写了参数就是写错了，必须报错而不是默默吞掉。
    if extra:
        if args.cmd in ("mcp", "lsp"):
            args.passthrough = list(getattr(args, "passthrough", []) or []) + extra
        else:
            parser.error("unrecognized arguments: " + " ".join(extra))
    return args.func(args)


def cmd_format(args) -> int:
    """自动格式化 AGL 代码。

    现在做最简单的：统一空行——去掉多余的空行，函数之间空一行。
    后面再做更复杂的：统一缩进、统一引号。
    """
    path = Path(args.file)
    try:
        src = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        print(f"读不了这个文件：{e}")
        return 1

    # 最简单的格式化：把多个空行合并成一个空行
    lines = src.splitlines()
    new_lines = []
    prev_blank = False
    for line in lines:
        # 把所有制表符替换成 2 个空格
        line = line.replace("\t", "  ")
        # 去掉行尾多余的空格
        line = line.rstrip()

        is_blank = (line.strip() == "")
        if is_blank and prev_blank:
            continue  # 跳过多余的空行

        # 如果是函数定义，而且前面不是空行，就加一个空行
        stripped = line.strip()
        if (stripped.startswith("函数") or stripped.startswith("异步 函数")) and new_lines:
            prev = new_lines[-1].strip()
            if prev != "" and not prev.startswith("#"):
                new_lines.append("")  # 加一个空行

        new_lines.append(line)
        prev_blank = is_blank

    # 检查文件头：有没有 `#语言=`
    first_line = new_lines[0] if new_lines else ""
    if not first_line.startswith("#语言="):
        # 没有文件头，加一行
        new_lines.insert(0, "#语言=中文")
        new_lines.insert(1, "")  # 空一行

    new_src = "\n".join(new_lines) + "\n"

    if args.check:
        # 只检查，不修改
        if new_src == src:
            print(f"✓ {path} 格式正确")
            return 0
        else:
            print(f"✗ {path} 格式不正确")
            return 1
    else:
        # 修改文件
        path.write_text(new_src, encoding="utf-8")
        print(f"✓ 已格式化：{path}")
        return 0


def cmd_lint(args) -> int:
    """检查代码问题。

    现在做最简单的：检查代码能不能正常解析。
    后面再做更复杂的：未使用的变量、命名不规范。
    """
    from compiler.lexer import detect_lang, tokenize
    from compiler.parser import parse

    path = Path(args.file)
    try:
        src = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        print(f"读不了这个文件：{e}")
        return 1

    errors = []
    warnings = []

    # 检查 0：文件是不是空的
    if not src.strip():
        errors.append("文件是空的")

    # 检查 0.5：有没有文件头 `#语言=`
    first_line = src.splitlines()[0] if src.splitlines() else ""
    if not first_line.startswith("#语言="):
        warnings.append("文件没有 `#语言=` 头注释——建议加上（用 `agl format` 自动加）")

    # 检查 1：能不能词法分析
    try:
        lang = detect_lang(src)
        tokens = tokenize(src, lang)
    except Exception as e:
        errors.append(f"词法分析失败：{e}")

    # 检查 2：能不能语法分析
    if not errors:
        try:
            tree = parse(tokens, lang)
        except Exception as e:
            errors.append(f"语法分析失败：{e}")

    # 检查 3：有没有 TODO / FIXME 注释
    if "TODO" in src or "FIXME" in src or "待办" in src:
        warnings.append("发现 TODO / FIXME 注释——记得清理一下")

    # 检查 4：文件是不是以换行结尾
    if src and not src.endswith("\n"):
        warnings.append("文件末尾没有换行——建议加上（用 `agl format` 自动加）")

    # 检查 5：文件是不是太大了
    line_count = len(src.splitlines())
    if line_count > 500:
        warnings.append(f"文件有 {line_count} 行——建议拆成多个模块，方便维护")

    if errors:
        print(f"✗ {path} 有 {len(errors)} 个问题：")
        for err in errors:
            print(f"  · {err}")
        if warnings:
            print()
            for w in warnings:
                print(f"  ! {w}")
        return 1
    else:
        if warnings:
            print(f"✓ {path} 没问题，但有 {len(warnings)} 个提示：")
            for w in warnings:
                print(f"  ! {w}")
        else:
            print(f"✓ {path} 没问题")
        return 0


def cmd_list_builtins(args) -> int:
    """列出所有内建函数。"""
    import tomllib

    # 读 keywords.toml
    keywords_path = Path(__file__).resolve().parents[2] / "spec" / "keywords.toml"
    try:
        with open(keywords_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"读不了 keywords.toml：{e}")
        return 1

    builtins = data.get("builtin", [])

    if _is_json(args):
        print(json.dumps(builtins, ensure_ascii=False, indent=2))
        return 0

    print(f"共 {len(builtins)} 个内建：\n")
    for b in builtins:
        zh = b.get("zh", "")
        en = b.get("en", "")
        params = ", ".join(b.get("params") or [])
        ret = b.get("ret", "")
        print(f"  {zh:10} ({en})  ({params}) -> {ret}")

    return 0


def _fallback_version() -> str:
    """版本号兜底：读安装元数据，不再把某个发布号写死在源码里。"""
    try:
        from importlib import metadata
        return metadata.version("agl-lang")
    except Exception:
        return "0.0.0+unknown"


def cmd_info(args) -> int:
    """显示 AGL 的基本信息。"""
    import tomllib

    info = {}

    # 版本号 / 内建函数数量：都从同一份关键字表读，避免版本号和内建数各写一份。
    keywords_path = Path(__file__).resolve().parents[2] / "spec" / "keywords.toml"
    try:
        with open(keywords_path, "rb") as f:
            data = tomllib.load(f)
        info["version"] = "v" + str(data.get("version") or _fallback_version())
        builtins = data.get("builtin", [])
        info["builtin_count"] = len(builtins)
    except Exception:
        info["version"] = "v" + _fallback_version()
        info["builtin_count"] = -1

    # 标准库模块数量
    stdlib_dir = Path(__file__).resolve().parents[2] / "stdlib"
    try:
        agl_files = list(stdlib_dir.glob("*.agl"))
        info["stdlib_count"] = len(agl_files)
    except Exception:
        info["stdlib_count"] = -1

    if _is_json(args):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print("AGL（Agent Grounded Language）")
    print(f"  版本：{info['version']}")
    print(f"  内建函数：{info['builtin_count']} 个")
    print(f"  标准库模块：{info['stdlib_count']} 个")
    print()
    print("常用命令：")
    print("  aglc run 文件名.agl    —— 编译并运行")
    print("  aglc format 文件名.agl —— 格式化代码")
    print("  aglc lint 文件名.agl   —— 检查代码")
    print("  aglc --version         —— 看版本号")

    return 0


def cmd_clean(args) -> int:
    """清理编译生成的临时文件。"""
    import shutil

    project_root = Path(__file__).resolve().parents[2]
    # wheel 安装后 project_root 是 site-packages，不能把整个环境当仓库清理；
    # 只清理调用者当前目录。
    if not (project_root / "pyproject.toml").is_file():
        project_root = Path.cwd()

    deleted = []

    # 找所有 .agl.ir 文件
    for f in project_root.rglob("*.agl.ir"):
        deleted.append(str(f.relative_to(project_root)))
        if not args.dry_run:
            f.unlink()

    # 找所有 __pycache__ 目录
    for d in project_root.rglob("__pycache__"):
        if d.is_dir():
            deleted.append(str(d.relative_to(project_root)) + "/")
            if not args.dry_run:
                shutil.rmtree(d)

    if args.dry_run:
        print(f"将删除 {len(deleted)} 个文件/目录：")
    else:
        print(f"已删除 {len(deleted)} 个文件/目录：")

    for item in deleted:
        print(f"  · {item}")

    if not deleted:
        print("  （没有要清理的）")

    return 0


def cmd_examples(args) -> int:
    """列出发布示例；代码本身在 examples/ 下，由 CI 真实编译运行。"""
    root = Path(__file__).resolve().parents[2]
    examples_dir = root / "examples"
    if not examples_dir.is_dir():
        print("没有找到 examples/ 目录", file=sys.stderr)
        return 1
    print("AGL 发布示例（均可直接 run；完整说明见 examples/README.md）：")
    for path in sorted(examples_dir.rglob("*.agl")):
        rel = path.relative_to(root).as_posix()
        print(f"  {rel}")
    print("\n运行单个示例：python -m compiler.cli run <上面的路径>")
    print("一次验证全部：python examples/run_examples.py")
    return 0


def cmd_test(args) -> int:
    """运行所有测试用例；用户运行时包没有 tests/ 时给出明确提示。"""
    import subprocess
    root = Path(__file__).resolve().parents[2]
    if not (root / "tests" / "suite" / "checks.py").is_file():
        print("用户运行时包不含开发测试套件（tests/）。")
        print("安装自检请运行：aglc doctor")
        return 0
    result = subprocess.run(
        [sys.executable, "-m", "tests.suite"],
        cwd=root,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
