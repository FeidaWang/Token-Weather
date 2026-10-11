"""Weather presentation of measured requests; never authorizes runtime control."""
from __future__ import annotations

from dataclasses import asdict
import math
import time

from .runtime_usage import LastUsage, count
from .storage import WeatherError, canonical_json, digest, identifier

LEVELS = (
    {"rank": 0, "icon": "☀️", "name": "Clear", "label": "Low use", "color": "#0ca30c", "tag": "Plenty of room"},
    {"rank": 1, "icon": "🌤️", "name": "Fair", "label": "Still roomy", "color": "#0ca30c", "tag": "Room left"},
    {"rank": 2, "icon": "☁️", "name": "Cloudy", "label": "Chat growing", "color": "#fab219", "tag": "This chat is growing"},
    {"rank": 3, "icon": "🌧️", "name": "Showers", "label": "Getting fuller", "color": "#ec835a", "tag": "Getting close to the limit"},
    {"rank": 4, "icon": "⛈️", "name": "Storm", "label": "Very full", "color": "#d03b3b", "tag": "Very little room left"},
)
CLEARING = {"rank": 5, "icon": "🌈", "name": "Clearing", "label": "Clearing up", "color": "#0ca30c", "tag": "Codex shortened the chat"}
UNKNOWN = {"rank": -1, "icon": "○", "name": "Unknown", "label": "Waiting for a reading", "color": "#88939e", "tag": "No recent reading yet"}
CUTS = (.35, .55, .70, .85)


def level_of(fraction: float) -> dict:
    return dict(LEVELS[next((i for i, cut in enumerate(CUTS) if fraction < cut), 4)])


