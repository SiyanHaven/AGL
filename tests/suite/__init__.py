# -*- coding: utf-8 -*-
"""一致性测试套件（阶段 2 / S2-G）。

**这套套件就是「验收标准」的可执行形式。**

拆出来的理由：以前这些检查全堆在 `compiler/cli/doctor.py` 一个文件里，
加特性时越来越难找到该往哪加。现在：

  · `tests/suite/checks.py` —— 检查怎么写（每条检查对应一条写下来的标准）；
  · `tests/suite/__init__.py` —— 本文件：跑哪些检查、怎么出报告；
  · `compiler/cli/doctor.py` —— 只剩壳：调套件 + 渲染 + 刷新期望产物。

单独跑（不经过命令行）：

    python -m tests.suite            # 人读的报告
    python -m tests.suite --json     # 结构化输出，给 AI 自循环修复用

退出码：全部通过 0，有失败 1。
"""
from __future__ import annotations

import json

from .checks import (
    Check,
    _load_expect,
    check_artifacts,
    check_agb_parity,
    check_agb_artifacts,
    check_vm_sandbox,
    check_async,
    check_builtin,
    check_card,
    check_dual_branch,
    check_error_codes,
    check_error_purity,
    check_ffi,
    check_golden_compile,
    check_golden_run,
    check_grammar,
    check_import,
    check_keywords,
    check_line_alignment,
    check_lsp,
    check_mcp,
    check_modules,
    check_optimize,
    check_resilience,
    check_roundtrip,
    check_schema,
    check_security_defaults,
    check_sources,
    check_spec_files,
    check_stdlib,
)

__all__ = ["Check", "all_checks", "run_suite"]


def all_checks() -> list[Check]:
    """跑完全部检查，按「验收标准」的编号顺序返回。

    顺序就是标准的编号顺序：第 1 项在第 2 项前面，改标准的人一眼能找到对应位置。
    """
    expect = _load_expect()
    return [
        check_spec_files(),
        check_keywords(),
        check_schema(),
        check_golden_compile(expect),
        check_golden_run(expect),
        check_dual_branch(),
        check_roundtrip(),
        check_error_codes(expect),
        check_line_alignment(),
        check_artifacts(),
        check_modules(),
        check_card(),
        check_mcp(),
        check_security_defaults(),
        check_stdlib(),
        check_optimize(),
        check_builtin(),
        check_grammar(),
        check_lsp(),
        check_async(),
        check_ffi(),
        check_sources(),
        check_error_purity(),
        check_resilience(),
        check_import(),
        check_agb_parity(),
        check_agb_artifacts(),
        check_vm_sandbox(),
    ]


def run_suite(verbose: bool = True, as_json: bool = False) -> int:
    """跑套件并打印报告。返回 0 表示全绿。"""
    checks = all_checks()
    passed = sum(1 for c in checks if c.ok)

    if as_json:
        print(json.dumps({
            "passed": passed,
            "total": len(checks),
            "ok": passed == len(checks),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                        "notes": c.notes} for c in checks],
        }, ensure_ascii=False, indent=2))
        return 0 if passed == len(checks) else 1

    print("AGL 体检报告")
    print("═" * 52)
    for c in checks:
        icon = "✓" if c.ok else "✗"
        print(f"  {icon}  {c.name:<22} {c.detail}")
        if verbose:
            for n in c.notes:
                print(f"       · {n}")
    print("═" * 52)
    if passed == len(checks):
        print(f"结论：{len(checks)} 项全部通过")
    else:
        print(f"结论：{len(checks)} 项中 {passed} 项通过，{len(checks) - passed} 项失败")
    return 0 if passed == len(checks) else 1
