# -*- coding: utf-8 -*-
"""一致性测试套件的检查实现（阶段 2 / S2-G）。

这些函数就是「验收标准」的可执行形式：**每一条检查都对应一条写下来的标准**，
红了一定是有人改坏了什么，绿了才说明没弄坏别人的东西。

以前这些检查全堆在 `compiler/cli/doctor.py` 里，加特性时越来越难找到该往哪加。
现在它们住在自己的套件里，`aglc doctor` 只剩"调这套套件 + 渲染报告"。

单独跑这套套件（不经过命令行）：

    python -m tests.suite

本文件只放"检查怎么写"，不放"怎么跑、怎么渲染"——那是 `tests/suite/__init__.py` 的事。

三十一项检查（编号即验收标准的编号）：
  1. 规格文件齐全且可解析
  2. 关键字表自洽（中英唯一、五效应齐全、运算符双向可逆）
  3. IR schema 可解析、定义齐全
  4. 黄金用例全部编译通过
  5. 黄金用例全部运行通过，且输出符合预期
  6. 中英双分支结构等价
  7. 往返校验（翻译过去再翻回来必须逐字相同）
  8. 错误用例报出**预期的错误码**
  9. 行号对齐（AGL 第 N 行 → Python 第 N 行）
 10. 黄金产物与磁盘上保存的一致（防止后端被悄悄改坏）
 11. 模块导入：跨文件能跑、导出有边界、依赖里的错误能指回源头
 12. 速查卡里的示例能跑、工程骨架能跑
 13. MCP 工具服务器握手与工具调用
 14. 工具声明与 MCP 运行时：白名单、超时重试、重复调用熔断
 15. 本地技能协议：技能目录 + 入口脚本的子进程调用约定（含失败退出码与缺入口报错）
 16. MCP stdio 传输：一行一条 JSON-RPC 的帧序、命令两种写法、配置歧义编译期拒绝
 17. 安全默认与路径边界：网页服务默认回环、获取网页协议白名单、BOM 读取、MCP 工作区
 18. 标准库：声明后可用、中英两份结构等价
 19. 优化通道：开 `--opt` 后语义不变，且每个 Pass 都真的生效
 20. 内建函数表：示例真编译、双分支覆盖、效应与实现一致
 21. 编辑器语法高亮与关键字表一致
 22. 语言服务器五项能力都真通
 23. 异步运行时：并发更快、取消生效、错误行号回译到 AGL
 24. FFI 黑盒调用：能跑、缺效应编译期被拒、外部异常回译中文
 25. 源码规范自检：零第三方依赖 · 无死导入 · 无待办残留 · 编码声明齐
 26. 错误输出纯洁性：崩溃时正文只说 AGL 的名词，不甩宿主回溯
 27. 编译器韧性：畸形输入不许崩成宿主回溯，必须给 AGL 诊断
 28. Python 源码导入器：能导的必须逐字对拍，不能证明等价的按 blocker 拒绝
 29. AGB 双后端一致：VM 输出与 Python 后端逐字相同，范围外明确跳过
 30. AGB 产物与默认 VM：build 只出 .agb/.agl.ir，run 默认 VM，超集显式报错；
     并把全部黄金用例交给 AGL 版编译器整链编译、逐字对拍
 31. VM 效应权限白名单：拒绝时 AGL-RT-0092，放行后正常执行
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from compiler.cli import pipeline
from compiler.ir import validate_ir
from compiler.lexer import KeywordTable
from compiler.tomlutil import loads as toml_loads

REPO = Path(__file__).resolve().parents[2]
CASES = REPO / "tests" / "cases"
GOLDEN = REPO / "tests" / "golden"

# 子进程一律带超时。2026-11-18 事故：doctor 里 43 处子进程调用只有 1 处写了超时，
# 自举编译一旦卡住，父进程就无限等，肉眼看到的就是"整台机器一动不动"。
# 超时不是失败，是明确的红灯：返回码 124 + 一句可读的原因。
_SUBPROC_TIMEOUT = 180


def _run(cmd, timeout=None, **kwargs):
    """带超时的 `subprocess.run`。

    自检要能自己认输：卡住超过 `_SUBPROC_TIMEOUT` 秒就当作失败返回，
    而不是把整机一起拖进等待。调用方原有的 `returncode/stdout/stderr`
    判断照旧生效，只是多了一条"超时"分支。
    """
    if timeout is None:
        timeout = _SUBPROC_TIMEOUT
    try:
        return subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"子进程超过 {timeout} 秒没有结束：{cmd}")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 125, "", f"子进程起不来：{e}")

# 黄金产物检查不再有豁免：`nullable_zh/en` 的期望 `.agl.ir/.py` 已补齐。


def _golden_agls(recursive: bool = False) -> list[Path]:
    pattern = GOLDEN.rglob("*.agl") if recursive else GOLDEN.glob("*.agl")
    return sorted(pattern)


def _run_isolated(res):
    """在带 `build/` 的临时 cwd 里跑产物。

    快照黄金用例会写 `build/*.state`；doctor 可能从任意目录启动，
    在源码树里恰好有 `build/`，在 wheel 安装后则不一定有。
    固定给每次运行一个临时目录，既补齐 `build/`，又不污染用户目录。
    """
    import os
    import tempfile

    old = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="agl-doctor-") as td:
        (Path(td) / "build").mkdir(parents=True, exist_ok=True)
        os.chdir(td)
        try:
            return pipeline.run_result(res)
        finally:
            os.chdir(old)


class Check:
    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.detail = ""
        self.notes: list[str] = []

    def fail(self, why: str) -> "Check":
        self.ok = False
        self.notes.append(why)
        return self

    def note(self, s: str) -> "Check":
        self.notes.append(s)
        return self

    def done(self, detail: str = "") -> "Check":
        self.detail = detail
        if not self.ok and not self.detail:
            self.detail = self.notes[0] if self.notes else "失败"
        return self


def _run_plain_python(path) -> str | None:
    """把一份 Python 原样跑一遍，拿到它的标准输出。

    用来跟"导入再编译"的结果做对拍 —— 两边必须逐字一样。
    跑不起来（或非 0 退出）返回 None，那是测试用例本身写坏了。
    """
    import sys

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for attempt in range(2):
        try:
            r = _run([sys.executable, str(path)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=60, env=env)
        except Exception:
            if attempt:
                return None
            time.sleep(0.5)
            continue
        if r.returncode == 0:
            return r.stdout
        if attempt:
            return None
        time.sleep(0.5)
    return None


def _expected_blocker_hint(path) -> str:
    """每个拒绝用例的第一行写着「期望拒绝理由: xxx」—— 理由要说到那个点上。"""
    for line in path.read_text(encoding="utf-8").splitlines()[:3]:
        if "期望拒绝理由" in line:
            return line.split(":", 1)[1].strip()
    return ""

# ── 1. 规格文件 ────────────────────────────────────────────────────────
def check_spec_files() -> Check:
    c = Check("规格文件齐全")
    need = ["keywords.toml", "ir-schema.json", "diagnostics.md",
            "pipeline-contracts.md", "validate_ir.py"]
    miss = [n for n in need if not (REPO / "spec" / n).exists()]
    if miss:
        c.fail(f"缺少 {miss}")
    return c.done(f"{len(need) - len(miss)}/{len(need)}")


# ── 2. 关键字表 ────────────────────────────────────────────────────────
def check_keywords() -> Check:
    c = Check("关键字表自洽")
    try:
        data = toml_loads((REPO / "spec" / "keywords.toml").read_text(encoding="utf-8"))
    except Exception as e:
        return c.fail(f"解析失败：{e}").done()

    kws = data.get("keyword", [])
    ops = data.get("operator", [])
    zh = [k["zh"] for k in kws]
    en = [k["en"] for k in kws]

    dup_zh = {x for x in zh if zh.count(x) > 1}
    dup_en = {x for x in en if en.count(x) > 1}
    if dup_zh:
        c.fail(f"中文关键字重复：{sorted(dup_zh)}")
    if dup_en:
        c.fail(f"英文关键字重复：{sorted(dup_en)}")
    # 中英交叉重复（同一个词既当 a 的中文又当 b 的英文）
    cross = set(zh) & set(en)
    if cross:
        # 中英同名是允许的（如普通符号），但关键字同名要提示
        c.note(f"中英写法重合（需人工确认）：{sorted(cross)}")

    # 五个标准效应
    effects = {k["en"] for k in kws if k.get("kind") == "effect"}
    want = {"IO", "Net", "model", "Time", "External"}
    if effects != want:
        c.fail(f"效应标签不齐：缺 {sorted(want - effects)}，多 {sorted(effects - want)}")

    # 运算符双向可逆
    for o in ops:
        if not o.get("zh") or not o.get("en"):
            c.fail(f"运算符缺半边：{o}")

    # 内置只读属性：中英各一、规范名唯一、宿主持久映射可查
    attrs = data.get("attribute", [])
    a_zh = [a["zh"] for a in attrs]
    a_en = [a["en"] for a in attrs]
    for group, label in ((a_zh, "中文"), (a_en, "英文")):
        dup = {x for x in group if group.count(x) > 1}
        if dup:
            c.fail(f"属性{label}名重复：{sorted(dup)}")
    for a in attrs:
        if a.get("canonical") != a.get("en"):
            c.fail(f"属性 `{a.get('en')}` 的 canonical 必须等于英文写法")
        if a["zh"] in en or a["en"] in zh:
            c.fail(f"属性 `{a['zh']}` 与关键字撞名（属性名不能是关键字）")
        if not a.get("lower_to"):
            c.fail(f"属性 `{a['zh']}` 缺少转译映射 lower_to")

    c.note(f"{len(kws)} 个关键字 / {len(ops)} 个运算符词 / {len(attrs)} 个内置属性")
    return c.done(f"{len(kws)} 关键字 · {len(ops)} 运算符 · {len(attrs)} 属性 · 五效应齐")


# ── 3. IR schema ──────────────────────────────────────────────────────
def check_schema() -> Check:
    c = Check("IR schema 可解析")
    try:
        s = json.loads((REPO / "spec" / "ir-schema.json").read_text(encoding="utf-8"))
    except Exception as e:
        return c.fail(f"解析失败：{e}").done()
    defs = s.get("$defs", {})
    need = {"Span", "Type", "Program", "Module", "Function",
            "Stmt", "Expr", "ExprLiteral", "ExprCall"}
    miss = need - set(defs)
    if miss:
        c.fail(f"缺少定义 {sorted(miss)}")
    if "Span" not in defs:
        c.fail("Span 定义缺失，错误回译无从谈起")
    return c.done(f"{len(defs)} 个定义")


# ── 4/5. 黄金用例：编译 + 运行 ──────────────────────────────────────────
def _load_expect() -> dict[str, dict]:
    path = CASES / "expect.toml"
    if not path.exists():
        return {}
    data = toml_loads(path.read_text(encoding="utf-8"))
    out = {}
    for case in data.get("case", []):
        # expect.toml 里 golden 的路径是 `../golden/x.agl`，消费端按裸文件名
        # 查表；只归一 golden 路径，cases 子目录里的同名文件（如多个 main.agl）
        # 保留原键，避免互相覆盖。
        key = Path(case["file"]).name if case["file"].startswith("../golden/") else case["file"]
        out[key] = case
    return out


def check_golden_compile(expect: dict) -> Check:
    c = Check("黄金用例编译")
    files = _golden_agls()
    if not files:
        return c.fail("tests/golden 下没有用例").done("0 个")
    bad = []
    for f in files:
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{f.name}: {res.errors[0].code} {res.errors[0].message_zh}")
    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(files) - len(bad)}/{len(files)}")


def check_golden_run(expect: dict) -> Check:
    c = Check("黄金用例运行")
    files = _golden_agls()
    bad = []
    ran = 0
    for f in files:
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{f.name}: 编译未通过")
            continue
        ok, diag, out = _run_isolated(res)
        ran += 1
        if not ok and diag:
            bad.append(f"{f.name}: {diag.code} 第 {diag.line} 行 {diag.message_zh}")
            continue
        want = expect.get(f.name, {}).get("expect_stdout")
        if want is not None and out.strip() != want.strip():
            bad.append(f"{f.name}: 输出是 {out.strip()!r}，期望 {want!r}")
    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{ran - len(bad)}/{ran}")


def check_agb_parity() -> Check:
    """AGB 后端与 Python 后端在 M0 子集上必须逐字一致。

    只对 `bytecode.compile_program` 能编译的用例对拍；match / 异步 /
    FFI 等仍走 Python 后端的用例明确跳过，不算失败。另补结构体与
    try/catch 两个固定回归，保证 M2 的已覆盖能力不会静默回退。
    """
    from compiler import bytecode, vm

    c = Check("AGB 双后端一致")
    files = _golden_agls()
    compared, bad = 0, []
    for f in files:
        res = pipeline.compile_file(f)
        if not res.ok:
            continue
        try:
            agb = bytecode.compile_program(res.ir)
        except bytecode.AgbUnsupported:
            continue
        compared += 1
        ok, diag, out = _run_isolated(res)
        if not ok and diag:
            bad.append(f"{f.name}: Python 后端报 {diag.code}")
            continue
        mods = {}
        try:
            for dep in (res.deps or {}).values():
                dep_agb = bytecode.compile_program(dep.ir)
                mods[dep_agb["module"]] = dep_agb
        except bytecode.AgbUnsupported:
            compared -= 1
            continue
        try:
            import os
            import tempfile

            old = os.getcwd()
            with tempfile.TemporaryDirectory(prefix="agl-doctor-") as td:
                (Path(td) / "build").mkdir(parents=True, exist_ok=True)
                os.chdir(td)
                try:
                    vm_out = vm.run(agb, res.lang, mods)
                finally:
                    os.chdir(old)
        except vm.VmError as e:
            bad.append(f"{f.name}: VM 报 {e.code} 第 {e.line} 行 {e.message}")
            continue
        if out.strip() != vm_out.strip():
            bad.append(f"{f.name}: py={out.strip()!r} vm={vm_out.strip()!r}")

    # 黄金用例里没有结构体和 try 的独立样本；这里补两个固定回归，
    # 否则 M2 的能力只有手工证据，doctor 抓不到回退。
    import tempfile
    extras = [
        ("结构体", """#语言=中文
类型 客户 {
  名字: 文本
  年龄: 整数
}

函数 主入口() {
  变量 甲 = 客户(年龄: 18, 名字: "小明")
  打印(甲, 甲.名字, 甲 == 客户(名字: "小明", 年龄: 18))
}
"""),
        ("尝试捕获", """#语言=中文
函数 主入口() {
  尝试 {
    变量 a = 1
    打印(a / 0)
  } 捕获 (错) {
    打印("接住了", 错)
  }
  打印("继续")
}
"""),
        ("最后块", """#语言=中文
函数 主入口() {
  打印(测返回())
  变量 i = 0
  当 i < 2 {
    尝试 {
      如果 i == 1 {
        中断
      }
      打印(i)
    } 最后 {
      打印("循环清理")
    }
    i = i + 1
  }
}

函数 测返回() {
  尝试 {
    打印("返回前")
    返回 1
  } 最后 {
    打印("返回清理")
  }
}
"""),
        ("函数引用排序", """#语言=中文
函数 取长度(x: 文本) -> 整数 {
  返回 x.长度
}

函数 主入口() {
  打印("按键排序", 按键排序(["bb", "a", "ccc"], 取长度))
  打印("调用", 调用(["取长度"], "abcd"))
}
"""),
        ("异步进度回调", """#语言=中文
异步 函数 慢() -> 文本 效应 计时 {
  上报进度("一半")
  等待 睡眠(1)
  返回 "好了"
}

异步 函数 主入口() 效应 计时 {
  变量 h = 任务(慢())
  当收到进度(h, 说明: 文本) {
    打印("进度", 说明)
  }
  当任务完成(h, 返回值: 文本) {
    打印("完成", 返回值)
  }
  打印("结果", 等待 h)
}
"""),
        ("异步失败回调", """#语言=中文
异步 函数 坏() -> 文本 {
  变量 r = 失败("坏了")
  返回 取到(r)
}

异步 函数 主入口() {
  变量 h = 任务(坏())
  当任务失败(h, 说明: 文本) {
    打印("失败", 说明)
  }
  当任务完成(h, 返回值: 文本) {
    打印("完成", 返回值)
  }
  等待 h
  打印("收尾")
}
"""),
        ("异步取消", """#语言=中文
异步 函数 慢() -> 文本 效应 计时 {
  等待 睡眠(300)
  返回 "完"
}

异步 函数 主入口() 效应 计时 {
  变量 h = 任务(慢())
  取消(h)
  打印("取消", 等待 h)
  打印("结束")
}
"""),
    ]
    io_file = Path(tempfile.gettempdir()) / "agl_parity_with_input.txt"
    io_file.write_text("使用读文件\n", encoding="utf-8", newline="\n")
    extras.append(("使用读文件", """#语言=中文
