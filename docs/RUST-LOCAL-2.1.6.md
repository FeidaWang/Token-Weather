# Token Weather Rust 2.1.6

## Compatibility fix

Token Weather can read local chats created by Codex Desktop engine `0.169.0-alpha.11.1`. This restores the token count and observed context window after the Desktop update. This release also includes compatibility with `0.167.0-alpha.4.1`.

The local package targets macOS on Apple silicon. [Download the ZIP](https://github.com/FeidaWang/Token-Weather/releases/download/rust-v2.1.6/token-weather-rust-local-2.1.6-darwin-arm64.zip) and extract it. For an existing local Token Weather installation, update its stdio command to the absolute path of the extracted `bin/token-weather-core` and keep `mcp` as the argument. Open a new local chat and invoke **Token Weather Context** after the first request records usage.

SHA-256 for `token-weather-rust-local-2.1.6-darwin-arm64.zip`

```text
e020ecc1d062e0de21967a2b321c08dccfff99271e201eebf7347cc3843e165d
```

## 简体中文

修复 Codex Desktop 引擎升级到 `0.169.0-alpha.11.1` 后，新聊天无法读取 token 数和上下文窗口的问题。此版本也包含对 `0.167.0-alpha.4.1` 的兼容支持。

[下载安装包](https://github.com/FeidaWang/Token-Weather/releases/download/rust-v2.1.6/token-weather-rust-local-2.1.6-darwin-arm64.zip)并解压。已安装本地插件的用户可将 stdio 命令更新为新版 `bin/token-weather-core` 的绝对路径，参数仍为 `mcp`。新建本地聊天，等首个请求记录用量后，打开 **Token Weather Context**。此安装包适用于 Apple 芯片 Mac。
