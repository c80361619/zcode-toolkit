#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zcode-toolkit 跨平台引导脚本（Windows / macOS / Linux）

一条命令完成：环境自检 → 依赖检查 → 语法构建校验 → 回归测试 → 启动。

设计原则（本文件的核心约束）：
  1. **零硬编码绝对路径**。仓库根 = 本文件所在目录（`__file__`），不需要任何配置。
  2. **所有外部可执行文件都靠自动检测**定位 —— 内部实现了 `which()`/`where` 等价逻辑
     （`shutil.which` + 平台特定兜底），找不到就带着可操作的提示失败，绝不假设路径。
  3. **只在标准库上运行**。本项目的运行时依赖是「Python ≥ 3.10 + 标准库」，
     没有 requirements.txt、不需要 pip install；因此「安装依赖」这一步是
     **校验依赖**（版本、可选工具是否在位），而不是执行网络安装。
     检测到 node 缺失时只跳过 JS 语法校验，不整体失败。
  4. 每个步骤幂等、可单独跳过、可整体干跑（`--dry-run`）。

用法：
    python bootstrap.py                 # 自检 + 构建校验 + 测试 + 状态
    python bootstrap.py --all-steps     # 额外把补丁真正注入客户端（需先退出 ZCode）
    python bootstrap.py --only test     # 只跑某几步，逗号分隔
    python bootstrap.py --skip install
    python bootstrap.py --dry-run       # 只打印将要执行的命令，不真的执行

退出码：0 全部成功；1 有关键步骤失败；2 环境不满足最低要求（Python 版本过低）。
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量：唯一的「事实来源」，全部相对本文件，无任何绝对路径
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent          # 仓库根 = 本脚本所在目录
SCRIPTS = HERE / "skills" / "zcode-tokenspeed" / "scripts"
TESTS = HERE / "tests"

try:
    sys.path.insert(0, str(SCRIPTS))
    from _console import safe_stdio
    safe_stdio()
except Exception:
    pass

PY_MIN = (3, 10)                                 # README 声明的 Python 下限
REQUIRED_PY_FILES = [                            # 「构建」要过语法关的 Python 源文件
    SCRIPTS / "zcode_patcher.py",
    SCRIPTS / "sync.py",
    SCRIPTS / "doctor.py",
    SCRIPTS / "apply_after_exit.py",
    SCRIPTS / "_console.py",
]
PATCHER = SCRIPTS / "zcode_patcher.py"

#: 子进程默认超时（秒）。本脚本会跑 `pip install` / `git clone` 等网络操作，
#: 没有超时一旦上游半开连接就会**永久挂死**（用户只看到「卡住」）。
#: 1200s 足够覆盖大依赖安装，又不至于让人无限等。需要更久的场景显式传 timeout=None。
DEFAULT_TIMEOUT = 1200.0

# 步骤标识 -> 人类可读标题（顺序即默认执行顺序）
STEP_ORDER = ["env", "install", "build", "test", "status"]
STEP_TITLES = {
    "env": "环境自检（Python / 平台 / 权限）",
    "install": "依赖检查（标准库 / 可选 Node）",
    "build": "构建校验（py_compile + node --check）",
    "test": "运行回归测试",
    "status": "客户端补丁状态（只读）",
}


# --------------------------------------------------------------------------- #
# 输出工具：自带最简 ANSI 着色，并做编码降级（cp936 管道下不能直接打 Unicode 符号）
# --------------------------------------------------------------------------- #

def _supports_color() -> bool:
    """判断当前 stdout 是否支持 ANSI 转义。"""
    if os.environ.get("NO_COLOR"):                # 遵守 NO_COLOR 约定
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name == "nt":                           # 老版 Windows 控制台默认不开 VT
        return bool(os.environ.get("WT_SESSION")           # Windows Terminal
                    or os.environ.get("ANSICON")
                    or os.environ.get("TERM_PROGRAM")
                    or os.environ.get("ConEmuANSI") == "ON")
    return True


_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"[*] {msg}")


def ok(msg: str) -> None:
    print(_c("32", f"[+] {msg}"))


