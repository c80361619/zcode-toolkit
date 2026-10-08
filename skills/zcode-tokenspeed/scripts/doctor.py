#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""zcode-tokenspeed 安装自检（doctor）——只读，不改任何文件。

用途：在「插件装了但功能没生效」时，一条命令跑出完整链路诊断：

    python doctor.py            # 完整体检
    python doctor.py --where    # 只打印「插件装在哪」以及扫过哪些目录
    python doctor.py --json     # 机器可读输出

⚠️ 相对路径是相对**当前所在目录**解析的。请在**克隆下来的仓库**里跑，
   或者用绝对路径：
       python "%USERPROFILE%\zcode-toolkit\skills\zcode-tokenspeed\scripts\doctor.py"
   （macOS / Linux：`python3 ~/zcode-toolkit/skills/zcode-tokenspeed/scripts/doctor.py`）
   若想直接跑**已安装的那份**，先 `doctor.py --where` 看命中路径 —— 注意 GitHub 来源
   的市场是 `cli/plugins/cache/<市场名>/<插件名>/<版本>/`，脚本在版本目录里面。

它会按顺序检查并打印结论：
  1. Python 环境（版本 / 解释器路径 / 是否满足 3.10+）
  2. ZCode 安装位置、app.asar、客户端版本
  3. ZCode 是否正在运行（运行中无法应用重打包级补丁）
  4. 插件是否真的「已安装」与「已启用」（两步缺一不可，钩子只在启用后进入新会话）
  5. 插件配置里的开关有没有被保存过（没保存过 → 同步脚本按设计什么也不做）
  6. hooks/hooks.json 是否存在、命令是否可执行
  7. 钩子到底跑没跑过（_sync.last / _sync.log 心跳与日志）
  7.5 退出后看护有没有真的等到 ZCode 退出（决定补丁到底写没写进去）
  8. 七项补丁当前在客户端里的实际状态

输出末尾给出「结论」，直接指出卡在哪一环、下一步该做什么。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCHER = HERE / "zcode_patcher.py"
LOG = HERE / "_sync.log"
STAMP = HERE / "_sync.last"
# 退出后看护的日志（apply_after_exit.py 写）。抽成常量是为了测试能替换它 ——
# 这一节要断言的正是「看护起来了却没等到退出」这种只体现在日志里的状态。
WATCHDOG_LOG = HERE / "_apply_after_exit.log"

try:                                   # 控制台编码/窗口安全网（见 _console.py 的说明）
    from _console import no_window_kwargs, safe_stdio
except ImportError:                    # 被别处 import 时脚本目录可能不在 sys.path
    sys.path.insert(0, str(HERE))
    from _console import no_window_kwargs, safe_stdio

PLUGIN_NAME = "zcode-tokenspeed"
REPO_NAME = "zcode-toolkit"
MIN_PY = (3, 10)

PATCH_KEYS = [
    ("reasoning_config", "思考档位配置"),
    ("usage_chart", "用量页去截断"),
    ("model_width", "模型弹窗加宽"),
    ("tps_footer", "TPS 状态栏"),
    ("thought_slider", "思考强度滑条"),
    ("enhance_prompt", "增强提示词"),
    ("model_puller", "模型拉取按钮"),
]
# 「重打包级」= 要改写整个 app.asar（其余几项只改 provider_config.json / 字节级原地覆盖）。
# 注意：**生效时机上八项没有区别** —— zcode_patcher.py 只要发现 ZCode.exe 在跑就一律拒绝写入，
# 所以都得等退出后由看护写。这个集合只用来区分「改的东西有多重」，不再代表「要不要重启」。
REPACK_KEYS = {"tps_footer", "thought_slider", "enhance_prompt", "model_puller"}

OK, WARN, BAD, INFO = "  [√]", "  [!]", "  [×]", "  [i]"


# ------------------------------------------------------------------ 基础工具

def hr(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 62 - len(title)))


def _load_patcher():
    """把同目录的 zcode_patcher 当模块用（复用它的安装探测逻辑）。"""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import zcode_patcher  # noqa: PLC0415

    return zcode_patcher


def _storage_roots() -> list[Path]:
    """ZCode 数据目录候选（默认 ~/.zcode，config 里 storage.dir 可改写）。"""
    roots: list[Path] = [Path.home() / ".zcode"]
    for cand in (Path.home() / ".zcode" / "cli" / "config.json",):
        cfg = _read_json(cand)
        d = ((cfg or {}).get("storage") or {}).get("dir") or ""
        if isinstance(d, str) and d.strip():
            p = Path(os.path.expanduser(d.strip()))
            if p not in roots:
                roots.append(p)
    return roots


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find_configs() -> list[Path]:
    out: list[Path] = []
    for root in _storage_roots():
        p = root / "cli" / "config.json"
        if p.is_file() and p not in out:
            out.append(p)
    return out


