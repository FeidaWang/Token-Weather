---
name: plan-context-budget
description: Plan token headroom, next-task reserves and a reference forecast across chat platforms from explicit numbers using plan_context_budget. Use for numeric budget scenarios; use the existing local weather skill for actual Codex readings.
---

# Plan a context budget

Use the `plan_context_budget` MCP tool for arithmetic on explicitly supplied
numbers. This skill is bundled in the MIT open-source 1.0.0 package. It is not automatically installed or connected.
Local arithmetic needs no paid model API or new subscription. A user choosing remote deployment controls its host and costs. The publisher has no shared service.

Discover the tool through the connected MCP server's tool list. Follow its
input schema. If no server is connected, say so; do not claim a tool call.
For an explicitly requested local prototype test, see
[the local instructions](../../README.md). Do not install or register it,
edit client configuration, deploy it, upload it, or submit it for review
merely to answer a planning question.

Collect the reference limit and used tokens from the user or numbers already
explicitly provided in the conversation. A platform/model name does not
establish either number. Missing fields may be omitted or null: return useful
partial calculations before asking for only the numbers needed to complete
the user's requested calculation. Never treat cumulative lifetime usage as
active context.

For a next-task scenario, send `planned_increase_tokens` and
`reserved_output_tokens` separately. The increase excludes the output reserve.
Missing estimates stay unknown; use zero only when explicitly intended.
Tag each supplied field in `sources` as `user_provided`, `assumed` or
`supplied_measurement`. Mixed measured usage and assumed growth need separate
tags. Do not call a user-supplied number verified telemetry.

For a forecast, send at most six chronological turn-end totals in
`recent_turn_tokens`. The last must match `used_tokens` if both are supplied.
Do not alter conflicting observations to make validation pass; ask the user
which observation applies. Growth needs at least three compatible totals
after the latest decrease. A decrease does not prove compaction.

Send only the documented numeric fields and source enums. Do not send message
text, full chat arrays, chat IDs, paths, API keys or identity material. Do not
read local transcripts, account data or browser sessions for this tool.
Requests for actual ChatGPT context or account quota stay unknown. An actual
local Codex weather request belongs to the existing local skill and its
permissions. Preserve the existing single-page UI and normal `/hooks` review.

Explain current headroom, next-task headroom and any overage separately. Weather
uses reference cuts at 35%, 55%, 70% and 85%. The five-turn forecast starts from
current usage; it excludes the one-off task plan and assumes the recent mean
change continues. It predicts reference-budget crossing, not an actual platform
compaction, enforced spending cap, subscription allowance or currency cost.

Example: limit 100,000; used 65,000; task increase 20,000; output reserve 5,000.
Current headroom is 35,000, planned usage is 90,000 and planned headroom is
10,000. This is a supplied-number scenario, not a reading of the current chat.
