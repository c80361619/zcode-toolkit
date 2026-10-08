# zcode-tokenspeed · ZCode 客户端一站式原生增强套件

<p align="left">
  <a href="https://github.com/c80361619/zcode-toolkit/releases"><img src="https://img.shields.io/badge/version-0.6.11-blue.svg?style=flat-square" alt="Version"></a>
  <img src="https://img.shields.io/badge/python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/dependencies-0%20(std%20only)-success.svg?style=flat-square" alt="Dependencies">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg?style=flat-square" alt="Platform">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

为 ZCode 桌面客户端补齐极致原生体验与生产力增强：
- 📊 **实时 TPS 与性能状态栏**：首 Token 延迟、tok/s 生成速度、Token 统计与会话命中率常驻展示。
- 🎚️ **思考强度悬浮滑条**：免去繁琐配置，直接在界面流畅拖拽调节 Reasoning Effort，原生即时生效。
- ✨ **提示词智能增强**：输入框旁一键重写润色草稿；**右键自由选择**润色所用模型，支持一键恢复。
- 📈 **用量图表全量展开**：破除官方 Top 5/6 截断限制，全量展示所有模型的调用趋势与占比。
- 🔍 **模型选择浮窗加宽**：选择框 192px 扩大至 320px，长模型名完整展示不再被省略号截断。
- ⚡ **一键模型同步**：设置页一键获取供应商 `/models` 模型列表，勾选即刻自动建条目并写入。
- 🛡️ **无感自愈看护（0.6.11 新特性）**：自动感知客户端与插件升级，后台单例看护静默守护，**彻底解决“客户端一升级补丁就失效”！**

> **纯 Python 标准库驱动**：零第三方依赖、免编译；改动仅作用于**本地客户端文件**；  
> 全量操作严格幂等，支持状态核查与逐项/全量精确还原。
>
> ⚠️ **声明**：非官方开源套件，与 ZCode 官方团队无关联。第三方声明见 [NOTICE.md](NOTICE.md)。  
> （仓库名 `zcode-toolkit` ≠ 插件名 **`zcode-tokenspeed`**，二者保持独立设计以保障配置兼容性）。

---

## ⚡ 三分钟极速上手（推荐，全程零命令）

适合所有使用 ZCode 桌面端的用户，安装过程**完全无需打开命令行**：

| 步骤 | 操作说明 |
|:---:|---|
| **1. 环境确认** | 确认电脑已安装 **Python 3.10+**（系统自带或环境变量已配置）及 **ZCode 客户端** |
| **2. 添加市场** | 打开 ZCode **设置 → 插件 → 右上角「创建」→「添加插件市场」**，来源填：<br>`c80361619/zcode-toolkit` |
| **3. 安装插件** | 在「个人」分段找到 **ZCode 原生体验增强**（`ZCode Patcher`），点 **安装**（装好默认启用） |
| **4. 自动激活** | **完全退出并重启 ZCode**（托盘右键退出），在新会话中发任意一条消息，后台看护进程将自动完成注入并在下次启动后展现全部增强功能！ |

> 💡 **全自动开箱即用**：插件清单内所有功能默认全开，无需打开配置页。如需关闭某项，在插件配置页拨成关并保存即可。

---

## 🧩 核心功能矩阵

| 特性 | 功能名称 | 核心效果 | 改动范围 |
|:---:|:---|:---|:---|
| 🛡️ | **升级自动自愈** | 动态监控客户端与插件版本，升级后自动感知并调度看护静默修复 | 状态监控器 |
| 📊 | **实时 TPS 状态栏** | 输入框常驻：本轮指标（首 token / tok/s / out）+ 会话累计，右键自由切换位置 | `app.asar` 注入脚本 |
| 🎚️ | **思考强度滑条** | 工具栏「思考 · 档名」入口，悬浮拖拽条吸附微调，主题自适应，原生链路即时生效 | `app.asar` 注入脚本 |
| ✨ | **提示词增强润色** | 输入框旁「增强提示词」按钮，右键支持按供应商自选润色模型，持久化且免重启 | `app.asar` 注入脚本 + IPC |
| 📈 | **用量图表去截断** | 「设置 → 用量」趋势图与饼图不再被 Top 5 / Top 6 截断，全量展示所有模型 | `app.asar` 渲染层 |
| 🔍 | **模型弹窗加宽** | 模型下拉列表宽度由 192px 扩展至 320px，超长模型名完整展示 | `app.asar` 主 bundle |
| ⚡ | **设置页模型拉取** | 设置页新增「⚡ 自动拉取模型」按钮，自动从供应商同步可用模型并批量写入 | `app.asar` 注入脚本 + IPC |
| 🧠 | **思考档位原生配置** | 为各模型配置 `provider_config.json` 档位（3.14+ 原生 optionSpecs，无需内核改动） | 用户配置 JSON |
| ⚙️ | **思考内核补丁（旧版）** | ≤3.11 旧版内核专用兜底方案（3.14+ 自动识别并跳过，安全无侵入） | `zcode.cjs` |

---

## 🛠️ 故障排查与一键自愈

> 💡 **0.6.11 自愈机制**：客户端升级会覆盖官方原版文件导致补丁失效。本插件在检测到升级后会自动重新排期看护，**通常只需在会话中收到提示后，完全退出并重启一次 ZCode 即可自动恢复！**

若遇到特殊情况（如杀毒软件拦截、网络断开等）导致功能未生效，请按以下方式排查与一键修复：

### 方案 A：客户端内一键修复（推荐，无需打开终端）
在 ZCode 任意对话窗口直接输入内置斜杠命令，AI 助手将自动帮您检测或打补丁：
- 输入 `/zcode-patch-doctor`：一键诊断健康状态与卡点；
- 输入 `/zcode-patch-apply`：一键自动重新应用所有增强补丁。

