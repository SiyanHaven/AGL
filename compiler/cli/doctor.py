# -*- coding: utf-8 -*-
"""一键体检（`aglc doctor`）。

**这个文件现在只是壳**：检查的实现全在 `tests/suite/` 里（阶段 2 / S2-G）。
它只负责三件事：调套件、把结果渲染成人能读的报告、刷新期望产物。

为什么要拆开：检查全堆在一个文件里时，加特性时越来越难找到该往哪加，
改一处还得担心别处。拆成套件之后，每个工作包都能自己带测试进来，
`doctor` 只负责调用 —— 检查本身不再依赖命令行。

单独跑套件（不经过本文件）：`python -m tests.suite`
"""
from __future__ import annotations

from pathlib import Path

from compiler.cli import pipeline

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden"


def _run_golden(f: Path) -> tuple[str, str] | None:
    """编译并运行一份黄金用例；要求先过 VM/Python 双后端对拍。

    返回 (Python 后端输出, VM 输出)；VM 编不出 AGB 时 VM 输出记 None 占位。
    Python 后端自身跑挂则返回 None —— 期望不该跟着一个跑挂的程序刷新。
    """
    from compiler import bytecode, vm

    res = pipeline.compile_file(f)
    if not res.ok:
        return None
    ok, diag, out = pipeline.run_result(res)
    if not ok and diag:
        return None
    try:
        agb = bytecode.compile_program(res.ir)
        mods = {}
        for dep in (res.deps or {}).values():
            dep_agb = bytecode.compile_program(dep.ir)
            mods[dep_agb["module"]] = dep_agb
        vm_out: str | None = vm.run(agb, res.lang, mods)
    except (bytecode.AgbUnsupported, vm.VmError):
        vm_out = None
    return out, vm_out if vm_out is not None else out


def _refresh_expect_stdout() -> int:
    """刷新 expect.toml 里 `../golden/*` 用例的 expect_stdout。

    只处理「运行输出」类期望：编译期 / 运行期错误码用例（没有 expect_stdout
    字段）不动。刷新前逐份做双后端对拍 —— VM 与 Python 输出不一致时拒绝刷新
    并点名，防止把一个真 bug 固化进期望文件。
    """
    expect_path = REPO / "tests" / "cases" / "expect.toml"
    text = expect_path.read_text(encoding="utf-8")
    chunks = text.split("[[case]]")
    patched = skipped = 0
    for idx in range(1, len(chunks)):
        chunk = chunks[idx]
        marker = 'file = "../golden/'
        pos = chunk.find(marker)
        if pos < 0:
            continue
        name = chunk[pos + len(marker):].split('"', 1)[0]
        f = GOLDEN / name
        if not f.exists():
            continue
        ran = _run_golden(f)
        if ran is None:
            print(f"✗ {name}: 编译或运行失败，expect_stdout 不刷新")
            skipped += 1
            continue
        out, vm_out = ran
        # 与 check_agb_parity 同一口径：末尾换行不参与对拍，但写盘用 Python 后端原文。
        if out.strip() != vm_out.strip():
            print(f"✗ {name}: VM 与 Python 后端输出不一致，expect_stdout 不刷新")
            skipped += 1
            continue
        # 多行字面量引号：输出里出现 ''' 就换 """，两者都出现就放弃这一条。
        if "'''" not in out:
            open_q = close_q = "'''"
        elif '"""' not in out:
            open_q = close_q = '"""'
        else:
            print(f"✗ {name}: 输出同时含 ''' 与 \"\"\"，无法写成 TOML 字面量，跳过")
            skipped += 1
            continue
        start = chunk.find("expect_stdout")
        if start < 0:
            continue
        # 先认出旧字面量用的引号，闭引号必须按旧引号找 —— 新引号（out 里没有
        # 那个序列时可以换）与旧引号可以不同，拿新引号找闭口永远找不到。
        old_open = chunk.find("= '''", start)
        if old_open >= 0:
            old_q = "'''"
        else:
            old_open = chunk.find('= """', start)
            if old_open < 0:
                continue
            old_q = '"""'
        q_open = old_open
        body_start = chunk.index("\n", q_open) + 1
        close = chunk.find(old_q, body_start)
        if close < 0:
            continue
        # 重发整个字面量：自己的开引号 + 内容 + 自己的闭引号，旧闭引号跳过
        # （2026-11-12 教训：沿用 chunk[close:] 会把旧闭引号留在新字面量外面，
        # 开 ''' 闭 """ 的错配直接把整个 expect.toml 写坏）。
        tail = chunk[close + len(old_q):]
        if out.endswith("\n"):
            new_block = chunk[:q_open] + "= " + open_q + "\n" + out + open_q + tail
        else:
            new_block = chunk[:q_open] + "= " + open_q + "\n" + out + "\n" + open_q + tail
        chunks[idx] = new_block
        patched += 1
    expect_path.write_text("[[case]]".join(chunks), encoding="utf-8")
    print(f"✓ expect.toml expect_stdout 刷新 {patched} 条" + (f"，跳过 {skipped} 条" if skipped else ""))
    return 0


