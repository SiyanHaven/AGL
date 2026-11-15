# -*- coding: utf-8 -*-
"""Run every published example end to end.

For each example this script:
  1. runs `aglc check`;
  2. builds `.agb` + `.agl.ir` into a temporary directory with the default compiler;
  3. runs the `.agb` on the default VM;
  4. compares stdout with the expected output.

Run from the extracted release root:

    python examples/run_examples.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXAMPLES = [
    (
        "examples/zh/01_销售日报.agl",
        """销售日报
总订单： 5  单；已完成： 3  单
完成金额： 414.40  元
完成率： 60.0 %

单号       客户        金额      状态
SO-1001 青荷书店 128.50 已完成
SO-1002 远山咖啡 86.00 已完成
SO-1003 南风文具 42.00 已取消
SO-1004 青荷书店 199.90 已完成
SO-1005 晨光设计 310.00 待付款""",
    ),
    (
        "examples/zh/02_待办清单.agl",
        """待办清单
[x] 写发布说明
[ ] 检查 MCP 工具清单
[ ] 跑一遍 doctor
已完成 1/3

更新后：
待办清单
[x] 写发布说明
[x] 检查 MCP 工具清单
[ ] 跑一遍 doctor
[ ] 整理示例索引
已完成 2/4""",
    ),
    (
        "examples/zh/03_日志分析.agl",
        """日志分析
文件：sample.log
总行数： 8
INFO： 4   WARN： 2   ERROR： 2
错误率： 25.0 %

错误明细：
   2026-11-15 08:01:13 ERROR database timeout after 5000ms
   2026-11-15 08:01:20 ERROR payment callback failed""",
    ),
    (
        "examples/en/01_sales_report.agl",
        """Sales Daily Report
Total orders: 5  Completed: 3
Completed amount: 414.40  CNY
Completion rate: 60.0 %

Order ID   Customer            Amount    Status
SO-1001 Qinghe Books 128.50 completed
SO-1002 Yuanshan Coffee 86.00 completed
SO-1003 Nanfeng Stationery 42.00 cancelled
SO-1004 Qinghe Books 199.90 completed
SO-1005 Chenguang Design 310.00 pending""",
    ),
    (
        "examples/en/02_todo_list.agl",
        """Todo List
[x] Write release notes
[ ] Check MCP tool list
[ ] Run doctor
Completed 1/3

After update:
Todo List
[x] Write release notes
[x] Check MCP tool list
[ ] Run doctor
[ ] Update example index
Completed 2/4""",
    ),
    (
        "examples/en/03_log_analyzer.agl",
        """Log Analysis
File: sample.log
Lines: 8
INFO: 4  WARN: 2  ERROR: 2
Error rate: 25.0 %

Errors:
   2026-11-15 08:01:13 ERROR database timeout after 5000ms
   2026-11-15 08:01:20 ERROR payment callback failed""",
    ),
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
    from compiler.cli.bootstrap import BUILTIN_AGB, INSTALL_TARGET, PIPELINE_SRC, install_bootstrap

    if INSTALL_TARGET.exists():
        return True
    if BUILTIN_AGB.is_file() and not PIPELINE_SRC.is_file():
        print("使用内置自举编译器……")
    else:
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
    with tempfile.TemporaryDirectory(prefix="agl_examples_") as temp:
        out_dir = Path(temp)
        for relative, expected in EXAMPLES:
            path = ROOT / relative
            check = run(["check", str(path)])
            if check.returncode != 0:
                failures.append(f"{relative}: check failed\n{check.stdout}{check.stderr}")
                continue

            build = run(["build", str(path), "--out", str(out_dir)])
            if build.returncode != 0:
                failures.append(f"{relative}: build failed\n{build.stdout}{build.stderr}")
                continue

            agb = out_dir / (path.stem + ".agb")
            if not agb.exists():
                failures.append(f"{relative}: build did not produce {agb.name}")
                continue

            result = run(["run", str(agb)])
            if result.returncode != 0:
                failures.append(f"{relative}: vm run failed\n{result.stdout}{result.stderr}")
                continue
            actual = result.stdout.strip()
            if actual != expected.strip():
                failures.append(
                    f"{relative}: output mismatch\n--- expected ---\n{expected}\n"
                    f"--- actual ---\n{actual}"
                )

    if failures:
        print("EXAMPLES FAILED")
        for failure in failures:
            print(failure)
        return 1
    print(f"EXAMPLES OK: {len(EXAMPLES)}/{len(EXAMPLES)} (check + build .agb + VM run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