def _installed_plugin_dirs() -> list[Path]:
    """找出本插件实际安装在哪。

    插件副本有三套落点，**都不能靠目录名硬猜**：
      * `<数据>/cli/plugins/marketplaces/<市场 id>/`         —— directory 来源的市场：市场根即插件根
      * `<数据>/cli/plugins/cache/<市场名>/<插件名>/<版本>/`  —— GitHub/URL 来源：**插件根在版本目录里**
      * `<数据>/cli/plugins/cache/<市场名>/plugins/<插件名>/` —— 市场仓库里带 plugins/ 子目录时
    所以这里改成「按清单里的 name 认」——把候选目录都扫一遍，
    谁的 plugin.json 里 name 匹配就是它。
    """
    found: list[Path] = []
    for cand in _candidate_roots():
        if cand in found:
            continue
        manifest = _read_manifest(cand)
        if manifest and manifest.get("name") == PLUGIN_NAME:
            found.append(cand)
    return found


def _plugin_bases() -> list[Path]:
    """`<数据目录>/cli/plugins` 候选。"""
    out = []
    for root in _storage_roots():
        p = root / "cli" / "plugins"
        if p.is_dir():
            out.append(p)
    return out


def _candidate_roots() -> list[Path]:
    """所有可能藏着插件清单的目录（市场缓存 + 市场子目录 + 配置里 plugins.dirs）。"""
    out: list[Path] = []

    def add_children(base: Path, depth: int) -> None:
        if depth <= 0 or not base.is_dir():
            return
        try:
            kids = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for k in kids:
            out.append(k)
            add_children(k, depth - 1)

    for base in _plugin_bases():
        for sub in ("marketplaces", "cache"):
            d = base / sub
            if not d.is_dir():
                continue
            try:
                markets = sorted(p for p in d.iterdir() if p.is_dir())
            except OSError:
                continue
            for m in markets:
                out.append(m)          # source: "./" → 市场根即插件根
                add_children(m, 2)     # source: "./plugins/x" 等更深一层
    # 配置里显式登记的插件目录
    for cfg_path in _find_configs():
        dirs = (((_read_json(cfg_path) or {}).get("plugins") or {}).get("dirs")) or []
        if isinstance(dirs, list):
            for d in dirs:
                if isinstance(d, str) and d.strip():
                    p = Path(os.path.expanduser(d.strip()))
                    out.append(p)
                    add_children(p, 1)
    return out


def _read_manifest(root: Path) -> dict | None:
    """读插件清单（按内核的查找顺序）。"""
    for rel in (".zcode-plugin/plugin.json", ".claude-plugin/plugin.json",
                ".codex-plugin/plugin.json"):
        m = _read_json(root / rel)
        if isinstance(m, dict) and m.get("name"):
            return m
    return None


def _prefix_entries(cfg, section: str) -> dict:
    """取 config.json 里 plugins.<section> 下属于本插件的条目。

    键的格式是 `<插件名>@<市场名>`（实测 `computer-use@zcode-plugins-official`）。
    只认 `<插件名>@…`，不要用裸 startswith —— 否则 `zcode-tokenspeed-legacy@m`
    这种别的插件会被误算进来。
    """
    block = ((cfg or {}).get("plugins") or {}).get(section) or {}
    if not isinstance(block, dict):
        return {}
    return {k: v for k, v in block.items()
            if str(k) == PLUGIN_NAME or str(k).startswith(PLUGIN_NAME + "@")}


def _manifest_version(root: Path) -> str:
    m = _read_manifest(root)
    return str(m["version"]) if m and m.get("version") else "?"


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def print_where(verbose: bool = False) -> int:
    """只打印「插件到底装在哪」——命中本插件的目录，以及可直接复制的命令。

    专治「照着 README 敲命令却报 No such file or directory」：
    命令行的相对路径是相对**当前目录**解析的，而插件的真实根目录往往比用户以为的深一层
    （GitHub 来源的市场是 `cache/<市场名>/<插件名>/<版本>/`）。
    这个模式把命中结果直接摊开，用户一眼就能看到该用哪个绝对路径。

    默认**只列命中项**：本机实测候选目录有 120 个（别的市场/插件一大堆），
    全列出来会把答案淹没。想看全量加 `--where-all`。
    """
    hr("插件位置扫描（--where）")
    roots = _candidate_roots()
    print(f"{INFO} 数据目录候选：")
    for r in _storage_roots():
        print(f"        {r}{'' if r.is_dir() else '   (不存在)'}")

    hits: list[Path] = []
    others: list[tuple[Path, str]] = []
    for c in roots:
        m = _read_manifest(c)
        if m and m.get("name") == PLUGIN_NAME:
            hits.append(c)
        elif m:
            others.append((c, str(m.get("name"))))

    if hits:
        print(f"\n{OK} 命中 {len(hits)} 份 {PLUGIN_NAME} 副本"
              f"（候选目录共 {len(roots)} 个，其余 {len(others)} 个是别的插件）：")
        for c in hits:
            print(f"    {c}")
            print(f"        清单版本 {_manifest_version(c)}"
                  f"   脚本目录 {c / 'skills' / PLUGIN_NAME / 'scripts'}")
        print(f"\n{INFO} 直接用绝对路径跑完整自检（复制下面这条）：")
        print(f'       python "{hits[0] / "skills" / PLUGIN_NAME / "scripts" / "doctor.py"}"')
        print(f"{INFO} 注意：GitHub 来源的市场缓存成 cache/<市场名>/<插件名>/<版本>/，")
        print("       脚本在**版本目录**里面 —— 站在 cache/<市场名> 这一层是找不到 skills/ 的。")
    else:
        print(f"\n{BAD} 没有任何候选目录的清单 name == {PLUGIN_NAME} —— 插件没装成功")
        print(f"       → 「设置 → 插件 → 创建 → 添加插件市场」填 {REPO_NAME}，再点安装")

    if verbose or not hits:
        label = "全部候选目录" if verbose else "扫过的候选目录（供你确认路径拼写）"
        print(f"\n{INFO} {label}（共 {len(roots)} 个）：")
        for c in roots:
            m = _read_manifest(c)
            if m and m.get("name") == PLUGIN_NAME:
                print(f"{OK} {c}")
            elif m:
                print(f"    {c}   （其它插件：{m.get('name')}）")
            else:
                print(f"    {c}")
    elif not verbose:
        print(f"\n{INFO} 想看扫过的全部 {len(roots)} 个候选目录：`doctor.py --where-all`")
    return 0