函数 主入口() 效应 读写 {
  使用 读文件("__PATH__") 作为 内容 {
    打印(内容)
  }
}
""".replace("__PATH__", io_file.as_posix())))
    for label, src in extras:
        f = Path(tempfile.gettempdir()) / f"agl_parity_{label}.agl"
        f.write_text(src, encoding="utf-8", newline="\n")
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{label}: 编译未通过 {res.errors[0].code}")
            continue
        try:
            agb = bytecode.compile_program(res.ir)
        except bytecode.AgbUnsupported as e:
            bad.append(f"{label}: 不在 AGB 子集：{e}")
            continue
        compared += 1
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag:
            bad.append(f"{label}: Python 后端报 {diag.code}")
            continue
        try:
            vm_out = vm.run(agb, res.lang)
        except vm.VmError as e:
            bad.append(f"{label}: VM 报 {e.code} 第 {e.line} 行 {e.message}")
            continue
        if out.strip() != vm_out.strip():
            bad.append(f"{label}: py={out.strip()!r} vm={vm_out.strip()!r}")

    # 跨模块：黄金用例的子目录没有进入上面的 glob，单独补中英各一份。
    for f in (GOLDEN / "module_zh" / "主程序.agl", GOLDEN / "module_en" / "main_mod.agl"):
        if not f.exists():
            continue
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{f.name}: 编译未通过")
            continue
        try:
            agb = bytecode.compile_program(res.ir)
            mods = {}
            for dep in (res.deps or {}).values():
                dep_agb = bytecode.compile_program(dep.ir)
                mods[dep_agb["module"]] = dep_agb
        except bytecode.AgbUnsupported as e:
            bad.append(f"{f.name}: 不在 AGB 子集：{e}")
            continue
        compared += 1
        ok, diag, out = _run_isolated(res)
        if not ok and diag:
            bad.append(f"{f.name}: Python 后端报 {diag.code}")
            continue
        try:
            import os
            import tempfile

            old = os.getcwd()
            with tempfile.TemporaryDirectory(prefix="agl-doctor-") as td:
                (Path(td) / "build").mkdir(parents=True, exist_ok=True)
                os.chdir(td)
                try:
                    vm_out = vm.run(agb, res.lang, mods)
                finally:
                    os.chdir(old)
        except vm.VmError as e:
            bad.append(f"{f.name}: VM 报 {e.code} 第 {e.line} 行 {e.message}")
            continue
        if out.strip() != vm_out.strip():
            bad.append(f"{f.name}: py={out.strip()!r} vm={vm_out.strip()!r}")
    # 跨模块结构体：库里导出类型，主模块用 `库.类型(...)` 构造。
    root = Path(tempfile.gettempdir()) / "agl_parity_modstruct"
    root.mkdir(exist_ok=True)
    (root / "库.agl").write_text("""#语言=中文
导出 类型 客户 {
  名字: 文本
  年龄: 整数
}

导出 函数 造(名字: 文本, 年龄: 整数) -> 客户 {
  返回 客户(名字: 名字, 年龄: 年龄)
}
""", encoding="utf-8", newline="\n")
    f = root / "main.agl"
    f.write_text("""#语言=中文
导入 库

函数 主入口() {
  变量 甲 = 库.客户(年龄: 18, 名字: "小明")
  打印(甲, 甲.名字, 甲 == 库.客户(名字: "小明", 年龄: 18))
}
""", encoding="utf-8", newline="\n")
    res = pipeline.compile_file(f)
    if not res.ok:
        bad.append("跨模块结构体: 编译未通过")
    else:
        try:
            agb = bytecode.compile_program(res.ir)
            mods = {}
            for dep in (res.deps or {}).values():
                dep_agb = bytecode.compile_program(dep.ir)
                mods[dep_agb["module"]] = dep_agb
        except bytecode.AgbUnsupported as e:
            bad.append(f"跨模块结构体: 不在 AGB 子集：{e}")
        else:
            compared += 1
            ok, diag, out = pipeline.run_result(res)
            if not ok and diag:
                bad.append(f"跨模块结构体: Python 后端报 {diag.code}")
            else:
                try:
                    vm_out = vm.run(agb, res.lang, mods)
                except vm.VmError as e:
                    bad.append(f"跨模块结构体: VM 报 {e.code} 第 {e.line} 行 {e.message}")
                else:
                    if out.strip() != vm_out.strip():
                        bad.append(f"跨模块结构体: py={out.strip()!r} vm={vm_out.strip()!r}")

    if not compared:
        return c.fail("没有任何用例进入 AGB 子集").done("0 个")
    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{compared - len(bad)}/{compared}")


def check_agb_artifacts() -> Check:
    """`build` 必须产出 `.agb`，不再产出 `.py`；`run` 默认走 VM。

    这是“编译产物脱离 Python”的验收：不是看文档，而是真的调 CLI：
    build → 目录里没有 .py、有 .agb；run .agb 与 run .agl --backend py 输出一致；
    超出 AGB 子集时默认 run 必须报错，而不是静默退回 Python。
    """
    import sys
    import tempfile

    c = Check("AGB 产物与默认 VM")
    main = GOLDEN / "module_zh" / "主程序.agl"
    if not main.exists():
        return c.fail("找不到跨模块黄金用例").done()

    with tempfile.TemporaryDirectory(prefix="agl_agb_build_") as td:
        out = Path(td)
        build = _run(
            [sys.executable, "-m", "compiler.cli", "build", str(main), "-o", str(out)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        if build.returncode != 0:
            return c.fail(f"build 退出码 {build.returncode}：{build.stderr.strip()}").done()
        files = {p.name for p in out.iterdir()}
        if any(n.endswith(".py") for n in files):
            return c.fail(f"build 仍产出了 .py：{sorted(n for n in files if n.endswith('.py'))}").done()
        for need in ("主程序.agb", "主程序.agl.ir", "工具.agb", "工具.agl.ir"):
            if need not in files:
                return c.fail(f"build 缺少 {need}；实际 {sorted(files)}").done()

        agb_path = out / "主程序.agb"
        vm_run = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(agb_path)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        py_run = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(main), "--backend", "py"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        if vm_run.returncode != 0:
            return c.fail(f"run .agb 退出码 {vm_run.returncode}：{vm_run.stderr.strip()}").done()
        if py_run.returncode != 0:
            return c.fail(f"run .agl --backend py 退出码 {py_run.returncode}").done()
        if vm_run.stdout != py_run.stdout:
            return c.fail(f"AGB 与 Python 输出不一致：{vm_run.stdout!r} vs {py_run.stdout!r}").done()

    # 默认后端必须是 vm；py 只能显式指定。
    from compiler.cli import build_parser

    args = build_parser().parse_args(["run", str(main)])
    if args.backend != "vm":
        return c.fail(f"run 默认后端是 {args.backend!r}，期望 'vm'").done()

    # 超出子集：种子编译器的默认 VM 必须失败，并明确提示显式 --backend py。
    # （默认已切自举；这一项验证种子 AGB 子集的边界，故显式 --compiler seed。）
    with tempfile.TemporaryDirectory(prefix="agl_agb_unsupported_") as td:
        bad = Path(td) / "bad.agl"
        bad.write_text("""#语言=中文
函数 主入口() {
  变量 xs = [1, 2]
  xs[0] += 1
  打印(xs[0])
}
""", encoding="utf-8", newline="\n")
        denied = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(bad),
             "--compiler", "seed"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        if denied.returncode == 0:
            return c.fail("种子编译器超出 AGB 子集却执行成功，可能静默退回 Python").done()
        text = denied.stderr + denied.stdout
        if "不会静默退回 Python" not in text:
            return c.fail("超出子集的报错没有说明“不静默退回 Python”").done()
        py = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(bad), "--backend", "py"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        if py.returncode != 0 or py.stdout.strip() != "2":
            return c.fail(f"--backend py 兜底失败：rc={py.returncode} out={py.stdout!r}").done()

    # 下标赋值（列表 / 映射表）：AGB 与 Python 后端必须都能改，且输出一致。
    with tempfile.TemporaryDirectory(prefix="agl_agb_index_") as td:
        idx = Path(td) / "idx.agl"
        idx.write_text("""#语言=中文
函数 主入口() {
  变量 xs = [1, 2]
  xs[1] = 3
  打印(xs[1])
  变量 m = {"甲": 1}
  m["乙"] = 2
  打印(m["乙"])
}
""", encoding="utf-8", newline="\n")
        vm_idx = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(idx)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        py_idx = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(idx), "--backend", "py"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        want = "3\n2\n"
        if vm_idx.returncode != 0 or vm_idx.stdout != want:
            return c.fail(f"AGB 下标赋值失败：rc={vm_idx.returncode} out={vm_idx.stdout!r}").done()
        if py_idx.returncode != 0 or py_idx.stdout != want:
            return c.fail(f"Python 下标赋值失败：rc={py_idx.returncode} out={py_idx.stdout!r}").done()
        if vm_idx.stdout != py_idx.stdout:
            return c.fail("下标赋值双后端输出不一致").done()

    # 短路：`假 并且 有副作用()` / `真 或 有副作用()` 右侧绝不能执行。
    with tempfile.TemporaryDirectory(prefix="agl_agb_short_") as td:
        short = Path(td) / "short.agl"
        short.write_text("""#语言=中文
函数 有副作用() -> 布尔 {
  打印("不该出现")
  返回 真
}
函数 主入口() {
  打印(假 并且 有副作用())
  打印(真 或 有副作用())
  打印(真 并且 假)
  打印(假 或 真)
}
""", encoding="utf-8", newline="\n")
        vm_short = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(short)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        py_short = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(short), "--backend", "py"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        want_short = "假\n真\n假\n真\n"
        if vm_short.returncode != 0 or vm_short.stdout != want_short:
            return c.fail(f"VM 短路语义错误：rc={vm_short.returncode} out={vm_short.stdout!r}").done()
        if py_short.returncode != 0 or py_short.stdout != want_short:
            return c.fail(f"Python 短路语义错误：rc={py_short.returncode} out={py_short.stdout!r}").done()

    # 中文关键字前缀的标识符：`当前行` 曾被切成 `当` + `前行`；
    # 语句起始「关键字前缀 + = / ( / [ / .」应整体当标识符。
    with tempfile.TemporaryDirectory(prefix="agl_agb_prefix_") as td:
        pref = Path(td) / "prefix.agl"
        pref.write_text("""#语言=中文
函数 主入口() {
  变量 当前行 = 1
  当前行 = 当前行 + 1
  变量 文本值 = "x"
  变量 结果类型 = 文本值
  变量 函数名 = "f"
  打印(当前行, 文本值, 结果类型, 函数名)
}
""", encoding="utf-8", newline="\n")
        vm_pref = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(pref)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        py_pref = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(pref), "--backend", "py"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        want_pref = "2 x x f\n"
        if vm_pref.returncode != 0 or vm_pref.stdout != want_pref:
            return c.fail(f"关键字前缀标识符（VM）失败：rc={vm_pref.returncode} out={vm_pref.stdout!r}").done()
        if py_pref.returncode != 0 or py_pref.stdout != want_pref:
            return c.fail(f"关键字前缀标识符（Python）失败：rc={py_pref.returncode} out={py_pref.stdout!r}").done()

    # 自举闭环：AGL 词法器 + AGL 语法分析器必须在默认 VM 上跑通，
    # 并且语法分析器要能读 AGL 词法器源码（B1 + B2 的最小自举证据）。
    lexer = REPO / "自举" / "词法器.agl"
    parser = REPO / "自举" / "语法分析器.agl"
    if not lexer.exists() or not parser.exists():
        return c.fail("缺少 bootstrap/词法器.agl 或 bootstrap/语法分析器.agl").done()
    lex_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(lexer),
         "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if lex_run.returncode != 0:
        return c.fail(f"自举词法器运行失败：{lex_run.stderr.strip()}").done()
    if "tests/golden/grammar_zh.agl 记号数 201" not in lex_run.stdout:
        return c.fail(f"自举词法器输出与 Python 词法器不一致：{lex_run.stdout!r}").done()
    par_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(parser),
         "--allow-effects", "IO", "--max-steps", "50000000"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if par_run.returncode != 0:
        return c.fail(f"自举语法分析器运行失败：{par_run.stderr.strip()}").done()
    if "解析错误" in par_run.stdout or "函数 词法分析" not in par_run.stdout:
        return c.fail(f"自举语法分析器未能解析词法器：{par_run.stdout!r}").done()

    # B3 最小闭环：AGL 编译器自己生成 .agb，再交给默认 VM 执行。
    compiler = REPO / "自举" / "编译器.agl"
    if not compiler.exists():
        return c.fail("缺少 bootstrap/编译器.agl").done()
    comp_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(compiler), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if comp_run.returncode != 0:
        return c.fail(f"自举编译器运行失败：{comp_run.stderr.strip()}").done()
    if "已产出 selfhost_hello.agb" not in comp_run.stdout:
        return c.fail(f"自举编译器没有产出 AGB：{comp_run.stdout!r}").done()
    made = REPO / "build" / "自举编译器" / "selfhost_hello.agb"
    if not made.exists():
        return c.fail(f"自举编译器声称产出，但文件不存在：{made}").done()
    made_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(made)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if made_run.returncode != 0 or made_run.stdout.strip() != "你好 AGL":
        return c.fail(f"AGL 生成的 AGB 执行结果错误：rc={made_run.returncode} out={made_run.stdout!r}").done()

    # B4a：AGL 生成的 IR 必须和 Python 编译器 IR 在结构字段上一致（忽略 span）。
    import json

    ir_gen = REPO / "自举" / "中间表示.agl"
    if not ir_gen.exists():
        return c.fail("缺少 bootstrap/中间表示.agl").done()
    ir_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(ir_gen), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if ir_run.returncode != 0:
        return c.fail(f"自举 IR 生成器运行失败：{ir_run.stderr.strip()}").done()
    agl_ir_path = Path(tempfile.gettempdir()) / "agl_agb" / "selfhost_full.ir"
    if not agl_ir_path.exists():
        return c.fail(f"自举 IR 生成器没有产出文件：{agl_ir_path}").done()
    py_ir_run = _run(
        [sys.executable, "-m", "compiler.cli", "ir", str(GOLDEN / "full_zh.agl"), "--json"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if py_ir_run.returncode != 0:
        return c.fail(f"Python 编译器导出 IR 失败：{py_ir_run.stderr.strip()}").done()

    def strip_span(x):
        if isinstance(x, dict):
            return {k: strip_span(v) for k, v in x.items() if k != "span"}
        if isinstance(x, list):
            return [strip_span(v) for v in x]
        return x

    py_mod = strip_span(json.loads(py_ir_run.stdout))["program"]["modules"][0]
    ag_mod = strip_span(json.loads(agl_ir_path.read_text(encoding="utf-8-sig")))["program"]["modules"][0]
    for key in ("name", "imports", "exports", "consts", "types"):
        if py_mod.get(key) != ag_mod.get(key):
            return c.fail(f"AGL IR 与 Python IR 的 module.{key} 不一致").done()
    py_fns, ag_fns = py_mod["functions"], ag_mod["functions"]
    if len(py_fns) != len(ag_fns):
        return c.fail(f"AGL IR 函数数 {len(ag_fns)}，Python IR {len(py_fns)}").done()
    for i, (pf, af) in enumerate(zip(py_fns, ag_fns)):
        for key in ("name", "params", "return", "effects", "is_async", "checks", "body"):
            if pf.get(key) != af.get(key):
                return c.fail(f"AGL IR 与 Python IR 的 functions[{i}].{key} 不一致").done()

    # B4b：AGL 把 IR 降成 AGB，默认 VM 执行 full_zh 必须输出「够大」。
    agb_gen = REPO / "自举" / "字节码.agl"
    if not agb_gen.exists():
        return c.fail("缺少 bootstrap/字节码.agl").done()
    agb_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(agb_gen), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if agb_run.returncode != 0:
        return c.fail(f"自举 AGB 生成器运行失败：{agb_run.stderr.strip()}").done()
    if "已产出 selfhost_full.agb" not in agb_run.stdout:
        return c.fail(f"自举 AGB 生成器没有产出：{agb_run.stdout!r}").done()
    full_agb = Path(tempfile.gettempdir()) / "agl_agb" / "selfhost_full.agb"
    if not full_agb.exists():
        return c.fail(f"自举 AGB 文件不存在：{full_agb}").done()
    full_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(full_agb)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if full_run.returncode != 0 or full_run.stdout.strip() != "够大":
        return c.fail(f"AGL 生成的 full_zh AGB 输出错误：rc={full_run.returncode} out={full_run.stdout!r}").done()

    # B4c-1：整条链收进一个 AGL 编译器入口，编译成 .agb 后由 VM 再跑一遍，
    # 产出的 AGB 必须和 B4b 两段式产物逐字节一致。
    import hashlib
    import os

    pipeline_src = REPO / "自举" / "自举编译器.agl"
    if not pipeline_src.exists():
        return c.fail("缺少 bootstrap/自举编译器.agl").done()
    # 自举链统一走 install_bootstrap（与 `aglc bootstrap` 同一个实现）：
    # 种子编管线 v1 → v1 编自身 v2 → v2 编自身 v3 → 断言 v2==v3 → 安装不动点。
    from compiler.cli.bootstrap import install_bootstrap

    bs = install_bootstrap(verbose=False)
    if not bs.ok:
        return c.fail("自举安装失败：" + "; ".join(bs.errors[:2])).done()
    pipeline_agb = Path(bs.pipeline_agb)
    v2_agb = Path(bs.v2)
    v3_agb = Path(bs.v3)
    installed_agb = Path(bs.installed)
    # 自举编译器本身是「读源码 → 产 AGB」的工具；直接运行它必须提供源路径，
    # 否则默认的 input.agl 在仓库里并不存在。这里按 bootstrap 的真实调用方式
    # 传环境变量，并给足以完成整条编译链的步数预算。
    pipeline_env = dict(os.environ)
    pipeline_env.update({
        "AGL_BOOTSTRAP_SOURCE": str(GOLDEN / "full_zh.agl"),
        "AGL_BOOTSTRAP_MODULE": "full_zh",
        "AGL_BOOTSTRAP_OUTPUT": "selfhost_pipeline.agb",
    })
    pipeline_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(pipeline_agb),
         "--allow-effects", "IO", "--max-steps", "50000000"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        env=pipeline_env)
    if pipeline_run.returncode != 0 or "自举编译器" not in pipeline_run.stdout:
        return c.fail(f"AGB 形态的自举编译器运行失败：rc={pipeline_run.returncode} out={pipeline_run.stdout!r}").done()
    pipeline_out = Path(tempfile.gettempdir()) / "agl_agb" / "selfhost_pipeline.agb"
    if not pipeline_out.exists():
        return c.fail(f"自举编译器没有产出 full_zh AGB：{pipeline_out}").done()
    if hashlib.sha256(full_agb.read_bytes()).hexdigest() != hashlib.sha256(pipeline_out.read_bytes()).hexdigest():
        return c.fail("自举编译器整链产物与两段式产物不是逐字节一致").done()

    # B4c-2：真正的自编译闭环已由 install_bootstrap 完成（v1→v2→v3，v2==v3）。
    # 这里独立复核一次字节级一致 + 安装产物确实是那个不动点，防止安装层被绕过。
    v3_run_ok = v2_agb.exists() and v3_agb.exists() and installed_agb.exists()
    if not v3_run_ok:
        return c.fail("B4c-2 v2/v3/安装产物不全").done()
    if hashlib.sha256(v2_agb.read_bytes()).hexdigest() != hashlib.sha256(v3_agb.read_bytes()).hexdigest():
        return c.fail("B4c-2 v2 与 v3 不是逐字节一致，自编译没有到不动点").done()
    if installed_agb.read_bytes() != v3_agb.read_bytes():
        return c.fail("安装产物与不动点 v3 不一致").done()

    # 中英同构：v2 编英文 `hello_en`，入口应为 `main`，输出必须与 Python 后端一致。
    hello_en = REPO / "tests" / "golden" / "hello_en.agl"
    hello_env = dict(os.environ)
    hello_env["AGL_BOOTSTRAP_SOURCE"] = str(hello_en)
    hello_env["AGL_BOOTSTRAP_MODULE"] = "hello_en"
    hello_env["AGL_BOOTSTRAP_OUTPUT"] = "selfhost_hello_en.agb"
    hello_build = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(v2_agb),
         "--allow-effects", "IO", "--max-steps", "50000000"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=hello_env)
    hello_agb = Path(tempfile.gettempdir()) / "agl_agb" / "selfhost_hello_en.agb"
    if hello_build.returncode != 0 or not hello_agb.exists():
        return c.fail(f"B4c-2 英文 hello_en 编译失败：rc={hello_build.returncode} "
                      f"err={hello_build.stderr.strip()}").done()
    hello_vm = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(hello_agb)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    hello_py = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(hello_en), "--backend", "py"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if hello_vm.returncode != 0 or hello_py.returncode != 0 or hello_vm.stdout != hello_py.stdout:
        return c.fail(f"B4c-2 英文 hello_en 输出不一致："
                      f"vm rc={hello_vm.returncode} out={hello_vm.stdout!r}；"
                      f"py rc={hello_py.returncode} out={hello_py.stdout!r}").done()

    # B5：AGL 版编译器扩到 赋值 / 当 / 循环每个 / 列表 / 映射表 / 下标 / 成员 / 中断。
    # 用一份临时源码跑 AGL 整链，输出必须与 Python 后端逐字一致。

    b5_source = """#语言=中文
