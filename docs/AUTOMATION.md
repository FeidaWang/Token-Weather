# Automatic observations and control boundaries

## What the MCP now does

The remote `plan_context_budget` is still an explicit-number calculator. Its
purpose is reproducible arithmetic and structured results, not private telemetry.

The separate **local stdio** `inspect_local_context` removes manual transcription
of available Codex metrics. It reads the exact bound root and workspace, reports
compatible usage/model/effort/cache/throughput/account observations, recalculates
the reference budget and suggests a task size/output reserve. A watch command
refreshes these local recommendations automatically. Unknown/stale data stays
unknown and cannot justify a current action. These are estimates, not exact
active context or monetary budgets.

The local tool never appears in remote HTTP discovery or the Docker image.
It has no account, credential or paid model API call. Its result omits message
text, chat/turn IDs and paths. It does not modify Codex configuration or hooks.

## Model, effort and chat changes

These three controls are **disabled in open-source 1.0.0**. A read-only transcript
does not grant control of the existing Desktop chat. Starting another app-server
does not establish ownership of that chat. The source contains existing guarded
continuation work, but its current Desktop owner/complete-state gates have not
been verified. This release does not promote it to automatic mode.

For a future controlled integration, require a supported connection to the exact
owner, correct root/turn binding, complete active-task/background state, explicit
allowed models and effort ranges, a cost/plan policy and normal hook review.
Check request receipts before retrying or creating another chat. Preserve policy
and task state during handover. Do not treat an interrupt reply as proof of a
complete stop or skip guards because a read-only estimate looks near a threshold.

App Server documentation describes model/effort overrides on user turns and
thread lifecycle primitives. Using those in a harness you own is different from
controlling the Desktop chat. A successful isolated fixture or local TLS test
does not verify the latter.

Until the supported owner path and runtime policy are available, show a concrete
recommendation and say the control was not executed. Do not change global config,
silently start model turns, spend credits, bypass `/hooks`, manipulate UI settings
or invent that a chat was moved. Missing cost/entitlement information remains
unknown; changing a model/effort is not a guaranteed saving.

Source: [official App Server documentation](https://learn.chatgpt.com/docs/app-server).
