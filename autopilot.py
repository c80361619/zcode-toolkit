#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zcode-toolkit 全自动流水线（Windows / macOS / Linux）

一条命令、零人工干预地跑完：
    环境自检 → 自动安装依赖 → 自动构建 → 自动测试 → 自动部署 → 验证 → 汇总报告

与 `bootstrap.py` 的分工：
  * `bootstrap.py` 是**面向人的交互式引导**（分步输出、可选步骤、给人读的建议）
  * `autopilot.py` 是**面向无人值守的流水线**（结构化日志、自动重试、自动装依赖、
    自动部署、机器可读的汇总报告 + 明确的退出码）—— 适合 CI 与本机一键跑

设计原则：
  1. **零硬编码绝对路径**。仓库根 = 本文件所在目录；外部工具一律 which/where 自动定位。
  2. **可恢复的错误自动重试，不可恢复的立刻停下并说清怎么办**。错误分七类
     （env/dep/build/test/deploy/permission/lock/timeout），每类有独立的处置策略。
  3. **一切都有日志**。同时写人读文本日志与 JSONL 结构化日志，失败时给出报告文件路径。
  4. **无人值守不等于无人可控**。默认「能自动做的自动做」，涉及不可逆动作（重建 asar、
     改 provider_config.json）一律先做备份；`--dry-run` 可全程预演。

典型用法：
    python autopilot.py                    # 全自动跑通（含部署）
    python autopilot.py --no-deploy        # 只跑到测试，不碰客户端
    python autopilot.py --unattended       # 无人值守：不提问、失败即退出（CI 用）
    python autopilot.py --dry-run          # 预演，不写任何文件
    python autopilot.py --max-retries 5    # 提高可恢复错误的默认重试次数

退出码：
    0   全绿
    1   有步骤失败（详情见汇总报告）
    2   命令行参数写错（步骤名拼错、过滤后无事可做）
    3   环境不满足（Python 版本过低、目录结构不对）
    4   依赖无法自动满足且不可忽略
    5   部署被阻塞（客户端在运行且看护挂不上）
    130 被用户中断
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量：唯一的「事实来源」，全部相对本文件
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent           # 仓库根 = 本脚本所在目录
SCRIPTS = HERE / "skills" / "zcode-tokenspeed" / "scripts"
TESTS = HERE / "tests"
LOGDIR = HERE / "logs"

PY_MIN = (3, 10)
PATCHER = SCRIPTS / "zcode_patcher.py"
DOCTOR = SCRIPTS / "doctor.py"
SYNC = SCRIPTS / "sync.py"
WATCHDOG = SCRIPTS / "apply_after_exit.py"

# 必需与可选的 Python 源文件（构建阶段逐个 py_compile）
REQUIRED_PY_FILES = [
    SCRIPTS / "zcode_patcher.py",
    SCRIPTS / "sync.py",
    SCRIPTS / "doctor.py",
    SCRIPTS / "apply_after_exit.py",
    SCRIPTS / "_console.py",
]

# 步骤标识 -> 标题
STEPS = ["env", "deps", "build", "test", "deploy", "verify"]
STEP_TITLES = {
    "env": "环境自检",
    "deps": "依赖自动安装/校验",
    "build": "构建（语法与产物校验）",
    "test": "自动化测试",
    "deploy": "自动部署（注入客户端）",
    "verify": "部署后验证",
}

# 步骤 -> 入口函数名。`deps` 有意不是 `step_deps`：它的实现是
# `ensure_dependencies()`，因为它还要向调用方**返回依赖清单**（deploy/verify 要用），
# 而其余步骤都是纯粹的「跑一遍或抛异常」。用表驱动，避免 main() 里再手写一遍映射。
STEP_FUNCS = {
    "env": "step_env",
    "deps": "ensure_dependencies",
    "build": "step_build",
    "test": "step_test",
    "deploy": "step_deploy",
    "verify": "step_verify",
}

# 退出码：CI 靠它区分「配置/环境问题」和「代码没过」
EXIT_OK = 0            # 全绿
EXIT_FAILED = 1        # 有步骤失败
EXIT_BAD_ARGS = 2      # 命令行参数写错（步骤名拼错等）——与「测试不过」区分开
EXIT_ENV = 3           # 环境不满足（Python 版本 != 已运行的本进程）
EXIT_DEP = 4           # 依赖无法自动满足且不可忽略
EXIT_DEPLOY_BLOCKED = 5  # 部署被阻塞（客户端在运行且看护挂不上）
EXIT_INTERRUPTED = 130   # 被 Ctrl-C 打断

#: 运行守卫。由 `_import_run_guard()` 在模块加载时尝试填充；填不上则为 None，
#: `_zcode_running()` 会用自带的兜底实现。
_NATIVE_ZCODE_RUNNING = None


def _import_run_guard():
    """从 `zcode_patcher` 借来真正的运行守卫（见 `_zcode_running()` 的说明）。

    有些测试/CI 场景只把本文件单独放进内存跑，并不在 sys.path 上放 scripts 目录；
    拿不到就老实返回 None，让调用方走兜底，而不是让整个模块 import 失败。
    """
    global _NATIVE_ZCODE_RUNNING
    if _NATIVE_ZCODE_RUNNING is not None:
        return _NATIVE_ZCODE_RUNNING
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from zcode_patcher import zcode_running as _fn      # noqa: PLC0415
    except Exception:                                       # noqa: BLE001
        return None
    _NATIVE_ZCODE_RUNNING = _fn
    return _fn


def _native_zcode_running():
    """返回权威守卫的判断结果；不可用时返回 None（区别于「没在运行」的 False）。"""
    guard = _import_run_guard()
    if guard is None:
        return None
    try:
        return bool(guard())
    except Exception:                                       # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 错误分类：决定「自动重试」还是「立刻停下」
# --------------------------------------------------------------------------- #

