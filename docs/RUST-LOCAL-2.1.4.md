# Token Weather Rust 2.1.4

[简体中文](#简体中文)

8 November 2026 · macOS on Apple silicon

- The local sidebar opens with the weather scale and token details expanded. Open MCP servers on demand.
- The entrance plays on the first successful reading or an explicit tool invocation. Polling and returning to the sidebar keep the view steady. Reduce Motion disables the entrance.
- The compact 2.1 layout has a five-band capacity meter, a recent growth chart and a forecast to a verified auto-compact threshold based on median recent growth.

## Install or preview

Download the local ZIP from the [GitHub release](https://github.com/FeidaWang/Token-Weather/releases/tag/rust-v2.1.4). Extract it and register its `bin/token-weather-core` as a local stdio MCP server with argument `mcp`. The tested Codex 0.167 compatibility path uses an absolute executable path. Reopen **Token Weather Context** in the intended local chat.

Run `./bin/token-weather-core serve --demo` from the extracted folder for a read-only preview with synthetic Clear and Storm states.

| ZIP | SHA-256 |
| --- | --- |
| `token-weather-rust-local-2.1.4-darwin-arm64.zip` | `0b195549ec3d6e470abdccb840837d4256cbdcad057fd8683767692a34647c2d` |
| `token-weather-platform-desktop-2.1.4-candidate.zip` | `03b7d57e59f0ba2f0f1e9b48728dfa8e9c02b216b3d25156b5e295f4b5c94e0c` |

## OpenAI Platform handoff

The candidate ZIP uses local stdio and targets the desktop-only track. The existing remote numeric planner is a separate deployment. Confirm the intended desktop listing and the submitting organization’s local MCP allowlist with OpenAI before uploading. Check the imported metadata and review requirements in the portal. Review cases in the ZIP are drafts, and any required recording should show the desktop version. [OpenAI desktop-only submission guide](https://developers.openai.com/plugins/deploy/desktop-only)

## 简体中文

2026 年 11 月 8 日 · Apple 芯片 macOS

- 侧栏打开即展开天气刻度和 Token details，MCP servers 按需展开。
- 首次成功读数或显式调用工具时播放入场动画。后台刷新和切回侧栏时画面保持稳定。系统启用「减少动态效果」时关闭入场动画。
- 2.1 版采用紧凑布局、五档容量色带和逐轮增长图，用近期增长中位数估算到已验证压缩阈值的剩余轮数。

从 [GitHub 发布页](https://github.com/FeidaWang/Token-Weather/releases/tag/rust-v2.1.4) 下载本地 ZIP。解压后，将 `bin/token-weather-core` 注册为本地 stdio MCP 服务，参数设为 `mcp`。经测试的 Codex 0.167 兼容安装使用绝对可执行路径。在目标本地聊天中重新打开 **Token Weather Context**。也可在解压目录运行 `./bin/token-weather-core serve --demo`，用模拟数据预览 Clear 和 Storm。

Platform 候选 ZIP 使用桌面端本机 stdio。现有远程数字预算工具有独立的部署。上传前请向 OpenAI 核对目标桌面插件及提交组织的本机 MCP 白名单资格，再在门户检查导入信息和审核要求。包内审核用例是草稿；如门户要求演示录像，应录制桌面版。 [OpenAI 桌面插件提交指南](https://developers.openai.com/plugins/deploy/desktop-only)
