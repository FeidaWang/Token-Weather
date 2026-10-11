# Local setup

The budget MCP and demo require an existing Python 3.9+ runtime and use only the standard library. From the package root:

```sh
python3 -B mcp/server.py --transport stdio
python3 -B mcp/demo/server.py --port 0
python3 -B -m unittest discover -s mcp/tests -v
```

stdio exits on EOF; stop the HTTP demo with Ctrl+C. Default HTTP binding is `127.0.0.1`. Optional external binding is described in SELF-HOSTING.md and requires an explicit public HTTPS origin/TLS proxy.

Root `mcp.json` launches `mcp/local_server.py` for planning and local observations. It requires a precisely bound root/workspace; automatic local inspection needs Python 3.11+ (or an existing interpreter selected through TOKEN_WEATHER_PYTHON). Remote deployments keep `mcp/server.py`, which has no local observation tool. Unpacking does not install the plugin, register a client, change account configuration or connect ChatGPT. A browser harness is not an installed ChatGPT/Codex plugin.

Optional local Codex weather requires Python 3.11+ and supported local records. Use the existing weather skill for the requested chat. Do not read unrelated chats or treat lifetime usage as active context. Budget planning itself has no local chat access.

This source copy has no bundled hooks and does not register them. The separate local package keeps supported setup and normal `/hooks` review. Do not edit trust stores or bypass review. Existing configuration and the original page are unchanged.

Protect installed browsers and personal profiles. Use provided browser tools or a separate isolated browser for automation; no browser repair or bundle modification is needed.