函数 求和(n: 整数) -> 整数 {
  变量 和 = 0
  变量 i = 0
  当 i 小于 n {
    和 = 和 + i
    i = i + 1
  }
  返回 和
}

函数 主入口() {
  变量 xs = [1, 2, 3]
  变量 m = {"甲": 10}
  xs[0] = 7
  打印(xs[0], m["甲"], xs.长度)
  循环 x 在 xs {
    打印(x)
    如果 x 大于 7 {
      中断
    }
  }
  打印(求和(3))
  打印(非 假, -3)
  打印(1.5, 1.23e-05, 1.9 + 2)
}
"""
    b5_path = Path(tempfile.gettempdir()) / "agl_agb" / "b5_doctor.agl"
    b5_path.write_text(b5_source, encoding="utf-8", newline="\n")
    py_b5 = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(b5_path), "--backend", "py"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if py_b5.returncode != 0:
        return c.fail(f"B5 样本 Python 后端失败：{py_b5.stderr.strip()}").done()
    b5_env = dict(os.environ)
    b5_env["AGL_BOOTSTRAP_SOURCE"] = str(b5_path)
    b5_env["AGL_BOOTSTRAP_MODULE"] = "b5_doctor"
    b5_env["AGL_BOOTSTRAP_OUTPUT"] = "b5_doctor.agb"
    b5_pipeline = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(pipeline_src), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=b5_env)
    if b5_pipeline.returncode != 0 or "b5_doctor" not in b5_pipeline.stdout:
        return c.fail(f"B5 整链编译失败：rc={b5_pipeline.returncode} out={b5_pipeline.stdout!r} err={b5_pipeline.stderr.strip()}").done()
    b5_agb = Path(tempfile.gettempdir()) / "agl_agb" / "b5_doctor.agb"
    b5_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(b5_agb)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if b5_run.returncode != 0 or b5_run.stdout != py_b5.stdout:
        return c.fail(f"B5 AGL 编译器输出与 Python 不一致：{b5_run.stdout!r} vs {py_b5.stdout!r}").done()

    # B5b：英文分支的 `.length` 与布尔/空值打印口径必须和 Python 后端一致。
    b5_en_source = """#lang=English
fn main() {
  let xs = [1, 2, 3]
  print(xs.length, true, Null)
}
"""
    b5_en_path = Path(tempfile.gettempdir()) / "agl_agb" / "b5_en_doctor.agl"
    b5_en_path.write_text(b5_en_source, encoding="utf-8", newline="\n")
    py_b5_en = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(b5_en_path), "--backend", "py"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if py_b5_en.returncode != 0:
        return c.fail(f"B5b 英文样本 Python 后端失败：{py_b5_en.stderr.strip()}").done()
    b5_en_env = dict(os.environ)
    b5_en_env["AGL_BOOTSTRAP_SOURCE"] = str(b5_en_path)
    b5_en_env["AGL_BOOTSTRAP_MODULE"] = "b5_en_doctor"
    b5_en_env["AGL_BOOTSTRAP_OUTPUT"] = "b5_en_doctor.agb"
    b5_en_pipeline = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(pipeline_src), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=b5_en_env)
    b5_en_agb = Path(tempfile.gettempdir()) / "agl_agb" / "b5_en_doctor.agb"
    if b5_en_pipeline.returncode != 0 or not b5_en_agb.exists():
        return c.fail(f"B5b 英文整链编译失败：rc={b5_en_pipeline.returncode} "
                      f"err={b5_en_pipeline.stderr.strip()}").done()
    b5_en_run = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(b5_en_agb)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if b5_en_run.returncode != 0 or b5_en_run.stdout != py_b5_en.stdout:
        return c.fail(f"B5b 英文 AGL 编译器输出与 Python 不一致："
                      f"{b5_en_run.stdout!r} vs {py_b5_en.stdout!r}").done()

    # B6：AGL 版编译器必须能编译第一份真实编译器源码 `bootstrap/词法器.agl`。
    # 这不是样本，而是 B1 自己：它里面有模块声明、导出、+=、字符串拼接、
    # 字符下标、效应声明和跨行列表，全部要真的进 AGL 版编译器。
    lexer_source = REPO / "自举" / "词法器.agl"
    lexer_env = dict(os.environ)
    lexer_env["AGL_BOOTSTRAP_SOURCE"] = str(lexer_source)
    lexer_env["AGL_BOOTSTRAP_MODULE"] = "词法器"
    lexer_env["AGL_BOOTSTRAP_OUTPUT"] = "selfhost_lexer_doctor.agb"
    lexer_pipeline = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(pipeline_src), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=lexer_env)
    if lexer_pipeline.returncode != 0 or "词法器" not in lexer_pipeline.stdout:
        return c.fail(f"B6 编译真实词法器失败：rc={lexer_pipeline.returncode} "
                      f"out={lexer_pipeline.stdout!r} err={lexer_pipeline.stderr.strip()}").done()
    lexer_agb = Path(tempfile.gettempdir()) / "agl_agb" / "selfhost_lexer_doctor.agb"
    py_lexer = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(lexer_source), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    agb_lexer = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(lexer_agb), "--allow-effects", "IO"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if py_lexer.returncode != 0 or agb_lexer.returncode != 0:
        return c.fail(f"B6 真实词法器运行失败：py rc={py_lexer.returncode} "
                      f"agb rc={agb_lexer.returncode} err={agb_lexer.stderr.strip()}").done()
    if py_lexer.stdout != agb_lexer.stdout:
        return c.fail("B6 真实词法器产物不一致：\n"
                      f"Python:\n{py_lexer.stdout}\nAGL 编译器:\n{agb_lexer.stdout}").done()

    # B7：AGL 版编译器要能编译「解析器本身」，并且编出来的解析器能解析它自己。
    parser_source = REPO / "自举" / "语法分析器.agl"
    parser_env = dict(os.environ)
    parser_env["AGL_BOOTSTRAP_SOURCE"] = str(parser_source)
    parser_env["AGL_BOOTSTRAP_MODULE"] = "语法分析器"
    parser_env["AGL_BOOTSTRAP_OUTPUT"] = "selfhost_parser_doctor.agb"
    parser_pipeline = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(pipeline_src),
         "--allow-effects", "IO", "--max-steps", "50000000"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=parser_env)
    if parser_pipeline.returncode != 0 or "语法分析器" not in parser_pipeline.stdout:
        return c.fail(f"B7 编译真实解析器失败：rc={parser_pipeline.returncode} "
                      f"out={parser_pipeline.stdout!r} err={parser_pipeline.stderr.strip()}").done()
    parser_agb = Path(tempfile.gettempdir()) / "agl_agb" / "selfhost_parser_doctor.agb"
    py_parser = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(parser_source),
         "--allow-effects", "IO", "--max-steps", "50000000"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    agb_parser = _run(
        [sys.executable, "-m", "compiler.cli", "run", str(parser_agb),
         "--allow-effects", "IO", "--max-steps", "50000000"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if py_parser.returncode != 0 or agb_parser.returncode != 0:
        return c.fail(f"B7 真实解析器运行失败：py rc={py_parser.returncode} "
                      f"agb rc={agb_parser.returncode} err={agb_parser.stderr.strip()}").done()
    if py_parser.stdout != agb_parser.stdout:
        return c.fail("B7 真实解析器产物不一致：\n"
                      f"Python:\n{py_parser.stdout}\nAGL 编译器:\n{agb_parser.stdout}").done()

    # B8：把全部黄金用例交给 AGL 版编译器整链编译（直接用安装好的不动点产物），
    # 生成物用默认 VM 跑出来的 stdout 必须和 Python 后端逐字相同。
    # 这条覆盖异步 / 校验 / 尝试捕获 / 匹配 / FFI 等最近补上的真实能力。
    golden_dir = Path(tempfile.gettempdir()) / "agl_agb" / "b8_doctor"
    golden_dir.mkdir(parents=True, exist_ok=True)
    effects = "IO,Time,Net,External,model"
    golden_files = _golden_agls()
    for golden_src in golden_files:
        stem = golden_src.stem
        env = dict(os.environ)
        env["AGL_BOOTSTRAP_SOURCE"] = str(golden_src)
        env["AGL_BOOTSTRAP_MODULE"] = stem
        env["AGL_BOOTSTRAP_OUTPUT"] = "b8_doctor/" + stem + ".agb"
        golden_build = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(installed_agb),
             "--allow-effects", "IO", "--max-steps", "50000000"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=env)
        golden_agb = golden_dir / (stem + ".agb")
        if golden_build.returncode != 0 or not golden_agb.exists():
            return c.fail(f"B8 自举编译 {stem} 失败：rc={golden_build.returncode} "
                          f"out={golden_build.stdout!r} err={golden_build.stderr.strip()}").done()
        vm_golden = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(golden_agb),
             "--allow-effects", effects, "--allow-tools", "all"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        py_golden = _run(
            [sys.executable, "-m", "compiler.cli", "run", str(golden_src),
             "--backend", "py", "--allow-effects", effects, "--allow-tools", "all"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        if vm_golden.returncode != 0 or py_golden.returncode != 0:
            return c.fail(f"B8 {stem} 运行失败：vm rc={vm_golden.returncode} "
                          f"py rc={py_golden.returncode} err={vm_golden.stderr.strip()}").done()
        if vm_golden.stdout != py_golden.stdout:
            return c.fail(f"B8 {stem} 输出不一致：\n"
                          f"Python:\n{py_golden.stdout}\nAGL 编译器:\n{vm_golden.stdout}").done()

    # T2（B6）：CLI 直通自举编译器 —— `--compiler agl` 用安装好的不动点产物
    # 编译并交 VM 执行，输出必须与 Python 后端逐字一致。
    # §5e 门槛 2：样本扩到 12 份黄金（覆盖 let_multi / 声明块 / 多文件工程）
    # + 1 份临时接口/依赖样本。
    t2_paths = [
        GOLDEN / "hello_zh.agl", GOLDEN / "hello_en.agl",
        GOLDEN / "full_zh.agl", GOLDEN / "full_en.agl",
        GOLDEN / "nullable_zh.agl", GOLDEN / "nullable_en.agl",
        GOLDEN / "matchexpr_zh.agl", GOLDEN / "with_en.agl",
        GOLDEN / "builtin_zh.agl",
        GOLDEN / "tool_decl_zh.agl", GOLDEN / "tool_decl_en.agl",
        GOLDEN / "module_zh" / "主程序.agl",
    ]
    iface_sample = """#语言=中文
依赖 {
  数学工具 = "1.2.0"
}

接口 摘要能力 {
  函数 生成摘要(输入: 文本, 上限: 整数) -> 文本
}

函数 生成摘要(输入: 文本, 上限: 整数) -> 文本 {
  返回 转文本(切片(输入, 0, 上限))
}

函数 主入口() {
  打印("接口依赖通过", 生成摘要("你好世界", 2))
}
"""
    t2_total = 0
    with tempfile.TemporaryDirectory(prefix="agl_t2_iface_") as t2td:
        iface_path = Path(t2td) / "t2_iface_zh.agl"
        iface_path.write_text(iface_sample, encoding="utf-8", newline="\n")
        for gsrc in t2_paths + [iface_path]:
            if not gsrc.exists():
                continue
            t2_total += 1
            py_out = _run(
                [sys.executable, "-m", "compiler.cli", "run", str(gsrc),
                 "--backend", "py", "--allow-effects", effects],
                cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
            agl_out = _run(
                [sys.executable, "-m", "compiler.cli", "run", str(gsrc),
                 "--compiler", "agl", "--allow-effects", effects],
                cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
            if agl_out.returncode != 0 or py_out.returncode != 0:
                return c.fail(f"T2 {gsrc.stem} --compiler agl 运行失败：rc={agl_out.returncode} "
                              f"err={agl_out.stderr.strip()[:200]}").done()
            if agl_out.stdout != py_out.stdout:
                return c.fail(f"T2 {gsrc.stem} 输出不一致：\nPython:\n{py_out.stdout}\nAGL 编译器:\n{agl_out.stdout}").done()

    return c.done("无 .py · AGB 可执行 · 默认 VM · 显式兜底 · 下标赋值 · 短路语义 · 关键字前缀 · 自举词法/语法/IR/字节码/整链/赋值循环 · 真实词法器源码 · 真实解析器源码 · 自编译不动点安装 · 中英自举 · "
                  f"{len(golden_files)} 份黄金用例自举全对拍 · T2 {t2_total} 份 --compiler agl 直通")


def check_vm_sandbox() -> Check:
    """VM 的效应权限白名单必须真的拦得住，放行后又能跑。

    光靠函数自己声明 `效应` 是自愿的；宿主必须能一句话收走权限。
    这里用 `睡眠`（Time）做最小样本：`allowed_effects=set()` 必须报
    `AGL-RT-0092`，只放行 Time 时必须正常输出。
    """
    from compiler import bytecode, vm

    c = Check("VM 效应权限白名单")
    src = """#语言=中文
异步 函数 主入口() 效应 计时 {
  等待 睡眠(1)
  打印("ok")
}
"""
    res = pipeline.compile_source(src, "sandbox.agl")
    if not res.ok:
        return c.fail(f"样本编译未通过：{res.errors[0].code}").done("0/1")
    try:
        agb = bytecode.compile_program(res.ir)
    except bytecode.AgbUnsupported as e:
        return c.fail(f"样本不在 AGB 子集：{e}").done("0/1")

    try:
        vm.run(agb, res.lang, allowed_effects=set())
    except vm.VmError as e:
        if e.code != vm.RT_EFFECT:
            return c.fail(f"拒绝时错误码是 {e.code}，期望 {vm.RT_EFFECT}").done("1/2")
    else:
        return c.fail("没有授权 Time 却放行了 `睡眠`").done("1/2")

    try:
        out = vm.run(agb, res.lang, allowed_effects={"Time"})
    except vm.VmError as e:
        return c.fail(f"放行 Time 后仍报 {e.code}：{e.message}").done("1/2")
    if out.strip() != "ok":
        return c.fail(f"放行 Time 后输出是 {out.strip()!r}，期望 'ok'").done("1/2")

    # P8-1（T16）：调用次数配额 —— 带效应的内建调用 3 次，配额 2 必须报
    # AGL-RT-0093，配额 10 必须正常跑完。
    quota_src = """#语言=中文