def _enabled_state() -> bool:
    enabled = False
    for cfg_path in _find_configs():
        for _, v in _prefix_entries(_read_json(cfg_path), "enabledPlugins").items():
            enabled = enabled or bool(v)
    return enabled


def _saved_options() -> dict:
    """合并所有 config.json 里保存过的插件开关。"""
    merged: dict = {}
    for cfg_path in _find_configs():
        opts = _prefix_entries(_read_json(cfg_path), "options")
        for _, v in opts.items():
            if isinstance(v, dict):
                merged.update(v)
    return merged


# ------------------------------------------------------------------ 各项检查

def check_python() -> bool:
    hr("1. Python 环境")
    ok = sys.version_info >= MIN_PY
    print(f"{OK if ok else BAD} {sys.version.split()[0]}  ({sys.executable})")
    if not ok:
        print(f"{BAD} 需要 Python {MIN_PY[0]}.{MIN_PY[1]} 或更高版本")
    else:
        print(f"{INFO} 钩子命令是 `python ... || python3 ...`；"
              "命令行里 `python --version` 能跑通即可被调用")
    return ok


def check_zcode() -> tuple[bool, Path | None, str]:
    hr("2. ZCode 客户端")
    try:
        zp = _load_patcher()
        targets = zp.resolve_target(None)
    except SystemExit as e:
        print(f"{BAD} 未找到 ZCode：{e}")
        return False, None, ""
    except Exception as e:  # pragma: no cover
        print(f"{BAD} 探测失败：{e!r}")
        return False, None, ""

    if not targets:
        print(f"{BAD} 未找到 zcode.cjs，请确认 ZCode 已安装")
        return False, None, ""

    asar = targets[0].parent.parent / "app.asar"
    ver = ""
    try:
        ver = str(zp.asar_version(asar) or "")
    except Exception:
        pass

    for t in targets:
        a = t.parent.parent / "app.asar"
        if a.is_file():
            print(f"{OK} {a}  ({a.stat().st_size / 1048576:.0f} MB)")
        else:
            print(f"{BAD} {a} 不存在")
    if ver:
        print(f"{INFO} 客户端版本：{ver}")
    return asar.is_file(), asar, ver


def check_running() -> bool:
    hr("3. 客户端运行状态")
    running = False
    try:
        running = _load_patcher().zcode_running()
    except Exception:
        pass
    if running:
        print(f"{WARN} ZCode 正在运行")
        print("       字节级补丁可热改；重打包级补丁必须等 ZCode 完全退出后才由看护进程应用")
    else:
        print(f"{OK} ZCode 未运行（可以安全打补丁）")
    return running


