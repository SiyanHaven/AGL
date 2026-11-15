# -*- coding: utf-8 -*-
"""自举编译器固定化安装。

把「种子编译器 → 管线 v1 → v1 编自身 v2 → v2 编自身 v3」整条链收成一个
可重复执行的安装步骤，并把不动点产物 v3 安装到 `build/自举编译器/自举编译器.agb`。

- `aglc bootstrap`：命令行入口，打印各阶段耗时与 SHA256；
- doctor 的「AGB 产物与默认 VM」改为调用本模块（复用同一个实现，
  体检不再自己造管线）；后续对拍直接使用安装好的不动点产物。

验收口径：v2 与 v3 逐字节一致才算到不动点。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 源码树里沿用 `REPO/build`，保持既有开发和发布包行为；
# 从 wheel 安装时 `REPO` 是 site-packages，不能往那里写运行产物，
# 改放用户目录（可用 AGL_HOME 覆盖）。
_AGL_HOME = os.environ.get("AGL_HOME")
if _AGL_HOME:
    _INSTALL_ROOT = Path(_AGL_HOME).expanduser()
elif (REPO / "pyproject.toml").is_file():
    _INSTALL_ROOT = REPO
else:
    _INSTALL_ROOT = Path.home() / ".agl"

PIPELINE_SRC = REPO / "自举" / "自举编译器.agl"
INSTALL_DIR = _INSTALL_ROOT / "build" / "自举编译器"
INSTALL_TARGET = INSTALL_DIR / "自举编译器.agb"
BUILTIN_AGB = Path(__file__).resolve().parents[1] / "_runtime" / "自举编译器.agb"
OUT_ROOT = Path(tempfile.gettempdir()) / "agl_agb"


@dataclass
class BootstrapResult:
    errors: list[str] = field(default_factory=list)
    pipeline_agb: str = ""
    v2: str = ""
    v3: str = ""
    installed: str = ""
    shas: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_cli(args: list[str], env: dict | None = None,
             timeout: int = 900) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "compiler.cli", *args],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        env=e, timeout=timeout)


class BootstrapError(Exception):
    """自举链路不可用或编译失败；消息面向用户，中文、不甩内部术语。

    `fallback` 为真表示「种子默认编译器可以接手」（自举链自身的问题），
    为假表示「源码本身没过静态检查，换哪个编译器都不该编」。
    """

    def __init__(self, message: str, fallback: bool = True):
        super().__init__(message)
        self.fallback = fallback


def selfhost_available() -> bool:
    return INSTALL_TARGET.exists()


def _agb_file_name(module: str) -> str:
    """与 CLI 的 build 产物命名保持一致。"""
    return module.replace("/", "_").replace("\\", "_").replace(":", "_") + ".agb"


def _write_seed_dependency_artifacts(check, out_dir: Path) -> None:
    """自举入口只产出扁平化主 AGB；多文件工程仍要按 build 契约落依赖 .agb。

    依赖模块由同一次种子静态检查得到的 IR 生成，命名与 `aglc build` 完全一致；
    主模块仍由自举编译器产出。
    """
    from compiler import bytecode, ir

    seen: set[str] = set()

    def emit(dep) -> None:
        module = dep.ir.get("program", {}).get("modules", [{}])[0].get("name", "")
        if not module or module in seen:
            return
        seen.add(module)
        agb = bytecode.compile_program(dep.ir)
        (out_dir / _agb_file_name(module)).write_text(
            bytecode.encode(agb), encoding="utf-8", newline="\n")
        (out_dir / (dep.path.name + ".ir")).write_text(
            ir.dumps(dep.ir), encoding="utf-8", newline="\n")
        for child in (dep.deps or {}).values():
            emit(child)

    for dep in (getattr(check, "deps", None) or {}).values():
        emit(dep)


def compile_with_selfhost(source: Path, out_dir: Path,
                          want_ir: bool = True) -> tuple[Path, Path | None]:
    """用安装好的不动点编译器把一份 `.agl` 编成 `.agb`。

    返回 (agb 路径, ir 路径或 None)。自举链编不动（语法超出 / 缺入口 /
    导入解析失败）时抛 BootstrapError，由调用方决定报错文案与兜底方式。
    """
    if not selfhost_available():
        # 用户包只带预编译自举器，不带自举源码；首次使用时复制过去即可。
        if BUILTIN_AGB.is_file() and not PIPELINE_SRC.is_file():
            boot = install_bootstrap(verbose=False)
            if not boot.ok:
                raise BootstrapError("内置自举编译器安装失败：" + "; ".join(boot.errors[:2]))
        else:
            raise BootstrapError("自举编译器尚未安装；先运行一次 `aglc bootstrap`")
    # §5e 门槛 1（B 方案）：自举链 = 编译通道，静态校验以 aglc check 为准 ——
    # 编译前先过种子全链检查，静态错误在编译期拦截，不让坏程序到运行期才炸。
    from compiler.cli import pipeline

    check = pipeline.compile_file(source)
    if not check.ok:
        e = check.errors[0]
        raise BootstrapError(
            f"静态检查未通过（{e.code} {e.file} 第 {e.line} 行）：{e.message_zh}",
            fallback=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_seed_dependency_artifacts(check, out_dir)
    agb_target = out_dir / (source.stem + ".agb")
    ir_target = out_dir / (source.name + ".ir") if want_ir else None
    if agb_target.exists():
        agb_target.unlink()
    if ir_target is not None and ir_target.exists():
        ir_target.unlink()

    env = {
        "AGL_BOOTSTRAP_SOURCE": str(source),
        "AGL_BOOTSTRAP_MODULE": source.stem,
        "AGL_BOOTSTRAP_OUTPUT": str(agb_target),
        "AGL_BOOTSTRAP_OUTPUT_FULL": "1",
    }
    if ir_target is not None:
        env["AGL_BOOTSTRAP_OUTPUT_IR"] = str(ir_target)
    r = _run_cli(["run", str(INSTALL_TARGET), "--allow-effects", "IO",
                  "--max-steps", "50000000"], env=env)
    if r.returncode != 0 or not agb_target.exists():
        # 自举编译器解析失败时会在 stdout 打一行结构化诊断
        # （自举编译器：解析失败 AGL-E-xxxx 第 N 行 …），优先用它报错。
        detail = ""
        for line in (r.stdout or "").splitlines():
            if "解析失败" in line and "AGL-E-" in line:
                detail = line.replace("自举编译器：", "").strip()
                break
        if not detail:
            lines = (r.stderr or r.stdout or "").strip().splitlines()
            detail = lines[0] if lines else "自举编译器没有产出字节码"
        raise BootstrapError(f"自举编译器编不了这份源码：{detail}")
    if not (want_ir and ir_target is not None and ir_target.exists()):
        return agb_target, None
    return agb_target, ir_target


def install_bootstrap(verbose: bool = True) -> BootstrapResult:
    """安装自举编译器：源码树跑不动点链，用户包直接复制内置 .agb。"""
    res = BootstrapResult()
    if BUILTIN_AGB.is_file() and not PIPELINE_SRC.is_file():
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUILTIN_AGB, INSTALL_TARGET)
        res.installed = str(INSTALL_TARGET)
        res.shas = {"builtin": _sha(BUILTIN_AGB)}
        res.timings["copy"] = 0.0
        if verbose:
            print(f"✓ 已安装内置自举编译器（{INSTALL_TARGET.stat().st_size:,} B）→ {INSTALL_TARGET}")
        return res
    if not PIPELINE_SRC.exists():
        res.errors.append(f"缺少 {PIPELINE_SRC}")
        return res

    # ── 1. 种子编译器编管线 v1 ────────────────────────────────────────
    t0 = time.perf_counter()
    pipeline_dir = OUT_ROOT / "bootstrap_install"
    if pipeline_dir.exists():
        shutil.rmtree(pipeline_dir)
    build = _run_cli(["build", str(PIPELINE_SRC), "--out", str(pipeline_dir),
                      "--compiler", "seed"])
    res.timings["seed_build"] = round(time.perf_counter() - t0, 1)
    v1 = pipeline_dir / "自举编译器.agb"
    if build.returncode != 0 or not v1.exists():
        res.errors.append(f"种子编译管线失败：{(build.stderr or build.stdout).strip()[:300]}")
        return res
    res.pipeline_agb = str(v1)

    # ── 2. v1 编自身 → v2 ────────────────────────────────────────────
    t0 = time.perf_counter()
    r2 = _run_cli(["run", str(v1), "--allow-effects", "IO",
                   "--max-steps", "50000000"],
                  env={"AGL_BOOTSTRAP_SOURCE": str(PIPELINE_SRC),
                       "AGL_BOOTSTRAP_MODULE": "自举编译器",
                       "AGL_BOOTSTRAP_OUTPUT": "selfhost_v2.agb"})
    res.timings["v1_selfcompile"] = round(time.perf_counter() - t0, 1)
    v2 = OUT_ROOT / "selfhost_v2.agb"
    if r2.returncode != 0 or not v2.exists():
        res.errors.append(f"v1 编自身失败：{(r2.stderr or r2.stdout).strip()[:300]}")
        return res
    res.v2 = str(v2)

    # ── 3. v2 编自身 → v3 ────────────────────────────────────────────
    t0 = time.perf_counter()
    r3 = _run_cli(["run", str(v2), "--allow-effects", "IO",
                   "--max-steps", "50000000"],
                  env={"AGL_BOOTSTRAP_SOURCE": str(PIPELINE_SRC),
                       "AGL_BOOTSTRAP_MODULE": "自举编译器",
                       "AGL_BOOTSTRAP_OUTPUT": "selfhost_v3.agb"})
    res.timings["v2_selfcompile"] = round(time.perf_counter() - t0, 1)
    v3 = OUT_ROOT / "selfhost_v3.agb"
    if r3.returncode != 0 or not v3.exists():
        res.errors.append(f"v2 编自身失败：{(r3.stderr or r3.stdout).strip()[:300]}")
        return res
    res.v3 = str(v3)

    # ── 4. 不动点断言 + 安装 ──────────────────────────────────────────
    sha_v2, sha_v3 = _sha(v2), _sha(v3)
    res.shas = {"v2": sha_v2, "v3": sha_v3}
    if sha_v2 != sha_v3:
        res.errors.append(f"v2 与 v3 不是逐字节一致（{sha_v2[:12]}… vs {sha_v3[:12]}…），未到不动点")
        return res

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(v3, INSTALL_TARGET)
    res.installed = str(INSTALL_TARGET)

    # ── 5. 历史记录：时间 / SHA / 各阶段耗时 / 大小 追加一行 ────
    history = INSTALL_DIR / "bootstrap_history.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    total = round(sum(res.timings.values()), 1)
    line = (f"{stamp}  sha={sha_v3[:16]}  size={INSTALL_TARGET.stat().st_size}B  "
            f"total={total}s  " + " ".join(f"{k}={v}s" for k, v in res.timings.items()))
    try:
        with open(history, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 历史记录失败不影响安装本身

    if verbose:
        print(f"自举安装完成：不动点 {sha_v3[:16]}…"
              f"（{INSTALL_TARGET.stat().st_size:,} B）→ {INSTALL_TARGET}")
        for stage, sec in res.timings.items():
            print(f"  {stage}: {sec}s")
        # 与上一次安装基线对比（历史至少两行时才比）
        try:
            prev_lines = [l for l in history.read_text(encoding="utf-8").splitlines()
                          if l.strip()]
        except OSError:
            prev_lines = []
        if len(prev_lines) >= 2:
            prev = prev_lines[-2]
            print(f"  上一次基线：{prev}")
    return res