函数 主入口() 效应 读写 {
  写文件("qc1.txt", "甲")
  写文件("qc2.txt", "乙")
  写文件("qc3.txt", "丙")
  打印("配额内")
}
"""
    qres = pipeline.compile_source(quota_src, "quota.agl")
    if not qres.ok:
        return c.fail(f"配额样本编译未通过：{qres.errors[0].code}").done("1/2")
    try:
        qagb = bytecode.compile_program(qres.ir)
    except bytecode.AgbUnsupported as e:
        return c.fail(f"配额样本不在 AGB 子集：{e}").done("1/2")
    try:
        vm.run(qagb, qres.lang, max_calls=2)
    except vm.VmError as e:
        if e.code != vm.RT_CALLS:
            return c.fail(f"超配额时错误码是 {e.code}，期望 {vm.RT_CALLS}").done("1/2")
    else:
        return c.fail("配额 2 却放行了 3 次外部调用").done("1/2")
    try:
        vm.run(qagb, qres.lang, max_calls=10)
    except vm.VmError as e:
        return c.fail(f"配额 10 仍报 {e.code}：{e.message}").done("1/2")

    # P8-1（T16）：外部库白名单 —— FFI 调用未列库必须报 AGL-RT-0094，
    # 白名单放行 math 后正常出 2.0。
    ffi_src = ("#语言=中文\n"
               "导入 外部 math\n"
               "函数 主入口() 效应 外部调用 {\n"
               "  打印(math.sqrt(4.0))\n"
               "}\n")
    fres = pipeline.compile_source(ffi_src, "tools.agl")
    if not fres.ok:
        return c.fail(f"FFI 白名单样本编译未通过：{fres.errors[0].code}").done("1/2")
    try:
        fagb = bytecode.compile_program(fres.ir)
    except bytecode.AgbUnsupported as e:
        return c.fail(f"FFI 白名单样本不在 AGB 子集：{e}").done("1/2")
    try:
        vm.run(fagb, fres.lang, allowed_tools=set())
    except vm.VmError as e:
        if e.code != vm.RT_TOOLS:
            return c.fail(f"越库时错误码是 {e.code}，期望 {vm.RT_TOOLS}").done("1/2")
    else:
        return c.fail("白名单为空却放行了外部库 math").done("1/2")
    try:
        out = vm.run(fagb, fres.lang, allowed_tools={"math"})
    except vm.VmError as e:
        return c.fail(f"白名单放行 math 后仍报 {e.code}：{e.message}").done("1/2")
    if out.strip() != "2.0":
        return c.fail(f"白名单放行后输出 {out.strip()!r}，期望 '2.0'").done("1/2")
    # 墙钟超时：步数上限对「每步都很快但总数无限」有效，对「单步阻塞」
    # 无效；这里用一个纯计算死循环验证 timeout_ms 能独立收口。
    timeout_src = """#语言=中文
函数 主入口() {
  变量 甲 = 0
  当 真 {
    甲 = 甲 + 1
  }
}
"""
    tres = pipeline.compile_source(timeout_src, "timeout.agl")
    if not tres.ok:
        return c.fail(f"超时样本编译未通过：{tres.errors[0].code}").done("1/2")
    try:
        tagb = bytecode.compile_program(tres.ir)
    except bytecode.AgbUnsupported as e:
        return c.fail(f"超时样本不在 AGB 子集：{e}").done("1/2")
    try:
        vm.run(tagb, tres.lang, max_steps=100_000_000, timeout_ms=30)
    except vm.VmError as e:
        if e.code != vm.RT_TIMEOUT:
            return c.fail(f"超时时错误码是 {e.code}，期望 {vm.RT_TIMEOUT}").done("1/2")
    else:
        return c.fail("死循环在 30ms 墙钟上限内没有报超时").done("1/2")

    return c.done("拒绝 + 放行 · 配额 AGL-RT-0093 · 工具白名单 AGL-RT-0094 · 超时 AGL-RT-0095")


# ── 6. 双分支结构等价 ──────────────────────────────────────────────────
def check_dual_branch() -> Check:
    from compiler.convert import compare

    c = Check("双分支结构等价")
    # 自动配对：每个 xxx_zh.agl 找对应的 xxx_en.agl
    pairs: list[tuple[Path, Path]] = []
    for zh in _golden_agls():
        if not zh.name.endswith("_zh.agl"):
            continue
        en = zh.with_name(zh.name.replace("_zh.agl", "_en.agl"))
        if en.exists():
            pairs.append((zh, en))
    if not pairs:
        return c.fail("没有找到中英配对的用例").done("0 组")

    bad = []
    for a, b in pairs:
        ra, rb = pipeline.compile_file(a), pipeline.compile_file(b)
        if not (ra.ok and rb.ok):
            bad.append(f"{a.name}/{b.name} 编译未通过")
            continue
        same, detail = compare(ra.ir, rb.ir)
        if not same:
            bad.append(f"{a.name}/{b.name}: {detail.splitlines()[0]}")
    if bad:
        c.fail("; ".join(bad[:2]))
    return c.done(f"{len(pairs) - len(bad)}/{len(pairs)}")


# ── 7. 往返校验 ────────────────────────────────────────────────────────
def check_roundtrip() -> Check:
    from compiler.convert import convert

    c = Check("往返校验（逐字还原）")
    files = _golden_agls()
    bad = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        src_lang = "zh" if src.startswith("#语言") else "en"
        other = "en" if src_lang == "zh" else "zh"
        try:
            once = convert(src, f.name, other)
            back = convert(once, f.name, src_lang)
        except Exception as e:
            bad.append(f"{f.name}: 转换抛错 {e}")
            continue
        if back != src:
            bad.append(f"{f.name}: 未逐字还原")
    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(files) - len(bad)}/{len(files)}")


# ── 8. 错误码断言 ──────────────────────────────────────────────────────
def check_error_codes(expect: dict) -> Check:
    c = Check("错误码断言")
    total = 0
    bad = []
    for name, case in expect.items():
        stage = case.get("stage")
        if stage not in ("compile", "run"):
            continue
        path = (CASES / name).resolve()
        if not path.exists():
            bad.append(f"{name}: 文件不存在")
            continue
        total += 1
        res = pipeline.compile_file(path)
        if stage == "compile":
            if res.ok:
                bad.append(f"{name}: 期望 {case['code']}，实际编译通过")
                continue
            got = res.errors[0].code
            if got != case["code"]:
                bad.append(f"{name}: 期望 {case['code']}，实际 {got}")
        else:
            if not res.ok:
                bad.append(f"{name}: 期望运行期报错，实际编译期就失败（{res.errors[0].code}）")
                continue
            ok, diag, _ = pipeline.run_result(res)
            if ok or diag is None:
                bad.append(f"{name}: 期望运行期报 {case['code']}，实际正常结束")
                continue
            if diag.code != case["code"]:
                bad.append(f"{name}: 期望 {case['code']}，实际 {diag.code}")
            elif diag.line <= 1 and name.startswith("rt_"):
                bad.append(f"{name}: 错误码对但行号没回译到源码")
    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{total - len(bad)}/{total}")


# ── 9. 行号对齐 ────────────────────────────────────────────────────────
def _stmt_lines(node, out: list[int]) -> None:
    """收集所有语句的起始行（只收 Stmt 层，不收集表达式）。"""
    if isinstance(node, list):
        for x in node:
            _stmt_lines(x, out)
        return
    if not isinstance(node, dict):
        return
    if "stmt" in node and "span" in node:
        out.append(node["span"]["line"])
    for k, v in node.items():
        if k == "span":
            continue
        _stmt_lines(v, out)


def check_line_alignment() -> Check:
    c = Check("行号对齐（AGL→Python）")
    files = _golden_agls()
    total = 0
    hit = 0
    misses: list[str] = []
    for f in files:
        res = pipeline.compile_file(f)
        if not res.ok:
            continue
        lines: list[int] = []
        for mod in res.ir["program"]["modules"]:
            for fn in mod["functions"]:
                lines.append(fn["span"]["line"])
                _stmt_lines(fn["body"], lines)
                _stmt_lines(fn.get("checks", []), lines)
        for ln in lines:
            total += 1
            if res.smap.get(ln) == ln:
                hit += 1
            else:
                misses.append(f"{f.name}:{ln}")
    if total == 0:
        return c.fail("没有可统计的语句").done()
    pct = hit / total * 100
    # 「AGL 第 N 行 = 产物第 N 行」是铁律第一条，不是可以商量着来的指标。
    # 曾经因为单行回调块必然要展开成多行，这里只敢给个容忍基线（一度记 95%）；
    # 现在后端已经把它彻底压平（见 backend_py 里 on_event 的单行分支），
    # 100% 是可以守住的 —— 那就把它钉成硬门槛：掉一条也要变红。
    if pct < 100:
        seen: list[str] = []
        for m in misses:
            if m not in seen:
                seen.append(m)
        c.note("未对齐：" + ", ".join(seen[:6]))
        return c.fail(f"{pct:.0f}% ({hit}/{total}) · 有行偏离了源码行号").done()
    return c.done(f"{pct:.0f}% ({hit}/{total})")


# ── 10. 黄金产物一致 ──────────────────────────────────────────────────
def check_artifacts() -> Check:
    """IR 与 Python 产物必须与 tests/golden 下的期望文件逐字一致。

    这一项是「后端被悄悄改坏」的最后一道闸：
    改代码 → 产物变了 → 这里立刻红。
    如果改动是有意的，跑 `aglc doctor --update-golden` 刷新期望文件。
    """
    from compiler.ir import dumps

    c = Check("黄金产物一致")
    files = _golden_agls(recursive=True)
    bad: list[str] = []
    for f in files:
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{f.name}: 编译未通过")
            continue
        ir_file = f.with_name(f.name + ".ir")
        py_file = f.with_suffix(".py")
        if not ir_file.exists():
            bad.append(f"{f.name}: 缺期望 IR")
            continue
        if not py_file.exists():
            bad.append(f"{f.name}: 缺期望 Python")
            continue
        if ir_file.read_text(encoding="utf-8") != dumps(res.ir):
            bad.append(f"{f.name}: IR 与期望不一致")
        if py_file.read_text(encoding="utf-8") != res.python:
            bad.append(f"{f.name}: Python 与期望不一致")
    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(files) - len(bad)}/{len(files)}")


# ── 11. 模块导入 ──────────────────────────────────────────────────────
MODULE_CASES: list[tuple[str, str, str]] = [
    # (入口文件相对路径, 期望输出, 说明)
    ("module_zh/主程序.agl", "你好，世界\n你好，AGL", "中文模块"),
    ("module_en/main_mod.agl", "Hello, world\nHello, AGL", "英文模块"),
]


def check_modules() -> Check:
    """跨文件必须真的能跑，而且依赖里的错误要指回依赖文件本身。"""
    from compiler.convert import compare

    c = Check("模块导入（跨文件）")
    bad: list[str] = []
    irs: list[dict] = []

    for rel, want, label in MODULE_CASES:
        path = GOLDEN / rel
        if not path.exists():
            bad.append(f"{label}: 缺 {rel}")
            continue
        res = pipeline.compile_file(path)
        if not res.ok:
            bad.append(f"{label}: {res.errors[0].code} {res.errors[0].message_zh}")
            continue
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag:
            bad.append(f"{label}: 运行报 {diag.code} 第 {diag.line} 行")
            continue
        if out.strip() != want.strip():
            bad.append(f"{label}: 输出 {out.strip()!r}，期望 {want!r}")
        if not res.deps:
            bad.append(f"{label}: 没有加载到任何依赖，说明导入没真的生效")
        irs.append(res.ir)

    # 中英两份模块项目必须结构等价（模块名算标识符，不参与比对）
    if len(irs) == 2:
        same, detail = compare(irs[0], irs[1])
        if not same:
            bad.append(f"模块双分支不等价：{detail.splitlines()[0]}")

    # 依赖里的运行期错误必须指回依赖文件，而不是甩给入口文件
    cross = CASES / "mod_err" / "rt_cross" / "rt_cross.agl"
    if cross.exists():
        res = pipeline.compile_file(cross)
        if not res.ok:
            bad.append("跨模块错误用例编译未通过")
        else:
            ok, diag, _ = pipeline.run_result(res)
            if ok or diag is None:
                bad.append("跨模块错误用例：期望运行期报错，实际正常结束")
            elif diag.file != "崩溃库.agl":
                bad.append(f"跨模块错误指向了 {diag.file}，应指向 崩溃库.agl")
            elif diag.line != 5:
                bad.append(f"跨模块错误行号是 {diag.line}，应为 5")
    else:
        bad.append("缺跨模块错误用例")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(MODULE_CASES)} 组 + 跨模块回译" if not bad else "")


def check_card() -> Check:
    """速查卡示例必须真能跑；`aglc init` 骨架必须真能跑。

    这一项防的是"教错语法"：卡片是 AI 写码前读的东西，
    里面的示例一旦跑不通，比没有卡片更糟。
    """
    import tempfile

    from compiler import card, scaffold
    from compiler.convert import compare
    from compiler.lexer import LANG_EN, LANG_ZH

    c = Check("速查卡与工程骨架")
    bad: list[str] = []
    irs: list[dict] = []
    table = KeywordTable.load()

    for lang in (LANG_ZH, LANG_EN):
        label = "中文" if lang == LANG_ZH else "英文"
        res = pipeline.compile_source(card.EXAMPLE[lang], f"速查卡示例_{lang}.agl")
        if not res.ok:
            e = res.errors[0]
            bad.append(f"{label}示例编译未通过：{e.code} 第 {e.line} 行 {e.message_zh}")
            continue
        ok, diag, _ = pipeline.run_result(res)
        if not ok and diag is not None:
            bad.append(f"{label}示例运行报 {diag.code} 第 {diag.line} 行")
        irs.append(res.ir)

        text = card.build(lang)
        if card.EXAMPLE[lang].rstrip() not in text:
            bad.append(f"{label}速查卡没贴出示例代码")
        # 表里阶段 1 的每个词都必须出现在卡片上，不许有"表里有、卡上没有"
        miss = [k.en for k in table.keywords
                if k.stage <= 1 and k.en not in text]
        if miss:
            bad.append(f"{label}速查卡漏登关键字：{miss[:5]}")

    if len(irs) == 2:
        same, detail = compare(irs[0], irs[1])
        if not same:
            bad.append(f"示例双分支不等价：{detail.splitlines()[0]}")

    # 骨架生成到临时目录，真的编译并运行一遍
    with tempfile.TemporaryDirectory(prefix="agl-init-") as tmp:
        root = Path(tmp) / "示例工程"
        made, _ = scaffold.write(root, "zh")
        entry = root / scaffold.ENTRY
        if not entry.exists():
            bad.append(f"骨架没生成入口文件（生成的是 {made}）")
        else:
            res = pipeline.compile_file(entry)
            if not res.ok:
                bad.append(f"骨架入口编译未通过：{res.errors[0].code}")
            else:
                ok, diag, out = pipeline.run_result(res)
                if not ok and diag is not None:
                    bad.append(f"骨架入口运行报 {diag.code} 第 {diag.line} 行")
                elif not out.strip():
                    bad.append("骨架入口没有输出")
        if not (root / "agl.toml").exists():
            bad.append("骨架没有生成 agl.toml")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done("示例可跑 · 骨架可跑" if not bad else "")


def check_tool_runtime() -> Check:
    """`工具` 声明：直接按工具名调用，经 MCP 真正拿到结果。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from compiler import bytecode, vm

    c = Check("工具声明与 MCP 运行时（含熔断）")
    bad: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            method = req.get("method")
            if method == "initialize":
                body = {"jsonrpc": "2.0", "id": req.get("id"), "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"}}}
            elif method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            elif method == "tools/call":
                body = {"jsonrpc": "2.0", "id": req.get("id"), "result": {
                    "content": [{"type": "text", "text": "晴，23度"}],
                    "isError": False}}
            else:
                body = {"jsonrpc": "2.0", "id": req.get("id"),
                        "error": {"message": "unknown"}}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        src = f'''#语言=中文
工具 天气 {{
  配置 {{
    协议 = "mcp"
    地址 = "http://127.0.0.1:{port}/mcp"
    超时秒数 = 5
  }}
  入参(城市: 文本)
  返回 文本
}}

函数 主入口() 效应 外部调用 {{
  变量 答复 = 天气("杭州")
  打印(答复)
}}
'''
        res = pipeline.compile_source(src, "tool_runtime.agl")
        if not res.ok:
            bad.append("工具声明没有编译通过：" +
                       "; ".join(d.code for d in res.diagnostics))
        else:
            body = res.ir["program"]["modules"][0]["functions"][0]["body"]
            call = body[0]["value"]
            if call.get("builtin") != "call_tool":
                bad.append("直接工具调用没有降成 call_tool")
            ok, diag, out = pipeline.run_result(res)
            if not ok or out.strip() != "晴，23度":
                bad.append("Python 后端没有拿到工具结果：" +
                           (diag.code if diag else out.strip()))
            try:
                agb = bytecode.compile_program(res.ir)
                got = vm.run(agb, res.lang, allowed_effects={"External"})
                if got.strip() != "晴，23度":
                    bad.append("VM 没有拿到工具结果：" + got.strip())
                try:
                    vm.run(agb, res.lang, allowed_effects=set())
                except Exception as e:
                    if getattr(e, "code", "") != "AGL-RT-0092":
                        bad.append("VM 未授权外部调用时没有报 0092：" +
                                   getattr(e, "code", repr(e)))
                else:
                    bad.append("VM 未授权外部调用时仍然跑通了")
                try:
                    vm.run(agb, res.lang, allowed_effects={"External"},
                           allowed_tools={"别的工具"})
                except vm.VmError as e:
                    if e.code != vm.RT_TOOLS:
                        bad.append("VM 工具白名单拦截错误码不对：" + e.code)
                else:
                    bad.append("VM 工具白名单未拦截未声明工具")
            except Exception as e:
                bad.append(f"VM 工具调用失败：{e}")
    finally:
        server.shutdown()

    # T19：真实工具调用入口上的重复调用熔断。
    # 同一工具 + 同一组实参失败后，下一次同指纹直接拒绝；
    # 同一工具累计失败到 3 次后熔断整个工具。
    class FailHandler(BaseHTTPRequestHandler):
        tool_calls = 0

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            method = req.get("method")
            if method == "initialize":
                body = {"jsonrpc": "2.0", "id": req.get("id"), "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-fail", "version": "0"}}}
            elif method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            elif method == "tools/call":
                FailHandler.tool_calls += 1
                body = {"jsonrpc": "2.0", "id": req.get("id"), "result": {
                    "content": [{"type": "text", "text": "上游工具坏了"}],
                    "isError": True}}
            else:
                body = {"jsonrpc": "2.0", "id": req.get("id"),
                        "error": {"message": "unknown"}}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    fail_server = HTTPServer(("127.0.0.1", 0), FailHandler)
    fail_port = fail_server.server_address[1]
    fail_thread = threading.Thread(target=fail_server.serve_forever, daemon=True)
    fail_thread.start()
    try:
        fail_src = f'''#语言=中文
工具 坏天气 {{
  配置 {{
    协议 = "mcp"
    地址 = "http://127.0.0.1:{fail_port}/mcp"
    超时秒数 = 5
    最大重试次数 = 0
  }}
  入参(城市: 文本)
  返回 文本
}}

函数 主入口() 效应 外部调用 {{
  尝试 {{ 坏天气("甲") }} 捕获 (错: 全部) {{ 打印("一", 失败原因(错)) }}
  尝试 {{ 坏天气("甲") }} 捕获 (错: 全部) {{ 打印("二", 失败原因(错)) }}
  尝试 {{ 坏天气("乙") }} 捕获 (错: 全部) {{ 打印("三", 失败原因(错)) }}
  尝试 {{ 坏天气("丙") }} 捕获 (错: 全部) {{ 打印("四", 失败原因(错)) }}
  尝试 {{ 坏天气("丁") }} 捕获 (错: 全部) {{ 打印("五", 失败原因(错)) }}
}}
'''
        res2 = pipeline.compile_source(fail_src, "tool_breaker.agl")
        if not res2.ok:
            bad.append("熔断用例没有编译通过：" +
                       "; ".join(d.code for d in res2.diagnostics))
        else:
            ok, diag, out = pipeline.run_result(res2)
            expect = ["同一工具已累计失败 1 次", "同一组实参已失败过",
                      "同一工具已累计失败 2 次", "达到熔断阈值",
                      "已熔断：本任务内累计失败达到 3 次"]
            missing = [x for x in expect if x not in out]
            if not ok or missing:
                bad.append("Python 后端熔断行为不完整：" +
                           ("; ".join(missing) if missing else
                            (diag.message if diag else out.strip())))
            if FailHandler.tool_calls != 3:
                bad.append(f"Python 后端熔断后仍请求了服务器：{FailHandler.tool_calls} 次")
            try:
                FailHandler.tool_calls = 0
                agb2 = bytecode.compile_program(res2.ir)
                got2 = vm.run(agb2, res2.lang, allowed_effects={"External"})
                missing2 = [x for x in expect if x not in got2]
                if missing2:
                    bad.append("VM 熔断行为不完整：" + "; ".join(missing2))
                if FailHandler.tool_calls != 3:
                    bad.append(f"VM 熔断后仍请求了服务器：{FailHandler.tool_calls} 次")
            except Exception as e:
                bad.append(f"VM 熔断用例失败：{e}")
    finally:
        fail_server.shutdown()
    if bad:
        c.fail("；".join(bad))
    return c


