# 第三方组件与许可声明

本项目代码为原创实现。以下部分包含来自第三方的代码或设计参考，按相应许可要求在此集中声明。

## 1. 模型拉取前端脚本（MIT License）

- 文件：`skills/zcode-tokenspeed/scripts/zcode-model-puller.js`
- 同一项目的配置同步逻辑被移植到 `skills/zcode-tokenspeed/scripts/model_pull.py`（命令行版）

```
MIT License

Copyright (c) HHQ-666

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 2. 设计参考（非代码引用）

- 输入框「增强提示词」的交互与提示词要点对齐了 WorkBuddy 的 `input.enhance` 功能，
  提示词模板为本项目自行撰写。
