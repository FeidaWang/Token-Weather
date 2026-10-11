"""Versioned 0.157/0.160 source reference; unknown inputs stay unknown."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
import math
from typing import Any

from .storage import WeatherError

I64_MAX = 2**63 - 1
MATCHED_REFERENCES = {"rust-v0.157.0", "rust-v0.160.0"}


@dataclass(frozen=True)
class Inputs:
    window: int | None = None
    usable_percent: int | None = None
    catalog_limit: int | None = None
    explicit_limit: int | None = None
    scope: str | None = None
    prefix: int | None = None
    buffer: int | None = None
    post_turn_percent: int | None = None
    reference: str = "rust-v0.160.0"
    runtime_matched: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    observed_usable_window: int | None = None

    def __post_init__(self):
        if self.scope not in (None, "total", "body_after_prefix"):
            raise WeatherError("Unknown compaction scope")
        if type(self.runtime_matched) is not bool:
            raise WeatherError("runtime_matched must be a boolean")
        if self.reference not in MATCHED_REFERENCES:
            raise WeatherError("No accounting adapter for this engine reference")
        for key, value in asdict(self).items():
            if key in {"window", "usable_percent", "catalog_limit", "explicit_limit", "prefix", "buffer", "post_turn_percent", "observed_usable_window"}:
                if value is not None and (type(value) is not int or value < 0 or value > I64_MAX):
                    raise WeatherError(f"Invalid nonnegative integer: {key}")
        if self.window == 0 or self.observed_usable_window == 0 or (self.usable_percent is not None and not 1 <= self.usable_percent <= 100):
            raise WeatherError("Window/usable percentage is outside supported bounds")


def minimum(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return min(known) if known else None


def ceiling_percent(value: int, percentage: int) -> int:
    return (value * percentage + 99) // 100


def resolve(inputs: Inputs, used: int | None = None) -> dict:
    if used is not None and (type(used) is not int or used < 0 or used > I64_MAX):
        raise WeatherError("Active context usage must be a nonnegative integer")
    w = inputs.window
    # Explicit total scope override becomes the ModelInfo limit before its 90% clamp.
    c = inputs.explicit_limit if inputs.explicit_limit is not None else inputs.catalog_limit
    a = minimum([c, w * 9 // 10 if w is not None else None])
    f = min(I64_MAX, w * inputs.usable_percent) // 100 if w is not None and inputs.usable_percent is not None else None
    if inputs.observed_usable_window is not None:
        if f is not None and f != inputs.observed_usable_window:
            raise WeatherError("Observed usable window conflicts with resolved model inputs")
        f = inputs.observed_usable_window
    limit = None if inputs.scope is None else inputs.explicit_limit if inputs.scope == "body_after_prefix" and inputs.explicit_limit is not None else a
    scope_used = used if inputs.scope == "total" else (max(0, used - inputs.prefix) if inputs.scope == "body_after_prefix" and used is not None and inputs.prefix is not None else None)
    buffered = min(I64_MAX, limit + inputs.buffer) if limit is not None and inputs.buffer is not None else None
    scope_boundary = buffered if inputs.scope == "total" else (min(I64_MAX, inputs.prefix + buffered) if inputs.prefix is not None and buffered is not None else None)
    step = minimum([scope_boundary, f])
    post = ceiling_percent(f, inputs.post_turn_percent) if f is not None and inputs.post_turn_percent is not None and inputs.post_turn_percent > 0 else None
    end = minimum([step, post])
    full_reached = used >= f if used is not None and f is not None else None
    scope_reached = scope_used >= buffered if scope_used is not None and buffered is not None else None
    def tri_or(a: bool | None, b: bool | None) -> bool | None:
        if a is True or b is True:
            return True
        return False if a is False and b is False else None
    reached = tri_or(full_reached, scope_reached)
    # Match the engine's integer cross-multiplication predicate, not float rounding.
    if inputs.post_turn_percent is None:
        engine_end = None
    elif inputs.post_turn_percent == 0:
        engine_end = False
    else:
        engine_end = tri_or(reached, used * 100 >= f * inputs.post_turn_percent if used is not None and f is not None else None)
    complete = (inputs.scope is not None and w is not None and f is not None and inputs.buffer is not None
                and inputs.post_turn_percent is not None and (inputs.scope == "total" or inputs.prefix is not None))
    confidence = "exact-for-matched-inputs" if complete and inputs.runtime_matched else "reference-only" if complete else "partial/unknown"
    return {"inputs": asdict(inputs), "active_context_used": used, "scope_used": scope_used,
            "scope_limit": limit, "usable_window": f, "auto_limit": a, "buffered_scope_limit": buffered,
            "step_boundary": step, "end_boundary": end, "full_window_reached": full_reached,
            "scope_limit_reached": scope_reached, "step_compaction_predicate": reached,
            "engine_post_turn_predicate": engine_end, "confidence": confidence,
            "unknown_inputs": [key for key in ("window", "usable_percent", "scope", "buffer", "post_turn_percent")
                               if getattr(inputs, key) is None and not (key == "usable_percent" and inputs.observed_usable_window is not None)]
                              + (["prefix"] if inputs.scope == "body_after_prefix" and inputs.prefix is None else []),
            "note": "Known scalar boundaries are partial bounds when inputs are missing; lifetime billed tokens are excluded."}


@dataclass(frozen=True)
class Occupancy:
    last_input: int | None = None
    last_output: int = 0
    cached_input: int = 0
    appended_estimate: int = 0
    pending_output_bound: int | None = None
    estimator_error: int = 0
    observed_at: float | None = None
    exact_active: int | None = None
    reasoning_accounted: bool = False
    model_fingerprint: str | None = None
    last_total: int | None = None

    def estimate(self, now: float | None = None, max_age: float = 30.0) -> dict:
        if type(self.reasoning_accounted) is not bool:
            raise WeatherError("reasoning_accounted must be a boolean")
        if self.observed_at is not None and (type(self.observed_at) not in (int, float) or not math.isfinite(self.observed_at)):
            raise WeatherError("observed_at must be a finite timestamp")
        for key in ("last_input", "last_output", "cached_input", "appended_estimate", "pending_output_bound", "estimator_error", "exact_active", "last_total"):
            v = getattr(self, key)
            if v is not None and (type(v) is not int or v < 0):
                raise WeatherError(f"Invalid usage observation: {key}")
        age = None if self.observed_at is None else max(0.0, (now if now is not None else time.time()) - self.observed_at)
        last = self.last_total if self.last_total is not None else (self.last_input + self.last_output if self.last_input is not None else None)
        estimate = self.exact_active if self.exact_active is not None else (last + self.appended_estimate if last is not None else None)
        # Cached input is already included in input occupancy. Never subtract it.
        upper = estimate + self.pending_output_bound + self.estimator_error if estimate is not None and self.pending_output_bound is not None else None
        stale = age is None or age > max_age
        exact = self.exact_active is not None and not stale and self.reasoning_accounted
        return {"estimate": estimate, "upper_bound": upper, "age_seconds": age, "stale": stale,
                "confidence": "validated-active" if exact else "stale" if stale else "estimate",
                "pending_output_known": self.pending_output_bound is not None,
                "reasoning_accounted": self.reasoning_accounted, "model_fingerprint": self.model_fingerprint}


def watermarks(boundary: int | None, bootstrap_budget: int = 1500, work_budget: int = 4000) -> dict:
    if boundary is None:
        return {"prepare_at": None, "rotate_at": None, "reserve": None, "reason": "native boundary unknown"}
    reserve = max(16_000, ceiling_percent(boundary, 10))
    available = boundary - bootstrap_budget - work_budget
    if available < 2:
        return {"prepare_at": None, "rotate_at": None, "reserve": None, "reason": "insufficient bootstrap/work headroom"}
    adjusted = False
    if 2 * reserve > available:
        reserve = available // 2
        adjusted = True
    return {"prepare_at": boundary - 2 * reserve, "rotate_at": boundary - reserve, "reserve": reserve,
            "reason": "small-window reserve requires measured budget validation" if adjusted else "conservative startup reserve",
            "measured_budget_required": adjusted}


def warning(usage: dict, marks: dict, predicted_burst: int = 0) -> dict:
    upper = usage.get("upper_bound")
    if usage.get("stale") or upper is None or marks.get("rotate_at") is None:
        return {"action": "checkpoint", "reason": "usage/headroom is stale or unknown; no prevention guarantee"}
    predicted = upper + predicted_burst
    action = "rotate-at-safe-milestone" if predicted >= marks["rotate_at"] else "prepare" if predicted >= marks["prepare_at"] else "observe"
    return {"action": action, "reason": f"Predicted active occupancy {predicted}", "predicted_burst": predicted_burst}