def warn(msg: str) -> None:
    print(_c("33", f"[!] {msg}"))


def fail(msg: str) -> None:
    print(_c("31", f"[x] {msg}"))


def step(title: str) -> None:
    print()
    print(_c("36;1", f"== {title} =="))


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    fail(msg)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# 可执行文件自动定位：等价于 which / where，且不依赖 shell
# --------------------------------------------------------------------------- #

# 各平台「约定目录」兜底表。仅当 PATH 查找失败时才扫这些位置。
# 用「相对家目录」的写法（~）而不是展开后的绝对路径，避免在 import 时就把
# 当前机器的家目录固化进模块属性（换台机器/换个用户就错了）。
_FALLBACK_DIRS = {
    "nt": [
        "~/AppData/Local/Programs/Python",       # python.org 安装器的默认位置
        "~/scoop/shims",
        "C:/ProgramData/chocolatey/bin",
        "C:/Program Files/Python313",            # 常见的系统级安装（版本号只是候选之一）
        "C:/Program Files/Python312",
        "C:/Program Files/Python311",
    ],
    "posix": [
        "/usr/local/bin", "/usr/bin", "/bin",
        "/opt/homebrew/bin",                     # Apple Silicon 的 Homebrew
        "/usr/local/opt/python/libexec/bin",     # Intel Mac 的 Homebrew Python
        "/opt/local/bin",                        # MacPorts
        "~/.local/bin",                          # pipx / pip --user
        "~/.pyenv/shims",                        # pyenv
        "~/bin",
    ],
}


def _candidate_dirs() -> list[Path]:
    """展开兜底目录表（含 `~`），并过滤掉实际不存在的项。"""
    out: list[Path] = []
    for raw in _FALLBACK_DIRS["nt" if os.name == "nt" else "posix"]:
        try:
            p = Path(raw).expanduser()
        except (RuntimeError, OSError):          # 极少数环境下取不到 home
            continue
        if p.is_dir():
            out.append(p)
    return out


def which(*names: str) -> str | None:
    """按候选名依次在 PATH 中查找可执行文件；找不到返回 None。

    这就是 `which` / `where` 的跨平台替身，分两层：
      1) `shutil.which` —— 标准库实现，Windows 上自动处理 PATHEXT（.exe/.cmd/.bat），
         且尊重当前进程的 PATH，是绝大多数情况下的正确答案。
      2) 约定目录兜底 —— PATH 里没有时（例如某些 GUI 启动的环境 PATH 被裁得很短，
         或用户是用安装器装的、忘了勾「Add to PATH」），再扫一遍各平台约定位置。

    两层都只依赖环境与平台惯例。**不假设任何本机绝对路径。**
    """
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    exts = [""]
    if os.name == "nt":
        # 复用系统 PATHEXT，而不是自己猜；缺失时退回常见组合
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        exts = [e.lower() for e in pathext.split(os.pathsep) if e] + [""]

    for base in _candidate_dirs():
        for name in names:
            for ext in exts:
                cand = base / (name + ext)
                try:
                    if cand.is_file() and os.access(cand, os.X_OK):
                        return str(cand)
                except OSError:                  # 权限不足/符号链接损坏等，跳过即可
                    continue
    return None


def find_python() -> str:
    """定位当前解释器，并确保它满足最低版本（本项目只要求标准库）。"""
    current = sys.executable
    if current and Path(current).exists():
        return current
    # 极端情况（被以某种方式内嵌调用）下退回 PATH 查找
    for name in ("python3", "python", "py"):
        found = which(name)
        if found:
            return found
    die("定位不到可用的 Python 解释器。请安装 Python %d.%d+ 后重试。" % PY_MIN)
    raise SystemExit(1)                            # 让类型检查器满意


def find_node() -> str | None:
    """Node 是**可选**依赖：只用于校验注入脚本语法。"""
    return which("node", "nodejs")


