# -*- coding: utf-8 -*-
"""模块解析：把 `导入 X` 变成一张依赖图。

规则（语言规范 3.11 + 工程配置 4.3）：
  · 模块名 = 源文件名去掉 `.agl`，`导入 工具` 先找同目录的 `工具.agl`；
  · 同目录没有，才看项目 `agl.toml` 的 `[dependencies]`：
    声明过的名字去内置标准库目录找（`stdlib/数学.agl`）；
  · 没声明就是编译错误，并且要告诉你去哪个文件加哪一行；
  · 禁止循环导入，编译期就拒绝，不留到运行期。

为什么要 `[dependencies]` 这一道：标准库模块和用户文件同名时不至于互相顶替，
而且"这个项目用了什么"是可读的 —— 不写就看不见依赖。
版本号当前版本 只记录不校验（标准库还没版本化），这条写进了能力边界表。

本文件只做"谁依赖谁"的拓扑排序，不编译、不校验。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from compiler.diagnostics import Code, FixHint, error
from compiler.lexer import detect_lang, tokenize
from compiler.parser import parse

SUFFIX = ".agl"
CONFIG_NAME = "agl.toml"

# 内置标准库目录（仓库根的 stdlib/），跨项目共享
STDLIB_DIR = Path(__file__).resolve().parents[1] / "stdlib"


@dataclass
class Project:
    """一个 AGL 工程：根目录（有 agl.toml 的那一层）与它的依赖声明。"""
    root: Path | None = None
    dependencies: dict[str, str] = field(default_factory=dict)
    config_error: str = ""      # 宿主侧的原始解析信息，进 cause
    config_line: int = 1        # 出错大约在第几行，报错指向配置文件本身

    @property
    def has_config(self) -> bool:
        return (self.root / CONFIG_NAME).exists() if self.root else False

    def declares(self, name: str) -> bool:
        return name in self.dependencies


def _error_line(detail: str) -> int:
    """从 TOML 解析器的报错里抠出行号；抠不到就按第 1 行报。"""
    m = re.search(r"line (\d+)", detail)
    return int(m.group(1)) if m else 1


def find_project(entry: Path) -> Project:
    """从入口文件往上找 agl.toml。找不到就是"没有工程配置"，标准库不可用。

    解析失败**不许静默当成"没有依赖"**：那会让标准库莫名其妙不可用，
    报出的却是"找不到模块"。这里把错误原样带出去，由 resolve 报 0605。
    """
    cur = entry.resolve().parent
    while True:
        cfg = cur / CONFIG_NAME
        if cfg.exists():
            try:
                data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                return Project(root=cur, config_error=detail,
                               config_line=_error_line(detail))
            deps = data.get("dependencies") or {}
            if not isinstance(deps, dict):
                return Project(root=cur, config_error="[dependencies] 必须是键值表")
            return Project(root=cur, dependencies={str(k): str(v) for k, v in deps.items()})
        if cur.parent == cur:
            return Project()
        cur = cur.parent


def stdlib_modules() -> dict[str, Path]:
    """内置标准库有哪些模块：{模块名: 文件路径}。"""
    if not STDLIB_DIR.exists():
        return {}
    return {p.stem: p for p in sorted(STDLIB_DIR.glob(f"*{SUFFIX}"))}


def _imports_of(path: Path) -> tuple[list[dict], str]:
    """读出这个文件里所有的导入语句。解析失败会抛 AglError，带着真实文件名。"""
    src = path.read_text(encoding="utf-8")
    lang = detect_lang(src)
    ast = parse(tokenize(src, path.name), path.name, lang)
    return ast["modules"][0].get("imports") or [], lang


def _locate(dep_name: str, here: Path, project: Project) -> tuple[Path | None, str]:
    """找一个被导入的模块。返回 (路径, 失败时给的提示)。"""
    # 模块名就是文件名，一个字都不许是路径。
    # 不拦住的话 `导入 ../../秘密` 会读到工程目录外面去，`导入 /某个绝对路径`
    # 更是能指到机器上任何地方 —— 编译器不该具备这种伸手的能力。
    # 这一关要排在"文件在不在"之前 —— 先问清楚"你写的是不是一个纯名字"。
    if dep_name != Path(dep_name).name or Path(dep_name).is_absolute():
        return None, "模块名里不能有路径分隔符，也不能写绝对路径（模块名就是同目录的文件名）"

    local = here / f"{dep_name}{SUFFIX}"
    if local.exists():
        return local, ""

    lib = stdlib_modules().get(dep_name)
    if lib is not None:
        if project.declares(dep_name):
            return lib, ""
        cfg = f"{project.root / CONFIG_NAME}" if project.root else CONFIG_NAME
        # TOML 的裸键只允许 ASCII；中文模块名必须加引号，这里直接给出可粘贴的一行
        key = dep_name if dep_name.isascii() else f'"{dep_name}"'
        hint = (f"在 {cfg} 的 [dependencies] 里加一行 `{key} = \"0.1.0\"`；"
                "（它是内置标准库模块，声明后才允许使用）")
        return None, hint

    return None, f"新建 {dep_name}{SUFFIX}，或把导入的名字改成实际存在的文件"


def resolve(entry: Path) -> list[tuple[str, Path]]:
    """返回依赖的拓扑序：被依赖的模块在前，入口在最后。

    每个元素是 (模块名, 文件路径)。模块名即文件名去掉后缀。
    """
    order: list[tuple[str, Path]] = []
    done: set[str] = set()
    stack: list[str] = []
    project = find_project(entry)
    if project.config_error:
        cfg = (project.root or entry.parent) / CONFIG_NAME
        raise error(
            Code.CONFIG_INVALID,
            f"`{CONFIG_NAME}` 第 {project.config_line} 行附近不是合法的 TOML，"
            "工程配置读不出来",
            file=CONFIG_NAME, line=project.config_line, col=0, end_col=1,
            cause=project.config_error,
            fix_hints=[FixHint(
                "fix_config", f"{CONFIG_NAME}:{project.config_line}",
                "按 TOML 写：键名里有中文就加双引号，如 `\"数学\" = \"0.1.0\"`")],
        )

    def visit(path: Path) -> None:
        name = path.stem
        if name in done:
            return
        if name in stack:
            chain = " → ".join(stack[stack.index(name):] + [name])
            raise error(
                Code.CIRCULAR_IMPORT,
                f"循环导入：{chain}",
                file=path.name, line=1, col=0, end_col=1,
                fix_hints=[FixHint("break_cycle", path.name,
                                   "把两边都要用的东西挪到第三个模块里，让依赖变成单向")],
            )

        stack.append(name)
        imports, _ = _imports_of(path)

        for imp in imports:
            # FFI 外部库不是 AGL 模块：不查文件、不进依赖图。
            # 它在宿主侧由运行时懒导入，编译期只需要记住「用了哪个库、什么效应」。
            if imp.get("kind") == "ffi":
                continue
            dep_name = imp["module"]
            sp = imp.get("span") or {}
            dep, hint = _locate(dep_name, path.parent, project)
            if dep is None:
                raise error(
                    Code.MODULE_NOT_FOUND,
                    f"找不到模块 `{dep_name}`：同目录没有 `{dep_name}{SUFFIX}`",
                    file=path.name,
                    line=sp.get("line", 1), col=sp.get("col", 0),
                    end_col=sp.get("endCol", 1),
                    fix_hints=[FixHint("declare_or_create", dep_name, hint)],
                )
            visit(dep)

        stack.pop()
        done.add(name)
        order.append((name, path))

    visit(entry)
    return order


def exports_of(ast: dict) -> dict[str, dict]:
    """从 AST 里取出可被外部使用的函数：{函数名: 函数节点}。

    只认带 `导出` 标记的；没标记的一律私有，外部访问会被拒绝。
    """
    mod = ast["modules"][0]
    exported = set(mod.get("exports") or [])
    return {fn["name"]: fn for fn in mod["functions"] if fn["name"] in exported}
