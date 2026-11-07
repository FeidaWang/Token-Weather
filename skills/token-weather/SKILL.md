---
name: token-weather
description: Read the current Codex chat's context weather and auto-compact forecast, or open its local panel.
---

# Token Weather

Use the installed `token_weather_context` MCP tool to open the current chat's
panel. The host supplies the chat identity. Do not put a different chat ID in
tool arguments.

If the MCP tool is unavailable, locate this plugin's root two directories above
this skill and run its bundled Rust executable:

```sh
"<plugin-root>/bin/token-weather-core" snapshot --thread-id "$CODEX_THREAD_ID"
```

If the exact current chat ID is missing, ask for it. Never substitute a recently
active chat. `--codex-home` can explicitly select a profile the user authorised.
The reader is read-only by default; persistent numeric caching requires an
explicit `--state-dir` in a task-owned location.

To open the local panel, run the executable's `serve --thread-id ID` command in a
persistent terminal session. Open the printed loopback URL and retain the tab
as the deliverable. Stop only that task-owned server when requested.

Context usage is an estimate from the latest reported request. The observed
window and a verified automatic-compaction boundary are distinct. Forecasts
use completed-turn history and pause when readings are stale or incomplete.
A compaction marker requires a reported compaction; a lower number alone does
not prove it. Token flow separates input, cache reads and writes, other output,
and reasoning when Codex reports those counts. Recorded cumulative chat usage
is not a session bill or a subscription allowance.

The ordinary local reader cannot prove ownership of a Desktop App Server, so
live plugin/connector inventories and effective compaction boundaries may be
unavailable. The separately configured, pinned isolated owner adapter must
verify its peer, engine ancestry, exact loaded root and workspace before
supplying those fields. Never invent unavailable measurements or promote a
local configuration value to a live boundary.

The ordinary Rust runtime does not create chats, compact sessions, install
plugins, authorise connectors or modify configuration. In the verified
isolated Desktop, selecting and sending the Token Weather plugin mention
registers that chat for the manual Compact session button. Historical
checkpoint/control helpers are separate legacy workflows.