def print_python_hint(python: str) -> None:
    """Python 版本不达标时，给出分平台的最小可操作提示。"""
    cur = platform.python_version()
    system = platform.system()
    fail(f"当前 Python {cur} 低于最低要求 {PY_MIN[0]}.{PY_MIN[1]}")
    info(f"当前解释器：{python}")
    if system == "Windows":
        info("Windows：到 https://www.python.org/downloads/windows/ 安装 3.10+，"
             "安装时勾选「Add python.exe to PATH」")
        info("  或： winget install -e --id Python.Python.3.12")
    elif system == "Darwin":
        info("macOS：brew install python@3.12   （或到 python.org 下载 .pkg）")
    else:
        info("Linux：sudo apt install python3.12   /   sudo dnf install python3.12")
    info("装好后重跑本脚本即可，无需改动任何路径。")


# --------------------------------------------------------------------------- #
# 子进程封装：统一处理编码、无窗口、干跑、失败定位
# --------------------------------------------------------------------------- #

def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
        dry_run: bool = False, quiet: bool = False,
        timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """执行子命令，返回 (退出码, 合并后的输出)。

    - 一律走列表形式传参，**不经过 shell**，因此路径含空格/中文都安全，
      也天然杜绝了「写死某个 shell 的语法」这类跨平台坑。
    - Windows 下如果父进程没有控制台，创建 console 子进程会弹出 cmd 窗口，
      因此统一带上 CREATE_NO_WINDOW（本项目 0.5.8 踩过的坑）。
    - 输出一律按 UTF-8 解码并替换非法字节，避免 cp936 管道下 UnicodeDecodeError。
    - **默认带超时**：本函数要跑 `pip install` / `git clone` 这类网络操作，
      没有超时的话一旦上游卡住（半开连接、代理不响应）脚本会**永久挂死**，
      而用户看到的只是「卡住不动」，没有任何可操作的信息。
      超时返回码约定用 124（与 GNU timeout 一致，便于脚本化判断）。
      确需长时间的操作可显式传 `timeout=None` 关掉。
    """
    shown = " ".join(_quote(c) for c in cmd)
    if dry_run:
        info(f"[dry-run] {shown}")
        return 0, ""

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000       # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout,
            **kwargs,
        )
    except FileNotFoundError as exc:
        fail(f"命令不存在：{cmd[0]}（{exc}）")
        if check:
            raise SystemExit(1)
        return 127, ""
    except subprocess.TimeoutExpired as exc:
        # 已经产出的部分输出对排查很有价值（例如 pip 卡在哪个包），尽量带出来
        partial = (exc.stdout or b"").decode("utf-8", errors="replace")
        if partial.strip():
            for line in partial.rstrip().splitlines():
                print(f"    {line}")
        fail(f"命令超时（{timeout}s）：{shown}")
        if check:
            raise SystemExit(124)
        return 124, partial
    except OSError as exc:
        fail(f"无法执行 {cmd[0]}：{exc}")
        if check:
            raise SystemExit(1)
        return 126, ""

    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    if not quiet and out.strip():
        # 缩进子进程输出，便于和本脚本自己的日志区分开
        for line in out.rstrip().splitlines():
            print(f"    {line}")
    if check and proc.returncode != 0:
        fail(f"命令失败（退出码 {proc.returncode}）：{shown}")
        raise SystemExit(proc.returncode or 1)
    return proc.returncode, out


def _quote(arg: str) -> str:
    """仅在打印时给带空格的参数加引号，纯展示用途。"""
    return f'"{arg}"' if " " in arg else arg


# --------------------------------------------------------------------------- #
# 各步骤实现
# --------------------------------------------------------------------------- #

def step_env(args) -> None:
    step(STEP_TITLES["env"])
    system = platform.system()
    release = platform.release()
    arch = platform.machine() or "?"
    info(f"平台：{system} {release} ({arch})")
    info(f"Python：{platform.python_version()}  ->  {sys.executable}")
    info(f"仓库根：{HERE}")

    if sys.version_info < PY_MIN:
        print_python_hint(sys.executable)
        sys.exit(2)
    ok(f"Python 版本满足最低要求（>= {PY_MIN[0]}.{PY_MIN[1]}）")

    if not SCRIPTS.is_dir():
        die(f"找不到脚本目录：{SCRIPTS}\n    "
            f"请确认本脚本位于仓库根目录（与 skills/ 同级）。")
    ok("脚本目录结构完整（skills/zcode-tokenspeed/scripts）")

    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        warn("当前以 root 运行。打补丁本身不需要 root，"
             "仅在客户端装在 /opt、/Applications 等受保护位置时才需要 sudo。")