class ErrorKind:
    """错误类别。前四类可自动重试，后四类重试无意义。"""

    TRANSIENT = "transient"        # 瞬时故障（文件被短暂占用）——重试
    NETWORK = "network"            # 网络抖动——重试
    TIMEOUT = "timeout"            # 超时——重试（可能是机器慢）
    LOCK = "lock"                  # 文件被占用（客户端在跑/杀软扫描）——重试
    ENV = "env"                    # 环境不满足（Python 版本）——不重试
    DEP = "dep"                    # 依赖缺失且装不上——不重试
    BUILD = "build"                # 语法/产物错误——不重试
    TEST = "test"                  # 测试失败——不重试
    PERMISSION = "permission"      # 权限不足——不重试
    UNKNOWN = "unknown"

    #: 值得自动重试的类别
    RETRYABLE = frozenset({TRANSIENT, NETWORK, TIMEOUT, LOCK})

    #: 中文说明，用于日志与报告
    LABELS = {
        TRANSIENT: "瞬时故障", NETWORK: "网络异常", TIMEOUT: "超时", LOCK: "文件被占用",
        ENV: "环境不满足", DEP: "依赖缺失", BUILD: "构建失败", TEST: "测试未通过",
        PERMISSION: "权限不足", UNKNOWN: "未知错误",
    }


def classify(text: str, exit_code: int = 0) -> str:
    """按输出内容与退出码给错误归类。

    分类只影响「要不要重试」，不影响「是否算失败」——不可恢复的错误一样会
    让流水线停下来，只是不浪费时间去重试。
    """
    low = (text or "").lower()

    # --- 权限：先判，因为它的特征串最独特 ---
    if any(k in low for k in ("permission denied", "access is denied", "拒绝访问",
                              "errno 13", "eacces", "operation not permitted")):
        return ErrorKind.PERMISSION

    # --- 文件占用：本项目最常见的可恢复错误 ---
    # 「检测到 ZCode 正在运行」是重点：此时写 asar 必然失败，但等退出后就能成功
    if any(k in low for k in ("检测到 zcode 正在运行", "winerror 32", "winerror 5",
                              "being used by another process", "另一个程序正在使用",
                              "resource busy", "text file busy", "cannot access the file")):
        return ErrorKind.LOCK

    # --- 超时：**必须排在网络之前** ---
    # `run()` 在超时分支返回的是「超时（300s）: <命令>\n<部分输出>」，
    # 后半段里可能含 "connection" / "proxy" 等字样。若把网络判断放在前面，
    # 一次正常的命令超时会被判成网络抖动 —— 虽然两者都会重试，但报告里
    # 给出的修复方向完全不同（「机器慢/加超时」vs「查网络/代理」）。
    # 这里用的是较精确的特征串，所以可以安全地排在 network 之前。
    if any(k in low for k in ("超时", "timed out")) or "timeout" in low:
        return ErrorKind.TIMEOUT

    # --- 网络 ---
    if any(k in low for k in ("connection reset", "connection refused",
                              "temporary failure in name resolution", "ssl",
                              "proxy", "tunnel connection failed", "502", "503", "504",
                              "could not resolve host", "network is unreachable")):
        return ErrorKind.NETWORK

    # --- 环境 ---
    if any(k in low for k in ("python 3.1", "低于最低要求")):
        return ErrorKind.ENV

    # --- 依赖 ---
    if any(k in low for k in ("command not found", "is not recognized", "no such file",
                              "未找到 node", "找不到", "not found",
                              "modulenotfounderror")):
        return ErrorKind.DEP

    # --- 测试：unittest 的结论串优先于构建，否则会被 "failed" 提前吃掉 ---
    if any(k in low for k in ("failed (failures", "failed (errors", "ok\n",
                              "ran ", "traceback (most recent call last)")):
        return ErrorKind.TEST

    # --- 构建 ---
    if any(k in low for k in ("syntaxerror", "syntax error", "py_compile",
                              "invalid syntax")):
        return ErrorKind.BUILD

    if "failed" in low or "failure" in low:
        return ErrorKind.TEST if exit_code else ErrorKind.UNKNOWN

    return ErrorKind.UNKNOWN


# --------------------------------------------------------------------------- #
# 日志：人读文本 + 机器可读 JSONL，双通道
# --------------------------------------------------------------------------- #

class Logger:
    """同时写终端、文本日志、JSONL 结构化日志。

    - 终端：带简单前缀（[+] 成功 / [!] 警告 / [x] 失败），只用 ASCII 标记，
      避免 cp936 管道下的 UnicodeEncodeError。
    - 文本日志：人类排障用，带时间戳。
    - JSONL：CI / 机器消费用，每条一行 JSON，含 step / level / kind / duration。
    """

    def __init__(self, path: Path | None, *, verbose: bool = False,
                 echo: bool = True, color: bool | None = None) -> None:
        self.verbose = verbose
        self.echo = echo
        self.color = _detect_color() if color is None else color
        self.text_path = path
        self.jsonl_path = path.with_suffix(".jsonl") if path else None
        self.entries: list[dict] = []
        self._step: str | None = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 每次运行截断重写，避免日志无限增长
            path.write_text("", encoding="utf-8")
            if self.jsonl_path:
                self.jsonl_path.write_text("", encoding="utf-8")

    # ---- 着色 ----

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    # ---- 步骤游标 ----

    def begin(self, step: str, title: str) -> None:
        self._step = step
        if self.echo:
            print()
            print(self._c("36;1", f"== [{step}] {title} =="))
        self._record("step-begin", f"[{step}] {title}", level="info")

    # ---- 各级输出 ----

    def _emit(self, prefix: str, msg: str, color: str | None) -> None:
        text = f"{prefix} {msg}" if prefix else msg
        if self.echo:
            print(self._c(color, text) if color else text)

    def info(self, msg: str) -> None:
        self._emit("[*]", msg, None)
        self._record("log", msg, level="info")

    def ok(self, msg: str) -> None:
        self._emit("[+]", msg, "32")
        self._record("log", msg, level="ok")

    def warn(self, msg: str) -> None:
        self._emit("[!]", msg, "33")
        self._record("log", msg, level="warn")

    def err(self, msg: str) -> None:
        self._emit("[x]", msg, "31")
        self._record("log", msg, level="error")

    def debug(self, msg: str) -> None:
        """只在 --verbose 时打到终端，但**始终**写日志（便于事后复盘）。"""
        if self.verbose:
            self._emit("[d]", msg, "90")
        self._record("log", msg, level="debug")

    def raw(self, text: str) -> None:
        """原样输出子进程的多行输出（缩进）。"""
        for line in text.rstrip().splitlines():
            if self.echo:
                print(f"    {line}")
        self._record("output", text, level="debug")

    # ---- 落盘 ----

    def _record(self, event: str, message: str, *, level: str = "info",
                kind: str | None = None, extra: dict | None = None) -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "step": self._step,
            "level": level,
            "message": message,
        }
        if kind:
            entry["kind"] = kind
        if extra:
            entry.update(extra)
        self.entries.append(entry)

        if self.text_path:
            try:
                stamp = entry["ts"][11:23]
                step = f"[{self._step}] " if self._step else ""
                with self.text_path.open("a", encoding="utf-8") as f:
                    f.write(f"{stamp} {level.upper():5} {step}{message}\n")
            except OSError:
                pass
        if self.jsonl_path:
            try:
                with self.jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def event(self, event: str, message: str, **kw) -> None:
        """显式记录一次结构化事件（供步骤执行器调用）。"""
        self._record(event, message, **kw)


