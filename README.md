# Token Weather

[简体中文](README.zh-CN.md)

Token Weather helps you see how much room a Codex chat may have left. Think of it like a weather forecast: it shows the conditions now and gives you a rough idea of what may happen next.

A **token** is a small piece of text. A chat's **context** is the information the model can use at once. As a conversation grows, that space can fill up. Token Weather helps you notice this before starting your next task.

## What can I do with it?

- **See your chat's weather:** the local Rust panel shows available context information, recent growth, token details and an estimate to the auto-compact threshold when Codex provides a verified reference.
- **Explore a budget:** enter your own numbers into the separate scenario planner to see whether a task may fit.
- **Keep control:** run the tools locally, or host the numeric planner on your own server.

Readings and forecasts are estimates, not promises. Missing or unsupported data stays unknown. Chat context is also different from your account usage allowance. Token Weather does not automatically move your chat or change your model.

## Try the panel

The latest Rust local release is **2.1.4**, for macOS on Apple silicon. [Download it and see the release instructions](https://github.com/FeidaWang/Token-Weather/releases/tag/rust-v2.1.4). The ZIP must be extracted and its `bin/token-weather-core` registered with a compatible local MCP host; downloading it alone does not install or connect it. See the [release record](docs/RUST-LOCAL-2.1.4.md) for details and the checksum.

Open **Token Weather Context** from the Codex chat sidebar. The weather scale and token details start expanded. MCP servers can be opened on demand. The entrance plays on the first successful reading or an explicit tool invocation. Switching back to the panel and background refreshes do not replay it. Reduce Motion disables the entrance.

Want a preview without connecting your chat? In a clean clone, use the release source:

```sh
git switch --detach rust-v2.1.4
cargo build --release --locked --manifest-path rust/token-weather-core/Cargo.toml
rust/token-weather-core/target/release/token-weather-core serve --demo
```

Open the local address printed in the terminal and use **Debug state** to switch between Clear and Storm. The preview uses **made-up data**, not your chat. Press Ctrl+C to stop it. Building requires Rust and Cargo. The actual MCP runtime reads the chat selected by the host; compatibility depends on the local Codex version and available records. Manual compaction needs a separately verified and enrolled isolated Codex profile.

## Try the scenario planner

The planner is a separate **1.0.0** tool. It works with numbers you enter; it does not automatically read your chat. With Python 3.9 or newer, run this from the repository folder:

```sh
python3 -B mcp/demo/server.py --port 0
```

Open the local address printed in the terminal. The demo starts with example numbers. Press Ctrl+C to stop it. No extra Python packages are needed.

For example, if your reference limit is 100,000 tokens and you've used 65,000, you have 35,000 left against that reference. A task estimated at 20,000 plus a 5,000 output reserve would leave 10,000. These numbers are only as reliable as the inputs; the planner cannot guarantee when Codex will compact a chat.

To connect the planner to an MCP host, see [Local setup](docs/LOCAL-SETUP.md). The optional local observation tool requires Python 3.11+ and an exact chat and workspace binding. For a server you control, see [Self-hosting](docs/SELF-HOSTING.md). The publisher does not operate a shared budget service.

## Privacy

The numeric planner accepts numbers and fixed source labels, not chat messages or API keys. The optional local weather tools read compatible local Codex records to extract usage data; they can keep local summaries and service files. Outputs requested in Codex may also be processed by the host. If you host the planner yourself, your hosting provider and proxy may have their own logs. See the [privacy policy](docs/PRIVACY.md) for details and deletion instructions.

## Learn more and get help

- [Getting started](docs/START-HERE.md) · [Local setup](docs/LOCAL-SETUP.md) · [Self-hosting](docs/SELF-HOSTING.md)
- [Automation limits](docs/AUTOMATION.md) · [Release notes](docs/RELEASE-NOTES.md) · [Contributing](CONTRIBUTING.md)
- [Website and guides](https://feidawang.github.io/Token-Weather/) · [Report a problem](https://github.com/FeidaWang/Token-Weather/issues)

Issues are public: please don't post passwords, API keys or private chat records. Token Weather is maintained by Feida Wang, trading as RSI AI, and the software is available under the [MIT licence](LICENSE). The scenario planner's weather images have their own [attribution and licence](mcp/demo/assets/ATTRIBUTION.md).