def step_install(args) -> None:
    step(STEP_TITLES["install"])
    info("本项目的运行时依赖 = Python 标准库，仓库内没有 requirements.txt；")
    info("因此这一步是「校验依赖」，不做网络安装。")

    # 1) 标准库自检：把脚本真正会 import 的模块拉一遍，提前暴露环境残缺
    import importlib.util
    needed = ["argparse", "json", "hashlib", "struct", "shutil", "subprocess",
              "pathlib", "tempfile", "unicodedata", "urllib.request"]
    missing = [m for m in needed
               if importlib.util.find_spec(m) is None]
    if missing:
        die("标准库缺失（Python 安装不完整）：" + ", ".join(missing)
            + "\n    请重新安装官方 Python 发行版。")
    ok(f"标准库完整（抽查 {len(needed)} 个模块）")

    # 2) 可选依赖：Node —— 缺了只跳过 JS 语法校验
    node = find_node()
    if node:
        rc, out = run([node, "--version"], check=False, quiet=True)
        ver = out.strip() or "?"
        ok(f"Node（可选）：{ver}  ->  {node}")
    else:
        warn("未找到 Node（可选）。将跳过注入脚本的语法校验。")
        info("  需要时安装：https://nodejs.org/  或  brew install node / apt install nodejs")

    # 3) 客户端安装位置探测（只读，交给项目自带的探测器，不重复实现）
    info("探测 ZCode 客户端安装位置（只读）…")
    rc, out = run([args.python, str(PATCHER), "--all", "--check", "--verbose"],
                  check=False, quiet=True)
    if rc == 0:
        # 只挑出探测相关的行，避免把整份 check 输出都刷出来
        hits = [ln.strip() for ln in out.splitlines()
                if "探测" in ln or "检查 " in ln or ln.strip().startswith("[·]")]
        for line in hits[:8]:
            print(f"    {line}")
        ok("客户端位置探测完成")
    else:
        warn("未探测到 ZCode 安装（客户端未装 / 装在非标准位置均可忽略本步）。")
        info("  稍后可用： python skills/zcode-tokenspeed/scripts/zcode_patcher.py <安装根>  ← 手动指定")


def step_build(args) -> None:
    step(STEP_TITLES["build"])

    # --- Python 语法编译 ---
    targets = [p for p in REQUIRED_PY_FILES if p.exists()]
    if not targets:
        die(f"在 {SCRIPTS} 下找不到待校验的 Python 文件。")
    info(f"py_compile：{len(targets)} 个 Python 源文件")
    run([args.python, "-m", "py_compile", *[str(p) for p in targets]],
        dry_run=args.dry_run, quiet=True)
    ok("Python 语法校验通过")

    # 顺带编译 scripts/ 下的全部脚本（含增强/滑条等未列入必需清单的）
    extra = sorted(p for p in SCRIPTS.glob("*.py") if p not in targets)
    if extra:
        info(f"py_compile：其余 {len(extra)} 个脚本")
        run([args.python, "-m", "py_compile", *[str(p) for p in extra]],
            dry_run=args.dry_run, quiet=True)
        ok("其余脚本语法校验通过")

    # --- 注入脚本语法（需要 Node，可选） ---
    node = find_node()
    js_files = sorted(SCRIPTS.glob("*.js"))
    if not js_files:
        warn("未找到任何注入脚本（*.js），跳过。")
    elif not node:
        warn(f"跳过 {len(js_files)} 个注入脚本的语法校验（未安装 Node）。")
    elif args.dry_run:
        for f in js_files:
            info(f"[dry-run] {node} --check {f}")
    else:
        info(f"node --check：{len(js_files)} 个注入脚本")
        bad = []
        for f in js_files:
            rc, _ = run([node, "--check", str(f)], check=False, quiet=True)
            if rc != 0:
                bad.append(f.name)
        if bad:
            die("注入脚本语法错误：" + ", ".join(bad))
        ok("注入脚本语法校验通过")


