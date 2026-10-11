"""Local-only numeric Codex observations, with advisory adaptation and no control RPCs."""
import math
import os
from pathlib import Path
import re
import sys
import time

from budget import BudgetInputError, plan_context_budget


def local_context(arguments, *, now=None, home=None):
    if type(arguments) is not dict or set(arguments) - {"thread_id", "workspace", "planned_increase_tokens", "reserved_output_tokens"}:
        raise BudgetInputError("Only documented local binding and numeric estimates are accepted.")
    thread = arguments.get("thread_id") or os.environ.get("CODEX_THREAD_ID")
    workspace = arguments.get("workspace") or os.environ.get("TOKEN_WEATHER_WORKSPACE")
    if not isinstance(thread, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", thread):
        raise BudgetInputError("Provide the exact current root thread ID from the host; do not select an unrelated chat.")
    if not isinstance(workspace, str) or not workspace or not Path(workspace).is_absolute():
        raise BudgetInputError("Provide the exact absolute workspace for that chat, or TOKEN_WEATHER_WORKSPACE.")
    if sys.version_info < (3, 11):
        raise BudgetInputError("Automatic local observations require an existing Python 3.11+ runtime.")
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "src"))
    from token_weather.weather_sources import find_transcript, read_rollout, configured_limit

    base = Path(home) if home is not None else None
    path = find_transcript(thread, base)
    history = read_rollout(path, thread, Path(workspace))
    limit, _source = configured_limit(Path(workspace), history.model, base)
    clock = time.time() if now is None else now
    report = history.report(limit, limit_source="local configured reference" if limit else None, now=clock)
    observed = {key: report.get(key) for key in (
        "model", "effort", "tokens", "observed_window", "reference_limit", "fraction", "age_seconds",
        "stale", "cache_hit_percent", "growth_per_turn", "compactions_observed",
    )}
    last = report.get("last_request") or {}
    observed["input_tokens"] = last.get("input")
    observed["output_tokens"] = last.get("output")
    observed["cached_input_tokens"] = last.get("cached_input")
    observed["reasoning_output_tokens"] = last.get("reasoning_output")
    observed["turn_output_tokens_per_second"] = (report.get("throughput") or {}).get("output_tokens_per_second")
    # Account observations are already present locally; no account/API requests.
    observed["account_windows"] = [{key: item.get(key) for key in (
        "window", "used_percent", "remaining_percent", "duration_minutes", "resets_at", "observed_at",
    )} for item in report.get("rate_limits", [])]
    observed["exact_active_context"] = False
    fresh = report.get("tokens") is not None and not report.get("stale")
    inputs = {"reference_limit_tokens": report.get("reference_limit"),
              "used_tokens": report.get("tokens") if fresh else None,
              "sources": {}}
    for field in ("reference_limit_tokens", "used_tokens"):
        if inputs[field] is not None:
            inputs["sources"][field] = "supplied_measurement" if field == "used_tokens" else "user_provided"
    for field in ("planned_increase_tokens", "reserved_output_tokens"):
        if field in arguments:
            inputs[field] = arguments[field]
            if arguments[field] is not None:
                inputs["sources"][field] = "assumed"
    recent = [sample for sample in history.samples if sample["epoch"] == history.epoch and sample["is_turn_end"]][-6:]
    if fresh and recent and recent[-1]["tokens"] == report["tokens"]:
        inputs["recent_turn_tokens"] = [sample["tokens"] for sample in recent]
        inputs["sources"]["recent_turn_tokens"] = "supplied_measurement"
    budget = plan_context_budget(inputs)
    remaining = budget["current"]["remaining_tokens"]
    output = observed["output_tokens"]
    # A visible policy heuristic, not a model max-output limit or spending guarantee.
    reserve = arguments.get("reserved_output_tokens")
    reserve_basis = "explicit_assumption" if reserve is not None else "unavailable"
    if reserve is None and fresh and remaining is not None and output is not None:
        reserve = min(remaining, max(1024, math.ceil(output * 1.5)))
        reserve_basis = "heuristic_1_5_times_observed_output_minimum_1024"
    max_task = max(0, remaining - reserve) if remaining is not None and reserve is not None else None
    return {
        "schema_version": 1, "mode": "local_read_only", "observations": observed, "budget": budget,
        "adaptation": {"fresh": fresh, "suggested_output_reserve_tokens": reserve,
                       "reserve_basis": reserve_basis, "suggested_max_task_increase_tokens": max_task,
                       "handover_advice": "prepare_handover" if fresh and budget["current"]["fraction"] is not None and budget["current"]["fraction"] >= .85 else "continue_within_reference" if fresh else "refresh_observation"},
        "automatic_control": {
            "model_changes_enabled": False, "effort_changes_enabled": False, "chat_rotation_enabled": False,
            "reason": "This reader has no verified Desktop owner/control connection. An isolated app-server cannot control the existing Desktop chat.",
            "required_before_enabling": ["supported exact-owner transport", "fresh runtime policy and complete task state",
                                        "explicit allowed model/effort/cost policy", "normal hook trust review and guarded continuation validation"],
        },
        "limitations": ["Local request-usage estimate, not exact active context.",
                        "Old observations remain visible but do not drive current recommendations.",
                        "Missing quotas/model/effort/throughput stay unknown; no paid or account API is called.",
                        "No configuration, model, effort, chat or hook is modified; no transcript text, IDs or paths are returned."],
    }
