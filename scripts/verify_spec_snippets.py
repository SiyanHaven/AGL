# -*- coding: utf-8 -*-
"""Verify every AGL code block that ships with the specification.

The specification is normative, so its examples must not rot. This script runs each
snippet through the real compiler and compares its output with the expected text.
Run from the repository root:

    python scripts/verify_spec_snippets.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SNIPPETS = [
    ("examples/zh/00_最小程序.agl", "有数据"),
    ("docs/spec-snippets/zh/01_函数.agl", "5"),
    ("docs/spec-snippets/zh/02_变量.agl", "agl-api 0"),
    ("docs/spec-snippets/zh/03_效应与校验.agl", "371"),
    ("docs/spec-snippets/zh/04_接口.agl", "成功(已付)"),
    ("docs/spec-snippets/zh/05_模块/主程序.agl", "80.0"),
    ("docs/spec-snippets/en/00_minimal.agl", "has data"),
    ("docs/spec-snippets/en/01_functions.agl", "5"),
    ("docs/spec-snippets/en/02_variables.agl", "agl-api 0"),
    ("docs/spec-snippets/en/03_effects.agl", "371"),
    ("docs/spec-snippets/en/04_interface.agl", "Ok(paid)"),
    ("docs/spec-snippets/en/05_module/main.agl", "80.0"),
]


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "compiler.cli", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def ensure_bootstrap() -> bool:
    """A fresh release archive has no build/; install the self-hosted compiler once."""
    from compiler.cli.bootstrap import INSTALL_TARGET, install_bootstrap

    if INSTALL_TARGET.exists():
        return True
    print("未找到自举编译器，先执行一次不动点安装（约 40 秒）……")
    result = install_bootstrap(verbose=False)
    if not result.ok:
        print("自举安装失败：" + "; ".join(result.errors[:2]))
        return False
    return True


def main() -> int:
    if not ensure_bootstrap():
        return 1
    failures: list[str] = []
    for relative, expected in SNIPPETS:
        path = ROOT / relative
        check = run(["check", str(path)])
        if check.returncode != 0:
            failures.append(f"{relative}: check failed\n{check.stdout}{check.stderr}")
            continue
        result = run(["run", str(path)])
        if result.returncode != 0:
            failures.append(f"{relative}: run failed\n{result.stdout}{result.stderr}")
            continue
        actual = result.stdout.strip()
        if actual != expected:
            failures.append(f"{relative}: expected {expected!r}, got {actual!r}")

    if failures:
        print("SPEC SNIPPETS FAILED")
        for failure in failures:
            print(failure)
        return 1
    print(f"SPEC SNIPPETS OK: {len(SNIPPETS)}/{len(SNIPPETS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