def check_plugin() -> tuple[bool, list[Path], bool]:
    hr("4. 插件安装与启用")
    dirs = _installed_plugin_dirs()
    if not dirs:
        print(f"{BAD} 没有找到已安装的插件目录（按清单里的 name 认，扫过 "
              f"{len(_candidate_roots())} 个候选目录）")
        print("       常见落点：")
        print("         <数据>/cli/plugins/cache/<市场名>/<插件名>/<版本>/    ← GitHub/URL 市场")
        print("         <数据>/cli/plugins/marketplaces/<市场 id>/            ← 本地目录市场")
        print(f"       → 「设置 → 插件 → 创建 → 添加插件市场」添加 {REPO_NAME} 后安装")
        print("       → 或直接跑 `doctor.py --where` 看它到底扫了哪些目录")
        return False, [], False

    for d in dirs:
        print(f"{OK} 安装位置：{d}")
        print(f"{INFO} 清单版本：{_manifest_version(d)}")
    if len(dirs) > 1:
        newest = max(dirs, key=_mtime)
        print(f"{WARN} 发现 {len(dirs)} 份副本；ZCode 一般加载最新的一份：{newest}")

    cfgs = _find_configs()
    if not cfgs:
        print(f"{BAD} 读不到 {Path.home() / '.zcode' / 'cli' / 'config.json'}")
        return True, dirs, False

    enabled = False
    for cfg_path in cfgs:
        cfg = _read_json(cfg_path) or {}
        plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
        master = plugins.get("enabled")
        if master is False:
            print(f"{BAD}   plugins.enabled = false —— **插件子系统总开关被关掉了，一切都不生效**")
            print("       → 把 ~/.zcode/cli/config.json 里 plugins.enabled 改为 true（或删掉该键）")
        else:
            print(f"{OK}   plugins.enabled 未关闭（缺省即开启）")
        en = _prefix_entries(cfg, "enabledPlugins")
        print(f"{INFO} {cfg_path}")
        if not en:
            print(f"{BAD}   plugins.enabledPlugins 里没有 {PLUGIN_NAME}* —— 插件未登记启用状态")
        for k, v in en.items():
            print(f"{OK if v else BAD}   {k} = {v}")
            enabled = enabled or bool(v)

    if enabled:
        print(f"{OK} 插件已启用")
    else:
        print(f"{BAD} 插件未处于「已启用」——**钩子不会进入会话，自动化全部不会发生**")
        print("       → 「设置 → 插件 → 管理已安装」把该插件的开关打开")
    return True, dirs, enabled


def check_options() -> bool:
    hr("5. 插件开关是否已保存")
    saved = _saved_options()
    if not saved:
        print(f"{INFO} config.json 的 plugins.options 里没有 {PLUGIN_NAME}* —— 从未保存过配置")
        print("       **这不是故障**：同步脚本会退回插件清单声明的默认值（全开）自动注入，")
        print("       所以装完重启、开个新会话就能用，不需要打开配置页。")
        print("       想关掉个别功能，再到「高级信息 → 配置」拨成关并保存（保存值优先于默认值）。")
        return False
    print(f"{INFO} 已保存的开关：")
    on_keys = []
    for key, label in PATCH_KEYS:
        if key in saved:
            on = bool(saved[key])
            tag = "（需 ZCode 完全退出后写入）" if on else ""
            print(f"        {label:<14} {key} = {saved[key]}{tag}")
            if on:
                on_keys.append(key)
    if "core_patch" in saved:
        print(f"        {'思考档位内核补丁':<14} core_patch = {saved['core_patch']}")
    unknown = [k for k in saved if k not in dict(PATCH_KEYS) and k != "core_patch"]
    for k in unknown:
        print(f"        (未知键) {k} = {saved[k]}")
    if on_keys:
        repack = [k for k in on_keys if k in REPACK_KEYS]
        extra = f"，其中 {len(repack)} 项属重打包级" if repack else ""
        print(f"{WARN} 以上 {len(on_keys)} 项都要等 ZCode **完全退出**后由看护写入"
              f"（客户端运行时会拒绝改写 app.asar 与 provider_config.json，"
              f"而会话钩子必然在运行中触发）{extra}；写完会自动重启 ZCode")
    print(f"{OK} 有已保存的开关")
    return True


def check_hook(dirs: list[Path]) -> bool:
    hr("6. 钩子文件")
    if not dirs:
        print(f"{BAD} 跳过：插件未安装")
        return False
    ok = False
    for d in dirs:
        hf = d / "hooks" / "hooks.json"
        if not hf.is_file():
            print(f"{BAD} {hf} 不存在")
            continue
        print(f"{OK} {hf}")
        events = ((_read_json(hf) or {}).get("hooks") or {})
        for ev, groups in events.items():
            for g in groups or []:
                for h in (g or {}).get("hooks") or []:
                    print(f"        {ev}  matcher={g.get('matcher', '(默认)')}")
                    print(f"        type={h.get('type')}  shell={h.get('shell')}  "
                          f"timeout={h.get('timeout') or h.get('timeoutMs')}")
                    print(f"        command: {h.get('command', '')}")
                    ok = True
        sync = d / "skills" / PLUGIN_NAME / "scripts" / "sync.py"
        print(f"{OK if sync.is_file() else BAD} 目标脚本：{sync}")
    return ok