def _detect_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        return bool(os.environ.get("WT_SESSION") or os.environ.get("ANSICON")
                    or os.environ.get("TERM_PROGRAM")
                    or os.environ.get("ConEmuANSI") == "ON")
    return True


# --------------------------------------------------------------------------- #
# 子进程封装（复用 bootstrap 的思路，但增加超时与错误分类）
# --------------------------------------------------------------------------- #

def run(cmd: list[str], *, cwd: Path | None = None, timeout: float | None = None,
        env_extra: dict | None = None) -> tuple[int, str]:
    """执行子命令，返回 (退出码, 合并输出)。

    - 列表传参、不经 shell：路径含空格/中文安全，也没有 shell 语法差异。
    - Windows 带 CREATE_NO_WINDOW：父进程无控制台时不会新弹 cmd 窗口。
    - 输出按 UTF-8 + errors=replace 解码：cp936 管道下不会打断输出。
    - 112 / 127 / 124 分别代表「找不到命令」「无法执行」「超时」，便于上层分类。
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)

    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, env=env, **kwargs,
        )
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"
    except PermissionError:
        return 126, f"无执行权限: {cmd[0]}"
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", errors="replace")
        return 124, f"超时（{timeout}s）: {' '.join(cmd)}\n{partial}"
    except OSError as exc:
        return 126, f"无法执行 {cmd[0]}: {exc}"

    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    return proc.returncode, out


def which(*names: str) -> str | None:
    """which / where 的跨平台替身：先 PATH，未命中再扫各平台约定目录。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    conv = {
        "nt": ["~/AppData/Local/Programs/Python", "~/scoop/shims",
               "C:/ProgramData/chocolatey/bin", "C:/Program Files/Python313",
               "C:/Program Files/Python312", "C:/Program Files/Python311",
               "C:/Program Files/nodejs"],
        "posix": ["/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin",
                  "/usr/local/opt/python/libexec/bin", "/opt/local/bin",
                  "~/.local/bin", "~/.pyenv/shims", "~/bin"],
    }
    exts = [""]
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        exts = [e.lower() for e in pathext.split(os.pathsep) if e] + [""]
    for raw in conv["nt" if os.name == "nt" else "posix"]:
        try:
            base = Path(raw).expanduser()
        except (RuntimeError, OSError):
            continue
        if not base.is_dir():
            continue
        for name in names:
            for ext in exts:
                cand = base / (name + ext)
                try:
                    if cand.is_file() and os.access(cand, os.X_OK):
                        return str(cand)
                except OSError:
                    continue
    return None


# --------------------------------------------------------------------------- #
# 步骤执行器：带重试、超时、错误分类、耗时统计
# --------------------------------------------------------------------------- #

@dataclass
class StepResult:
    """一个步骤的执行结果。"""

    name: str
    status: str = "pending"          # ok / failed / skipped / warned
    kind: str = ErrorKind.UNKNOWN
    detail: str = ""
    attempts: int = 0
    duration: float = 0.0
    retries_used: int = 0
    fatal: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped", "warned")


