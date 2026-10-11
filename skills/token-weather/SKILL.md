---
name: token-weather
description: Explain how full the current Codex chat may be, show recent changes and open its local weather page when asked.
---

# Token Weather

For the person using this plugin: ask "Show the weather for this chat". A token
is a small piece of text. The weather reading is a rough guide to how much room
may be left in the chat. No setup is needed for a reading on request.

For Codex: find this skill's plugin folder two levels up. In the current Codex
project, run:

`python3 "<plugin-root>/scripts/weather.py" --workspace "<current-project>" --thread-id "$CODEX_THREAD_ID" weather`

Use `--compact` for a short answer or `--json` when you need the numbers. If
the current chat ID is not available, ask for it rather than choosing another
chat. Do not enrol the chat in a handover workflow just to show weather.

When the person asks to see the page, use `weather-service start` with the same
project and chat ID. Open the returned local URL in the browser. A running page
for the same chat is reused. `weather-service status` and `weather-service stop`
can check or close it. Use `weather --html <output.html>` for an offline page.

The reader checks that the local record belongs to this chat and project. If
the record format is new or cannot be verified, explain that the reading is
unavailable. Do not read another chat to guess. It keeps only number summaries,
not message text, in its history. A first scan may need time to catch up.

The page shows an estimate of chat space, a rough outlook and the current
model and thinking setting when known. The output tokens per second figure is
an average for the whole turn, including tool waits. It is not the model's
writing speed. Missing or old readings stay unknown. Account usage is separate
from chat space. A rainbow needs a real Codex shortening event; a lower number
alone is not proof.

For setup in another project, follow the `token-weather-setup` skill. The
public plugin has no automatic alerts. The local version can add alerts only
after Codex's normal `/hooks` review. This skill does not start new chats,
send messages, change model settings or turn on automatic handover.

## Numeric planning is separate

For explicit reference-budget scenarios use the plan-context-budget skill and its connected MCP tool. This local reading skill does not supply private chat records to the budget MCP. Do not claim a ChatGPT web context reading or quota from assumed numbers.

## Local automatic observations

If `inspect_local_context` is connected locally, use it for an explicitly requested automatic metric/budget reading. Supply the exact current root ID and workspace from trusted host metadata; do not guess the latest chat. It returns numeric estimates, available model/effort/quota readings and adaptive advice. Respect stale/unknown fields. This local tool never belongs to a remote MCP connection. Model/effort changes and chat rotation remain disabled without the exact-owner control gates in docs/AUTOMATION.md. Do not claim a recommendation was executed.