def check_heartbeat(dirs: list[Path]) -> tuple[bool, dict]:
    hr("7. 钩子执行痕迹")
    fired = False
    for d in dirs:
        scripts = d / "skills" / PLUGIN_NAME / "scripts"
        stamp, logf = scripts / STAMP.name, scripts / LOG.name
        if stamp.is_file():
            fired = True
            print(f"{OK} 心跳 {stamp} → "
                  f"{stamp.read_text(encoding='utf-8', errors='replace').strip()}")
        else:
            print(f"{WARN} 没有心跳文件 {stamp}")
        if logf.is_file():
            fired = True
            lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"{OK} 日志 {logf}（{len(lines)} 行，末尾 5 行）")
            for ln in lines[-5:]:
                print(f"        {ln}")
        else:
            print(f"{WARN} 没有日志文件 {logf}")
    if not fired:
        print(f"{BAD} 已安装副本里没有任何执行痕迹 —— 钩子**从未运行过**")
        print("       常见原因：插件没启用 / 保存配置后没开过新会话 / python 不在 PATH /")
        print("                 装的是改动前的旧版本（旧版钩子没有心跳文件）")
    # 心跳只能证明「跑没跑」；要判断「为什么没跑」，得看 ZCode 自己的日志
    log_info = report_zcode_log()
    if STAMP.is_file():
        print(f"{INFO} 本目录（源码仓库）心跳："
              f"{STAMP.read_text(encoding='utf-8', errors='replace').strip()}")
    return fired, log_info


def check_watchdog() -> None:
    """第 7.5 节：退出后看护的「等 vs 写」状态。

    ★ 这一节是为一个真实困惑加的：用户退出并重启后功能不生效，**必须重启两遍**才行。

    机制的根在于「写入发生在**退出**时，而不是启动时」：

        启动① → 钩子跑 sync → 发现待办 → 挂看护 W（W 进
                 `while zcode_running(): sleep(3)`）
        退出   → W 醒来 → 写 app.asar → 主动把 ZCode 重新拉起来
        启动② → asar 已是新版 → 功能生效 ✓

    所以「两遍」本身是**设计使然**，不是 bug。真正的故障是：
    **第一次退出没退干净**（残留 ZCode 子进程 / 点了「关闭窗口」最小化到托盘），
    `zcode_running()` 一直为真 → W 一直等（最长 24h）→ 永远不写 →
    于是必须再来一遍，而第二遍恰好真的退干净了，才写进去。

    判据只有一处：`_apply_after_exit.log`。
      * 只有「看护启动，等待 ZCode 退出…」→ W 起来了但**从未等到退出** ← 这就是要抓的
      * 有「ZCode 已退出（等待 Ns），开始处理」→ 写入了
      * 有「DONE」→ 写完并已（尝试）重启
    """
    hr("7.5 退出后看护（写入时机）")
    logf = WATCHDOG_LOG
    if not logf.is_file():
        print(f"{INFO} 没有看护日志 {logf} —— 说明还没挂过看护（正常：无待办时不挂）")
        return
    lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()
    started = [l for l in lines if "看护启动" in l]
    woke = [l for l in lines if "已退出（等待" in l]
    done = [l for l in lines if "DONE" in l]
    skipped = [l for l in lines if "已再次运行" in l]
    timed_out = [l for l in lines if "等待超时" in l]

    print(f"{INFO} 看护启动 {len(started)} 次，等到退出 {len(woke)} 次，"
          f"完成 {len(done)} 次")
    for ln in lines[-4:]:
        print(f"        {ln}")

    pending = len(started) - len(woke) - len(timed_out)
    if pending > 0:
        print(f"{WARN} 有 {pending} 个看护**起来了却从未等到 ZCode 退出** —— 补丁没写进去。")
        print("       这就是「必须重启两遍才生效」的直接原因：第一次其实没退出干净。")
        print("       怎么彻底退出：")
        print("         · Windows：托盘图标**右键 → 退出**；关窗口只是最小化，进程还在")
        print("         · 退完在任务管理器里确认 **没有 ZCode.exe 残留**（ZCode 是多进程，")
        print("           主窗口关了常留渲染/GPU 子进程）")
        print("         · 或直接跑（只读，会列出所有残留进程）：")
        print("             tasklist /FI \"IMAGENAME eq ZCode.exe\"")
    elif done:
        print(f"{OK} 看护正常完成过写入（最近一次见上）")
    if skipped:
        print(f"{INFO} 有 {len(skipped)} 次是「ZCode 已再次运行，跳过重启」——")
        print("       说明写入完成后客户端已被别的原因拉起，不影响补丁生效。")
    if timed_out:
        print(f"{WARN} 有 {len(timed_out)} 次等待 24h 超时放弃。")


def _zcode_log_dir() -> Path | None:
    """ZCode 的 jsonl 日志目录（钩子执行记录在里面）。"""
    for root in _storage_roots():
        d = root / "cli" / "log"
        if d.is_dir():
            return d
    return None


def _latest_zcode_logs(limit: int = 3) -> list[Path]:
    d = _zcode_log_dir()
    if not d:
        return []
    try:
        files = sorted((p for p in d.glob("zcode-*.jsonl") if p.is_file()),
                       key=lambda p: p.name)
    except OSError:
        return []
    return files[-limit:]