class StepFailure(Exception):
    """步骤抛出的失败。带错误类别，可恢复类会自动重试。

    `fatal=False` 只用于「这一步放弃重试、但整个流水线不算失败」的场景（目前没有），
    因此默认 `fatal=True` 的语义是「这一步最终会记为失败」——**不是**「禁止重试」。
    要不要重试完全由 `kind` 是否属于 `ErrorKind.RETRYABLE` 决定。
    """

    def __init__(self, message: str, kind: str = ErrorKind.UNKNOWN,
                 fatal: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.fatal = fatal


def execute_step(step: str, fn, log: Logger, *, max_retries: int,
                 dry_run: bool = False) -> StepResult:
    """跑一个步骤：捕获异常 → 分类 → 可恢复则退避重试 → 记录耗时。

    `fn(log)` 无返回值；失败时抛 `StepFailure`。也可以用 `log` 自行记录非致命问题。
    """
    title = STEP_TITLES.get(step, step)
    log.begin(step, title)
    started = time.time()
    result = StepResult(name=step)
    attempt = 0

    while True:
        attempt += 1
        try:
            fn(log, dry_run)
            result.status = "ok"
            result.attempts = attempt
            result.retries_used = attempt - 1
            break
        except KeyboardInterrupt:
            raise
        except StepFailure as exc:
            result.kind = exc.kind
            result.detail = exc.message
            result.attempts = attempt
            result.fatal = exc.fatal
            # 注意：**只看 kind**。早先写成 `and not exc.fatal`，而 fatal 默认 True，
            # 于是所有可恢复错误都被静默剥夺了重试机会 —— 重试机制形同虚设。
            retryable = exc.kind in ErrorKind.RETRYABLE
            if retryable and attempt <= max_retries:
                delay = min(2 ** (attempt - 1), 8)       # 1s, 2s, 4s, 8s 封顶
                label = ErrorKind.LABELS.get(exc.kind, exc.kind)
                log.warn(f"{label}，{delay}s 后重试（第 {attempt}/{max_retries} 次）")
                log.event("retry", f"{step} 重试 {attempt}/{max_retries}",
                          kind=exc.kind, extra={"delay": delay})
                result.retries_used = attempt
                time.sleep(0 if dry_run else delay)
                continue
            result.status = "failed"
            log.err(exc.message)
            break
        except Exception as exc:                          # noqa: BLE001
            # 未预期的异常：归类后按可恢复性决定重试
            detail = f"{type(exc).__name__}: {exc}"
            kind = classify(detail)
            result.kind = kind
            result.detail = detail
            result.attempts = attempt
            retryable = kind in ErrorKind.RETRYABLE
            if retryable and attempt <= max_retries:
                delay = min(2 ** (attempt - 1), 8)
                log.warn(f"未预期异常（{kind}），{delay}s 后重试：{detail}")
                result.retries_used = attempt
                time.sleep(0 if dry_run else delay)
                continue
            result.status = "failed"
            log.err(f"未预期异常：{detail}")
            if log.verbose:
                log.debug(traceback.format_exc())
            break

    result.duration = time.time() - started
    log.event("step-end", f"[{step}] {result.status}",
              level="ok" if result.ok else "error",
              kind=result.kind,
              extra={"duration": round(result.duration, 3),
                     "attempts": result.attempts,
                     "retries": result.retries_used})
    if result.ok:
        log.ok(f"[{step}] 完成（{result.duration:.1f}s"
               + (f"，重试 {result.retries_used} 次" if result.retries_used else "") + "）")
    return result


# --------------------------------------------------------------------------- #
# 自动依赖安装
# --------------------------------------------------------------------------- #

def _install_node(log: Logger, dry_run: bool) -> str | None:
    """尝试自动安装 Node.js。返回装好后的可执行文件路径，失败返回 None。

    只走「已有包管理器」这条路 —— 不去 curl 下载安装包，因为那在不同发行版上
    差异太大，且静默下载执行二进制对用户不安全。装不上就降级（Node 本来就只是
    可选的语法校验工具）。
    """
    if os.name == "nt":
        cands = [
            (["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS",
              "--accept-source-agreements", "--accept-package-agreements",
              "--silent"], "winget"),
            (["choco", "install", "nodejs-lts", "-y"], "chocolatey"),
            (["scoop", "install", "nodejs-lts"], "scoop"),
        ]
    elif platform.system() == "Darwin":
        cands = [(["brew", "install", "node"], "Homebrew")]
    else:
        cands = [
            (["apt-get", "install", "-y", "nodejs"], "apt-get"),
            (["dnf", "install", "-y", "nodejs"], "dnf"),
            (["yum", "install", "-y", "nodejs"], "yum"),
            (["pacman", "-S", "--noconfirm", "nodejs"], "pacman"),
            (["apk", "add", "nodejs"], "apk"),
        ]

    for cmd, label in cands:
        tool = which(cmd[0])
        if not tool:
            continue
        full = [tool, *cmd[1:]]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0 \
                and cmd[0] in ("apt-get", "dnf", "yum", "pacman", "apk"):
            sudo = which("sudo")
            if sudo:
                full = [sudo, "-n", *full]      # -n：不要交互式要密码
            else:
                log.debug(f"{label} 需要 root，但没有 sudo，跳过")
                continue
        log.info(f"尝试用 {label} 安装 Node：{' '.join(full)}")
        if dry_run:
            log.info("[dry-run] 跳过实际安装")
            return None
        rc, out = run(full, timeout=900)
        if rc == 0:
            found = which("node", "nodejs")
            if found:
                log.ok(f"{label} 安装成功：{found}")
                return found
            log.warn(f"{label} 报告成功，但在 PATH 里仍找不到 node（可能需要重开终端）")
        else:
            log.debug(f"{label} 安装失败（exit={rc}）：{out.strip()[:300]}")
    return None


def ensure_dependencies(log: Logger, dry_run: bool) -> dict:
    """自动安装/校验依赖。返回一份「依赖清单」供后续步骤使用。

    策略：
      * 必需项（Python 标准库）——缺失即失败（说明 Python 安装不完整）
      * 可选项（Node.js）——缺失时**先尝试自动安装**；装不上则降级并继续
    """
    import importlib.util

    report: dict = {"stdlib_ok": False, "node": None, "node_auto_installed": False}

    # --- 1) 标准库：必需 ---
    needed = ["argparse", "json", "hashlib", "struct", "shutil", "subprocess",
              "pathlib", "tempfile", "unicodedata", "urllib.request",
              "dataclasses", "logging", "traceback"]
    missing = [m for m in needed if importlib.util.find_spec(m) is None]
    if missing:
        raise StepFailure(
            "Python 标准库缺失（安装不完整）：" + ", ".join(missing)
            + "\n    请重装官方 Python 发行版。",
            kind=ErrorKind.DEP)
    log.ok(f"标准库完整（抽查 {len(needed)} 个模块）")
    report["stdlib_ok"] = True

    # --- 2) Node：可选，但尽量自动补齐 ---
    node = which("node", "nodejs")
    if node:
        rc, out = run([node, "--version"], timeout=30)
        log.ok(f"Node 已就位：{out.strip() or '?'}  ->  {node}")
    elif dry_run:
        log.info("[dry-run] Node 缺失，将尝试自动安装")
    else:
        log.warn("未找到 Node（用于校验注入脚本语法），尝试自动安装…")
        node = _install_node(log, dry_run)
        if node:
            report["node_auto_installed"] = True
        else:
            log.warn("Node 自动安装未成功 —— 将跳过 JS 语法校验（不影响其余环节）")
            log.info("  手动安装： Windows=winget install OpenJS.NodeJS.LTS  |  "
                     "macOS=brew install node  |  Linux=apt install nodejs")
    report["node"] = node

    # --- 3) 客户端安装位置（只读探测，供部署步骤复用）---
    rc, out = run([sys.executable, str(PATCHER), "--all", "--check"], timeout=180)
    report["client_found"] = rc == 0
    if rc == 0:
        m = re.search(r"客户端版本\s+([\d.]+)", out)
        report["client_version"] = m.group(1) if m else None
        log.ok(f"探测到 ZCode 客户端"
               + (f"（版本 {report['client_version']}）" if report.get("client_version") else ""))
    else:
        log.warn("未探测到 ZCode 客户端 —— 部署步骤会被跳过（代码本身仍可用）")
    return report


# --------------------------------------------------------------------------- #
# 各步骤实现
# --------------------------------------------------------------------------- #

def step_env(log: Logger, dry_run: bool) -> None:
    log.info(f"平台：{platform.system()} {platform.release()} ({platform.machine() or '?'})")
    log.info(f"Python：{platform.python_version()}  ->  {sys.executable}")
    log.info(f"仓库根：{HERE}")

    if sys.version_info < PY_MIN:
        raise StepFailure(
            f"Python {platform.python_version()} 低于最低要求 "
            f"{PY_MIN[0]}.{PY_MIN[1]}。装好新版后重跑即可（无需改动路径）。",
            kind=ErrorKind.ENV)

    if not SCRIPTS.is_dir():
        raise StepFailure(f"找不到脚本目录：{SCRIPTS}\n"
                          f"    请确认本脚本位于仓库根目录（与 skills/ 同级）。",
                          kind=ErrorKind.ENV)
    log.ok(f"目录结构完整，Python 版本达标（>= {PY_MIN[0]}.{PY_MIN[1]}）")

    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        log.warn("当前以 root 运行。打补丁本身不需要 root，"
                 "仅在客户端装在 /opt、/Applications 等受保护位置时才需要 sudo。")


def step_build(log: Logger, dry_run: bool) -> None:
    """构建：语法编译 + 注入产物校验。

    「构建」在这个项目里的含义 = **确认所有脚本语法合法、注入产物能生成**，
    没有编译产物要产出（纯 Python 标准库 + 直接注入的 JS）。
    """
    targets = [p for p in REQUIRED_PY_FILES if p.exists()]
    if not targets:
        raise StepFailure(f"在 {SCRIPTS} 下找不到待校验的 Python 文件。", kind=ErrorKind.BUILD)

    if dry_run:
        log.info(f"[dry-run] py_compile {len(targets)} 个必需文件")
    else:
        log.info(f"py_compile：{len(targets)} 个必需 Python 文件")
        rc, out = run([sys.executable, "-m", "py_compile",
                       *[str(p) for p in targets]], timeout=180)
        if rc != 0:
            raise StepFailure(f"Python 语法错误：\n{out.strip()[:800]}",
                              kind=ErrorKind.BUILD)

    extra = sorted(p for p in SCRIPTS.glob("*.py") if p not in targets)
    if extra:
        if dry_run:
            log.info(f"[dry-run] py_compile 其余 {len(extra)} 个脚本")
        else:
            log.info(f"py_compile：其余 {len(extra)} 个脚本")
            rc, out = run([sys.executable, "-m", "py_compile",
                           *[str(p) for p in extra]], timeout=180)
            if rc != 0:
                raise StepFailure(f"其余脚本语法错误：\n{out.strip()[:800]}",
                                  kind=ErrorKind.BUILD)
    log.ok("Python 语法校验通过")

    # --- 注入脚本（JS）：Node 在位就校验，不在位则跳过 ---
    node = which("node", "nodejs")
    js_files = sorted(SCRIPTS.glob("*.js"))
    if not js_files:
        log.warn("未找到任何注入脚本（*.js）")
    elif not node:
        log.warn(f"跳过 {len(js_files)} 个注入脚本的语法校验（无 Node）—— 属降级，不算失败")
    elif dry_run:
        log.info(f"[dry-run] node --check {len(js_files)} 个注入脚本")
    else:
        log.info(f"node --check：{len(js_files)} 个注入脚本")
        bad = []
        for f in js_files:
            rc, out = run([node, "--check", str(f)], timeout=60)
            if rc != 0:
                bad.append(f"{f.name}: {out.strip()[:200]}")
        if bad:
            raise StepFailure("注入脚本语法错误：\n    " + "\n    ".join(bad),
                              kind=ErrorKind.BUILD)
        log.ok("注入脚本语法校验通过")


def step_test(log: Logger, dry_run: bool) -> None:
    """跑全套回归测试。失败即停 —— 测试不绿就不该部署。"""
    if not TESTS.is_dir():
        raise StepFailure(f"找不到测试目录：{TESTS}", kind=ErrorKind.TEST)

    if dry_run:
        log.info("[dry-run] unittest discover")
        return

    log.info("python -m unittest discover -s tests")
    rc, out = run([sys.executable, "-m", "unittest", "discover", "-s", str(TESTS)],
                  timeout=900)
    tail = out.rstrip().splitlines()[-8:]
    log.raw("\n".join(tail))

    if rc != 0:
        # 从输出里抽一行「Ran N tests」便于报告
        m = re.search(r"Ran (\d+) tests?", out)
        count = f"（共 {m.group(1)} 个用例）" if m else ""
        raise StepFailure(f"回归测试未通过{count}。修掉上面失败的用例后再重跑。",
                          kind=ErrorKind.TEST)
    m = re.search(r"Ran (\d+) tests?", out)
    log.ok(f"回归测试全部通过{('（' + m.group(1) + ' 个用例）') if m else ''}")



def _zcode_running() -> bool:
    """跨平台判断 ZCode 是否在运行。

    ★ 优先复用 `zcode_patcher.zcode_running()` —— 运行守卫是**唯一事实来源**。

    自己再写一份进程探测看着很无害，但它会和主实现悄悄漂移：
    客户端改了进程名、加了 macOS 的 `.app` 包装、或守卫那边补了「正在退出中」
    的宽限期判断，这里的副本都不知道。而这里的判断决定的是「直接写 app.asar」
    还是「转交看护」—— 判错就会绕过运行守卫去动正在运行的文件，
    是整条流水线里后果最严重的一类错误。

    下面的实现只作为**兜底**：万一宿主仓库布局特殊、导入不到主实现也得能跑。
    """
    guard = _native_zcode_running()
    if guard is not None:
        return guard

    if os.name == "nt":
        tasklist = which("tasklist") or "tasklist"
        # 不用 /FI：从 Git Bash/MSYS 启动时 "/FI" 会被路径转换破坏
        rc, out = run([tasklist], timeout=60)
        return "zcode.exe" in out.lower()
    pgrep = which("pgrep")
    if pgrep:
        rc, _ = run([pgrep, "-x", "ZCode"], timeout=30)
        return rc == 0
    rc, out = run(["ps", "-A"], timeout=30)
    return "zcode" in out.lower()


def step_deploy(log: Logger, dry_run: bool, *, unattended: bool,
                deferred_step: dict) -> None:
    """自动部署：把补丁注入客户端。

    ★ 这里解决「无人值守的核心矛盾」：
      补丁写入要求 ZCode **已退出**（否则 app.asar 被占用、配置会被回写覆盖），
      但无人值守场景下 ZCode 恰恰**正在运行**。所以：
        * 客户端没跑 → 立即写入，本步完成
        * 客户端在跑 → **自动挂载退出后看护**（apply_after_exit.py）。
          看护会等到 ZCode 退出、自动写入、再把 ZCode 拉起来 —— 全程无需人工。
          此时本步记为「已排期」，并把它移交给 verify 步骤说明。
    """
    rc, out = run([sys.executable, str(PATCHER), "--all", "--check"], timeout=180)
    if rc != 0:
        log.warn("未探测到 ZCode 客户端 —— 跳过部署（代码本身仍然可用）")
        deferred_step["skipped"] = "未安装客户端"
        return

    if dry_run:
        log.info("[dry-run] 将执行：zcode_patcher.py --all")
        return

    if _zcode_running():
        log.info("检测到 ZCode 正在运行 —— 自动挂载「退出后看护」，无需人工干预")
        if not WATCHDOG.exists():
            raise StepFailure(f"缺少看护脚本：{WATCHDOG}", kind=ErrorKind.DEP)

        # 看护的 --want 参数与 sync.py 保持同一套键名
        wants = [
            "--want=usage_chart=on",
            "--want=tps_footer=on", "--want=enhance_prompt=on",
            "--want=model_puller=on",
        ]
        flags = 0x00000008 | 0x08000000 if os.name == "nt" else 0   # DETACHED|NO_WINDOW
        try:
            kwargs: dict = {"cwd": str(SCRIPTS),
                            "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([sys.executable, str(WATCHDOG), *wants], **kwargs)
        except Exception as exc:                          # noqa: BLE001
            raise StepFailure(
                f"挂载退出后看护失败：{type(exc).__name__}: {exc}\n"
                f"    请完全退出 ZCode 后手动执行：\n"
                f"    {sys.executable} {PATCHER} --all",
                kind=ErrorKind.LOCK)

        deferred_step["scheduled"] = True
        log.ok("看护已挂载 —— 你下次**完全退出 ZCode** 时会自动写入并重新拉起客户端")
        log.info("  （这期间可以正常使用 ZCode，不会被打断）")
        return

    # ---- 客户端没在跑：立即写入 ----
    log.info("ZCode 未运行，直接写入补丁")
    rc, out = run([sys.executable, str(PATCHER), "--all"], timeout=1800)
    if rc == 0:
        log.ok("补丁已写入客户端")
    else:
        kind = classify(out, rc)
        if kind == ErrorKind.LOCK:
            # 运行守卫在写入过程中发现客户端被拉起 —— 退回看护路线
            log.warn("写入途中检测到客户端被拉起，改走「退出后看护」")
            step_deploy(log, dry_run=False, unattended=unattended,
                        deferred_step=deferred_step)
            return
        raise StepFailure(f"写入失败（exit={rc}）：\n{out.strip()[:800]}", kind=kind)


def step_verify(log: Logger, dry_run: bool, *, deferred_step: dict,
                deps: dict) -> None:
    """部署后验证：只读复核每一项的实际状态。"""
    if dry_run:
        log.info("[dry-run] 跳过验证")
        return
    if deferred_step.get("skipped"):
        log.warn(f"跳过验证（{deferred_step['skipped']}）")
        return

    rc, out = run([sys.executable, str(PATCHER), "--all", "--check"], timeout=180)
    if rc != 0:
        log.warn("无法复核状态（未探测到客户端）")
        return

    # 汇总表里「√ / 失败」的行就是每项的结论
    rows = [ln.strip() for ln in out.splitlines()
            if ln.strip().startswith("√") or ln.strip().startswith("×")]
    for r in rows:
        log.raw(r)
    total = len(rows)
    bad = [r for r in rows if r.startswith("×")]

    if deferred_step.get("scheduled"):
        log.warn("本次为「已排期」部署：补丁会在 ZCode 完全退出后写入，"
                 "现在复核到的仍是旧状态，属预期")
        log.info(f"  排期项：{total} 项；想现在就生效：完全退出 ZCode 即可（看护会自动拉起）")
        return

    if bad:
        log.warn(f"有 {len(bad)} 项未生效（可能是上游版本变更导致锚点不匹配）")
    if "版本旧" in out or "旧版" in out:
        log.warn("检测到注入的是旧版组件 —— 看护写入时会自动更新为新版")
    else:
        log.ok(f"状态复核完成（{total} 项）")


# --------------------------------------------------------------------------- #
# 汇总报告
# --------------------------------------------------------------------------- #

def build_summary(results: list[StepResult], *, elapsed: float,
                  log_path: Path | None, deps: dict,
                  deferred_step: dict) -> str:
    """生成人读的汇总报告。失败时这份内容会同时落到 stdout 与日志文件。"""
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f"zcode-toolkit 自动化流水线 · 汇总（耗时 {elapsed:.1f}s）")
    lines.append("=" * 68)

    icon = {"ok": "[+]", "warned": "[!]", "skipped": "[-]", "failed": "[x]", "pending": "[ ]"}
    for r in results:
        title = STEP_TITLES.get(r.name, r.name)
        dur = f"{r.duration:6.1f}s"
        retry = f" 重试{r.retries_used}次" if r.retries_used else ""
        lines.append(f"  {icon.get(r.status, '[?]')} {r.name:7} {title:24} {dur}{retry}")
        if r.status == "failed":
            kind_label = ErrorKind.LABELS.get(r.kind, r.kind)
            lines.append(f"        原因（{kind_label}）：{r.detail.splitlines()[0][:110]}")

    failed = [r for r in results if r.status == "failed"]
    lines.append("-" * 68)

    # 环境/依赖信息
    if deps:
        parts = []
        if deps.get("client_version"):
            parts.append(f"客户端 {deps['client_version']}")
        if deps.get("node"):
            parts.append("Node " + ("（本次自动安装）" if deps.get("node_auto_installed")
                                    else "已就位"))
        else:
            parts.append("Node 缺失（已降级）")
        lines.append("  环境：" + " | ".join(parts))

    # 部署状态
    if deferred_step.get("scheduled"):
        lines.append("  部署：已排期 —— 完全退出 ZCode 时自动写入并重新拉起（无需人工）")
    elif deferred_step.get("skipped"):
        lines.append(f"  部署：跳过（{deferred_step['skipped']}）")

    if log_path:
        lines.append(f"  日志：{log_path}")
        lines.append(f"        {log_path.with_suffix('.jsonl')}（机器可读）")

    lines.append("-" * 68)
    if failed:
        lines.append(f"  结论：失败 {len(failed)} 步 —— "
                     + "、".join(r.name for r in failed))
        lines.append("")
        lines.append("  怎么修：")
        for r in failed:
            lines.append(f"    · [{r.name}] {ErrorKind.LABELS.get(r.kind, r.kind)}"
                         f"：{r.detail.splitlines()[0][:100]}")
            hint = _remedy(r)
            if hint:
                lines.append(f"       -> {hint}")
    else:
        lines.append("  结论：全部通过" + ("（部署已排期，退出 ZCode 即生效）"
                                          if deferred_step.get("scheduled") else ""))
    lines.append("=" * 68)
    return "\n".join(lines)


def _remedy(result: StepResult) -> str:
    """按错误类别给出可操作的下一步。"""
    return {
        ErrorKind.ENV: "装好 Python >= 3.10 后重跑本脚本（不需要改任何路径）",
        ErrorKind.DEP: "安装缺失的依赖后重跑；本项目的必需依赖只有 Python 标准库",
        ErrorKind.BUILD: "看上面的语法错误行号，改完代码重跑",
        ErrorKind.TEST: "运行 python -m unittest discover -s tests -v 看具体哪个用例失败",
        ErrorKind.PERMISSION: "用管理员 / sudo 重跑，或把仓库放到有写权限的目录",
        ErrorKind.LOCK: "确保 ZCode 完全退出（Windows 托盘右键退出；macOS 用 Cmd+Q）后重跑",
        ErrorKind.NETWORK: "检查网络或代理设置；CI 环境请确认能访问依赖源",
        ErrorKind.TIMEOUT: "机器较慢或被杀软拖慢，可加大超时或用 --max-retries 提高重试次数",
        ErrorKind.TRANSIENT: "重跑一次通常即可；持续出现请跑 doctor.py 定位",
    }.get(result.kind, "加 --verbose 重跑，日志里有完整堆栈")


def _exit_code_for(result: StepResult) -> int:
    """把「哪一步、因为什么挂的」映射成精确的退出码。

    这一步看着多余（反正都是失败），但对外部编排者是关键信息：
    容器/CI 可以据此决定「重跑一次」还是「先去修环境」。
    """
    if result.kind == ErrorKind.ENV:
        return EXIT_ENV
    if result.kind == ErrorKind.DEP:
        return EXIT_DEP
    if result.name == "deploy" and result.kind == ErrorKind.LOCK:
        return EXIT_DEPLOY_BLOCKED
    return EXIT_FAILED


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="autopilot.py",
        description="zcode-toolkit 全自动流水线：环境自检 → 自动装依赖 → 构建 → 测试 → 部署 → 验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python autopilot.py                     # 全自动（含部署）\n"
               "  python autopilot.py --no-deploy         # 只构建 + 测试，不碰客户端\n"
               "  python autopilot.py --unattended        # 无人值守（CI 用，不提问、失败即退）\n"
               "  python autopilot.py --dry-run           # 全程预演，不写任何文件\n"
               "  python autopilot.py --only build,test   # 只跑指定步骤\n"
               "  python autopilot.py --report out.md     # 额外把汇总写成文件",
    )
    ap.add_argument("--only", metavar="STEPS",
                    help=f"只执行指定步骤（逗号分隔）。可选：{','.join(STEPS)}")
    ap.add_argument("--skip", metavar="STEPS", help="跳过指定步骤（逗号分隔）")
    ap.add_argument("--no-deploy", action="store_true",
                    help="不做部署（等价于 --skip deploy,verify）")
    ap.add_argument("--unattended", action="store_true",
                    help="无人值守模式：不提问、任何步骤失败立即停止（CI 默认）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将要执行的动作，不写任何文件")
    ap.add_argument("--max-retries", type=int, default=3, metavar="N",
                    help="可恢复错误（占用/超时/网络）的最大重试次数，默认 3")
    ap.add_argument("--log", metavar="PATH", default=None,
                    help="日志文件路径（默认 logs/autopilot-<时间戳>.log）")
    ap.add_argument("--report", metavar="PATH", default=None,
                    help="额外把汇总报告写到指定文件")
    ap.add_argument("--verbose", action="store_true", help="打印调试细节")
    ap.add_argument("--quiet", action="store_true", help="只在终端打最终结论")
    return ap


