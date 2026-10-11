"""0.160.0 notification accounting. Reports are estimates, never capability proof.

The public token window is the usable, initial-turn window (F), not raw W.
Public events omit the server-reasoning flag and the measured prefix baseline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections import deque
import math
from pathlib import Path
import time

from .accounting import I64_MAX, Inputs, Occupancy, resolve, warning, watermarks
from .storage import WeatherError, canonical_json, digest, identifier

ENGINE_VERSION = "codex-cli 0.160.0"
SCOPES = {"replay", "isolated-native", "desktop-usage-discovery"}
CONFIG_FIELDS = {"model": str, "model_provider": str, "model_reasoning_effort": str,
                 "model_context_window": int, "model_auto_compact_token_limit": int,
                 "model_auto_compact_token_limit_scope": str}
TOOL_ITEMS = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "webSearch"}


def count(value, name: str) -> int:
    if type(value) is not int or not 0 <= value <= I64_MAX:
        raise WeatherError(f"Invalid runtime token count: {name}")
    return value


def label(value, name: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > 256:
        raise WeatherError(f"Invalid runtime metadata: {name}")
    return value


@dataclass(frozen=True)
class LastUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cache_write_input_tokens: int = 0

    @classmethod
    def parse(cls, value: dict) -> LastUsage:
        if not isinstance(value, dict):
            raise WeatherError("Missing last-request usage")
        fields = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
                  "cached_input_tokens": "cachedInputTokens", "reasoning_output_tokens": "reasoningOutputTokens",
                  "total_tokens": "totalTokens", "cache_write_input_tokens": "cacheWriteInputTokens"}
        result = cls(**{key: count(value.get(wire, 0 if key == "cache_write_input_tokens" else None), wire)
                        for key, wire in fields.items()})
        if result.cached_input_tokens > result.input_tokens or result.reasoning_output_tokens > result.output_tokens:
            raise WeatherError("Runtime usage subsets exceed their parent counts")
        # After compaction/context overflow the engine can emit an estimated total
        # with zero component counters. Keep totalTokens instead of reconstructing it.
        return result


class UsageObserver:
    """Consume one ordered connection for an exact root; no transcript retention.

    Controllers can bind independently resolved limits and growth bounds. Events
    alone cannot validate active occupancy, reasoning semantics or numeric policy.
    """
    def __init__(self, thread: str, workspace: Path, engine_version: str, scope: str = "replay"):
        identifier(thread)
        if engine_version != ENGINE_VERSION or scope not in SCOPES:
            raise WeatherError("Unsupported usage protocol version or observation scope")
        self.thread, self.workspace, self.scope = thread, workspace.resolve(strict=True), scope
        self.settings = None
        self.config_candidates = {}
        self.inputs = Inputs()
        self.occupancy = Occupancy()
        self.last = None
        self.turn = None
        self.seen_turns = deque(maxlen=64)
        self.turn_active = False
        self.epoch = 0
        self.revision = 0
        self.last_event_at = None
        self.reset_reason = "awaiting root settings and usage"
        self.pending_tools: dict[str, str] = {}
        self.appended_unknown = False
        self.growth_bounds_known = False
        self.usage_events = 0
        self.compactions = 0
        self.rerouted = False

    @property
    def fingerprint(self) -> str | None:
        return digest(canonical_json(self.settings)) if self.settings is not None else None

    def _reset(self, reason: str, keep_limits: bool = False) -> None:
        self.epoch += 1
        self.last = None
        self.occupancy = Occupancy(model_fingerprint=self.fingerprint)
        self.inputs = replace(self.inputs, prefix=None,
                              provenance={k: v for k, v in self.inputs.provenance.items() if k != "prefix"}) if keep_limits else Inputs()
        self.appended_unknown = False
        self.growth_bounds_known = False
        self.reset_reason = reason

    def observe_settings(self, settings: dict, source: str) -> None:
        if not isinstance(settings, dict):
            raise WeatherError("Malformed root settings")
        cwd = settings.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or Path(cwd).resolve() != self.workspace:
            raise WeatherError("Usage settings workspace does not match the enrolled root")
        values = {"model": label(settings.get("model"), "model", optional=True),
                  "provider": label(settings.get("modelProvider"), "provider"),
                  "effort": label(settings.get("effort", settings.get("reasoningEffort")), "effort", optional=True),
                  "workspace": str(self.workspace)}
        changed = values != self.settings
        self.settings = values
        self.settings_source = source
        if changed:
            self.rerouted = False
            self._reset("model/provider/effort or root settings changed")

    def observe_thread(self, response: dict, source: str = "loaded-thread-metadata") -> None:
        thread = response.get("thread") if isinstance(response, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != self.thread or "parentThreadId" not in thread or thread["parentThreadId"] is not None:
            raise WeatherError("Usage observation requires the exact root identity, not a child")
        data = dict(thread)
        # thread/start's resolved settings are outside the Thread object.
        data.update({k: response[k] for k in ("model", "modelProvider", "reasoningEffort", "cwd") if k in response})
        self.observe_settings(data, source)

    def observe_config(self, response: dict) -> None:
        config = response.get("config") if isinstance(response, dict) else None
        if not isinstance(config, dict):
            raise WeatherError("Malformed config/read response")
        safe = {}
        for key, kind in CONFIG_FIELDS.items():
            value = config.get(key)
            if value is not None:
                safe[key] = count(value, key) if kind is int else label(value, key)
        if safe != self.config_candidates:
            self.config_candidates = safe
            self.revision += 1
            self._reset("layered disk configuration changed; active numeric policy unresolved")

    def bind_limits(self, inputs: Inputs, fingerprint: str, epoch: int, revision: int) -> None:
        """Only a controller with fresh, resolved metadata may bind numeric inputs."""
        if (fingerprint != self.fingerprint or self.fingerprint is None or self.settings["model"] is None or epoch != self.epoch
                or revision != self.revision or self.rerouted):
            raise WeatherError("Resolved limits belong to stale model/config/context")
        current_window = self.inputs.observed_usable_window
        if current_window is not None and inputs.observed_usable_window not in (None, current_window):
            raise WeatherError("Resolved limits conflict with the current runtime window")
        candidate = replace(inputs, observed_usable_window=current_window if current_window is not None else inputs.observed_usable_window)
        resolve(candidate)
        self.inputs = candidate
        self.revision += 1

    def bound_growth(self, *, appended_estimate: int, pending_output_bound: int,
                     estimator_error: int, fingerprint: str, epoch: int, revision: int) -> None:
        if (fingerprint != self.fingerprint or self.fingerprint is None or epoch != self.epoch or revision != self.revision
                or self.last is None):
            raise WeatherError("Growth bounds require the current usage revision")
        for name, value in (("appended_estimate", appended_estimate), ("pending_output_bound", pending_output_bound),
                            ("estimator_error", estimator_error)):
            count(value, name)
        self.occupancy = replace(self.occupancy, appended_estimate=appended_estimate,
                                 pending_output_bound=pending_output_bound, estimator_error=estimator_error)
        self.growth_bounds_known = True

    def _invalidate_growth(self) -> None:
        self.growth_bounds_known = False
        self.occupancy = replace(self.occupancy, pending_output_bound=None)

    def consume(self, message: dict, observed_at: float | None = None) -> bool:
        if not isinstance(message, dict) or "id" in message:
            return False  # RPC responses and server requests are not notifications.
        params = message.get("params", {})
        if not isinstance(params, dict) or params.get("threadId") != self.thread:
            return False
        method = message.get("method")
        supported = {"thread/settings/updated", "thread/tokenUsage/updated", "turn/started", "turn/completed",
                     "item/started", "item/completed", "model/rerouted"}
        if method not in supported:
            return False
        stamp = time.time() if observed_at is None else observed_at
        if (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0 or stamp > time.time() + 1
                or (self.last_event_at is not None and stamp < self.last_event_at)):
            raise WeatherError("Usage events require ordered, finite, non-future receipt times")
        if method == "thread/settings/updated":
            self.observe_settings(params.get("threadSettings"), method)
        elif method == "turn/started":
            turn = params.get("turn", {})
            new_turn = identifier(turn.get("id"))
            if new_turn != self.turn:
                if new_turn in self.seen_turns:
                    return False
                self.turn = new_turn
                self.seen_turns.append(new_turn)
                self.turn_active = True
                self._reset("new turn requires a fresh usage sample", keep_limits=not self.rerouted)
                self.rerouted = False
        else:
            turn = params.get("turn", {}).get("id") if method == "turn/completed" else params.get("turnId")
            identifier(turn)
            if self.turn is None:
                self.turn = turn  # A read-only observer may join mid-turn.
                self.seen_turns.append(turn)
                self.turn_active = True
            if turn != self.turn:
                item = params.get("item", {})
                if (method == "item/completed" and isinstance(item, dict)
                        and isinstance(item.get("id"), str) and item.get("type") in TOOL_ITEMS
                        and self.pending_tools.get(item.get("id")) == turn):
                    del self.pending_tools[item["id"]]
                    self.appended_unknown = True
                    self._invalidate_growth()
                    self.last_event_at = stamp
                    self.revision += 1
                    return True
                return False  # Never let a delayed old turn refresh current usage.
            if method == "thread/tokenUsage/updated":
                if self.settings is None:
                    raise WeatherError("Root settings must precede runtime usage")
                raw = params.get("tokenUsage")
                if not isinstance(raw, dict):
                    raise WeatherError("Malformed token usage notification")
                last = LastUsage.parse(raw.get("last"))
                window = raw.get("modelContextWindow")
                if window is not None:
                    count(window, "modelContextWindow")
                    if window == 0:
                        raise WeatherError("Runtime usable window must be positive")
                if self.inputs.observed_usable_window is not None and window != self.inputs.observed_usable_window:
                    self._reset("runtime window changed")
                elif self.last is not None and last.total_tokens < self.last.total_tokens:
                    self._reset("runtime usage decreased; measured prefix invalidated", keep_limits=True)
                # A reroute can retain the frozen initial-turn window in 0.160.0.
                # It cannot establish the step model's limit.
                provenance = {**self.inputs.provenance, "observed_usable_window": {
                    "source": "thread/tokenUsage/updated.modelContextWindow", "observed_at": stamp,
                    "semantics": "initial-turn usable window; unresolved after model reroute"}}
                self.inputs = replace(self.inputs, observed_usable_window=None if self.rerouted else window,
                                      provenance=provenance)
                resolve(self.inputs)  # Reject conflicting independently resolved inputs.
                self.last = last
                self.occupancy = Occupancy(last_input=last.input_tokens, last_output=last.output_tokens,
                    cached_input=last.cached_input_tokens, last_total=last.total_tokens,
                    observed_at=stamp, model_fingerprint=self.fingerprint)
                self.growth_bounds_known = False
                self.usage_events += 1
                self.reset_reason = None
                # Existing pending tools remain pending even when usage refreshes.
                self.appended_unknown = bool(self.pending_tools)
            elif method == "model/rerouted":
                label(params.get("toModel"), "rerouted model")
                self.rerouted = True
                self._reset("per-request model rerouted; initial-turn window is insufficient")
            elif method == "turn/completed":
                status = params["turn"].get("status")
                if status not in ("completed", "interrupted", "failed"):
                    raise WeatherError("Missing real terminal turn status")
                self.turn_active = False
                self._invalidate_growth()
                # Turn completion is not proof that background output is accounted.
            else:
                item = params.get("item")
                if not isinstance(item, dict):
                    raise WeatherError("Malformed runtime item")
                item_id = label(item.get("id"), "item ID")
                kind = label(item.get("type"), "item type")
                if kind == "contextCompaction" and method == "item/completed":
                    self.compactions += 1
                    self._reset("native context compaction requires fresh usage and prefix", keep_limits=True)
                elif kind in TOOL_ITEMS:
                    if method == "item/started":
                        if len(self.pending_tools) >= 128 and item_id not in self.pending_tools:
                            raise WeatherError("Pending runtime items exceeded the observation bound")
                        self.pending_tools[item_id] = turn
                    else:
                        self.pending_tools.pop(item_id, None)
                    self.appended_unknown = True
                    self._invalidate_growth()
                elif kind in {"userMessage", "functionCallOutput"}:
                    self.appended_unknown = True
                    self._invalidate_growth()
        self.last_event_at = stamp
        self.revision += 1
        return True

    def report(self, now: float | None = None) -> dict:
        occupancy = self.occupancy.estimate(now)
        # No public event proves the server-reasoning flag or complete appended
        # history. A user-supplied bound alone cannot make the upper bound exact.
        occupancy.update(reasoning_output_tokens=self.last.reasoning_output_tokens if self.last else None,
                         appended_items_known=not self.appended_unknown,
                         pending_tools=len(self.pending_tools), growth_bounds_known=self.growth_bounds_known,
                         upper_bound_validated=False)
        calculated = resolve(self.inputs, occupancy["estimate"])
        marks = watermarks(calculated["end_boundary"] if not calculated["unknown_inputs"] else None)
        decision = warning(occupancy, marks)
        if not occupancy["reasoning_accounted"] or self.appended_unknown or self.rerouted:
            decision = {"action": "checkpoint", "reason": "runtime reasoning/appended history or step model remains unresolved; no prevention guarantee"}
        return {"schema_version": 1, "kind": "runtime-usage-observation", "engine_version": ENGINE_VERSION,
                "acceptance_scope": self.scope, "thread_id": self.thread, "workspace": str(self.workspace),
                "model_fingerprint": self.fingerprint, "context_epoch": self.epoch, "revision": self.revision,
                "turn_id": self.turn, "turn_active": self.turn_active, "usage_events": self.usage_events,
                "compactions": self.compactions, "reset_reason": self.reset_reason,
                "settings": self.settings, "settings_source": getattr(self, "settings_source", None),
                "config_candidates": {"values": self.config_candidates, "effective_for_active_thread": False,
                                      "source": "config/read layered disk settings"},
                "last_request_usage": asdict(self.last) if self.last else None,
                "observation": {"limits": asdict(self.inputs), "occupancy": asdict(self.occupancy)},
                "accounting": calculated, "occupancy": occupancy, "watermarks": marks, "rotation_reason": decision,
                "production_promotion": False, "exact_active_occupancy": False,
                "note": "Last total is a runtime sample/estimate, not validated active occupancy. Cached input stays included. Lifetime totals and raw item text are excluded."}


def replay_usage(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"schema_version", "thread_id", "workspace", "engine_version", "thread_response", "config_response", "events"}:
        raise WeatherError("Invalid runtime usage replay envelope")
    if value.get("schema_version") != 1 or not isinstance(value.get("events"), list) or len(value["events"]) > 500:
        raise WeatherError("Usage replay requires schema 1 and at most 500 events")
    workspace = value.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise WeatherError("Usage replay workspace must be absolute")
    observer = UsageObserver(value.get("thread_id"), Path(workspace), value.get("engine_version"))
    observer.observe_thread(value.get("thread_response"), "replayed-root-metadata")
    if "config_response" in value:
        observer.observe_config(value["config_response"])
    for entry in value["events"]:
        if not isinstance(entry, dict) or set(entry) != {"observed_at", "message"}:
            raise WeatherError("Replay events need their original receipt time and notification")
        if type(entry["observed_at"]) not in (int, float):
            raise WeatherError("Replay cannot replace a missing receipt time with the current time")
        observer.consume(entry["message"], entry["observed_at"])
    return observer.report()