def update_golden() -> int:
    """刷新期望产物。只在**确认改动是有意的**之后运行。

    刷两样东西：
    1. tests/golden 下的期望 IR（.agl.ir）与期望 Python（.py）；
    2. tests/cases/expect.toml 里 `../golden/*` 用例的 expect_stdout ——
       之前只有手改一条路，语言加了新内建、打印口径调整后这里必然漂移
       （2026-11-12 的 7 份黄金输出差异就是这么来的）。
    刷新前逐份过双后端对拍，两个后端说不一致就拒绝写盘。
    """
    from compiler.ir import dumps

    n = 0
    for f in sorted(GOLDEN.rglob("*.agl")):
        res = pipeline.compile_file(f)
        if not res.ok:
            print(f"✗ {f.name}: 编译未通过，跳过")
            continue
        f.with_name(f.name + ".ir").write_text(dumps(res.ir), encoding="utf-8")
        f.with_suffix(".py").write_text(res.python, encoding="utf-8")
        n += 1
    print(f"✓ 已刷新 {n} 组期望产物")
    _refresh_expect_stdout()
    return 0


# ── 主流程 ────────────────────────────────────────────────────────────
def _runtime_doctor(verbose: bool, as_json: bool) -> int:
    """用户包自检：不依赖 tests/、editor/、自举源码。

    用户包只带预编译自举器和运行时数据，所以这里只验证“装完能不能用”：
    规格/关键字、标准库、自举器、真实编译运行、MCP 元数据。
    """
    import json
    import subprocess
    import sys
    import tempfile

    checks = []

    def add(name: str, ok: bool, detail: str, notes: list[str] | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "notes": notes or []})

    # 1. 规格与关键字
    keyword_version = "unknown"
    try:
        from compiler.lexer import KeywordTable

        table = KeywordTable.load()
        keyword_version = table.version
        add("规格与关键字", True, f"v{table.version} · {len(table.keywords)} 关键字")
    except Exception as e:  # noqa: BLE001 - 自检必须把异常变成报告
        add("规格与关键字", False, f"读取失败：{e}")

    # 2. 标准库
    stdlib = REPO / "stdlib"
    n_stdlib = len(list(stdlib.glob("*.agl"))) if stdlib.is_dir() else 0
    add("标准库", n_stdlib > 0, f"{n_stdlib} 个模块" if n_stdlib else "stdlib/ 缺失")

    # 3. 自举编译器（用户包是复制内置 .agb，不跑 40 秒自举链）
    from compiler.cli.bootstrap import (
        INSTALL_TARGET, compile_with_selfhost, install_bootstrap,
    )
    boot = install_bootstrap(verbose=False) if not INSTALL_TARGET.exists() else None
    if boot is not None and not boot.ok:
        add("自举编译器", False, "；".join(boot.errors[:2]))
    elif INSTALL_TARGET.exists():
        add("自举编译器", True,
            f"{INSTALL_TARGET.stat().st_size:,} B · {INSTALL_TARGET}")
    else:
        add("自举编译器", False, "安装目录里没有自举编译器")

    # 4. 真实编译 + VM 运行
    if INSTALL_TARGET.exists():
        td = Path(tempfile.mkdtemp(prefix="agl-runtime-doctor-"))
        try:
            src = td / "runtime_check.agl"
            src.write_text(
                '#语言=中文\n函数 主入口() {\n  打印("AGL-RUNTIME-OK")\n}\n',
                encoding="utf-8",
            )
            agb, _ = compile_with_selfhost(src, td, want_ir=False)
            r = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "compiler.cli", "run",
                 str(agb), "--allow-effects", "IO"],
                cwd=str(td), capture_output=True, text=True, encoding="utf-8",
                timeout=120,
            )
            out = (r.stdout or "").strip()
            add("编译运行", r.returncode == 0 and out == "AGL-RUNTIME-OK",
                f"输出 {out!r}" if out else "没有输出")
        except Exception as e:  # noqa: BLE001
            add("编译运行", False, str(e)[:200])
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)
    else:
        add("编译运行", False, "自举编译器不可用")

    # 5. MCP 元数据
    try:
        from compiler.mcp import PROTOCOL_VERSION, SERVER_VERSION, TOOLS

        ok = len(TOOLS) == 10 and SERVER_VERSION == keyword_version
        add("MCP 工具服务器", ok,
            f"{SERVER_VERSION} · {PROTOCOL_VERSION} · {len(TOOLS)} 个工具")
    except Exception as e:  # noqa: BLE001
        add("MCP 工具服务器", False, str(e)[:200])

    passed = sum(1 for c in checks if c["ok"])
    if as_json:
        print(json.dumps({
            "passed": passed,
            "total": len(checks),
            "ok": passed == len(checks),
            "checks": checks,
            "mode": "runtime",
        }, ensure_ascii=False, indent=2))
        return 0 if passed == len(checks) else 1

    print("AGL 运行时自检")
    print("═" * 52)
    for c in checks:
        print(f"  {'✓' if c['ok'] else '✗'}  {c['name']:<18} {c['detail']}")
        if verbose:
            for note in c["notes"]:
                print(f"       · {note}")
    print("═" * 52)
    if passed == len(checks):
        print(f"结论：{len(checks)} 项全部通过（运行时包）")
    else:
        print(f"结论：{len(checks)} 项中 {passed} 项通过，{len(checks) - passed} 项失败")
    return 0 if passed == len(checks) else 1