def _scan_zcode_log(files: list[Path]) -> dict:
    """从 ZCode 自己的 jsonl 日志里提取「插件解析」与「会话启动阶段」记录。

    只认两类记录（本机 ZCode 3.14.3 实测结构）：
      * `bootstrap.app.startup.plugins.completed` —— context 带
        pluginCount / enabledPluginCount / **hookCount** / diagnosticCount / skillRootCount。
        **hookCount 是关键**：它表示 ZCode 这次启动到底注册了几个钩子。
        为 0 就说明「插件没启用」或「hooks.json 没被读到」——
        轮不到讨论钩子有没有执行，这是比心跳文件更靠前的一层证据。
      * `turn.phase.*` 且 phase == "session_start_hooks" —— 会话启动阶段跑过。
    """
    startups: list[dict] = []
    phase_count = 0
    last_phase = ""
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "session_start_hooks" in line:
                phase_count += 1
                try:
                    rec = json.loads(line)
                    last_phase = str(rec.get("timestamp", ""))[:19].replace("T", " ")
                except Exception:
                    pass
                continue
            if '"hookCount"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") != "bootstrap.app.startup.plugins.completed":
                continue
            ctx = rec.get("context") or {}
            startups.append({
                "ts": str(rec.get("timestamp", ""))[:19].replace("T", " "),
                "kind": str(ctx.get("startupKind") or "?"),
                "plugins": ctx.get("pluginCount"),
                "enabled": ctx.get("enabledPluginCount"),
                "hooks": ctx.get("hookCount"),
                "diagnostics": ctx.get("diagnosticCount"),
            })
    return {"startups": startups, "phase_count": phase_count, "last_phase": last_phase}


def report_zcode_log() -> dict:
    """把 ZCode 日志里的钩子证据读出来打印（比让用户自己去翻强得多）。"""
    files = _latest_zcode_logs()
    logd = _zcode_log_dir()
    if not files:
        print(f"{WARN} 读不到 ZCode 日志（{logd or '日志目录不存在'}）")
        return {}
    data = _scan_zcode_log(files)
    print(f"{INFO} ZCode 日志：{logd}（读的是 {', '.join(f.name for f in files)}）")
    st = data["startups"]
    if not st:
        print(f"{WARN} 日志里没有「插件解析」记录")
    else:
        print("        最近几次启动（时间 / 插件数 / 启用 / 钩子 / 诊断）：")
        for s in st[-4:]:
            print(f"          {s['ts']}  {s['kind']:<10} {s['plugins']} / {s['enabled']} / "
                  f"**{s['hooks']}** / {s['diagnostics']}")
        last = st[-1]
        if last["hooks"] in (0, None):
            print(f"{BAD} 最近一次启动注册了 **0 个钩子** —— ZCode 根本没把本插件的钩子挂上。")
            print("       也就是说，那次启动时插件还没启用（或 hooks.json 没被读到），")
            print("       「钩子没跑」是必然结果，**不是钩子本身有问题**。")
        else:
            print(f"{OK} 最近一次启动注册了 {last['hooks']} 个钩子 —— 插件侧已经就绪")
            print("       若仍没有心跳，那才是「注册了但没执行」，重点查 python 是否在 PATH、")
            print("       以及钩子命令里的 ${CLAUDE_PLUGIN_ROOT} 有没有被正确展开。")
        if last["diagnostics"]:
            print(f"{WARN} diagnosticCount={last['diagnostics']} —— 插件解析有诊断信息，值得细看日志")
    if data["phase_count"]:
        print(f"{INFO} session_start_hooks 阶段出现 {data['phase_count']} 次"
              f"（最近 {data['last_phase']}）—— 会话启动链路本身是通的")
    else:
        print(f"{WARN} 日志里没有 session_start_hooks 阶段")
    return data


def check_patches() -> None:
    hr("8. 补丁在客户端里的实际状态")
    if not PATCHER.is_file():
        print(f"{BAD} 找不到 {PATCHER}")
        return
    # 强制子进程用 UTF-8：中文 Windows 的 cp936 编不出 ✓/✗，会让子进程在
    # 打印汇总表时抛 UnicodeEncodeError（整张表断在半路）。这里两边都用 utf-8 对齐。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, str(PATCHER), "--all", "--check"],
                           capture_output=True, encoding="utf-8", errors="replace",
                           cwd=str(HERE), timeout=180, env=env,
                           **no_window_kwargs())
    except Exception as e:
        print(f"{BAD} 执行失败：{e!r}")
        return
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if not out:
        print(f"{BAD} 没有任何输出（退出码 {r.returncode}）")
        return
    for ln in out.splitlines():
        print(f"    {ln}")

    # ★ 「已打但装的是旧版」必须单独点出来。它的表现和「已是最新」在肉眼上极像
    # （都是「已打」），但含义完全相反：前者说明**插件更新过、补丁却没跟着更新**。
    # 插件市场「更新」只替换插件目录，**不会**重新注入 app.asar —— 用户按提示
    # 退出重启多少次都不会生效，而这一刻正是唯一能看出问题的地方。
    stale = [ln for ln in out.splitlines() if "含旧版组件" in ln]
    if stale:
        print(f"{WARN} 有 {len(stale)} 项注入的是**旧版片段**（插件更新过，但 app.asar 没跟着更新）。")
        print("    插件市场「更新」只替换插件目录，不会重新注入 app.asar —— 所以退出重启也不会变。")
        print("    0.6.1 起同步脚本会自动识别并重跑；旧版本请手动修（**完全退出 ZCode 后**执行）：")
        print(f'      python "{PATCHER}" --all')


