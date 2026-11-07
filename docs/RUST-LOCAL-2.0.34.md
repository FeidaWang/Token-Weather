# Token Weather Rust 2.0.34

8 November 2026 · macOS on Apple silicon

The Codex panel reveals each subtitle with its chart, in order from top to bottom. The full entrance takes about 1.7 seconds. This version removes the decorative scan line. Context forecast and Chat usage open by default when data is available. You can collapse either one. The panel respects the system's Reduce Motion setting.

The standalone weather page in the repository uses the borderless history chart and the same slower entrance for its sections.

## Restore this version

Download `token-weather-rust-local-2.0.34-darwin-arm64.zip` from the release, extract it and point your local MCP registration at the extracted `bin/token-weather-core`. Reload the plugin in the host and run the executable with `--version` to confirm `2.0.34`. The tested legacy Codex 0.167 profile uses an absolute MCP executable path.

The ZIP's SHA-256 is `66c193c4f279625f57b9809e5f0a7b24118f96c40b47ee2ec93115fe73790066`.

[Release assets and checksums](https://github.com/FeidaWang/Token-Weather/releases/tag/rust-v2.0.34)

To build the exact source snapshot, switch a clean clone to the release tag.

```sh
git switch --detach rust-v2.0.34
cargo build --release --locked --manifest-path rust/token-weather-core/Cargo.toml
rust/token-weather-core/target/release/token-weather-core serve --demo
```

Open the printed loopback URL for a read-only preview with synthetic data. The ordinary MCP runtime reads the chat selected by the host. Manual compaction requires a separately verified and enrolled isolated Codex profile.
