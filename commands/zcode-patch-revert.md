---
description: 从备份精确还原 ZCode 客户端补丁
---

加载本插件自带的 `zcode-tokenspeed` 技能（`${CLAUDE_PLUGIN_ROOT}/skills/zcode-tokenspeed/SKILL.md`；若该变量未展开，就按插件安装目录下的同名路径读取），按用户点名的功能执行还原：

```bash
python "<skill目录>/scripts/zcode_patcher.py" --usage-chart --revert
python "<skill目录>/scripts/zcode_patcher.py" --tps-footer --revert
python "<skill目录>/scripts/zcode_patcher.py" --enhance-prompt --revert
python "<skill目录>/scripts/zcode_patcher.py" --model-puller --revert
```

要求：

- 先确认 ZCode 已**完全退出**——重打包级补丁（TPS / 增强提示词 / 拉取按钮）还原会写 `app.asar`；
  脚本会预检进程，运行中直接拒绝（退出码 2）。
- 还原按外科手术式进行，不依赖整包备份；多个补丁共存时互不误伤，可按需只还原其中一个。
- 还原后提示用户重启 ZCode 确认已恢复原状。
- 若客户端已经异常到打不开，改用紧急整包还原：`python "<skill目录>/scripts/restore_clean.py" --latest`（改文件前可先 `--backup` 存一份干净副本）。