def verdict(py_ok: bool, zcode_ok: bool, has_plugin: bool, enabled: bool,
            saved: bool, hook_ok: bool, fired: bool, log_info: dict | None = None) -> None:
    hr("结论")
    if not py_ok:
        print("Python 版本不满足要求 —— 先装 Python 3.10+，并确保 `python --version` 能跑通。")
        return
    if not zcode_ok:
        print("没找到 ZCode 客户端 —— 补丁没有可注入的目标。")
        return
    if not has_plugin:
        print("插件没装成功。按 README「方式 A」重新添加插件市场并安装。")
        return
    if not enabled:
        print("★ 卡点：插件已安装但**未启用**。")
        print("  ZCode 只在插件启用后，把它的 Hook 注册进**新会话**。")
        print("  → 「设置 → 插件 → 管理已安装」打开开关，然后开一个新会话。")
        return
    if not saved:
        # **不再是卡点。** 没保存过配置时 sync.py 会退回插件清单声明的默认值（全开）自动注入，
        # 这正是「装完即用」的实现方式；旧版把「没表态」当成「不要做」，才导致装完什么都没发生。
        print("提示：配置里没有保存过任何开关 —— 同步脚本会按**插件清单声明的默认值（全开）**注入。")
        print("      这不是故障。想关掉个别功能：高级信息 → 配置 → 拨成关 → 保存配置 → 退出并重启。")
    if not hook_ok:
        print("★ 卡点：钩子文件缺失或结构不对 —— 重新安装插件（升级到最新版）。")
        return
    if not fired:
        print("★ 卡点：插件已启用，但**钩子从未运行过**。")
        st = (log_info or {}).get("startups") or []
        if st and st[-1].get("hooks") in (0, None):
            print("  日志已经给出直接原因：**最近一次启动注册的钩子数是 0**。")
            print("  也就是说那次启动时 ZCode 没把本插件的钩子挂上（插件未启用 /")
            print("  hooks.json 没被读到），「钩子没跑」是必然的，跟钩子写法无关。")
            print("  → ① 「设置 → 插件 → 管理已安装」确认是**启用**状态；")
            print("    ② **完全退出** ZCode（托盘图标右键 → 退出，关窗口不算）再启动；")
            print("    ③ 启动后**开一个新会话**（SessionStart 在新会话第一轮才触发）；")
            print("    ④ 再跑一次本自检，第 7 节应出现心跳。")
        else:
            print("  依次确认：① 是否**完全退出**（托盘右键退出）并重启过 ZCode；")
            print("            ② 重启后是否**开了一个新会话**（SessionStart 在新会话第一轮才触发）；")
            print("            ③ 命令行 `python --version` 是否可用（macOS/Linux 试 `python3 --version`）；")
            print("            ④ 已安装副本是不是最新版（「检查更新」）。")
        print("  → 兜底：不依赖钩子，直接用命令行打补丁（**完全退出 ZCode 后**执行）：")
        print(f'       python "{PATCHER}" --all')
        return
    print("链路完整：插件已启用、钩子跑过。")
    print("若功能仍不可见，注意**重打包级补丁需要两次启动**：")
    print("  第一次启动 → 钩子登记待办 → 完全退出 ZCode（看护进程改写 app.asar）→ 第二次启动才生效。")
    print("第 8 节里显示「未打」的重打包项，退出 ZCode 后再看一次即可确认。")