def run(verbose: bool = True, as_json: bool = False) -> int:
    """跑一致性测试套件并渲染报告。检查本身在 `tests/suite/` 里。"""
    import sys

    # 用户 wheel 不含 tests/；只做运行时自检，避免把开发套件伪装成安装成功。
    if not (REPO / "tests" / "suite" / "checks.py").is_file():
        from compiler.cli.bootstrap import INSTALL_TARGET, install_bootstrap

        if not INSTALL_TARGET.exists():
            boot = install_bootstrap(verbose=False)
            if not boot.ok:
                print("✗ 内置自举编译器安装失败：" + "; ".join(boot.errors[:2]))
                return 1
        return _runtime_doctor(verbose=verbose, as_json=as_json)

    # 套件住在仓库的 tests/ 下，不在编译器包里。MCP 服务器、编辑器插件
    # 都可能从别的目录调进来，所以这里自己把仓库根塞进搜索路径 ——
    # 以前 doctor.py 用 `__file__` 定位，不受当前目录影响，这个性质不能丢。
    root = str(REPO)
    if root not in sys.path:
        sys.path.insert(0, root)

    # 全新发布包没有 build/：先安装自举编译器，再跑体检。
    # 否则「AGB 产物与默认 VM」会在安装之前调 build，直接报“自举编译器尚未安装”；
    # 快照黄金用例也会因为 build/ 目录不存在而失败。安装同时创建 build/ 目录。
    from compiler.cli.bootstrap import INSTALL_TARGET, install_bootstrap

    if not INSTALL_TARGET.exists():
        if verbose:
            print("首次体检：未找到自举编译器，先执行一次不动点安装（约 40 秒）……")
        boot = install_bootstrap(verbose=False)
        if not boot.ok:
            print("✗ 自举安装失败：" + "; ".join(boot.errors[:2]))
            return 1

    from tests.suite import run_suite

    return run_suite(verbose=verbose, as_json=as_json)
