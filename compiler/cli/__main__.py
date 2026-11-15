# -*- coding: utf-8 -*-
"""允许 `python -m compiler.cli` 直接运行。"""
import sys

from compiler.cli import main

if __name__ == "__main__":
    sys.exit(main())