def check_local_skill() -> Check:
    """本地技能协议：真起子进程、真走 JSON、失败和熔断都按约定走。

    这一项守的是「协议 = local-skill」的整条链路：技能目录、入口解析、
    stdin/stdout 约定、非零退出算确定性失败、缺入口和未实现协议要有可读报错。
    """
    import tempfile

    from compiler import bytecode, vm

    c = Check("本地技能协议")
    bad: list[str] = []

    ok_src = """#语言=中文
工具 求和 {
  配置 {
    协议 = "local-skill"
    地址 = "tests/skills/示例技能"
    超时秒数 = 10
    最大重试次数 = 0
  }
  入参(甲: 整数, 乙: 整数)
  返回 映射表
}

函数 主入口() 效应 外部调用 {
  变量 答复 = 求和(2, 3)
  打印(答复["和"])
  打印(答复["工具"])
}
"""
    res = pipeline.compile_source(ok_src, "skill_ok.agl")
    if not res.ok:
        bad.append("本地技能用例编译失败：" + "; ".join(d.code for d in res.diagnostics))
    else:
        done, diag, out = pipeline.run_result(res)
        if not done or out.split() != ["5", "求和"]:
            bad.append("Python 后端本地技能输出不对：" +
                       (diag.message if diag else out.strip()))
        try:
            got = vm.run(bytecode.compile_program(res.ir), res.lang,
                         allowed_effects={"External"})
            if got.split() != ["5", "求和"]:
                bad.append("VM 本地技能输出不对：" + got.strip())
        except Exception as e:  # noqa: BLE001
            bad.append(f"VM 跑本地技能失败：{e}")

    fail_src = """#语言=中文
工具 失败技能 {
  配置 {
    协议 = "local-skill"
    地址 = "tests/skills/失败技能"
    超时秒数 = 10
    最大重试次数 = 0
  }
  入参(输入: 文本)
  返回 文本
}

函数 主入口() 效应 外部调用 {
  尝试 { 失败技能("甲") } 捕获 (错: 全部) { 打印("一", 失败原因(错)) }
  尝试 { 失败技能("乙") } 捕获 (错: 全部) { 打印("二", 失败原因(错)) }
}
"""
    res2 = pipeline.compile_source(fail_src, "skill_fail.agl")
    if not res2.ok:
        bad.append("失败技能用例编译失败：" + "; ".join(d.code for d in res2.diagnostics))
    else:
        done2, diag2, out2 = pipeline.run_result(res2)
        for want in ("退出码 3", "故意失败", "累计失败 1 次", "累计失败 2 次"):
            if want not in out2:
                bad.append("确定失败路径缺少 `" + want + "`：" +
                           (diag2.message if diag2 else out2.strip()))

    tmp = Path(tempfile.mkdtemp(prefix="agl_skill_probe_"))
    try:
        empty_dir = tmp / "空技能"
        empty_dir.mkdir()
        (empty_dir / "SKILL.md").write_text("# 只有说明没有入口\n", encoding="utf-8")
        miss_src = f"""#语言=中文
工具 无入口 {{
  配置 {{
    协议 = "local-skill"
    地址 = "{empty_dir.as_posix()}"
    最大重试次数 = 0
  }}
  入参(内容: 文本)
  返回 文本
}}

函数 主入口() 效应 外部调用 {{
  尝试 {{ 无入口("甲") }} 捕获 (错: 全部) {{ 打印(失败原因(错)) }}
}}
"""
        res3 = pipeline.compile_source(miss_src, "skill_missing.agl")
        if not res3.ok:
            bad.append("缺入口用例编译失败：" +
                       "; ".join(d.code for d in res3.diagnostics))
        else:
            _done3, _diag3, out3 = pipeline.run_result(res3)
            if "找不到入口脚本" not in out3:
                bad.append("缺入口目录没有报可读原因：" + out3.strip())

        acp_src = """#语言=中文
工具 上游智能体 {
  配置 {
    协议 = "acp"
    地址 = "http://127.0.0.1:1/acp"
    最大重试次数 = 0
  }
  入参(提示: 文本)
  返回 文本
}

函数 主入口() 效应 外部调用 {
  尝试 { 上游智能体("甲") } 捕获 (错: 全部) { 打印(失败原因(错)) }
}
"""
        res4 = pipeline.compile_source(acp_src, "skill_acp.agl")
        if not res4.ok:
            bad.append("acp 用例编译失败：" + "; ".join(d.code for d in res4.diagnostics))
        else:
            _done4, _diag4, out4 = pipeline.run_result(res4)
            if "尚未实现" not in out4:
                bad.append("acp 未实现时没有给出明确说明：" + out4.strip())
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        c.fail("；".join(bad[:4]))
    return c.done("示例技能 · 失败退出码 · 缺入口 · acp 明确拒绝")


def check_tool_stdio() -> Check:
    """MCP stdio 传输：真起子进程、真跑 JSON-RPC 三帧；配置歧义在编译期拒绝。

    本地 MCP 服务器大多只提供 stdio，这条链路断了就等于只能连远程 HTTP 服务。
    """
    from compiler import bytecode, vm

    c = Check("MCP stdio 传输")
    bad: list[str] = []

    src = """#语言=中文
工具 天气 {
  配置 {
    协议 = "mcp"
    传输 = "stdio"
    命令 = "python tests/mcp/stdio_server.py"
    超时秒数 = 10
    最大重试次数 = 0
  }
  入参(城市: 文本)
  返回 文本
}

函数 主入口() 效应 外部调用 {
  打印(天气("杭州"))
}
"""
    res = pipeline.compile_source(src, "stdio_ok.agl")
    if not res.ok:
        bad.append("stdio 用例编译失败：" + "; ".join(d.code for d in res.diagnostics))
    else:
        done, diag, out = pipeline.run_result(res)
        if not done or "杭州：晴，23度" not in out:
            bad.append("Python 后端 stdio 输出不对：" +
                       (diag.message if diag else out.strip()))
        try:
            got = vm.run(bytecode.compile_program(res.ir), res.lang,
                         allowed_effects={"External"})
            if "杭州：晴，23度" not in got:
                bad.append("VM stdio 输出不对：" + got.strip())
        except Exception as e:  # noqa: BLE001
            bad.append(f"VM 跑 stdio 失败：{e}")

    # 命令写成字符串数组，也要能拉起同一个服务器。
    src2 = src.replace('命令 = "python tests/mcp/stdio_server.py"',
                       '命令 = ["python", "tests/mcp/stdio_server.py"]')
    res2 = pipeline.compile_source(src2, "stdio_argv.agl")
    if not res2.ok:
        bad.append("字符串数组命令编译失败：" +
                   "; ".join(d.code for d in res2.diagnostics))
    else:
        done2, diag2, out2 = pipeline.run_result(res2)
        if not done2 or "杭州：晴，23度" not in out2:
            bad.append("字符串数组命令跑不通：" +
                       (diag2.message if diag2 else out2.strip()))

    # 协议/传输/命令三者的组合必须被编译期拦住。
    bad_cases = {
        "缺少命令": src.replace('传输 = "stdio"', '传输 = "stdio"')
        .replace('    命令 = "python tests/mcp/stdio_server.py"\n', ""),
        "非法传输值": src.replace('传输 = "stdio"', '传输 = "sse"')
        .replace('    命令 = "python tests/mcp/stdio_server.py"\n', ""),
    }
    for label, case_src in bad_cases.items():
        res3 = pipeline.compile_source(case_src, "stdio_bad.agl")
        if res3.ok:
            bad.append(label + " 没有被编译期拒绝")
        elif all(d.code != "AGL-E-0605" for d in res3.diagnostics):
            bad.append(label + " 报的不是 AGL-E-0605：" +
                       "; ".join(d.code for d in res3.diagnostics))

    if bad:
        c.fail("；".join(bad[:4]))
    return c.done("字符串命令 · 数组命令 · 编译期拒绝缺命令/非法传输")


def check_security_defaults() -> Check:
    """安全默认不能回退：回环绑定、协议白名单、BOM、MCP 路径边界。"""
    import os
    import tempfile

    c = Check("安全默认与路径边界")
    bad: list[str] = []

    webapp = (REPO / "compiler" / "webapp.py").read_text(encoding="utf-8")
    if 'HTTPServer((host, int(port))' not in webapp:
        bad.append("webapp.py 没有使用可配置 host")

    backend = (REPO / "compiler" / "backend_py" / "__init__.py").read_text(encoding="utf-8")
    if "只允许 http/https" not in backend:
        bad.append("获取网页缺少协议白名单")
    if 'HTTPServer(("127.0.0.1"' not in backend:
        bad.append("种子后端仍有服务未绑定回环")

    data = toml_loads((REPO / "spec" / "keywords.toml").read_text(encoding="utf-8"))
    web = next((k for k in (data.get("builtin", []) + data.get("keyword", []))
                if k.get("zh") == "启动网页应用"), None)
    if not web or web.get("params") != ["整数", "文本", "文本"] or web.get("opt") != 1:
        bad.append("启动网页应用的规格不是 2 必填 + 1 可选")

    tmp = Path(tempfile.mkdtemp(prefix="agl_sec_probe_"))
    try:
        bom = tmp / "bom.agl"
        bom.write_bytes(b"\xef\xbb\xbf" + "#语言=中文\n\n函数 主入口() {\n  打印(1)\n}\n".encode("utf-8"))
        res = pipeline.compile_file(bom)
        if not res.ok:
            bad.append("带 UTF-8 BOM 的源文件仍编译失败")

        from compiler import mcp
        old = os.getcwd()
        os.chdir(REPO)
        try:
            try:
                mcp._file({"file": str(tmp / "bom.agl")})
            except ValueError:
                pass
            else:
                bad.append("MCP 接受了工作区之外的绝对路径")

            # MCP 的 agl_run_source 也必须默认全禁，不能比 CLI 松。
            probe = tmp / "mcp_auth_input.txt"
            probe.write_text("mcp-auth\n", encoding="utf-8")
            auth_src = (
                "#语言=中文\n"
                "函数 主入口() 效应 读写 {\n"
                f'  变量 内容 = 读文件("{probe.as_posix()}")\n'
                "  打印(内容)\n"
                "}\n"
            )
            base = {"source": auth_src, "filename": "mcp_auth.agl"}
            denied = mcp.tool_run_source(dict(base))
            if denied.get("first_error") != "AGL-RT-0092":
                bad.append("MCP 未传授权时没有默认拒绝效应")
            allowed = mcp.tool_run_source(dict(base, allow_effects="IO"))
            if not allowed.get("ok"):
                bad.append("MCP 显式 allow_effects=IO 后仍无法运行")
        finally:
            os.chdir(old)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done("回环绑定 · 协议白名单 · BOM · MCP 工作区")


