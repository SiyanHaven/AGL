# -*- coding: utf-8 -*-
"""示例技能入口：读 stdin 的 JSON 请求，把两个参数相加后以 JSON 返回。

只依赖标准库，用来验证 AGL `协议 = "local-skill"` 的完整链路：
子进程、stdin 请求、stdout 结果、退出码。
"""
import json
import sys


def main() -> int:
    try:
        raw = sys.stdin.read()
        req = json.loads(raw or "{}")
    except Exception as exc:  # noqa: BLE001 - 入口层必须把任何解析失败变成可读原因
        sys.stderr.write(f"请求不是合法 JSON：{exc}\n")
        return 2
    args = req.get("arguments") or {}
    try:
        a = int(args.get("甲", 0))
        b = int(args.get("乙", 0))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"参数不是整数：{exc}\n")
        return 2
    print(json.dumps({
        "和": a + b,
        "工具": req.get("tool"),
        "技能目录": req.get("config", {}).get("入口") or "run.py",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