def auto_fix() -> int:
    """自动修复升级或配置异常：
    1. 启用插件（enabledPlugins 改为 true）
    2. 清理陈旧的看护锁
    3. 如果 ZCode 未运行，立即应用全部补丁；若正在运行，调度退出后看护并给出提示
    """
    hr("一键自愈与修复（--fix）")
    fixed_items = []

    # 1. 修复 config.json 中的启用状态
    cfgs = _find_configs()
    for cfg_path in cfgs:
        try:
            cfg = _read_json(cfg_path)
            if not isinstance(cfg, dict):
                continue
            plugins = cfg.setdefault("plugins", {})
            changed = False
            if plugins.get("enabled") is False:
                plugins["enabled"] = True
                changed = True
                fixed_items.append("启用插件总开关 (plugins.enabled = true)")

            en = plugins.setdefault("enabledPlugins", {})
            matched = False
            for k in list(en.keys()):
                if str(k) == PLUGIN_NAME or str(k).startswith(PLUGIN_NAME + "@"):
                    matched = True
                    if not en[k]:
                        en[k] = True
                        changed = True
                        fixed_items.append(f"启用插件：{k} = true")
            if not matched:
                default_key = f"{PLUGIN_NAME}@{REPO_NAME}"
                en[default_key] = True
                changed = True
                fixed_items.append(f"登记并启用插件：{default_key} = true")

            if changed:
                cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"{BAD} 修复配置失败 {cfg_path}：{e}")

    # 2. 清理陈旧看护锁
    pid_file = HERE / "_watchdog.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            from apply_after_exit import is_pid_alive
            if not is_pid_alive(pid):
                pid_file.unlink(missing_ok=True)
                fixed_items.append(f"清理已失效的看护锁文件 (PID {pid})")
        except Exception:
            pid_file.unlink(missing_ok=True)

    # 3. 检查客户端补丁状态
    zp = _load_patcher()
    running = False
    try:
        running = zp.zcode_running()
    except Exception:
        pass

    if running:
        print(f"{WARN} ZCode 正在运行，重打包补丁无法立即写入。")
        try:
            import sync
            wanted, _ = sync.resolve_wanted()
            sync.start_watchdog(wanted)
            fixed_items.append("已激活后台看护 apply_after_exit.py")
        except Exception as e:
            print(f"{BAD} 启动看护失败：{e}")
        print(f"{INFO} 提示：请在托盘图标【右键 → 退出】完全退出 ZCode 一次，")
        print("       看护将在后台自动把全部补丁写进客户端并重新拉起 ZCode！")
    else:
        print(f"[*] ZCode 未运行，开始直接注入全部补丁...")
        try:
            r = subprocess.run([sys.executable, str(PATCHER), "--all"],
                               cwd=str(HERE), capture_output=True, encoding="utf-8", errors="replace",
                               **no_window_kwargs())
            print((r.stdout or "") + (r.stderr or ""))
            if r.returncode == 0:
                fixed_items.append("全量补丁注入完成 (zcode_patcher.py --all)")
            else:
                print(f"{BAD} 补丁注入退出码: {r.returncode}")
        except Exception as e:
            print(f"{BAD} 执行补丁失败：{e}")

    hr("修复汇总")
    if fixed_items:
        for item in fixed_items:
            print(f"{OK} {item}")
        print("\n[+] 自愈修复已完成！现在可以启动 ZCode 验证。")
    else:
        print(f"{INFO} 未发现需修复的配置项。")
    return 0


# ------------------------------------------------------------------ 入口

def main() -> int:
    safe_stdio()          # 输出被重定向时 cp936 会编不出符号，先把这条路封死
    ap = argparse.ArgumentParser(description="zcode-tokenspeed 安装自检（只读，或配合 --fix 自动修复）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于贴给别人看）")
    ap.add_argument("--where", action="store_true",
                    help="只打印扫描到的插件目录，排查「插件到底装在哪」")
    ap.add_argument("--where-all", action="store_true",
                    help="配合 --where：把扫过的全部候选目录都列出来（默认只列命中项）")
    ap.add_argument("--fix", action="store_true",
                    help="一键自愈修复：自动启用插件、重打补丁或调度退出看护")
    args = ap.parse_args()

    if args.fix:
        return auto_fix()

    if args.where or args.where_all:
        return print_where(verbose=args.where_all)

    dirs = _installed_plugin_dirs()
    enabled = _enabled_state()
    saved = bool(_saved_options())

    if args.json:
        log_files = _latest_zcode_logs()
        log_data = _scan_zcode_log(log_files) if log_files else {}
        print(json.dumps({
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "plugin_dirs": [str(d) for d in dirs],
            "manifest_versions": [_manifest_version(d) for d in dirs],
            "enabled": enabled,
            "options_saved": saved,
            "saved_options": _saved_options(),
            "hook_fired": any(
                (d / "skills" / PLUGIN_NAME / "scripts" / STAMP.name).is_file() for d in dirs),
            "zcode_log": {
                "files": [f.name for f in log_files],
                "startups": (log_data.get("startups") or [])[-5:],
                "session_start_hooks_phases": log_data.get("phase_count"),
            },
        }, ensure_ascii=False, indent=2))
        return 0

    print("zcode-tokenspeed 自检报告（只读，不会修改任何文件；加 --fix 可自动修复）")
    py_ok = check_python()
    zcode_ok, _, _ = check_zcode()
    check_running()
    has_plugin, dirs, enabled = check_plugin()
    saved = check_options()
    hook_ok = check_hook(dirs)
    hooked, log_info = check_heartbeat(dirs)
    check_watchdog()
    check_patches()
    verdict(py_ok, zcode_ok, has_plugin, enabled, saved, hook_ok, hooked, log_info)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