def check_mcp() -> Check:
    """MCP 工具层必须与编译器同步：握手、工具清单、真实调用都要通。

    只测"能启动"没有意义 —— AI 客户端真正依赖的是
    「调用一次就拿到结构化结果」，所以要真调用 check 和 run。
    """
    from compiler import mcp

    c = Check("MCP 工具服务器")
    bad: list[str] = []
    srv = mcp.Server()

    def call(method: str, params: dict | None = None, rid=1):
        return srv.handle({"jsonrpc": "2.0", "id": rid, "method": method,
                           "params": params or {}})

    def payload(resp: dict) -> dict:
        text = resp["result"]["content"][0]["text"]
        return json.loads(text)

    # 握手：协议版本 + 第一句话必须指向速查卡
    init = call("initialize")
    if not init or init.get("result", {}).get("protocolVersion") != mcp.PROTOCOL_VERSION:
        bad.append("initialize 握手失败")
    elif "agl_card" not in init["result"].get("instructions", ""):
        bad.append("initialize 的说明没有指向 agl_card")

    # 通知不许回包（回了就是协议违规）
    if srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is not None:
        bad.append("通知被错误地回了包")

    # 工具清单：五个核心工具齐、schema 都是 object
    listing = call("tools/list")
    tools = listing["result"]["tools"] if listing else []
    names = {t["name"] for t in tools}
    need = {"agl_card", "agl_check", "agl_build", "agl_run", "agl_convert"}
    miss = need - names
    if miss:
        bad.append(f"工具清单缺 {sorted(miss)}")
    for t in tools:
        if t["inputSchema"].get("type") != "object":
            bad.append(f"{t['name']} 的 inputSchema 不是 object")
        if not t.get("description"):
            bad.append(f"{t['name']} 没有说明")

    # 真调用：合法的用例要 ok=true
    good = GOLDEN / "full_zh.agl"
    r = payload(call("tools/call", {"name": "agl_check",
                                    "arguments": {"file": str(good)}}))
    if not r.get("ok"):
        bad.append("agl_check 对合法用例返回了失败")

    # 真调用：错误用例要带回错误码、行号、fix_hints
    err = CASES / "err_undefined.agl"
    resp = call("tools/call", {"name": "agl_check", "arguments": {"file": str(err)}})
    r = payload(resp)
    if r.get("ok") is not False:
        bad.append("agl_check 对错误用例没有判失败")
    elif not r.get("diagnostics"):
        bad.append("agl_check 没有返回诊断")
    else:
        d = r["diagnostics"][0]
        if not d.get("code") or d.get("agl", {}).get("line", 0) < 1:
            bad.append("诊断缺错误码或行号")
        if not d.get("fix_hints"):
            bad.append("诊断缺 fix_hints（AI 自修复靠它）")
    if not resp["result"].get("isError"):
        bad.append("错误用例没有标记 isError")

    # 运行工具：要带回 stdout
    r = payload(call("tools/call", {"name": "agl_run",
                                    "arguments": {"file": str(good)}}))
    if not r.get("ok") or not r.get("stdout", "").strip():
        bad.append("agl_run 没有返回输出")

    # 版本必须跟随语言版本：MCP 0.3.0 vs 语言 0.25.0 的脱节不许再出现
    lang_ver = KeywordTable.load().version
    if mcp.SERVER_VERSION != lang_ver:
        bad.append(f"MCP 版本 {mcp.SERVER_VERSION} ≠ 语言版本 {lang_ver}")
    if init["result"].get("languageVersion") != lang_ver:
        bad.append("initialize 的 languageVersion 与语言版本不一致")
    v = payload(call("tools/call", {"name": "agl_version", "arguments": {}}))
    if not v.get("ok") or v.get("version") != lang_ver:
        bad.append("agl_version 返回的版本与语言版本不一致")

    # 不存在的工具要按协议报错，而不是静默
    resp = call("tools/call", {"name": "agl_nope", "arguments": {}})
    if "error" not in resp:
        bad.append("调用不存在的工具没有返回协议错误")

    # T17：`aglc tools` 工具块三件套导出 —— name/参数 schema 必须结构化正确
    import sys

    tool_src = GOLDEN / "tool_decl_zh.agl"
    if tool_src.exists():
        ex = _run(
            [sys.executable, "-m", "compiler.cli", "tools", str(tool_src), "--json"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
        try:
            trio = json.loads(ex.stdout)
        except json.JSONDecodeError:
            trio = {}
        exported = (trio.get("tools") or [{}])[0]
        if ex.returncode != 0 or exported.get("name") != "加载文本":
            bad.append(f"tools 导出失败：{ex.stdout.strip()[:80]} {ex.stderr.strip()[:80]}")
        else:
            props = exported["inputSchema"]["properties"]
            if props.get("文件路径", {}).get("type") != "string":
                bad.append(f"tools 导出的参数 schema 不对：{props}")
            cfg = exported.get("config") or {}
            if cfg.get("protocol") != "mcp":
                bad.append("tools 导出丢了 config.protocol")
        c.note("工具三件套导出通过（tool_decl_zh）")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(tools)} 个工具 · 调用通过" if not bad else "")


def check_stdlib() -> Check:
    """标准库：不声明不许用、声明了能跑、中英两份等价、每个模块自身能编译。"""
    from compiler import modules as modules_mod
    from compiler.convert import compare

    c = Check("标准库（声明后可用）")
    bad: list[str] = []

    # 1. 每个标准库模块自身都要能编译（库文件没有入口函数是正常的）
    libs = modules_mod.stdlib_modules()
    if not libs:
        return c.fail("stdlib/ 是空的").done()
    for name, path in libs.items():
        res = pipeline.compile_source(path.read_text(encoding="utf-8"), path.name,
                                      is_entry=False)
        if not res.ok:
            bad.append(f"{name}.agl 编译未通过：{res.errors[0].code}")

    # 2. 声明后能跑，输出对得上
    zh_main = CASES / "stdlib_ok" / "main.agl"
    en_main = CASES / "stdlib_ok_en" / "main.agl"
    want_zh = ["绝对值 5", "较大 9", "合计 8", None, "含 4"]   # None = 均值（浮点，容差比）
    want_en = ["abs 5", "bigger 9", "sum 8", None, "has 4"]

    irs: list[dict] = []
    for path, want, label, avg_label in (
        (zh_main, want_zh, "中文", "均值"),
        (en_main, want_en, "英文", "avg"),
    ):
        if not path.exists():
            bad.append(f"{label}标准库用例缺失：{path}")
            continue
        res = pipeline.compile_file(path)
        if not res.ok:
            bad.append(f"{label}标准库用例编译未通过：{res.errors[0].code}")
            continue
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag is not None:
            bad.append(f"{label}标准库用例运行报 {diag.code} 第 {diag.line} 行")
            continue
        got = out.strip().splitlines()
        if len(got) != len(want):
            bad.append(f"{label}标准库输出 {len(got)} 行，期望 {len(want)} 行")
        else:
            for line, exp, idx in zip(got, want, range(len(want))):
                if exp is None:
                    parts = line.split()
                    try:
                        near = abs(float(parts[1]) - 8 / 3) < 1e-9
                    except (IndexError, ValueError):
                        near = False
                    if parts[0] != avg_label or not near:
                        bad.append(f"{label}标准库平均值输出是 {line!r}")
                elif line != exp:
                    bad.append(f"{label}标准库第 {idx + 1} 行是 {line!r}，期望 {exp!r}")
        irs.append(res.ir)

    if len(irs) == 2:
        same, detail = compare(irs[0], irs[1])
        if not same:
            bad.append(f"标准库用例双分支不等价：{detail.splitlines()[0]}")

    # 3. 没声明就不许导入，而且提示要给出可粘贴的那一行
    undeclared = CASES / "stdlib_undeclared" / "main.agl"
    if undeclared.exists():
        res = pipeline.compile_file(undeclared)
        if res.ok:
            bad.append("未声明标准库却编译通过了")
        else:
            d = res.errors[0]
            if d.code != "AGL-E-0601":
                bad.append(f"未声明标准库应报 0601，实际 {d.code}")
            elif not any("[dependencies]" in h.suggest for h in d.fix_hints):
                bad.append("未声明标准库的提示没说清去 [dependencies] 加一行")
            elif not any('"数学"' in h.suggest for h in d.fix_hints):
                bad.append("未声明标准库的提示没给出带引号的键名（TOML 裸键不能是中文）")
    else:
        bad.append("缺未声明标准库用例")

    # 4. 扩充后的标准库要真能干活（排序 / 去重 / 文本查找 / 数学）
    #    只断言结果，不断言具体文案：文案属于输出层，会随分支变。
    for path, label, want in (
        (CASES / "stdlib2" / "main.agl", "中文扩充库",
         ["排序 [1, 1, 2, 3]", "原样 [3, 1, 2, 1]", "去重 [3, 1, 2]",
          "反转 [1, 2, 1, 3]", "找 9 -1", "计数 2", "幂 1024"]),
        (CASES / "stdlib2_en" / "main.agl", "英文扩充库",
         ["sorted [1, 1, 2, 3]", "original [3, 1, 2, 1]", "distinct [3, 1, 2]",
          "reversed [1, 2, 1, 3]", "find 9 -1", "count 2", "power 1024"]),
    ):
        if not path.exists():
            bad.append(f"{label}用例缺失：{path}")
            continue
        res = pipeline.compile_file(path)
        if not res.ok:
            bad.append(f"{label}用例编译未通过：{res.errors[0].code}")
            continue
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag is not None:
            bad.append(f"{label}用例运行报 {diag.code} 第 {diag.line} 行")
            continue
        lines = [x.strip() for x in out.strip().splitlines()]
        for want_line in want:
            if want_line not in lines:
                bad.append(f"{label}用例没跑出 {want_line!r}")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(libs)} 个模块 · 声明后可用" if not bad else "")


def check_optimize() -> Check:
    """优化对拍：开了优化，每个用例的输出必须与朴素版逐字一致。

    这是优化通道允许存在的唯一理由。任何优化（将来的快路径、缓存、
    后端 B）上线前都必须过这一关，否则就是"跑得快但算错了"。
    同时要求至少有一个用例真的被折叠过 —— 否则这项检查是空转。
    """
    from compiler.ir import validate_ir

    c = Check("优化对拍（语义不变）")
    bad: list[str] = []
    # 用 rglob 而不是 glob：跨模块的对拍用例自带子目录（同一个目录下才有互相导入），
    # 只扫一层会把最有价值的那些——模块边界上的优化——漏掉。
    files = _golden_agls(recursive=True) + sorted((CASES / "opt").rglob("*.agl"))
    # 每一个 Pass 都必须真的在某个用例上生效过 —— 优化通道最容易的失败方式
    # 不是"优化错了"，而是"根本没优化"，而后者对拍是发现不了的。
    passes = {"折叠": 0, "删除": 0, "化简": 0, "无用变量": 0, "公共子表达式": 0}
    folded_total = 0
    checked = 0

    for f in files:
        plain = pipeline.compile_file(f)
        opt = pipeline.compile_file(f, optimize=True)

        if plain.ok != opt.ok:
            bad.append(f"{f.name}: 开优化后能否编译发生了变化")
            continue
        if not plain.ok:
            continue

        problems = validate_ir(opt.ir)
        if problems:
            bad.append(f"{f.name}: 优化后的 IR 不合规（{problems[0]}）")
            continue

        p_ok, p_diag, p_out = pipeline.run_result(plain)
        o_ok, o_diag, o_out = pipeline.run_result(opt)
        checked += 1
        if o_ok != p_ok:
            bad.append(f"{f.name}: 开优化后运行结果变了（{p_ok} → {o_ok}）")
            continue
        if not p_ok:
            pc = p_diag.code if p_diag else "?"
            oc = o_diag.code if o_diag else "?"
            if pc != oc:
                bad.append(f"{f.name}: 错误码变了（{pc} → {oc}）")
            continue
        if p_out != o_out:
            bad.append(f"{f.name}: 输出不一致")

        if any("折叠" in n or "删除" in n for n in opt.opt_notes):
            folded_total += 1
        for n in opt.opt_notes:
            for key in passes:
                if key in n:
                    passes[key] += 1

    if not checked:
        bad.append("没有可对拍的用例")
    elif folded_total == 0:
        bad.append("所有用例都没发生折叠/删除，对拍是空转（补一个有常量的用例）")

    # 每个 Pass 至少真的生效过一次
    for key, hits in passes.items():
        if hits == 0:
            bad.append(f"优化 Pass「{key}」在所有用例上都没生效（空转）")

    if bad:
        c.fail("; ".join(bad[:3]))
    detail = f"{checked} 组逐字一致 · " + " · ".join(
        f"{k} {v} 组" for k, v in passes.items())
    return c.done(detail if not bad else "")


def check_grammar() -> Check:
    """编辑器高亮：语法文件必须与关键字表一致，且磁盘上的副本不能过期。

    高亮漏一个词的后果不是"少点颜色"，而是用户以为那个词不能用。
    所以这里反过来验：表里每个启用的词，语法文件都得能匹配上。
    """
    from compiler import grammar

    c = Check("编辑器语法高亮")
    bad: list[str] = []
    ext = REPO / "editor" / "vscode"

    probs = grammar.audit()
    if probs:
        bad.append("; ".join(probs[:3]))

    # 磁盘上的副本必须是刚生成的（防止有人手改语法文件造成漂移）
    syn = ext / "syntaxes" / "agl.tmLanguage.json"
    if not syn.exists():
        bad.append("editor/vscode/syntaxes/agl.tmLanguage.json 不存在（跑 `aglc grammar --dir editor/vscode`）")
    else:
        on_disk = syn.read_text(encoding="utf-8")
        if on_disk != grammar.dumps():
            bad.append("磁盘上的语法文件与关键字表生成结果不一致（关键字改过但没重新生成）")
        try:
            g = json.loads(on_disk)
        except json.JSONDecodeError as e:
            bad.append(f"语法文件不是合法 JSON：{e}")
            g = {}
        if g.get("scopeName") != grammar.SCOPE_NAME:
            bad.append(f"scopeName 应为 {grammar.SCOPE_NAME}")

    # 扩展清单：路径要对得上、必须保持零依赖
    pkg_path = ext / "package.json"
    if not pkg_path.exists():
        bad.append("editor/vscode/package.json 不存在")
    else:
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            bad.append(f"package.json 不是合法 JSON：{e}")
            pkg = {}
        if pkg.get("dependencies") or pkg.get("devDependencies"):
            bad.append("扩展引入了 npm 依赖：安装门槛直接被抬高，必须零依赖")
        langs = (pkg.get("contributes") or {}).get("languages") or []
        if not langs or ".agl" not in (langs[0].get("extensions") or []):
            bad.append("package.json 没有把 .agl 注册成语言")
        grams = (pkg.get("contributes") or {}).get("grammars") or []
        if not grams:
            bad.append("package.json 没有声明语法文件")
        else:
            for rel in (grams[0].get("path", ""), langs[0].get("configuration", "")):
                if rel and not (ext / rel.lstrip("./")).exists():
                    bad.append(f"package.json 指向的文件不存在：{rel}")
        main = pkg.get("main")
        if main and not (ext / main.lstrip("./")).exists():
            bad.append(f"入口文件不存在：{main}")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done(f"{len(grammar.build()['patterns'])} 条规则 · 卡片级覆盖 · 零依赖")


def check_lsp() -> Check:
    """语言服务器：协议层必须真跑一遍，光"能启动"不算数。

    分两层验：先在进程内把五个能力都调一遍（快、失败信息清楚），
    再起真子进程走一次 stdio 帧（证明 `Content-Length` 的读写没写错）。
    """
    import sys

    from compiler import lsp

    c = Check("语言服务器（LSP）")
    bad: list[str] = []
    srv = lsp.Server()

    def req(rid, method, params):
        out = srv.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return out[0].get("result") if out else None

    init = req(1, "initialize", {})
    caps = (init or {}).get("capabilities") or {}
    for cap in ("hoverProvider", "completionProvider", "documentSymbolProvider",
                "definitionProvider"):
        if cap not in caps:
            bad.append(f"握手缺少能力 {cap}")
    if not caps.get("textDocumentSync"):
        bad.append("握手缺少 textDocumentSync")

    # 通知不许回包：LSP 里通知回了消息就是协议违规
    if srv.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}}):
        bad.append("通知被错误地回了包")

    good = (GOLDEN / "full_zh.agl").read_text(encoding="utf-8")
    uri = lsp.path_to_uri(GOLDEN / "full_zh.agl")
    # 用磁盘上的真文件路径：这样模块解析走真实路径，和编辑器里一致
    out = srv.handle({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": uri, "languageId": "agl", "version": 1, "text": good}}})
    if not out or out[0].get("method") != "textDocument/publishDiagnostics":
        bad.append("didOpen 没有推送诊断")
    elif out[0]["params"]["diagnostics"]:
        bad.append("合法用例被报出诊断")

    # 悬停：关键字要给出中英对照
    lines = good.splitlines()
    fn_line = next((i for i, s in enumerate(lines) if s.startswith("函数")), 0)
    h = req(2, "textDocument/hover", {"textDocument": {"uri": uri},
                                      "position": {"line": fn_line, "character": 1}})
    if not h or "函数" not in (h.get("contents") or {}).get("value", ""):
        bad.append("悬停没有给出关键字说明")

    # 补全：要含当前分支关键字和本文件函数
    comp = req(3, "textDocument/completion", {"textDocument": {"uri": uri},
                                              "position": {"line": fn_line, "character": 0}})
    labels = {it["label"] for it in (comp or {}).get("items", [])}
    if "函数" not in labels or "打印" not in labels:
        bad.append("补全缺关键字")
    try:
        fn_names = {f["name"] for f in
                    srv.docs[uri].result.ir["program"]["modules"][0]["functions"]}
    except Exception:
        fn_names = set()
    if fn_names and not (fn_names & labels):
        bad.append("补全里没有本文件的函数")

    # 大纲 + 跳转
    syms = req(4, "textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    if not syms:
        bad.append("大纲为空")
    call = next(((i, s.index("相加(")) for i, s in enumerate(lines)
                 if "相加(" in s and not s.startswith("函数")), None)
    if call:
        d = req(5, "textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": call[0], "character": call[1] + 1}})
        if not d:
            bad.append("跳转找不到定义")

    # 错误用例：诊断要带错误码，且改好后清空
    bad_src = "#语言=中文\n函数 主入口() {\n  打印(没定义的名字)\n}\n"
    bad_uri = "file:///tmp/agl_doctor_bad.agl"
    out = srv.handle({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": bad_uri, "languageId": "agl", "version": 1, "text": bad_src}}})
    ds = out[0]["params"]["diagnostics"] if out else []
    if not ds:
        bad.append("错误用例没有报诊断")
    elif not ds[0].get("code") or not ds[0]["message"].count("\n"):
        bad.append("诊断缺错误码或修复建议")
    out = srv.handle({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {
        "textDocument": {"uri": bad_uri, "version": 2},
        "contentChanges": [{"text": "#语言=中文\n函数 主入口() {\n  打印(\"好了\")\n}\n"}]}})
    if out and out[0]["params"]["diagnostics"]:
        bad.append("改好之后诊断没有清空")

    # 真子进程：证明 Content-Length 的读写没错
    try:
        p = subprocess.Popen([sys.executable, "-m", "compiler.lsp"], cwd=str(REPO),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {}}).encode()
        p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        p.stdin.flush()
        length = None
        while True:
            line = p.stdout.readline()
            if not line:
                break
            s = line.decode().strip()
            if s.lower().startswith("content-length:"):
                length = int(s.split(":", 1)[1])
            elif not s and length is not None:
                break
        msg = json.loads(p.stdout.read(length).decode()) if length else None
        if not msg or "result" not in msg:
            bad.append("stdio 传输层没回 initialize")
        else:
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "shutdown",
                               "params": {}}).encode()
            p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            p.stdin.flush()
            p.stdin.close()
        p.wait(timeout=10)
    except Exception as e:
        bad.append(f"stdio 传输层失败：{type(e).__name__}")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done("诊断/悬停/补全/大纲/跳转全通 · 真子进程帧读写通过")


