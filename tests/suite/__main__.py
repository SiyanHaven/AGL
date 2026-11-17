# -*- coding: utf-8 -*-
"""独立运行一致性测试套件：

    python -m tests.suite
    python -m tests.suite --json

这是 `aglc doctor` 背后的那一套检查。拆出来是为了让"跑测试"这件事
不依赖命令行、不依赖 doctor 这个壳 —— CI、编辑器插件、AI 自循环
都可以直接调它。

退出码：全绿 0，有失败 1。
"""
from __future__ import annotations

import sys

from . import run_suite


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args or "--诊断" in args
    verbose = "--quiet" not in args and "--安静" not in args
    return run_suite(verbose=verbose, as_json=as_json)


if __name__ == "__main__":
    sys.exit(main())
