# -*- coding: utf-8 -*-
"""失败技能入口：总是以非零退出码结束，用于验证确定性失败不重试、以及熔断。"""
import sys

sys.stdin.read()
sys.stderr.write("这个技能故意失败：上游数据源不可用\n")
sys.exit(3)
