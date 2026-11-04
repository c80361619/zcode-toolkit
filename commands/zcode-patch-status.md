---
description: 只读检查 ZCode 客户端各补丁的注入状态（不修改任何文件）
---

加载本插件自带的 `zcode-tokenspeed` 技能（`${CLAUDE_PLUGIN_ROOT}/skills/zcode-tokenspeed/SKILL.md`；若该变量未展开，就按插件安装目录下的同名路径读取），按其「只读核实」步骤对本机 ZCode 安装执行全部检查命令：

```bash
python "<skill目录>/scripts/zcode_patcher.py" --all --check
```

或者逐项检查：

```bash
python "<skill目录>/scripts/zcode_patcher.py" --usage-chart --check
python "<skill目录>/scripts/zcode_patcher.py" --tps-footer --check
python "<skill目录>/scripts/zcode_patcher.py" --enhance-prompt --check
python "<skill目录>/scripts/zcode_patcher.py" --model-puller --check
```

要求：

- **纯只读**，不要执行任何写入或 `--revert`。
- 用中文汇总成一张表：每个补丁「已打 / 未打 / 锚点失配」，失配时给出脚本报告的原因，不要绕过脚本的拒绝结论。
