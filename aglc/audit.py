#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aglc audit —— 从已有的 Python 代码中提取 AGL 契约草案。

定位：AGL 不是替代 Python，而是压在 Python 之上的「契约层」。
AI 照常写 Python（它最擅长），aglc audit 把「它到底会做什么」扫出来，
翻译成人能看懂的中文，再生成一份可审阅、可冻结、可校验偏差的 AGL 契约。

用法：
    python aglc/audit.py <目标.py 或 目录>
    python aglc/audit.py <目标> --json
"""
from __future__ import annotations

import ast
import json
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _w(s: str) -> int:
    """按终端显示宽度算字符宽度（中文占 2 列）"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _w(s))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ── 效应规则表 ────────────────────────────────────────────────────────────
# (匹配前缀, 效应类别, 中文动作名, 风险等级, 说明)
# 匹配规则：调用名以该前缀开头即命中（如 "requests.get" 命中 "requests."）
RULES: list[tuple[str, str, str, str, str]] = [
    # 网络
    ("requests.", "net", "发起网络请求", "中", "会连到外部服务器，发送或接收数据"),
    ("httpx.", "net", "发起网络请求", "中", "会连到外部服务器，发送或接收数据"),
    ("aiohttp.", "net", "发起网络请求", "中", "会连到外部服务器，发送或接收数据"),
    ("urllib.request.", "net", "发起网络请求", "中", "会连到外部服务器，发送或接收数据"),
    ("socket.", "net", "建立网络连接", "中", "会直接打开网络端口"),
    # 删除 / 覆盖文件
    ("os.remove", "fs_delete", "删除文件", "高", "文件被彻底删掉，不进回收站，无法恢复"),
    ("os.unlink", "fs_delete", "删除文件", "高", "文件被彻底删掉，不进回收站，无法恢复"),
    ("os.rmdir", "fs_delete", "删除目录", "高", "目录被彻底删除，无法恢复"),
    ("os.removedirs", "fs_delete", "递归删除目录", "高", "整棵目录树被删除，无法恢复"),
    ("shutil.rmtree", "fs_delete", "递归删除目录", "高", "整棵目录树被删除，无法恢复"),
    ("Path.unlink", "fs_delete", "删除文件", "高", "文件被彻底删掉，无法恢复"),
    # 改写文件
    ("shutil.move", "fs_write", "移动/重命名文件", "中", "文件位置会被改变"),
    ("shutil.copy", "fs_write", "复制文件", "低", "会产生新文件"),
    ("Path.write_text", "fs_write", "写入文件", "中", "会覆盖原文件内容"),
    ("Path.write_bytes", "fs_write", "写入文件", "中", "会覆盖原文件内容"),
    ("Path.mkdir", "fs_write", "创建目录", "低", "会在磁盘上新建目录"),
    ("os.rename", "fs_write", "重命名文件", "中", "文件位置会被改变"),
    ("os.replace", "fs_write", "覆盖文件", "中", "会替换掉目标文件"),
    # 读文件 / 遍历目录（读本身风险低，但绝不能被判成"纯计算"）
    ("Path.read_text", "fs_read", "读取文件", "低", "会读取文件内容"),
    ("Path.read_bytes", "fs_read", "读取文件", "低", "会读取文件内容"),
    ("Path.open", "fs_read", "读取文件", "低", "会读取文件内容"),
    ("Path.iterdir", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("Path.glob", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("Path.rglob", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("Path.exists", "fs_read", "检查文件是否存在", "低", "只是看一眼，不改动"),
    ("os.listdir", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("os.walk", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("os.path.", "fs_read", "检查路径", "低", "只是查看路径信息，不改动"),
    # 执行命令
    ("subprocess.", "exec", "执行系统命令", "高", "能在你的电脑上运行任意程序"),
    ("os.system", "exec", "执行系统命令", "高", "能在你的电脑上运行任意命令"),
    ("os.popen", "exec", "执行系统命令", "高", "能在你的电脑上运行任意命令"),
    ("os.exec", "exec", "执行系统命令", "高", "会替换当前进程去运行别的程序"),
    ("os.spawn", "exec", "执行系统命令", "高", "会启动新进程"),
    ("eval", "exec", "动态执行代码", "高", "把一段文本当代码跑，行为无法预先审查"),
    ("exec", "exec", "动态执行代码", "高", "把一段文本当代码跑，行为无法预先审查"),
    # 数据库
    ("sqlite3.connect", "db", "访问数据库", "中", "会读写信数据库"),
    ("psycopg2.connect", "db", "访问数据库", "中", "会读写外部数据库"),
    ("pymysql.connect", "db", "访问数据库", "中", "会读写外部数据库"),
    # 模型调用
    ("openai.", "model", "调用大模型", "中", "会把内容发给外部模型服务，可能产生费用"),
    ("anthropic.", "model", "调用大模型", "中", "会把内容发给外部模型服务，可能产生费用"),
    ("google.generativeai.", "model", "调用大模型", "中", "会把内容发给外部模型服务，可能产生费用"),
    # 环境变量
    ("os.getenv", "env", "读取环境变量", "低", "会读走本机配置"),
    ("os.environ", "env", "读取环境变量", "低", "会读走本机配置"),
]

# 方法名规则：不依赖接收者叫什么（path / p / self.f 都可能），只看方法名。
# 原则：只收语义明确、不会误伤的方法名；宁可报出来让人看一眼，也不漏。
METHOD_RULES: list[tuple[str, str, str, str, str]] = [
    ("read_text", "fs_read", "读取文件", "低", "会读取文件内容"),
    ("read_bytes", "fs_read", "读取文件", "低", "会读取文件内容"),
    ("readlines", "fs_read", "读取文件", "低", "会读取文件内容"),
    ("read_csv", "fs_read", "读取表格文件", "低", "会读取表格内容"),
    ("read_json", "fs_read", "读取数据文件", "低", "会读取数据内容"),
    ("write_text", "fs_write", "写入文件", "中", "会覆盖原文件内容"),
    ("write_bytes", "fs_write", "写入文件", "中", "会覆盖原文件内容"),
    ("writelines", "fs_write", "写入文件", "中", "会覆盖原文件内容"),
    ("to_csv", "fs_write", "写入表格文件", "中", "会生成或覆盖表格"),
    ("to_excel", "fs_write", "写入表格文件", "中", "会生成或覆盖表格"),
    ("unlink", "fs_delete", "删除文件", "高", "文件被彻底删掉，无法恢复"),
    ("rmtree", "fs_delete", "递归删除目录", "高", "整棵目录树被删除，无法恢复"),
    ("iterdir", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("rglob", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("glob", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("listdir", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("scandir", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
    ("walk", "fs_read", "遍历目录", "低", "会列出目录里有哪些文件"),
]

# 密钥类关键词：命中则升级为高危（这条是安全审查的核心）
SECRET_KEYS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API_KEY")

EFFECT_LABEL = {
    "net": "网络",
    "fs_write": "写文件",
    "fs_delete": "删除文件",
    "fs_read": "读文件",
    "exec": "执行命令",
    "db": "数据库",
    "model": "大模型",
    "env": "读环境",
}
# 细粒度动作 → AGL 标准效应标签（3.7：读写 / 网络 / 大模型 / 计时 / 外部调用）
AGL_EFFECT = {
    "net": "网络",
    "fs_write": "读写",
    "fs_delete": "读写",
    "fs_read": "读写",
    "env": "读写",
    "db": "网络",
    "model": "大模型",
    "exec": "外部调用",
}
EFFECT_ORDER = ["net", "fs_write", "fs_delete", "exec", "db", "model", "env", "fs_read"]
RISK_ICON = {"高": "⚠", "中": "△", "低": "·"}


@dataclass
class Finding:
    line: int
    effect: str
    action: str
    risk: str
    detail: str
    call: str
    func: str
    note: str = ""


@dataclass
class Audit:
    source: str
    findings: list[Finding] = field(default_factory=list)
    purpose: str = ""
    functions: list[str] = field(default_factory=list)


# ── 扫描 ──────────────────────────────────────────────────────────────────
def _attr_chain(node: ast.AST) -> str:
    """把属性链还原成点号全名，如 requests.get / os.environ"""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _call_name(node: ast.Call) -> str:
    """把 Call 节点的函数名还原成点号全名，如 requests.get / os.environ.get"""
    parts: list[str] = []
    cur: ast.AST | None = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Subscript) and isinstance(cur.value, ast.Attribute):
        parts.append(cur.value.attr)
    return ".".join(reversed(parts))


def _open_mode(node: ast.Call) -> str:
    """判断 open() 的模式：写 / 读"""
    mode = ""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value)
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value)
    if any(c in mode for c in ("w", "a", "x", "+")):
        return "fs_write"
    return "fs_read"


def _match(call: str) -> tuple[str, str, str, str] | None:
    # 1) 模块级全名前缀匹配（requests.get / os.remove …）
    for prefix, effect, action, risk, detail in RULES:
        if call == prefix or call.startswith(prefix):
            return effect, action, risk, detail
    # 2) 方法名匹配（path.read_text / p.unlink …，不依赖接收者变量名）
    last = call.rsplit(".", 1)[-1]
    for method, effect, action, risk, detail in METHOD_RULES:
        if last == method:
            return effect, action, risk, detail
    return None


def scan_file(path: Path) -> Audit:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        print(f"× 语法错误，无法解析 {path}：第 {e.lineno} 行 {e.msg}", file=sys.stderr)
        sys.exit(2)

    audit = Audit(source=str(path))

    # 收集函数名（用于报告"哪一步做的"）
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            audit.functions.append(node.name)

    # 模块开头第一个字符串常量当作"意图说明"
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        doc = str(tree.body[0].value.value).strip().splitlines()
        if doc:
            audit.purpose = doc[0]

    # 定位每个调用所属的函数
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    owner.setdefault(sub.lineno, node.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node)

        # open() 单独判断读写
        if call == "open" or call.endswith(".open"):
            effect = _open_mode(node)
            if effect == "fs_write":
                hit = ("fs_write", "写入文件", "中", "会覆盖或追加写入文件内容")
            else:
                hit = ("fs_read", "读取文件", "低", "会读取文件内容")
        else:
            hit = _match(call)
            if hit is None:
                continue

        effect, action, risk, detail = hit
        note = ""

        # 密钥升级：读环境变量 / 参数里出现 KEY TOKEN 等
        seg = ast.get_source_segment(src, node) or ""
        if any(k in seg.upper() for k in SECRET_KEYS):
            if effect == "env":
                risk, action, note = "高", "读取密钥", "读到的是凭证，泄露等于交出权限"
            else:
                note = "参数里含疑似凭证"

        audit.findings.append(
            Finding(
                line=node.lineno,
                effect=effect,
                action=action,
                risk=risk,
                detail=detail,
                call=call,
                func=owner.get(node.lineno, "<模块级>"),
                note=note,
            )
        )

    # 密钥读取：os.environ["OPS_TOKEN"] 这类下标访问不是函数调用，必须单独扫
    # —— 这是最该报警、也最容易漏掉的一条
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        base = _attr_chain(node.value)
        if not base.startswith("os.environ"):
            continue
        seg = ast.get_source_segment(src, node) or base
        is_secret = any(k in seg.upper() for k in SECRET_KEYS)
        audit.findings.append(
            Finding(
                line=node.lineno,
                effect="env",
                action="读取密钥" if is_secret else "读取环境变量",
                risk="高" if is_secret else "低",
                detail="读到的是登录凭证，泄露等于把权限交出去" if is_secret else "会读走本机配置",
                call=base,
                func=owner.get(node.lineno, "<模块级>"),
            )
        )

    audit.findings.sort(key=lambda f: (EFFECT_ORDER.index(f.effect) if f.effect in EFFECT_ORDER else 99, f.line))
    return audit


def scan_target(target: Path) -> list[Audit]:
    if target.is_file():
        return [scan_file(target)]
    files = sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)
    return [scan_file(p) for p in files]


# ── 渲染：给人看的报告 ────────────────────────────────────────────────────
def _plain_summary(audits: list[Audit]) -> list[str]:
    """把技术结论翻成一句人话——给完全不懂代码的人看的第一眼。"""
    acts = {f.action for a in audits for f in a.findings}
    lines: list[str] = []
    if acts & {"删除文件", "删除目录", "递归删除目录"}:
        lines.append("它会删掉你磁盘上的文件，删除后找不回来。")
    if "执行系统命令" in acts:
        lines.append("它能在你电脑上运行任意命令，权限和你一样大。")
    if acts & {"发起网络请求", "建立网络连接", "调用大模型", "访问数据库"}:
        lines.append("它会把数据发送到外部服务器。")
    if "读取密钥" in acts:
        lines.append("它会读取你的登录凭证（密钥 / 令牌）。")
    if acts & {"写入文件", "覆盖文件", "移动/重命名文件", "重命名文件", "复制文件", "写入表格文件"}:
        lines.append("它会改写你磁盘上的文件。")
    reads = acts & {"读取文件", "遍历目录", "检查文件是否存在", "检查路径", "读取表格文件", "读取数据文件"}
    writes = acts & {"写入文件", "覆盖文件", "移动/重命名文件", "重命名文件", "复制文件", "写入表格文件"}
    if reads and not writes and not (acts & {"删除文件", "删除目录", "递归删除目录"}):
        lines.append("它会读取你磁盘上的文件，但不会修改或删除。")
    if not lines:
        lines.append("它只做计算，不碰网络、不碰文件、不执行任何命令。")
    return lines


def render(audits: list[Audit]) -> None:
    total = sum(len(a.findings) for a in audits)
    high = sum(1 for a in audits for f in a.findings if f.risk == "高")

    print()
    print("═" * 68)
    print("  AGL 契约检查报告")
    print("═" * 68)
    print(f"  审查对象  {', '.join(Path(a.source).name for a in audits)}")
    print(f"  发现动作  {total} 处" + (f"    其中需要你特别确认的 {high} 处" if high else ""))
    print()

    # 第一眼就要看懂的一段话
    print("  ┌" + "─" * 64)
    for line in _plain_summary(audits):
        print("  │ " + line)
    print("  └" + "─" * 64)
    print()

    for audit in audits:
        if len(audits) > 1:
            print(f"── {Path(audit.source).name} " + "─" * 40)

        if audit.purpose:
            print(f"  它自己说它要干的事：{audit.purpose}")
            print()

        if not audit.findings:
            print("  ✓ 未发现对外部世界产生影响的操作（纯计算）")
            print()
            continue

        # 按效应分组
        groups: dict[str, list[Finding]] = {}
        for f in audit.findings:
            groups.setdefault(f.effect, []).append(f)

        print("  【它会碰到什么】")
        for effect in EFFECT_ORDER:
            if effect not in groups:
                continue
            items = groups[effect]
            lines = "、".join(str(f.line) for f in items)
            print(f"    {_pad(EFFECT_LABEL.get(effect, effect), 10)} {len(items)} 处    第 {lines} 行")
        print()

        # 高危动作单独列，这是人真正要看的
        risky = [f for f in audit.findings if f.risk == "高"]
        if risky:
            print("  【需要你特别确认的动作】")
            for f in risky:
                print(f"    {RISK_ICON['高']} {f.action}    第 {f.line} 行（在 {f.func} 里）")
                print(f"       它做什么：{f.detail}")
                if f.note:
                    print(f"       注意：{f.note}")
            print()

        # 其余动作
        others = [f for f in audit.findings if f.risk != "高"]
        if others:
            print("  【其他动作】")
            for f in others:
                print(f"    {RISK_ICON[f.risk]} {_pad(f.action, 12)} 第 {f.line:<4} 行  {f.detail}")
            print()

    print("═" * 68)
    print("  以上是从代码里实际扫出来的，不是它自己声明的。")
    print("  下一步：确认无误后，下面这份契约会被冻结；以后代码再变动，")
    print("          超出这份契约范围就会报警。")
    print("═" * 68)
    print()


# ── 渲染：AGL 契约草案 ───────────────────────────────────────────────────
def emit_contract(audits: list[Audit]) -> str:
    name = Path(audits[0].source).stem if audits else "未命名"
    ORDER5 = ["网络", "读写", "大模型", "计时", "外部调用"]
    effects = sorted(
        {AGL_EFFECT[f.effect] for a in audits for f in a.findings if f.effect in AGL_EFFECT},
        key=lambda x: ORDER5.index(x) if x in ORDER5 else 99,
    )
    risky_actions = sorted({f.action for a in audits for f in a.findings if f.risk == "高"})
    secrets = sorted(
        {
            seg
            for a in audits
            for f in a.findings
            if f.action == "读取密钥"
            for seg in [f.note or "疑似凭证"]
        }
    )

    out: list[str] = []
    out.append("#语言=中文")
    out.append("// 由 aglc audit 从现有代码自动生成，待人工确认后冻结")
    out.append("// 确认方式：删掉这一行注释，并把 状态 改为 已批准")
    out.append("")
    out.append(f"智能体 {name}")
    if audits and audits[0].purpose:
        out.append(f'目标 "{audits[0].purpose}"')
    out.append("状态 待批准")
    out.append("")
    out.append(f"效应 {' '.join(effects) if effects else '无'}")
    out.append("")
    out.append("约束 {")
    out.append("  // 下面几条是空的，需要你亲手填。空着就是没有限制，等于默认全部允许。")
    out.append("  允许处理的目录 \"/\"")
    out.append("  单次执行最多动作 100")
    out.append("}")
    out.append("")
    if risky_actions:
        out.append("高危 {")
        for a in risky_actions:
            out.append(f"  {a}")
        out.append("}")
        out.append("")
    if secrets:
        out.append("// 检测到读取凭证的行为，请确认是否必要")
        out.append("")
    return "\n".join(out)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv

    if not args:
        print("用法：python aglc/audit.py <文件.py 或 目录> [--json]", file=sys.stderr)
        sys.exit(1)

    target = Path(args[0])
    if not target.exists():
        print(f"× 目标不存在：{target}", file=sys.stderr)
        sys.exit(2)

    audits = scan_target(target)

    if as_json:
        payload = {
            "source": [a.source for a in audits],
            "summary": {
                "total": sum(len(a.findings) for a in audits),
                "high": sum(1 for a in audits for f in a.findings if f.risk == "高"),
            },
            "findings": [asdict(f) for a in audits for f in a.findings],
            "contract_agl": emit_contract(audits),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    render(audits)
    print("── AGL 契约草案 " + "─" * 50)
    print()
    print(emit_contract(audits))
    print()
    print("─" * 68)


if __name__ == "__main__":
    main()
