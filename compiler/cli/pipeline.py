# -*- coding: utf-8 -*-
"""编译管线 + 运行时错误回译。

所有阶段的错误都必须走这里，**对外只暴露 AGL 的行号**，
宿主语言的位置只放在 `cause` 里备查。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from compiler import backend_py, checker, diagnostics
from compiler.diagnostics import AglError, Code, Diagnostic, FixHint
from compiler.ir import build, lower, validate_ir
from compiler.lexer import detect_lang, tokenize
from compiler.parser import parse


@dataclass
class Result:
    path: Path
    source: str = ""
    lang: str = "zh"
    ir: dict | None = None
    python: str = ""
    smap: dict[int, int] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    tokens: list = field(default_factory=list)
    exports: dict[str, dict] = field(default_factory=dict)
    deps: dict[str, "Result"] = field(default_factory=dict)
    opt_notes: list[str] = field(default_factory=list)   # 优化通道做了什么

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _attach_source(diag: Diagnostic, lines: list[str]) -> None:
    """把源码行贴进诊断，渲染时能画出插入符。"""
    if 1 <= diag.line <= len(lines):
        diag.source_line = lines[diag.line - 1]


def compile_source(source: str, filename: str,
                   externals: dict[str, dict] | None = None,
                   is_entry: bool = True,
                   optimize: bool = False,
                   source_path: str | None = None) -> Result:
    res = Result(path=Path(filename), source=source)
    lines = source.splitlines()

    lang = detect_lang(source)
    if lang:
        res.lang = lang

    # ── 1. 词法 ──────────────────────────────────────────────────────
    try:
        res.tokens = tokenize(source, filename)
    except AglError as e:
        _attach_source(e.diag, lines)
        res.diagnostics.append(e.diag)
        return res

    # ── 2. 语法 ──────────────────────────────────────────────────────
    try:
        ast = parse(res.tokens, filename, res.lang)
    except AglError as e:
        _attach_source(e.diag, lines)
        res.diagnostics.append(e.diag)
        return res

    # ── 3. IR ────────────────────────────────────────────────────────
    ir = build(ast, res.lang)

    # 库文件不需要入口函数：写了 `模块 X` 又不含入口的，本身就是给别人导入的库
    mod = ir["program"]["modules"][0]
    entry_name = ir["program"]["entry"]
    defines_entry = any(f["name"] == entry_name for f in mod["functions"])
    if is_entry and not defines_entry and mod.get("name"):
        is_entry = False

    # ── 4. 语义 / 效应 ────────────────────────────────────────────────
    for d in checker.check(ir, res.lang, externals=externals, is_entry=is_entry,
                           source_path=source_path):
        _attach_source(d, lines)
        res.diagnostics.append(d)

    # 本模块对外可见的函数表（给导入它的模块用）
    exported = set(mod.get("exports") or [])
    res.exports = {f["name"]: f for f in mod["functions"] if f["name"] in exported}

    # ── 5. 降级告警 ──────────────────────────────────────────────────
    ir, warns = lower(ir)
    for d in warns:
        _attach_source(d, lines)
        res.diagnostics.append(d)

    # ── 5.5 优化通道（默认关闭；开了就必须与朴素版输出一致）──────────
    if optimize:
        from compiler.ir import opt as opt_mod
        ir, res.opt_notes = opt_mod.optimize(ir)

    # ── 6. IR 自校验（M4 的硬门槛）────────────────────────────────────
    if not res.errors:
        problems = validate_ir(ir)
        if problems:
            sp = {"file": filename, "line": 1, "col": 0, "endCol": 1}
            first_fn = ir["program"]["modules"][0]["functions"][0]
            sp = first_fn["span"]
            res.diagnostics.append(Diagnostic(
                code=Code.IR_SCHEMA,
                message_zh="编译器的中间表示不合规（这是编译器自身的问题，不是你的代码问题）",
                file=sp["file"], line=sp["line"], col=sp["col"], end_col=sp["endCol"],
                cause="; ".join(problems[:3]),
                fix_hints=[FixHint("report_bug", f"{filename}:{sp['line']}",
                                   "把这段代码和报错一起上报，用于补用例")],
            ))

    res.ir = ir

    # ── 7. 后端 ──────────────────────────────────────────────────────
    if res.ok:
        try:
            code, smap = backend_py.generate(ir, lines)
            res.python, res.smap = code, smap
        except Exception as e:  # 后端自身缺陷，不能伪装成用户代码错
            res.diagnostics.append(Diagnostic(
                code=Code.UNSUPPORTED,
                message_zh="这段代码里有一处语法，当前版本 的转译后端还不支持",
                file=filename, line=1, col=0, end_col=1,
                cause=f"{type(e).__name__}: {e}",
                fix_hints=[FixHint("report_bug", f"{filename}:1",
                                   "先用更基础的写法绕开，并把这个样例上报补支持")],
            ))
    return res


def _symbols(res: Result) -> dict:
    """把一个编译好的模块拆成「导出的」和「私有的」几张表。

    函数之外，`导出 类型` / `导出 常量` 也在这里汇总：导入方要拿结构体的字段
    定义做类型校验，要拿常量的类型做注解推断，都需要跨文件可见。
    """
    mod = res.ir["program"]["modules"][0]
    exported = set(mod.get("exports") or [])
    return {
        "exports": {f["name"]: f for f in mod["functions"] if f["name"] in exported},
        "private": {f["name"]: f for f in mod["functions"] if f["name"] not in exported},
        "types": {t["name"]: t for t in (mod.get("types") or []) if t["name"] in exported},
        "consts": {c["name"]: c for c in (mod.get("consts") or []) if c["name"] in exported},
        "private_consts": [c["name"] for c in (mod.get("consts") or [])
                           if c["name"] not in exported],
    }


def compile_project(entry: Path, optimize: bool = False,
                    override_source: str | None = None) -> Result:
    """编译入口文件及其全部依赖，返回入口文件的编译结果。

    依赖先编译（拓扑序），它们的导出表成为后续模块的 externals。
    任何一环失败就原样返回那一环的诊断 —— 报错指向真正出问题的文件，
    而不是一律甩给入口文件。

    override_source 用于编辑器场景：编译缓冲区里的内容（还没存盘），
    但模块解析仍按磁盘上的路径关系走，这样"边打字边看错"才不会误报模块找不到。
    """
    from compiler import modules as modules_mod

    try:
        order = modules_mod.resolve(entry)
    except AglError as e:
        return Result(path=entry, diagnostics=[e.diag])

    externals: dict[str, dict] = {}
    built: dict[str, Result] = {}

    for name, path in order:
        if override_source is not None and path == entry:
            src = override_source
        else:
            try:
                src = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return Result(path=path, diagnostics=[Diagnostic(
                    code=Code.BAD_CHAR, severity="error",
                    message_zh="文件不是 UTF-8 编码，读不出来",
                    file=path.name, line=1, col=0, end_col=1,
                    fix_hints=[FixHint("save_utf8", path.name, "用编辑器另存为 UTF-8")],
                )])
        res = compile_source(src, path.name, externals=dict(externals),
                             is_entry=(path == entry), optimize=optimize,
                             source_path=str(path))
        if not res.ok:
            return res
        externals[name] = _symbols(res)
        built[name] = res

    main = built[entry.stem]
    main.deps = {n: r for n, r in built.items() if n != entry.stem}
    return main


def compile_file(path: str | Path, optimize: bool = False) -> Result:
    p = Path(path).resolve()
    if not p.exists():
        return Result(path=p, diagnostics=[Diagnostic(
            code="AGL-E-0101", severity="error",
            message_zh=f"找不到文件：{p.name}",
            file=p.name, line=1, col=0, end_col=1,
            fix_hints=[FixHint("check_path", p.name, "确认路径拼写，或用相对路径重试")],
        )])
    try:
        p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Result(path=p, diagnostics=[Diagnostic(
            code=Code.BAD_CHAR, severity="error",
            message_zh="文件不是 UTF-8 编码，读不出来",
            file=p.name, line=1, col=0, end_col=1,
            fix_hints=[FixHint("save_utf8", p.name, "用编辑器另存为 UTF-8")],
        )])
    return compile_project(p, optimize=optimize)


# ══════════════════════════════════════════════════════════════════════
# 运行时错误回译（AGL-RT-00xx）
# ══════════════════════════════════════════════════════════════════════
_EXC_MAP: list[tuple[tuple[type, ...], str, str, str]] = [
    ((ZeroDivisionError,), Code.RT_DIV_ZERO, "不能除以零", "检查除数是否可能为 0；先判断再除"),
    ((IndexError,), Code.RT_INDEX, "列表取值越界", "先判断列表长度，再按索引取"),
    ((KeyError,), Code.RT_KEY, "映射表里没有这个键", "先用「键 在 表」判断，或给默认值"),
    ((FileNotFoundError,), Code.RT_NOFILE, "文件不存在", "检查路径；先判断文件是否存在"),
    ((PermissionError,), Code.RT_PERM, "没有权限访问", "换一个路径，或以更高权限运行"),
    ((AttributeError,), Code.RT_NULL_MEMBER, "这里读不到它的成员（值可能是空）", "先判断是否为空，再取成员"),
]


def _classify(exc: BaseException) -> tuple[str, str, str]:
    for types, code, msg, hint in _EXC_MAP:
        if isinstance(exc, types):
            return code, msg, hint
    return Code.RT_GENERIC, "程序意外停止", "看下面的原因"


# FFI 异常：这些异常类定义在**生成出来的 Python 里**（运行时支持段），
# 编译器这边拿不到它们的类型对象，只能按类名认 —— 但类名是后端写死的，
# 前后端同属一套代码，不存在"对不上"的风险。
def _classify_ffi(exc: BaseException) -> tuple[str, str, str] | None:
    """FFI 异常 → (错误码, 中文原因, 修复建议)。不是 FFI 异常返回 None。"""
    name = type(exc).__name__
    if name == "_AglFfiMissing":
        lib = getattr(exc, "lib", "")
        return (Code.RT_FFI_MISSING,
                f"找不到外部库 `{lib}`：它需要先在环境里装好才能用",
                f"先把 `{lib}` 装上，或在代码里换一个已经装好的库")
    if name == "_AglFfiNoMember":
        lib = getattr(exc, "lib", "")
        method = getattr(exc, "method", "")
        return (Code.RT_FFI_NO_MEMBER,
                f"外部库 `{lib}` 里没有 `{method}` 这个方法",
                "查一下这个库的方法名到底怎么拼")
    if name == "_AglFfiCallError":
        lib = getattr(exc, "lib", "")
        method = getattr(exc, "method", "")
        reason = str(getattr(exc, "reason", "") or "")
        msg = f"调用外部库 `{lib}.{method}` 时出错"
        if reason:
            msg += f"：{reason}"
        return (Code.RT_FFI_ERROR, msg, "看看这个外部方法要什么参数、什么输入")
    return None


# 宿主语言的类型名 → AGL 里的叫法。给人看的消息里不许出现前者。
_HOST_TYPE_ZH = {
    "str": "文本",
    "list": "列表",
    "dict": "映射表",
    "int": "整数",
    "float": "浮点数",
    "bool": "真假值",
    "NoneType": "空值",
    "module": "模块",
}

# 回调函数在 Python 侧叫 `_agl_cb_<源码行号>` —— 那是给编译器自己看的内部
# 生成名，给人看的「AGL 调用栈」和错误横幅里必须换成 AGL 的说法。
_CB_FRAME = re.compile(r"_agl_cb_\d+")


def _agl_disp_name(py_name: str) -> str:
    return "任务回调" if _CB_FRAME.fullmatch(py_name) else py_name


def translate_runtime_error(
    exc: BaseException,
    smap: dict[int, int],
    source_lines: list[str],
    agl_name: str,
    virt_name: str,
    filemap: dict[str, tuple[str, dict[int, int], list[str]]] | None = None,
) -> Diagnostic:
    """把宿主异常翻译成指向 AGL 源码的诊断。

    filemap 用于跨模块情形：被导入模块的 Python 产物是真实文件，
    traceback 里出现的是它的路径，需要按这张表换回对应的 AGL 文件与行号。
    """
    frames: list[tuple[str, int, str]] = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append((tb.tb_frame.f_code.co_filename, tb.tb_lineno,
                       tb.tb_frame.f_code.co_name))
        tb = tb.tb_next

    # 每一帧换回 AGL 侧的位置：(agl 文件, AGL 行号, 函数名)
    agl_frames: list[tuple[str, int, str]] = []
    for fname, ln, fn_name in frames:
        disp = _agl_disp_name(fn_name)
        if fname == virt_name:
            al = smap.get(ln, 0)
            if al:
                agl_frames.append((agl_name, al, disp))
        elif filemap and fname in filemap:
            dep_name, dep_smap, _ = filemap[fname]
            al = dep_smap.get(ln, 0)
            if al:
                agl_frames.append((dep_name, al, disp))

    code, msg, hint = _classify(exc)
    ffi = _classify_ffi(exc)
    if ffi is not None:
        # FFI 异常的说明已经是完整中文，不再套通用模板。
        code, msg, hint = ffi

    if not agl_frames:
        return Diagnostic(
            code=Code.RT_UNMAPPED,
            message_zh="程序停在一个无法定位到源码的地方",
            file=agl_name, line=1, col=0, end_col=1,
            cause=f"{type(exc).__name__}: {exc}",
            fix_hints=[FixHint("report_sample", agl_name, hint)],
        )

    # 最后一帧就是出错处；它可能在被导入的模块里
    err_file, agl_line, _ = agl_frames[-1]

    stack = [{"fn": name, "file": f, "line": ln} for f, ln, name in agl_frames]

    detail = str(exc)
    if code == Code.RT_INDEX:
        # 内建函数自己抛的越界已经是一句完整中文（如「切片越界：这段文本只有 2 个字符…」），
        # 再套一层模板会变成「列表取值越界（切片越界：…）」这种叠词。
        if any("\u4e00" <= c <= "\u9fff" for c in detail):
            msg = detail
            hint = "先判断长度够不够，再取这一段"
        else:
            msg = f"列表取值越界（{detail}）"
    elif code == Code.RT_KEY:
        msg = f"映射表里没有这个键（{detail}）"
    elif code in (Code.RT_NOFILE, Code.RT_PERM):
        msg = f"{msg}：{detail}"
    elif code == Code.RT_NULL_MEMBER:
        # 读成员失败有两种原因，必须分开说，否则会把人引到错误方向：
        #   值是空 → 先判空
        #   值不空但没这个成员 → 名字写错了，或用了语言没提供的属性
        if "NoneType" in detail:
            msg = "这里读不到它的成员：这个值是空"
            hint = "先判断这个值是不是空，再取成员"
        else:
            code = Code.RT_NO_MEMBER
            attr = detail.rsplit("'", 2)[-2] if detail.count("'") >= 2 else ""
            holder = detail.split("'")[1] if detail.count("'") >= 2 else ""
            # 宿主语言的类型名不能出现在给人看的消息里，换成 AGL 的说法
            holder = _HOST_TYPE_ZH.get(holder, "值")
            msg = f"这个{holder}没有 `{attr}` 这个成员" if attr else f"这个{holder}没有这个成员"
            hint = "检查成员名拼写；文本、列表、映射表目前只提供 `.长度`"
    elif code == Code.RT_GENERIC and any("一" <= c <= "鿿" for c in detail):
        # 内建函数自己抛的中文诊断（如「合并映射的第 N 个参数要映射表」、
        # 或「取到(失败(…))：拿不到值…」）已是一句完整中文，直接当「问题」
        # 文案 —— 与 RT_INDEX「已是完整中文就不套模板」同一条纪律。
        # 否则现成的中文会被压进「原因」栏，正文只剩一句「程序意外停止」。
        msg = detail

    # 原因栏是「备查」用的：允许出现宿主那边的类型名，方便排查。
    # 但 FFI 异常要报**原始**异常（ValueError 等），而不是我们自己的包装类。
    cause = f"{type(exc).__name__}: {detail}"
    if ffi is not None:
        orig = getattr(exc, "orig", None)
        if orig is not None:
            cause = f"{type(orig).__name__}: {detail}"

    diag = Diagnostic(
        code=code,
        message_zh=msg,
        file=err_file,
        line=agl_line or 1,
        col=0,
        end_col=1,
        stack=stack,
        cause=cause,
        fix_hints=[FixHint("runtime_fix", f"{err_file}:{agl_line or 1}", hint)],
    )

    # 出错处的源码行：主文件用传入的，依赖模块用自己的
    err_lines = source_lines
    if filemap and err_file != agl_name:
        for dep_name, _, dep_lines in filemap.values():
            if dep_name == err_file:
                err_lines = dep_lines
                break
    if agl_line and 1 <= agl_line <= len(err_lines):
        diag.source_line = err_lines[agl_line - 1]
    return diag


def run_result(res: Result) -> tuple[bool, Diagnostic | None, str]:
    """执行编译产物。返回 (是否成功, 错误诊断, 捕获到的 stdout)。"""
    import io
    import shutil
    import sys
    import tempfile

    virt = f"<agl:{res.path.name}>"
    buf = io.StringIO()
    ns = {"__name__": "__main__", "__file__": virt}
    old = sys.stdout
    tmpdir: str | None = None
    # 依赖模块的 Python 产物路径 -> (AGL 文件名, 行号映射, AGL 源码行)
    filemap: dict[str, tuple[str, dict[int, int], list[str]]] = {}
    try:
        if res.deps:
            tmpdir = tempfile.mkdtemp(prefix="agl-run-")
            for name, dep in res.deps.items():
                py = Path(tmpdir) / f"{backend_py.host_module_name(name)}.py"
                py.write_text(dep.python, encoding="utf-8")
                filemap[str(py)] = (dep.path.name, dep.smap, dep.source.splitlines())
            sys.path.insert(0, tmpdir)

        compiled = compile(res.python, virt, "exec")
        sys.stdout = buf
        exec(compiled, ns)
        sys.stdout = old
        return True, None, buf.getvalue()
    except SystemExit:
        sys.stdout = old
        return True, None, buf.getvalue()
    except BaseException as e:  # noqa: BLE001 —— 用户代码什么都可能抛
        sys.stdout = old
        diag = translate_runtime_error(
            e, res.smap, res.source.splitlines(), res.path.name, virt, filemap)
        return False, diag, buf.getvalue()
    finally:
        sys.stdout = old
        if tmpdir:
            try:
                sys.path.remove(tmpdir)
            except ValueError:
                pass
            shutil.rmtree(tmpdir, ignore_errors=True)
        # 必须清掉本次导入的模块：Python 会按模块名永久缓存，
        # 同一个进程里再跑另一个目录的同名模块就会拿到上次的旧代码
        # （它的 traceback 还指着已删除的临时文件，行号映射全错）。
        for name in (res.deps or {}):
            sys.modules.pop(backend_py.host_module_name(name), None)