def parse_steps(text: str | None, valid: list[str], what: str) -> list[str]:
    if not text:
        return []
    items = [s.strip().lower() for s in text.split(",") if s.strip()]
    unknown = [s for s in items if s not in valid]
    if unknown:
        print(f"[x] {what} 中出现未知步骤：{', '.join(unknown)}")
        print(f"    可选：{', '.join(valid)}")
        # 用专用退出码：CI 里「参数写错」和「测试没过」是两件完全不同的事，
        # 都返回 1 会让人往错误方向查半天日志。
        raise SystemExit(EXIT_BAD_ARGS)
    return items


def main() -> int:
    args = build_parser().parse_args()

    # ---- 决定要跑哪些步骤 ----
    steps = list(STEPS)
    if args.no_deploy:
        steps = [s for s in steps if s not in ("deploy", "verify")]
    only = parse_steps(args.only, STEPS, "--only")
    if only:
        steps = [s for s in only if s in steps or not args.no_deploy]
    skip = parse_steps(args.skip, STEPS, "--skip")
    steps = [s for s in steps if s not in skip]
    if not steps:
        print("[x] 所有步骤都被过滤掉了，无事可做。")
        return EXIT_BAD_ARGS

    # ---- 日志 ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = Path(args.log) if args.log else (LOGDIR / f"autopilot-{stamp}.log")
    log = Logger(None if args.dry_run else log_path,
                 verbose=args.verbose, echo=not args.quiet)

    started = time.time()
    log.info("zcode-toolkit 全自动流水线")
    log.info(f"步骤：{' → '.join(f'{s}({STEP_TITLES[s]})' for s in steps)}")
    log.info(f"模式：{'dry-run（不写盘）' if args.dry_run else '实际执行'}"
             + ("，无人值守" if args.unattended else "")
             + f"，可恢复错误最多重试 {args.max_retries} 次")
    if not args.dry_run:
        log.info(f"日志：{log_path}")

    deps: dict = {}
    deferred_step: dict = {}
    results: list[StepResult] = []

    # 步骤 -> 可调用对象。deploy/verify 需要额外上下文，用 lambda 绑定。
    callbacks = {
        "env": lambda lg, dr: step_env(lg, dr),
        "deps": lambda lg, dr: deps.update(ensure_dependencies(lg, dr) or {}),
        "build": lambda lg, dr: step_build(lg, dr),
        "test": lambda lg, dr: step_test(lg, dr),
        "deploy": lambda lg, dr: step_deploy(lg, dr, unattended=args.unattended,
                                             deferred_step=deferred_step),
        "verify": lambda lg, dr: step_verify(lg, dr, deferred_step=deferred_step,
                                             deps=deps),
    }

    exit_code = 0
    try:
        for name in steps:
            res = execute_step(name, callbacks[name], log,
                               max_retries=args.max_retries, dry_run=args.dry_run)
            results.append(res)

            if res.status == "failed":
                # 后面的步骤大多依赖前面的结果，失败即停（fail-fast）
                remaining = [s for s in steps[steps.index(name) + 1:]]
                if remaining:
                    log.warn(f"后续步骤依赖本步结果，已跳过：{', '.join(remaining)}")
                    for s in remaining:
                        results.append(StepResult(name=s, status="skipped",
                                                  detail="前序步骤失败"))
                # 退出码按「哪一步挂的」细化：CI 与外部看护靠它决定如何响应
                # （例如 env/dep 挂了该去装环境，而不是回头翻测试日志）。
                exit_code = _exit_code_for(res)
                break
    except KeyboardInterrupt:
        log.warn("被用户中断")
        exit_code = EXIT_INTERRUPTED
    except Exception as exc:                              # noqa: BLE001
        log.err(f"编排器自身异常：{type(exc).__name__}: {exc}")
        if args.verbose:
            log.debug(traceback.format_exc())
        exit_code = EXIT_FAILED

    elapsed = time.time() - started
    # 注意：`log_path` 在 dry-run 下也传真实值 —— 报告要打印日志路径供人按图索骥。
    # 早先写成 `None if args.dry_run else log_path`，dry-run 时报告里就没有日志路径；
    # 而 dry-run 恰恰不写文件，正是最需要给出「真跑时日志会落在哪里」的时候。
    report = build_summary(results, elapsed=elapsed,
                           log_path=log_path,      # dry-run 也给出，供人知道真跑时日志在哪
                           deps=deps, deferred_step=deferred_step)
    print()
    print(report)

    log.event("summary", "流水线结束", level="ok" if exit_code == 0 else "error",
              extra={"exit_code": exit_code, "elapsed": round(elapsed, 3),
                     "failed": [r.name for r in results if r.status == "failed"]})

    if args.report and not args.dry_run:
        try:
            Path(args.report).write_text(report + "\n", encoding="utf-8")
            print(f"\n[*] 汇总报告已写入：{args.report}")
        except OSError as exc:
            print(f"[!] 汇总报告写入失败：{exc}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