def step_test(args) -> None:
    step(STEP_TITLES["test"])
    if not TESTS.is_dir():
        die(f"找不到测试目录：{TESTS}")
    info("python -m unittest discover -s tests")
    info("（其中「真实 app.asar 只读校验」在装了客户端的机器上才会执行，属预期内的条件跳过）")
    rc, out = run([args.python, "-m", "unittest", "discover", "-s", str(TESTS)],
                  check=False, quiet=True, dry_run=args.dry_run)
    if args.dry_run:
        return
    tail = out.rstrip().splitlines()[-6:]
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    for line in tail:
        try:
            print(f"    {line}")
        except UnicodeEncodeError:
            safe_line = line.encode(enc, errors="replace").decode(enc, errors="replace")
            print(f"    {safe_line}")
    if rc != 0:
        fail("回归测试未通过 —— 本次「构建」视为失败，请先修掉上面的用例。")
        raise SystemExit(1)
    ok("回归测试全部通过")

    # 滑条专项冒烟（需要 Node，可选）
    # 注意：slider_smoke.js 要求把「被测脚本路径」作为第 1 个参数传进来；
    # 缺参数时它自己会打「用法」提示并退 2（0.6.3 起不再抛裸堆栈）。
    smoke = TESTS / "slider_smoke.js"
    target = SCRIPTS / "zcode-thought-slider.js"
    node = find_node()
    if smoke.exists() and target.exists() and node:
        info("tests/slider_smoke.js（滑条最小 DOM 冒烟）")
        rc, out = run([node, str(smoke), str(target)], check=False, quiet=True)
        if rc == 0:
            tail = out.strip().splitlines()
            ok(f"滑条冒烟通过{('：' + tail[-1]) if tail else ''}")
        else:
            warn("滑条冒烟未通过（不影响主流程，但改了滑条脚本时应当关注）。")
            for line in out.rstrip().splitlines()[-5:]:
                print(f"    {line}")
    elif not node:
        warn("跳过滑条冒烟（未安装 Node）。")


def step_status(args) -> None:
    step(STEP_TITLES["status"])
    if not PATCHER.exists():
        die(f"找不到 {PATCHER}")

    # 只读体检。没装客户端时这一步会返回非 0，属正常，不当作失败。
    rc, out = run([args.python, str(PATCHER), "--all", "--check"],
                  check=False, quiet=True, dry_run=args.dry_run)
    if args.dry_run:
        return
    if rc != 0:
        warn("未探测到 ZCode 客户端 —— 跳过状态检查（不影响代码本身的可用性）。")
        return
    for line in out.rstrip().splitlines():
        print(f"    {line}")
    ok("状态检查完成（只读，未修改任何文件）")

    if "版本旧" in out or "旧版" in out:
        warn("检测到已注入的补丁版本较旧（或与当前脚本不同源）。")
        info("  查看差异： python skills/zcode-tokenspeed/scripts/zcode_patcher.py --all --dry-run")
    info("要把补丁真正写入客户端：**先完全退出 ZCode**，再执行")
    info(f"  {args.python} {PATCHER} --all")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="zcode-toolkit 跨平台引导：环境自检 → 依赖检查 → 构建校验 → 测试 → 状态。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python bootstrap.py\n"
               "  python bootstrap.py --only build,test\n"
               "  python bootstrap.py --skip test --dry-run\n"
               "  python bootstrap.py --all-steps          # 额外真正注入客户端（需先退出 ZCode）",
    )
    ap.add_argument("--only", metavar="STEPS",
                    help=f"只执行指定步骤（逗号分隔）。可选：{','.join(STEP_ORDER)}")
    ap.add_argument("--skip", metavar="STEPS",
                    help="跳过指定步骤（逗号分隔）")
    ap.add_argument("--all-steps", action="store_true",
                    help="执行完整链路，含真正把补丁注入客户端（需要 ZCode 已退出）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将要执行的命令，不做任何实际改动")
    ap.add_argument("--python", metavar="PATH", default=None,
                    help="指定 Python 解释器（缺省 = 运行本脚本的那个）")
    return ap