def timestamp(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise WeatherError("Weather timestamps must be finite nonnegative seconds")
    return float(value)


def rate_limits(value: object, at: float) -> list[dict]:
    """Accept public camelCase or the versioned rollout's snake_case representation."""
    if not isinstance(value, dict):
        return []
    buckets = value.get("rateLimitsByLimitId")
    if isinstance(buckets, dict):
        sources = [(key, item) for key, item in buckets.items()]
    else:
        item = value.get("rateLimits", value)
        sources = [(item.get("limitId", item.get("limit_id", "codex")), item)] if isinstance(item, dict) else []
    result = []
    for key, bucket in sources:
        if not isinstance(key, str) or not isinstance(bucket, dict):
            continue
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent", window.get("used_percent"))
            minutes = window.get("windowDurationMins", window.get("window_minutes"))
            reset = window.get("resetsAt", window.get("resets_at"))
            if (type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100
                    or type(minutes) is not int or minutes <= 0):
                continue
            result.append({"bucket": key[:96], "window": name, "used_percent": used,
                           "remaining_percent": 100 - used, "duration_minutes": minutes,
                           "resets_at": reset if type(reset) in (int, float) and math.isfinite(reset) and reset >= 0 else None,
                           "observed_at": at})
    return result


class WeatherHistory:
    def __init__(self, thread: str, source: str):
        self.thread, self.source = thread, source
        self.samples: list[dict] = []
        self.epoch = 0
        self.model = None
        self.effort = None
        self.window = None
        self.turn = None
        self.turn_started_at = None
        self.turn_output_tokens = 0
        self.turn_last_usage_at = None
        self.limits: list[dict] = []
        self.compactions = 0
        self.reset_reason = None
        self.pending_compaction = False
        self.current = None
        self.signature = None
        self.last_request_key_digest = None
        self.truncated = False

    def reset(self, reason: str, compacted: bool = False) -> None:
        self.epoch += 1
        self.current = self.signature = None
        self.reset_reason = reason
        self.pending_compaction = compacted
        self.turn_started_at = None
        self.turn_output_tokens = 0
        self.turn_last_usage_at = None
        if compacted:
            self.compactions += 1

    def settings(self, model: object, window: object = None, effort: object = None) -> None:
        if model is not None and (not isinstance(model, str) or not model or len(model) > 256):
            raise WeatherError("Invalid weather model metadata")
        if effort is not None and (not isinstance(effort, str) or not effort or len(effort) > 64):
            raise WeatherError("Invalid weather effort metadata")
        if window is not None and count(window, "modelContextWindow") == 0:
            raise WeatherError("Weather window must be positive")
        if ((self.model is not None and model is not None and self.model != model)
                or (self.effort is not None and effort is not None and self.effort != effort)
                or (self.window is not None and window is not None and self.window != window)):
            self.reset("model/effort/window changed")
        if model is not None:
            self.model = model
        if effort is not None:
            self.effort = effort
        if window is not None:
            self.window = window

    def begin_turn(self, turn: object, at: float) -> None:
        if turn is None:
            return
        turn = identifier(turn)
        stamp = timestamp(at)
        if self.turn != turn or self.turn_started_at is None:
            self.turn_output_tokens = 0
            self.turn_last_usage_at = None
            self.turn_started_at = stamp
        self.turn = turn

    def request(self, usage: dict, window: int | None, at: float, request_key: object = None) -> None:
        stamp = timestamp(at)
        last = LastUsage.parse(usage)
        self.settings(self.model, window)
        signature = digest(canonical_json([self.turn, asdict(last), request_key]))
        if signature == self.signature:
            return  # Rate-limit-only repeats do not refresh the context reading's age.
        meter_key = (digest(canonical_json(request_key)) if type(request_key) is int and request_key >= 0 else None)
        fresh_request = meter_key is not None and meter_key != self.last_request_key_digest
        if self.current is not None and last.total_tokens < self.current["tokens"]:
            self.reset("usage decreased; compaction not proven")
        sample = {"at": stamp, "tokens": last.total_tokens, "input": last.input_tokens,
                  "output": last.output_tokens, "cached_input": last.cached_input_tokens,
                  "reasoning_output": last.reasoning_output_tokens, "turn": self.turn,
                  "epoch": self.epoch, "is_compaction": self.pending_compaction,
                  "is_turn_end": False}
        self.samples.append(sample)
        self.samples = self.samples[-400:]
        self.current, self.signature = sample, signature
        if fresh_request and self.turn_started_at is not None and self.turn is not None and stamp >= self.turn_started_at:
            self.turn_output_tokens += last.output_tokens
            self.turn_last_usage_at = stamp
        if meter_key is not None:
            self.last_request_key_digest = meter_key
        self.pending_compaction = False

    def end_turn(self, turn: str | None) -> None:
        if self.current is not None and self.current["turn"] == turn:
            self.current["is_turn_end"] = True

    def report(self, compact_limit: int | None = None, *, limit_source: str | None = None,
               now: float | None = None, max_age: float = 120) -> dict:
        clock = timestamp(time.time() if now is None else now)
        if not math.isfinite(max_age) or max_age <= 0:
            raise WeatherError("Weather max age must be positive and finite")
        if compact_limit is not None and count(compact_limit, "compact_limit") == 0:
            raise WeatherError("Weather reference limit must be positive")
        last = self.current
        tokens = last["tokens"] if last else None
        limit = min(x for x in (compact_limit, self.window) if x is not None) if compact_limit or self.window else None
        is_compaction_limit = compact_limit is not None and limit == compact_limit
        age = max(0, clock - last["at"]) if last else None
        stale = age is None or age > max_age or (last is not None and last["at"] > clock + 1)
        fraction = tokens / limit if tokens is not None and limit is not None else None
        level = dict(CLEARING) if self.pending_compaction or (last and last["is_compaction"]) else level_of(fraction) if fraction is not None else dict(UNKNOWN)
        recent = [s for s in self.samples if s["epoch"] == self.epoch]
        ends = [s for s in recent if s["is_turn_end"]]
        if recent and (not ends or ends[-1] is not recent[-1]):
            ends.append(recent[-1])
        ends = ends[-6:]
        rises = [b["tokens"] - a["tokens"] for a, b in zip(ends, ends[1:]) if b["tokens"] > a["tokens"]]
        rate = sum(rises) / len(rises) if rises else None
        # A single active turn is not enough evidence for a per-turn prediction.
        turns_left = max(0, math.ceil((limit - tokens) / rate)) if rate and fraction is not None and not stale else None
        outlook = [{"step": step, "tokens": tokens + rate * step,
                    "fraction": (tokens + rate * step) / limit,
                    "level": level_of((tokens + rate * step) / limit)}
                   for step in range(1, 6)] if rate and fraction is not None and not stale else []
        cache_hit = 100 * last["cached_input"] / last["input"] if last and last["input"] else None
        duration = (self.turn_last_usage_at - self.turn_started_at
                    if self.turn_started_at is not None and self.turn_last_usage_at is not None else None)
        throughput = ({"output_tokens_per_second": self.turn_output_tokens / duration,
                       "output_tokens": self.turn_output_tokens, "duration_seconds": duration,
                       "observed_at": self.turn_last_usage_at, "scope": "turn-wall-clock-through-last-usage"}
                      if duration is not None and duration >= 1 and self.turn_output_tokens > 0
                      and last is not None and last["turn"] == self.turn and not stale
                      and self.turn_last_usage_at <= clock + 1
                      and clock - self.turn_last_usage_at <= max_age else None)
        result = {"schema_version": 1, "thread_id": self.thread, "source": self.source,
                  "model": self.model, "effort": self.effort, "observed_window": self.window, "reference_limit": limit,
                  "reference_kind": "configured-compaction-target" if is_compaction_limit else "observed-window",
                  "limit_source": limit_source if is_compaction_limit else "runtime modelContextWindow",
                  "tokens": tokens, "fraction": fraction,
                  "window_percent": 100 * tokens / self.window if tokens is not None and self.window else None,
                  "level": level, "age_seconds": age, "stale": stale, "generated_at": clock,
                  "max_age_seconds": max_age,
                  "growth_per_turn": rate, "rate_basis": len(rises), "turns_left": turns_left,
                  "throughput": throughput,
                  "outlook": outlook, "cache_hit_percent": cache_hit,
                  "last_request": dict(last) if last else None, "samples": self.samples,
                  "rate_limits": self.limits, "compactions_observed": self.compactions,
                  "reset_reason": self.reset_reason, "history_truncated": self.truncated,
                  "exact_active_context": False, "usd": None, "context_categories": None,
                  "note": "The chat space number is an estimate. Reused input and thinking tokens are already included. Output tokens per second is a whole turn average that includes tool waits. It is not the model's writing speed. The set limit is only a guide."}
        return result


def fmt_tokens(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1e6:
        return f"{value / 1e6:.1f}".removesuffix(".0") + "M"
    if value >= 1e3:
        return f"{value / 1e3:.1f}".removesuffix(".0") + "k"
    return str(round(value))


def terminal_weather(report: dict, compact: bool = False) -> str:
    level = report["level"]
    fraction = report["fraction"]
    percent = f" | {fraction:.0%}" if fraction is not None else ""
    stale = " | Old reading" if report["stale"] else ""
    text = f'{level["icon"]} {level["name"]} | {level["label"]} | {fmt_tokens(report["tokens"])}/{fmt_tokens(report["reference_limit"])}{percent}{stale}'
    if report["turns_left"] is not None:
        text += f' | about {report["turns_left"]} turns to the reference limit'
    if compact:
        return text
    filled = round(min(1, max(0, fraction or 0)) * 24)
    lines = [text, "█" * filled + "░" * (24 - filled),
             f'Model {report["model"] or "unknown"} | effort {report["effort"] or "unknown"} | Source {report["source"]} | {report["reference_kind"]}']
    if report["throughput"] is not None:
        lines.append(f'Average output this turn: {report["throughput"]["output_tokens_per_second"]:.1f} tokens per second (including tool waits)')
    if report["growth_per_turn"] is not None:
        lines.append(f'Recent {report["rate_basis"]} turns, average growth {fmt_tokens(report["growth_per_turn"])} tokens per turn')
    if report["cache_hit_percent"] is not None:
        lines.append(f'Recent reused input: {report["cache_hit_percent"]:.0f}% (reused input still uses chat space)')
    for quota in report["rate_limits"]:
        lines.append(f'Account {quota["bucket"]} | {quota["duration_minutes"]} minute window | used {quota["used_percent"]:g}%')
    lines.append(report["note"])
    return "\n".join(lines)