### 方案 B：全电脑通用单行自愈命令（适配所有电脑，任何路径直接粘贴）
无论您是否克隆过本仓库，也不管您在哪个路径打开终端，**直接复制并运行以下命令**（脚本会自动在系统插件缓存中搜寻并执行自愈，纠正启用状态并自动补齐所有补丁）：

```powershell
# Windows (CMD 或 PowerShell 任意路径直接运行)
python -c "import pathlib,subprocess,sys; p=next(pathlib.Path.home().glob('.zcode/**/doctor.py'),None); subprocess.run([sys.executable,str(p),'--fix']) if p else print('未找到已安装的 ZCode 插件，请先在客户端内安装')"
```

```bash
# macOS / Linux (任意终端直接运行)
python3 -c "import pathlib,subprocess,sys; p=next(pathlib.Path.home().glob('.zcode/**/doctor.py'),None); subprocess.run([sys.executable,str(p),'--fix']) if p else print('未找到已安装的 ZCode 插件，请先在客户端内安装')"
```

---

## 🎯 常用特性使用技巧

### 1. 增强提示词（右键自由指定模型）
- **左键点击**：根据当前选定模型一键润色重写草稿，支持一键「恢复原文」；
- **右键点击**：弹出供应商模型列表，可自由指定轻量、快速或专用模型（如 Claude 3.5 Haiku、GPT-4o-mini）专门用于润色，选择后**持久化保存且立即生效**。

### 2. 思考强度悬浮滑条
- 点击输入框下方工具栏的「思考 · 档位」文字，会弹出平滑拖动条；
- 拖拽至所需强度（关闭 / 低 / 中 / 高）释放即刻生效，原生联动客户端配置。

---

## 🔄 卸载与还原

若您想完全停用增强补丁并恢复官方纯净状态：
1. 打开终端运行全域还原命令：
   ```powershell
   python -c "import pathlib,subprocess,sys; p=next(pathlib.Path.home().glob('.zcode/**/zcode_patcher.py'),None); subprocess.run([sys.executable,str(p),'--all','--revert']) if p else None"
   ```
2. 在 ZCode **设置 → 插件 → 管理已安装** 中，点击本插件的「卸载」即可。

---

## 📚 进阶文档与技术档案

<details>
<summary><b>🛠️ 开发者指南：本地离线安装、纯 CLI 与自动化构建</b></summary>

### 方式 B：本地源码离线安装
适合内网、GitHub 网络受限或本地二次开发的场景：
```bash
git clone https://github.com/c80361619/zcode-toolkit.git
```
在 ZCode「设置 → 插件 → 创建 → 添加插件市场」中选择该仓库目录，校验通过后即可安装。

### 方式 C：纯命令行模式（不装插件）
克隆仓库后，直接通过主脚本打补丁：
```bash
# 只读体检（自动探测 ZCode 安装位置）
python skills/zcode-tokenspeed/scripts/zcode_patcher.py --all --check

# 先完全退出 ZCode，然后一次性注入全部补丁
python skills/zcode-tokenspeed/scripts/zcode_patcher.py --all

# 单独还原某项（例如还原 TPS 状态栏）
python skills/zcode-tokenspeed/scripts/zcode_patcher.py --tps-footer --revert
```

### 方式 D：一键引导脚本（开发者专用）
```bash
python bootstrap.py            # macOS/Linux 使用 python3 bootstrap.py
```
自动执行平台环境校验、标准库检查、语法构建与 280+ 项回归测试。

### 方式 E：全自动流水线
```bash
./run.sh                       # Linux / macOS
run.cmd                        # Windows
```

</details>

<details>
<summary><b>🔬 自动注入机制与 ASAR 安全设计</b></summary>

### 1. 为什么补丁需要退出后写入？
`zcode_patcher.py` 具有严格的运行预检机制：只要系统中存在 `ZCode.exe` 进程便拒绝写盘。这是因为客户端运行时 `app.asar` 文件会被底层文件系统锁定，强行改写会导致文件损坏。因此本套件设计了看护单例（Watchdog）：在会话中记录期望状态，待客户端完全退出的一瞬间原子完成写入并自动重新拉起。

### 2. ASAR 重打包安全性
- **全域 Integrity 校验**：重打包时对所有 entry 的 SHA-256 与 offset 排布进行全局自洽检验，杜绝错位加载失败；
- **跨进程互斥锁**：写入全程施加文件锁，避免多进程并发争抢冲突。

</details>

<details>
<summary><b>📜 历史版本审计与故障排查记录</b></summary>

#### 0.6.11（升级自愈与看护优化）
- `sync.py` 引入客户端指纹比对，自动识别客户端升级并重置通知标记，自动调度看护；
- `apply_after_exit.py` 补齐 7 项补丁，并实现 `_watchdog.pid` + `_watchdog.want` 单例互斥与需求合并；
- `doctor.py` 增加 `--fix` 一键自愈命令。

#### 0.6.10（旧脚本失效检测）
- 修复 `_process_script_inject` 的 check 分支仅检查结构未比对内容的缺陷，新增内容哈希比对并标记 stale。

#### 0.6.8 - 0.6.9（TPS 精度与菜单交互）
- 状态栏平均命中率改为精确到两位小数；
- 优化提示词右键菜单的关闭机制，消除滚动与多频事件误关问题。

#### 0.6.5（ASAR 布局错位保护）
- 引入 `_AsarWriteLock` 进程锁与文件锁；
- 全域逐条 integrity 校验，防范错位启动崩溃。

</details>

---

## 许可协议

本项目采用 [MIT License](LICENSE) 开源协议。第三方组件声明见 [NOTICE.md](NOTICE.md)。
