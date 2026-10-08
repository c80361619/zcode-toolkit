---
description: 诊断 ZCode 增强补丁链路状态与安装健康度，支持一键自愈修复（--fix）
---

加载本插件自带的 `zcode-tokenspeed` 技能（`${CLAUDE_PLUGIN_ROOT}/skills/zcode-tokenspeed/SKILL.md`；若该变量未展开，就按插件安装目录下的同名路径读取），对本机 ZCode 客户端及插件环境执行诊断或自愈：

```bash
# 诊断模式：检查插件配置、启用状态、客户端路径与补丁注入状态
python "<skill目录>/scripts/doctor.py"

# 自愈模式：自动将插件置为启用，并在退出 ZCode 后自动补齐全部补丁
python "<skill目录>/scripts/doctor.py" --fix
```

要求：
1. 默认执行诊断模式，将报告用清晰的中文摘要呈现给用户。
2. 若用户要求「修复」、「自愈」或诊断发现异常，执行 `--fix` 自愈模式，并友好提示用户完全退出 ZCode 以完成看护注入。
