---
name: token-weather-setup
description: Help someone start Token Weather budget planning in ChatGPT or Codex, distinguish explicit-number MCP planning from optional local Codex readings, and preserve normal hook review.
---

# Start using Token Weather

Explain the two modes using the packaged README and docs/START-HERE.md. Numeric planning uses explicit numbers; the local inspector and weather mode use precisely bound compatible Codex records. Do not imply either mode is connected merely because this skill was loaded.

For a supplied-number planning request, discover `plan_context_budget` in the connected MCP tool list and use the planning skill. If unavailable, report the missing connection and return useful guidance without claiming a tool call. Do not auto-install, register a client, deploy or upload a package.

The local stdio MCP exposes planning and `inspect_local_context`; remote HTTP exposes numeric planning only. The README shows a dependency-free local demo. docs/SELF-HOSTING.md describes deployment controlled by the user; the publisher has no shared endpoint. Neither unpacking nor a demo connects ChatGPT. Provide deployment guidance when requested, but do not create resources, incur charges or publish without the user choosing that action.

For an explicitly requested local Codex reading/page, use the existing weather skill in the requested chat. Python 3.11+ and supported local records are needed; missing or stale values stay unknown. Numeric planning itself does not need those files.

This public preparation copy has no bundled hooks. Do not register project hooks from it. In the separate local hooks package, reuse the supported setup/check flow and let the person review exact new definitions in normal `/hooks`. Do not edit trust stores or bypass review. Preserve the existing one-page weather display and installed configuration.

Support is public GitHub Issues. Never request full transcripts, credentials or identity documents. Automatic chat moving remains off. This skill does not authorise purchases, paid API calls or external publishing.