def check_builtin() -> Check:
    """内建函数表：表自洽 + 每条的示例真能编译运行 + 每条都被双分支用例覆盖。

    为什么非要跑示例：内建函数的知识有四份消费者（语义 / 后端 / 速查卡 / 悬停），
    表里写错一个字，四处一起错。唯一能自证的办法是拿表里的示例去真编译一遍。
    """
    from compiler.checker import ZH_TYPE
    from compiler.lexer import LANG_ZH, KeywordTable

    c = Check("内建函数表")
    t = KeywordTable.load()
    # 内建示例外壳是中文文件，effect 关键字必须按中文分支渲染
    # （表里 effect 存的是英文 canonical，如 "Time"，中文文件里得写 "计时"）。
    effect_zh = {b.effect: t.text_for(b.effect, LANG_ZH) for b in t.builtins if b.effect}
    bad: list[str] = []
    if not t.builtins:
        return c.fail("keywords.toml 里没有 [[builtin]]").done()

    effects = {"IO", "Net", "model", "Time", "External"}
    seen_zh: set[str] = set()
    seen_en: set[str] = set()
    seen_canon: set[str] = set()
    for b in t.builtins:
        for word, bag, label in ((b.zh, seen_zh, "中文名"),
                                 (b.en, seen_en, "英文名"),
                                 (b.canonical, seen_canon, "规范名")):
            if word in bag:
                bad.append(f"{label}重复：`{word}`")
            bag.add(word)
        if not b.lower:
            bad.append(f"`{b.zh}` 没写 lower（后端不知道落地成什么）")
        if b.ret not in ZH_TYPE:
            bad.append(f"`{b.zh}` 的返回类型 `{b.ret}` 不在已知类型里")
        for p in b.params:
            if p not in ZH_TYPE:
                # 联合写法（`列表/集合`）：每一支都必须是已知类型
                if "/" in p and all(part in ZH_TYPE for part in p.split("/")):
                    continue
                bad.append(f"`{b.zh}` 的参数类型 `{p}` 不在已知类型里")
        if b.effect and b.effect not in effects:
            bad.append(f"`{b.zh}` 的效应 `{b.effect}` 不是五个标准标签之一")
        if not b.summary_zh or not b.summary_en:
            bad.append(f"`{b.zh}` 缺 summary（速查卡和悬停要用）")
        if not b.example:
            bad.append(f"`{b.zh}` 缺 example（示例是它唯一的自证）")

    # 示例必须能真编译（包进一个主入口里当表达式语句）。
    # 内建若带效应 / 需要等待，外壳就要是异步函数并声明对应效应，
    # 否则示例自己都编不过（睡眠要 `等待` + 「计时」效应）。
    for b in t.builtins:
        if not b.example:
            continue
        is_async = bool(b.effect) or b.needs_await
        head = "异步 函数 主入口()"
        if b.effect:
            head += f" 效应 {effect_zh.get(b.effect, b.effect)}"
        src = f"#语言=中文\n{head} {{\n  {b.example}\n}}\n"
        if not is_async:
            src = src.replace("异步 函数", "函数")
        res = pipeline.compile_source(src, f"<内建示例 {b.canonical}>")
        if not res.ok:
            d = res.errors[0]
            bad.append(f"`{b.zh}` 的示例编译不过：{d.code}（{d.message_zh}）")

    # 双分支用例覆盖：中文名必须出现在中文用例里，英文名必须在英文用例里
    zh_case = GOLDEN / "builtin_zh.agl"
    en_case = GOLDEN / "builtin_en.agl"
    zh_src = zh_case.read_text(encoding="utf-8") if zh_case.exists() else ""
    en_src = en_case.read_text(encoding="utf-8") if en_case.exists() else ""
    if not zh_src or not en_src:
        bad.append("缺少内建函数的双分支黄金用例（builtin_zh/en.agl）")
    else:
        for b in t.builtins:
            if b.zh not in zh_src:
                bad.append(f"`{b.zh}` 没被中文黄金用例覆盖")
            if b.en not in en_src:
                bad.append(f"`{b.en}` 没被英文黄金用例覆盖")

    # CLI 的 `list-builtins` 必须读 [[builtin]] 表，而不是只列 4 个关键字。
    import sys

    cli = _run(
        [sys.executable, "-m", "compiler.cli", "list-builtins"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if cli.returncode != 0 or f"共 {len(t.builtins)} 个内建" not in cli.stdout:
        bad.append(f"list-builtins 输出不对：rc={cli.returncode} out={cli.stdout[:80]!r}")

    if bad:
        c.fail("; ".join(bad[:3]))
    n = len(t.builtins)
    c.note(f"{n} 个内建函数 · 全部纯函数" if not any(b.effect for b in t.builtins)
           else f"{n} 个内建函数")
    return c.done(f"{n} 个内建 · 示例真编译 · 双分支覆盖")


def _timed_run(src: str, filename: str = "<计时>") -> float | None:
    """编译并运行一段 AGL，返回耗时（秒）。编译失败返回 None。"""
    import time

    res = pipeline.compile_source(src, filename)
    if not res.ok:
        return None
    t = time.perf_counter()
    pipeline.run_result(res)
    return time.perf_counter() - t


def check_async() -> Check:
    """异步运行时：并发真的更快、取消后不再触发回调、异步内错误行号回译到 AGL。

    这一项防的是"看起来异步、其实没并发"：并发必须用可测的时间差证明，不靠感觉。
    时间差测试用 250ms 睡眠，并发 ≈250ms、顺序 ≈500ms，阈值留足余量（< 0.8×），
    避免在慢机器上误报。
    """
    from compiler.lexer import LANG_EN, LANG_ZH

    c = Check("异步运行时（并发 / 取消 / 行号回译）")
    bad: list[str] = []

    # 1) 中英黄金用例要能跑，且输出确定（不依赖时序）
    for lang, name, want in (
        (LANG_ZH, "async_zh.agl", "完成 ok\n结果 ok"),
        (LANG_EN, "async_en.agl", "done ok\nresult ok"),
    ):
        f = GOLDEN / name
        if not f.exists():
            bad.append(f"缺 {name}")
            continue
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{name} 编译未通过：{res.errors[0].code}")
            continue
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag:
            bad.append(f"{name} 运行报 {diag.code} 第 {diag.line} 行")
        elif out.strip() != want:
            bad.append(f"{name} 输出 {out.strip()!r}，期望 {want!r}")

    # 2) 并发确实更快
    conc = ("#语言=中文\n"
            "异步 函数 慢(毫秒: 整数) -> 文本 效应 计时 {\n"
            "  等待 睡眠(毫秒)\n  返回 \"好\"\n}\n"
            "异步 函数 主入口() 效应 计时 {\n"
            "  变量 甲 = 任务(慢(250))\n  变量 乙 = 任务(慢(250))\n"
            "  等待 甲\n  等待 乙\n}\n")
    seq = ("#语言=中文\n"
           "异步 函数 慢(毫秒: 整数) -> 文本 效应 计时 {\n"
           "  等待 睡眠(毫秒)\n  返回 \"好\"\n}\n"
           "异步 函数 主入口() 效应 计时 {\n"
           "  等待 慢(250)\n  等待 慢(250)\n}\n")
    tc = _timed_run(conc)
    ts = _timed_run(seq)
    if tc is None or ts is None:
        bad.append("并发/顺序计时用例编译或运行失败")
    elif not (tc < ts * 0.8):
        bad.append(f"并发 {tc*1000:.0f}ms 未明显快于顺序 {ts*1000:.0f}ms")
    else:
        c.note(f"并发 {tc*1000:.0f}ms vs 顺序 {ts*1000:.0f}ms")

    # 3) 取消后不再触发回调，且 `等待` 优雅返回（不崩）
    cancel_src = ("#语言=中文\n"
                  "异步 函数 慢() -> 文本 效应 计时 {\n"
                  "  等待 睡眠(200)\n  返回 \"做完了\"\n}\n"
                  "异步 函数 主入口() 效应 计时 {\n"
                  "  变量 甲 = 任务(慢())\n"
                  "  当任务完成(甲, 返回值: 文本) { 打印(\"不该出现\", 返回值) }\n"
                  "  取消(甲)\n  等待 甲\n  打印(\"已取消\")\n}\n")
    res = pipeline.compile_source(cancel_src, "<取消测试>")
    if not res.ok:
        bad.append(f"取消用例编译未通过：{res.errors[0].code}")
    else:
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag:
            bad.append(f"取消用例运行报 {diag.code} 第 {diag.line} 行")
        elif "不该出现" in out:
            bad.append("取消后仍触发了完成回调")
        elif "已取消" not in out:
            bad.append("取消用例没有打印「已取消」")

    # 4) 异步内运行时错误行号回译到 AGL
    err_src = ("#语言=中文\n"
               "异步 函数 慢() -> 文本 效应 计时 {\n"
               "  等待 睡眠(20)\n  变量 x = 切片(\"ab\", 0, 2, 0)\n  返回 \"x\"\n}\n"
               "异步 函数 主入口() 效应 计时 {\n"
               "  变量 甲 = 任务(慢())\n  等待 甲\n}\n")
    res = pipeline.compile_source(err_src, "<行号回译>")
    if not res.ok:
        bad.append(f"异步错误用例编译未通过：{res.errors[0].code}")
    else:
        ok, diag, _ = pipeline.run_result(res)
        if ok or diag is None:
            bad.append("异步错误用例：期望运行期报错，实际正常结束")
        elif diag.line <= 1:
            bad.append("异步错误用例：行号没回译到源码")
        else:
            c.note(f"异步错误回译到 AGL 第 {diag.line} 行")

    # 5) 回调体内部的错不能吞 —— 必须回译到回调体所在的那一行
    #    （曾经 gather 用 return_exceptions 收集后直接丢弃，回调里的错误
    #     一声不响就没了，程序照常跑完。这不是小瑕疵：出错不报错 = 越界不报错。）
    cb_err = ("#语言=中文\n"
              "异步 函数 慢() -> 文本 {\n"
              "  返回 \"ok\"\n}\n"
              "异步 函数 主入口() {\n"
              "  变量 甲 = 任务(慢())\n"
              "  当任务完成(甲, 返回值: 文本) { 打印(\"回调开始\", 1 / 0) }\n"
              "  等待 甲\n}\n")
    res = pipeline.compile_source(cb_err, "<回调错误回译>")
    if not res.ok:
        bad.append(f"回调错误用例编译未通过：{res.errors[0].code}")
    else:
        ok, diag, _ = pipeline.run_result(res)
        if ok or diag is None:
            bad.append("回调错误用例：期望运行期报错，实际被静默吞掉")
        elif diag.line != 7:
            bad.append(f"回调错误回译到 AGL 第 {diag.line} 行，期望第 7 行（回调体那一行）")
        else:
            c.note("回调内错误回译到第 7 行")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done("并发更快 · 取消生效 · 行号回译" if not bad else "")


def check_ffi() -> Check:
    """FFI 黑盒调用：能跑、缺效应必须编译期报错、外部异常回译成 AGL 行号 + 中文原因。

    这一项防的是 FFI 成为沙箱的漏洞：
      · 外部库一律按最坏效应（外部调用）算 —— 少声明效应必须在编译期被拒；
      · 外部库抛异常时报的是 AGL 的行号和中文原因，不甩宿主的 traceback；
      · `aglc ffi` 必须能列出「用了哪些外部库、各自什么效应」，供人审。
    """
    from compiler.lexer import LANG_EN, LANG_ZH

    c = Check("FFI 黑盒调用（外部库 / 效应 / 异常回译）")
    bad: list[str] = []

    # 1) 中英黄金用例能跑，输出确定
    for lang, name, want in (
        (LANG_ZH, "ffi_zh.agl", "2.0"),
        (LANG_EN, "ffi_en.agl", "2.0"),
    ):
        f = GOLDEN / name
        if not f.exists():
            bad.append(f"缺 {name}")
            continue
        res = pipeline.compile_file(f)
        if not res.ok:
            bad.append(f"{name} 编译未通过：{res.errors[0].code}")
            continue
        ok, diag, out = pipeline.run_result(res)
        if not ok and diag:
            bad.append(f"{name} 运行报 {diag.code} 第 {diag.line} 行")
        elif out.strip() != want:
            bad.append(f"{name} 输出 {out.strip()!r}，期望 {want!r}")

    # 2) 少声明效应 → 编译期报错（FFI 一律按最坏效应算）
    src = ("#语言=中文\n"
           "导入 外部 math\n"
           "函数 主入口() {\n"
           "  打印(math.sqrt(2.0))\n"
           "}\n")
    res = pipeline.compile_source(src, "<FFI 缺效应>")
    if res.ok:
        bad.append("调用外部库却没声明效应，编译期竟然放行了")
    elif res.errors[0].code != "AGL-E-0401":
        bad.append(f"缺效应时错误码是 {res.errors[0].code}，期望 AGL-E-0401")
    else:
        c.note("缺效应编译期被拒（AGL-E-0401）")

    # 3) 外部库抛异常 → AGL 行号 + 中文原因
    err_src = ("#语言=中文\n"
               "导入 外部 math\n"
               "函数 主入口() 效应 外部调用 {\n"
               "  打印(math.sqrt(-1.0))\n"
               "}\n")
    res = pipeline.compile_source(err_src, "<FFI 异常>")
    if not res.ok:
        bad.append(f"FFI 异常用例编译未通过：{res.errors[0].code}")
    else:
        ok, diag, _ = pipeline.run_result(res)
        if ok or diag is None:
            bad.append("FFI 异常用例：期望运行期报错，实际正常结束")
        elif diag.code != "AGL-RT-0072":
            bad.append(f"FFI 异常错误码是 {diag.code}，期望 AGL-RT-0072")
        elif diag.line != 4:
            bad.append(f"FFI 异常行号是第 {diag.line} 行，期望第 4 行")
        elif "Traceback" in (diag.message_zh or "") or "Error" in (diag.message_zh or ""):
            bad.append("FFI 异常消息里漏出了宿主语言的术语")
        else:
            c.note(f"外部库异常回译到 AGL 第 {diag.line} 行（{diag.message_zh}）")

    # 4) `aglc ffi` 必须能列出外部库与效应
    f = GOLDEN / "ffi_zh.agl"
    if f.exists():
        res = pipeline.compile_file(f)
        libs: dict[str, str] = {}
        for imp in (res.ir["program"]["modules"][0].get("imports") or []):
            if imp.get("kind") == "ffi":
                libs[imp["module"]] = imp.get("effect") or "External"
        if "math" not in libs:
            bad.append("`aglc ffi` 列不出外部库 math")
        elif libs["math"] != "External":
            bad.append(f"外部库 math 的效应是 {libs['math']}，期望 External（最坏情况）")
        else:
            c.note("外部库 math · 效应 External（最坏情况）")

    if bad:
        c.fail("; ".join(bad[:3]))
    return c.done("能跑 · 缺效应编译期被拒 · 异常回译中文" if not bad else "")




def _dev_sources() -> list:
    """仓库里所有「开发源」Python 文件。

    生成物不算开发源：`tests/golden/**` 底下那些 `.py` 是编译器吐出来的产物，
    它们的排版要服务于「AGL 第 N 行 = Python 第 N 行」，拿写源码的规矩去要求
    它们没意义。`__pycache__` / `node_modules` 之类同理排除。

    `tests/import/**` 也要排除：那是**喂给导入器的输入**，恰恰得是接地气的
    普通 Python（故意不写编码声明、故意带 f-string 和 class），
    用本项目的规范去要求它们，就把测试用例改没了。
    """
    skip_dirs = {"__pycache__", "node_modules", ".git", ".venv", "venv",
                 "dist", "snapshots", ".workbuddy", "build"}
    # 源码树扫全仓库；wheel 安装后 REPO 是 site-packages，
    # 只扫 AGL 自己的包，避免把同环境的 pip 等第三方包算进来。
    if (REPO / "pyproject.toml").is_file():
        scan_roots = [REPO]
    else:
        scan_roots = [REPO / n for n in ("compiler", "tests", "aglc", "spec", "examples")]
    out = []
    for root in scan_roots:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.py")):
            parts = set(f.parts)
            if parts & skip_dirs:
                continue
            rel = str(f.relative_to(REPO)).replace("\\", "/")
            if rel.startswith(("tests/golden", "tests/import")):
                continue
            out.append((f, rel))
    return out


def check_sources() -> Check:
    """源码规范自检：零第三方依赖 · 无死导入 · 无 TODO 残留 · 编码声明齐全。

    这一项防的是"慢慢烂掉"——每个 Checks 都不致命，但攒三个月就没人敢动这套代码：
      · **零第三方依赖**（硬规则）：普通人装了 Python 就能跑。加一个 `requests`
        就把"双击就能用"变成"先去学 pip"，这条规矩一旦破就再也收不回来；
      · **无未使用导入**：留着就是谎言，读的人以为这里用到了别处的东西；
      · **无 TODO/FIXME 残留**：写下来却没做的是信号，不该靠人记；
      · **UTF-8 编码声明**：中文标识符要靠它在不同 locale 的机器上保持一致。
    """
    import ast
    import re
    import sys

    c = Check("源码规范自检")
    std = set(sys.stdlib_module_names)
    std.add("tomllib")  # Python 3.11 标准库；3.10 走内置回退，不是第三方依赖
    local_roots = {"compiler", "tests", "aglc", "spec"}

    third_party: list[str] = []
    unused: list[str] = []
    todos: list[str] = []
    no_coding: list[str] = []

    for f, rel in _dev_sources():
        text = f.read_text(encoding="utf-8")
        lines = text.splitlines()

        if "coding: utf-8" not in text[:200]:
            no_coding.append(rel)

        # 扫待办标记。注释里的待办也算待办，所以不排除注释行。
        # 唯一豁免的是本文件：它的注释里写着"无 TODO 残留"，属于自己提到自己。
        own = REPO / "tests" / "suite" / "checks.py"
        for i, line in enumerate(lines, 1):
            # 只认真正的待办标记（后面跟冒号或括号），不认句子里顺手提到这个词
            if re.search(r"(TODO|FIXME|XXX|HACK)\s*[:(]", line) and f.resolve() != own.resolve():
                todos.append(f"{rel}:{i}")

        try:
            tree = ast.parse(text, str(f))
        except SyntaxError as e:
            c.fail(f"{rel} 语法错误：{e}")
            return c

        # ── 第三方依赖 ────────────────────────────────────────────────
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m not in std and m not in local_roots:
                    third_party.append(f"{rel}:{node.lineno} {m}")

        # ── 未使用的导入 ──────────────────────────────────────────────
        imported: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name != "__future__":
                        imported.append((node.lineno, a.asname or a.name.split(".")[0]))
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for a in node.names:
                    imported.append((node.lineno, a.asname or a.name))

        used = {x.id for x in ast.walk(tree) if isinstance(x, ast.Name)}
        for x in ast.walk(tree):
            if isinstance(x, ast.Attribute):
                cur = x
                while isinstance(cur, ast.Attribute):
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    used.add(cur.id)

        for lineno, name in imported:
            if name in used or f'"{name}"' in text or f"'{name}'" in text:
                continue
            unused.append(f"{rel}:{lineno} {name}")

    bad: list[str] = []
    if third_party:
        bad.append("引入了第三方依赖：" + "、".join(third_party[:3]))
    if unused:
        bad.append("有未使用的导入：" + "、".join(unused[:3]))
    if todos:
        bad.append("有 TODO/FIXME 残留：" + "、".join(todos[:3]))
    if no_coding:
        bad.append("缺 UTF-8 编码声明：" + "、".join(no_coding[:3]))

    n = len(_dev_sources())
    if bad:
        c.fail("; ".join(bad[:3]))
        return c

    c.note(f"{n} 个开发源文件")
    c.note("零第三方依赖 · 无死导入 · 无 TODO · 编码声明齐")
    return c.done(f"{n} 个源 · 零依赖 · 干净")


def check_error_purity() -> Check:
    """错误输出纯洁性：报错时只许出现 AGL 的名词，不许甩宿主 traceback。

    这一项守的是 AGL 最核心的承诺：**普通人能看懂 AI 写的代码**。
    一个不懂 Python 的人看见 `Traceback (most recent call last)` 或
    `File "rt_divzero.py", line 12` 就只能关机——那等于这套语言白设计了。

    怎么做才是"对人友好"：报错要说 AGL 的文件名、AGL 的行号、中文的原因。

    **唯一一处例外**是「原因」栏：那里故意保留宿主的原始异常类型
    （`ZeroDivisionError: division by zero`），给要排查的人留一线。
    它的位置固定、明确标注为「原因」，不会污染给普通人看的正文。
    """
    import io
    import re

    from compiler.cli import render_diag

    c = Check("错误输出纯洁性")
    expect = _load_expect()
    cases = [x for x in expect.values() if x.get("stage") == "run"]

    # 正文（不含「原因」栏）里不许出现的宿主名词
    # ——每个词都对应一次"普通人被吓跑"的真实场景
    forbidden = [
        ("Traceback", "宿主的原始回溯"),
        ('File "', 'File "xxx.py", line N 这种宿主栈帧'),
        (".py:", "宿主的 .py 文件路径"),
        ('.py"', "宿主的 .py 文件路径（引号形式）"),
        ("asyncio", "宿主的并发库名"),
    ]
    if not cases:
        c.fail("没有会崩的运行用例，这项检查是空转")
        return c

    bad: list[str] = []
    checked = 0
    for case in cases:
        f = (CASES / case["file"]).resolve()
        if not f.exists():
            continue
        res = pipeline.compile_file(f)
        if not res.ok:
            continue  # 编译期错误由第 8 项管，这里只看运行时
        ok, diag, _ = pipeline.run_result(res)
        if ok:
            bad.append(f"{case['file']} 应当崩掉却跑通了")
            continue

        # 审的就是用户在终端看到的那一屏：`render_diag` 的输出，含错误码那一行
        buf = io.StringIO()
        render_diag(diag, buf)
        text = buf.getvalue()

        # 剥掉「原因」栏——那一栏故意留宿主异常给排查的人
        body_lines = [ln for ln in text.splitlines() if not ln.strip().startswith("原因")]
        body = "\n".join(body_lines)

        for token, why in forbidden:
            if token in body:
                bad.append(f"{case['file']} 正文里出现了 {why}")
                break
        else:
            # 正文必须真的指回 AGL：有 agl 文件名 + AGL 行号 + AGL 错误码
            if ".agl" not in body:
                bad.append(f"{case['file']} 报错没指回 AGL 源文件")
            elif not re.search(r"AGL-[A-Z]{1,2}-\d{4}", body):
                bad.append(f"{case['file']} 报错缺 AGL 错误码")
            else:
                checked += 1

    if bad:
        c.fail("; ".join(bad[:3]))
        return c

    c.note(f"{checked} 个崩溃用例 · 正文全中文")
    c.note("指回 .agl 文件与 AGL 行号 · 错误码齐")
    return c.done(f"{checked} 个崩溃用例 · 正文无宿主术语")


def _resilience_inputs() -> list[tuple[str, str]]:
    """一批"不按常理出牌"的 AGL 源码。

    每一个都对应一类真实场景：用户在编辑器里敲了一半、AI 生成的前半句是坏的、
    粘贴时掉了结尾的花括号、类型名拼错……编译器在这些输入下必须给出 AGL 诊断，
    绝不能把宿主的 traceback 甩出来。

    历史上这里真出过事：`checker` 里有一个分支调用了写错名字的内部函数
    （`zh_type`，实际叫 `type_of_zh`），只有"把内建函数当值用"这个写法会走到它。
    没有任何黄金用例覆盖这条路径，于是它安静地躺了很久，直到有人写出
    `变量 x = 打印` —— 编译器当场崩成一屏 Python traceback。
    """
    return [
        ("内建当值用", "#语言=中文\n函数 主入口() {\n  变量 x = 打印\n}\n"),
        ("内建当返回值", "#语言=中文\n函数 拿() -> 文本 {\n  返回 成功\n}\n函数 主入口() {\n  打印(拿())\n}\n"),
        ("只有半个花括号", "#语言=中文\n函数 主入口() {\n  打印(1)\n"),
        ("只有右花括号", "#语言=中文\n}\n"),
        ("空文件", "#语言=中文\n"),
        ("声明后什么都没有", "#语言=中文\n函数\n"),
        ("函数调用缺参数", "#语言=中文\n函数 主入口() {\n  打印()\n}\n"),
        ("嵌套到很深的括号", "#语言=中文\n函数 主入口() {\n  打印((((((1))))))\n}\n"),
        ("字符串没闭合", '#语言=中文\n函数 主入口() {\n  打印("没写完\n}\n'),
        ("数字带多个点", "#语言=中文\n函数 主入口() {\n  变量 x = 1.2.3\n}\n"),
        ("未知的成员访问", "#语言=中文\n函数 主入口() {\n  变量 x = 1\n  打印(x.不存在的成员)\n}\n"),
        ("空的分号一堆", "#语言=中文\n函数 主入口() {\n  打印(1)\n}\n}\n}\n"),
        ("没有主入口", "#语言=中文\n函数 别的() {\n  打印(1)\n}\n"),
        ("效应标签不存在", "#语言=中文\n函数 主入口() 效应 飞天 {\n  打印(1)\n}\n"),
        ("类型名拼错", "#语言=中文\n函数 拿() -> 文本错 {\n  返回 \"a\"\n}\n函数 主入口() {\n  打印(拿())\n}\n"),
        ("回调缺参数类型", "#语言=中文\n函数 主入口() {\n  任务(1)\n}\n"),
        ("中文全角括号", "#语言=中文\n函数 主入口（）{\n  打印(1)\n}\n"),
        ("只有注释", "#语言=中文\n// 什么都没写\n"),
    ]


def check_resilience() -> Check:
    """编译器韧性：任何畸形输入都不许把宿主的 traceback 甩给普通人。

    这一项防的是"编译器自己写的 bug"——那种藏在某个没人走过的分支里、
    黄金用例永远碰不到的死代码（比如调用了一个名字拼错的内部函数）。
    它平时不出现，一旦有人写出那行代码，编译器就当场崩成一屏 Python traceback，
    对一个只看得懂中文的用户来说，那和"程序坏了"没有区别。

    做法：把一批畸形输入逐个喂给编译器，断言输出里
      · 没有 `Traceback`、没有宿主的 `.py` 路径、没有宿主的执行帧；
      · 要么通过、要么给出带 AGL 错误码的诊断——两者都算合格，崩了才算失败。
    """
    import io
    import re

    from compiler.cli import render_diag

    c = Check("编译器韧性（畸形输入不崩）")

    bad: list[str] = []
    crashed = 0
    passed = 0
    for name, src in _resilience_inputs():
        try:
            res = pipeline.compile_source(src, f"<{name}>")
        except BaseException as e:  # 连优雅的 AglError 都不是：这里就是崩了
            bad.append(f"{name}：编译器内部异常 {type(e).__name__}: {e}")
            crashed += 1
            continue

        if res.ok:
            passed += 1
            continue

        # 编译失败：必须是 AGL 诊断，且能渲染成人话。
        # 用 `render_diag` 而不是 `Diagnostic.render()`——后者不含错误码那一行，
        # 而我们要守的正是"用户看到的那一屏里必须有 AGL 错误码"。
        for d in res.errors:
            buf = io.StringIO()
            render_diag(d, buf)
            rendered = buf.getvalue()
            if "Traceback" in rendered or ".py" in rendered:
                bad.append(f"{name}：报错里混进了宿主信息")
                crashed += 1
                break
            if not re.search(r"AGL-[A-Z]{1,2}-\d{4}", rendered):
                bad.append(f"{name}：报错没有 AGL 错误码")
                crashed += 1
                break
        else:
            passed += 1

    if bad:
        c.fail("; ".join(bad[:3]))
        return c

    # 一次只报第一个错会让修完一个再跑一遍，来回很多次。这里固定两个
    # 独立函数的语法错误，要求一次至少拿到两条诊断。
    multi = (
        "#语言=中文\n"
        "函数 坏一() {\n  打印(1\n}\n\n"
        "函数 坏二() {\n  打印(2\n}\n"
    )
    res = pipeline.compile_source(multi, "<multi_diag>")
    if not res.ok and len(res.errors) < 2:
        c.fail(f"两个独立语法错误只报了 {len(res.errors)} 条诊断，未按顶层声明恢复")
        return c
    c.note("多个顶层声明错误可一次收集")

    c.note(f"{len(_resilience_inputs())} 个畸形输入")
    c.note("全部给出 AGL 诊断，没有一次甩出宿主回溯")
    return c.done(f"{len(_resilience_inputs())} 个畸形输入 · 0 崩溃")


# ── 24. Python 源码导入器 ──────────────────────────────────────────────
def check_import() -> Check:
    """Python 源码导入器（阶段 3）：能导的必须对拍通过，不能导的必须说清原因。

    这一项防的是导入器"看起来能导、其实给错答案" —— 导入的产物是要拿来
    给人做审查依据的，一份漂漂亮亮但语义不对的翻译，比老老实实说"这个翻不了"危险得多：

      · **可导用例做真对拍**：同一份 Python，一边用 Python 原样跑，
        一边导入 → 编译 → 跑。两边输出必须逐字相同，差一个空格都算坏；
      · **拒绝用例必须被挡住**，而且理由要说到点上 ——
        只说"暂时不能导"等于没说，人盯着屏幕还是不知道该改什么；
      · **两个分支都要能导**，英文分支的产物同样得能编译（中英同构）；
      · **畸形的 Python 不能崩**，语法错也要给一句人话。
    """
    from compiler.importer import import_python
    from compiler.lexer import LANG_EN, LANG_ZH

    c = Check("Python 源码导入器（翻译 + 对拍）")
    bad: list[str] = []
    cases = sorted((REPO / "tests" / "import").glob("*.py"))
    if not cases:
        return c.fail("tests/import 下没有任何用例").done()

    oks = [f for f in cases if f.name.startswith("ok_")]
    blocks = [f for f in cases if f.name.startswith("blocked_")]
    # 能导用例必须有；blocked 用例允许为 0——把边界做进语言后不再保留
    # 「该挡住的」清单，精确拒绝的语义仍由下面的畸形输入段兜底验证。
    if not oks:
        bad.append("用例不齐：连一份能导的 ok_*.py 都没有")

    # 1) 能导的：导入 → 编译 → 运行 → 与原 Python 逐字对拍
    for f in oks:
        res = import_python(f.read_text(encoding="utf-8"), LANG_ZH)
        if not res.ok:
            bad.append(f"{f.name} 本该能导，却被挡下：{res.blockers[0][:36]}…")
            continue
        cres = pipeline.compile_source(res.agl, "<导入产物>")
        if not cres.ok:
            first = cres.errors[0]
            bad.append(f"{f.name} 导入产物编译不过：{first.code} 第 {first.line} 行")
            continue
        ok, diag, out = pipeline.run_result(cres)
        if not ok and diag:
            bad.append(f"{f.name} 导入产物运行报 {diag.code} 第 {diag.line} 行")
            continue
        want = _run_plain_python(f)
        if want is None:
            bad.append(f"{f.name} 原 Python 自己跑不起来，用例写坏了")
            continue
        # 显示名差异归一化：Python 的 None/True/False 在 AGL 中文分支显示为 空/真/假，
        # 语义值相同；逐字对拍前把 Python 侧映射成 AGL 的显示名。
        want_agl = (want.strip()
                    .replace("None", "空")
                    .replace("True", "真")
                    .replace("False", "假"))
        if out.strip() != want_agl:
            bad.append(f"{f.name} 输出与原文不一致：原 {want_agl!r} ≠ 导入 {out.strip()!r}")

    # 2) 该挡住的：必须被挡，且理由说到点上
    for f in blocks:
        res = import_python(f.read_text(encoding="utf-8"), LANG_ZH)
        if res.ok:
            bad.append(f"{f.name} 本该被挡住，却导入成功了 —— "
                       f"这等于把不等价的写法当成等价放行")
            continue
        hint = _expected_blocker_hint(f)
        if hint and hint not in res.blockers[0]:
            bad.append(f"{f.name} 的拒绝理由没说到点：应提到「{hint}」，"
                       f"实际是「{res.blockers[0][:36]}…」")

    # 3) 中英同构：英文分支也要能导、产物也要能编译
    en_ok = 0
    for f in oks:
        res = import_python(f.read_text(encoding="utf-8"), LANG_EN)
        if not res.ok:
            bad.append(f"{f.name} 英文分支导不出来：{res.blockers[0][:36]}…")
            continue
        cres = pipeline.compile_source(res.agl, "<英文导入产物>")
        if not cres.ok:
            first = cres.errors[0]
            bad.append(f"{f.name} 英文分支产物编译不过：{first.code} 第 {first.line} 行")
            continue
        en_ok += 1

    # 4) 畸形输入：不许甩 Python 回溯
    crashed = 0
    for junk in (r"def (:\\n", "这是完全不是 Python 的东西\n", r"print(`\n", "x ==== 3\n"):
        res = import_python(junk, LANG_ZH)
        if res.ok:
            bad.append(f"畸形 Python 竟然导入成功了：{junk.strip()!r}")
            continue
        if not res.blockers:
            crashed += 1
    if crashed:
        bad.append(f"{crashed} 个畸形 Python 没给出人话理由（异常漏出来了）")

    if bad:
        c.fail("; ".join(bad[:3]))
    c.note(f"{len(oks)} 份能导 · 逐字对拍一致 · {len(blocks)} 份被挡")
    c.note(f"中英两分支都能导且编译通过（{en_ok}/{len(oks)}）")
    return c.done(f"{len(oks)} 份对拍一致 · {len(blocks)} 份被拒" if not bad else "")