def parse_steps(text: str | None, *, valid: list[str], what: str) -> list[str]:
    if not text:
        return []
    items = [s.strip().lower() for s in text.split(",") if s.strip()]
    unknown = [s for s in items if s not in valid]
    if unknown:
        die(f"{what} 中出现未知步骤：{', '.join(unknown)}\n    可选：{', '.join(valid)}")
    return items


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    args.python = args.python or find_python()

    steps = list(STEP_ORDER)
    extra_step: str | None = None
    if args.all_steps:
        extra_step = "apply"
        steps.append(extra_step)
        STEP_TITLES[extra_step] = "注入补丁到客户端（需要 ZCode 已完全退出）"

    only = parse_steps(args.only, valid=STEP_ORDER + ([extra_step] if extra_step else []),
                       what="--only")
    if only:
        steps = only
    skip = parse_steps(args.skip, valid=STEP_ORDER + ([extra_step] if extra_step else []),
                       what="--skip")
    steps = [s for s in steps if s not in skip]
    if not steps:
        die("所有步骤都被过滤掉了，无事可做。")

    started = time.time()
    print(_c("1", "zcode-toolkit · 跨平台引导脚本"))
    print(_c("2", f"将要执行的步骤：{' → '.join(STEP_TITLES.get(s, s) for s in steps)}"))
    if args.dry_run:
        warn("dry-run 模式：不会执行任何写操作")

    handlers = {
        "env": step_env,
        "install": step_install,
        "build": step_build,
        "test": step_test,
        "status": step_status,
        "apply": step_apply,
    }

    try:
        for name in steps:
            handlers[name](args)
    except KeyboardInterrupt:
        print()
        warn("被用户中断。")
        return 130
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        print()
        fail(f"引导中断（退出码 {code}）。上面最后一条错误就是原因。")
        return code

    elapsed = time.time() - started
    print()
    print(_c("32;1", f"全部完成（{elapsed:.1f}s）。"))
    print()
    info("下一步：")
    info("  1. 没装插件： ZCode → 设置 → 插件 → 创建 → 添加插件市场 → c80361619/zcode-toolkit")
    info("  2. 想用命令行： python skills/zcode-tokenspeed/scripts/zcode_patcher.py --all --check")
    info("  3. 打了补丁但没生效： python skills/zcode-tokenspeed/scripts/doctor.py")
    return 0


def step_apply(args) -> None:
    """真正写入客户端（--all-steps）。会被运行守卫拦下，这里提前给出明确提示。"""
    step(STEP_TITLES["apply"])

    # 跨平台判断 ZCode 是否在运行 —— 不依赖 tasklist / ps 的具体输出格式
    running = False
    if os.name == "nt":
        rc, out = run(["tasklist", "/FI", "IMAGENAME eq ZCode.exe", "/NH"],
                      check=False, quiet=True)
        running = "zcode.exe" in out.lower()
    else:
        pgrep = which("pgrep")
        if pgrep:
            rc, _ = run([pgrep, "-x", "ZCode"], check=False, quiet=True)
            running = rc == 0
        else:
            rc, out = run(["ps", "-A"], check=False, quiet=True)
            running = "zcode" in out.lower()

    if running and not args.dry_run:
        die("ZCode 正在运行 —— app.asar 被占用，此时写入必然失败。\n"
            "    请**完全退出** ZCode（Windows 托盘右键退出；macOS 用 Cmd+Q），然后重跑。")
    if not running:
        ok("ZCode 未运行，可以安全写入。")

    info(f"{args.python} {PATCHER} --all")
    run([args.python, str(PATCHER), "--all"], dry_run=args.dry_run)
    ok("补丁已写入。重新启动 ZCode 即可看到效果。")


if __name__ == "__main__":
    sys.exit(main())
