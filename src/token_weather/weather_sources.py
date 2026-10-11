"""Public notification adapter plus an explicitly versioned read-only fallback."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import tomllib

from .storage import WeatherError, identifier, read_bytes
from .weather import WeatherHistory, rate_limits, timestamp

ROLLOUT_VERSIONS = {"0.160.0", "0.162.0-alpha.17.2"}
TAIL_BYTES = 4 * 1024 * 1024
LINE_BYTES = 256 * 1024


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def find_transcript(thread: str, home: Path | None = None) -> Path:
    identifier(thread)
    base = home or codex_home()
    matches = []
    for name in ("sessions", "archived_sessions"):
        directory = base / name
        if directory.is_dir() and not directory.is_symlink():
            matches.extend(directory.rglob("*" + thread + ".jsonl"))
    if len(matches) != 1:
        raise WeatherError("An exact unique transcript is required; pass weather --transcript PATH")
    return matches[0]


def _stamp(value: object) -> float:
    if not isinstance(value, str):
        raise WeatherError("Transcript record has no timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return timestamp(parsed.timestamp())
    except ValueError as exc:
        raise WeatherError("Invalid transcript timestamp") from exc


def _last_usage(value: dict) -> dict:
    if not isinstance(value, dict):
        raise WeatherError("Unsupported transcript last_token_usage shape")
    fields = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
              "cachedInputTokens": "cached_input_tokens", "reasoningOutputTokens": "reasoning_output_tokens",
              "totalTokens": "total_tokens", "cacheWriteInputTokens": "cache_write_input_tokens"}
    return {key: value.get(wire, 0 if key == "cacheWriteInputTokens" else None) for key, wire in fields.items()}


def _effort(value: dict) -> object:
    for key in ("reasoningEffort", "reasoning_effort", "model_reasoning_effort", "effort"):
        if value.get(key) is not None:
            return value[key]
    return None


def read_rollout(path: Path, thread: str, workspace: Path) -> WeatherHistory:
    """Read only the exact root's header and bounded tail, retaining no messages."""
    identifier(thread)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise WeatherError("Weather transcript must not be a symlink")
    with path.open("rb") as stream:
        header = stream.readline(LINE_BYTES + 1)
        if len(header) > LINE_BYTES or not header.endswith(b"\n"):
            raise WeatherError("Unsupported or incomplete transcript header")
        try:
            first = json.loads(header)
        except (ValueError, UnicodeError) as exc:
            raise WeatherError("Unsupported transcript header") from exc
        if not isinstance(first, dict):
            raise WeatherError("Unsupported transcript header")
        meta = first.get("payload", {})
        if (first.get("type") != "session_meta" or not isinstance(meta, dict) or meta.get("id") != thread
                or not isinstance(meta.get("cwd"), str) or Path(meta["cwd"]).resolve() != workspace.resolve()):
            raise WeatherError("Transcript identity/workspace does not match the requested root")
        source = meta.get("source")
        if (isinstance(source, dict) and "subagent" in source) or any(meta.get(key) is not None for key in ("parent_thread_id", "parentThreadId")):
            raise WeatherError("Weather fallback requires a root transcript, not a subagent")
        version = meta.get("cli_version")
        if version not in ROLLOUT_VERSIONS:
            raise WeatherError(f"Transcript format for {version!r} is unverified; use public weather --events instead")
        stream.seek(0, 2)
        size = stream.tell()
        start = max(len(header), size - TAIL_BYTES)
        stream.seek(start)
        if start > len(header):
            stream.readline(LINE_BYTES + 1)  # discard a possibly partial record
        data = stream.read(TAIL_BYTES)
    history = WeatherHistory(thread, "local-rollout/" + version)
    history.truncated = start > len(header)
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) > LINE_BYTES:
            history.truncated = True
            continue  # append-in-progress and large text records are not measurements
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            history.truncated = True
            continue
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            continue
        payload, kind = row["payload"], row.get("type")
        if kind == "turn_context":
            history.settings(payload.get("model"), effort=_effort(payload))
            history.begin_turn(payload.get("turn_id"), _stamp(row.get("timestamp")))
        elif kind == "compacted":
            if not history.pending_compaction:
                history.reset("native transcript compaction", compacted=True)
        elif kind == "event_msg":
            event = payload.get("type")
            if event == "task_started":
                history.settings(history.model, payload.get("model_context_window"))
                history.begin_turn(payload.get("turn_id"), _stamp(row.get("timestamp")))
            elif event in ("task_complete", "task_completed", "turn_aborted"):
                history.end_turn(payload.get("turn_id", history.turn))
            elif event == "context_compacted":
                # Some versions emit both an event and a compacted record.
                if not history.pending_compaction:
                    history.reset("native transcript compaction", compacted=True)
            elif event == "token_count":
                at = _stamp(row.get("timestamp"))
                if payload.get("rate_limits") is not None:
                    history.limits = rate_limits(payload["rate_limits"], at)
                info = payload.get("info")
                if info is None:
                    continue
                if not isinstance(info, dict):
                    raise WeatherError("Unsupported transcript token_count shape")
                total = info.get("total_token_usage")
                key = total.get("total_tokens") if isinstance(total, dict) else None
                history.request(_last_usage(info.get("last_token_usage")), info.get("model_context_window"), at, key)
    return history


def read_notifications(path: Path, thread: str) -> WeatherHistory:
    """Bounded replay of captured public notifications, not a Desktop connection."""
    identifier(thread)
    history = WeatherHistory(thread, "app-server-notification-replay")
    last_at = None
    for line in read_bytes(path, TAIL_BYTES).splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise WeatherError("Weather event record must be an object")
        at = timestamp(row.get("observed_at"))
        if last_at is not None and at < last_at:
            raise WeatherError("Weather event replay must be in receipt order")
        last_at = at
        message = row.get("message")
        if not isinstance(message, dict) or "id" in message:
            continue
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(params, dict):
            continue
        if method == "account/rateLimits/updated":
            history.limits = rate_limits(params, at)
            continue
        if params.get("threadId") != thread:
            continue
        if method == "thread/settings/updated":
            settings = params.get("threadSettings", {})
            if not isinstance(settings, dict):
                raise WeatherError("Malformed thread settings notification")
            history.settings(settings.get("model"), effort=_effort(settings))
        elif method == "turn/started":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise WeatherError("Malformed turn notification")
            history.begin_turn(turn.get("id"), at)
        elif method == "model/rerouted":
            history.reset("model rerouted")
            history.window = None
            history.settings(params.get("toModel"), effort=_effort(params))
        elif method == "thread/tokenUsage/updated":
            turn = identifier(params.get("turnId"))
            if history.turn is not None and history.turn != turn:
                continue  # delayed updates cannot refresh another active turn
            history.turn = turn
            usage = params.get("tokenUsage")
            if not isinstance(usage, dict):
                raise WeatherError("Malformed usage notification")
            total = usage.get("total")
            key = total.get("totalTokens") if isinstance(total, dict) else None
            history.request(usage.get("last"), usage.get("modelContextWindow"), at, key)
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise WeatherError("Malformed turn notification")
            history.end_turn(turn.get("id"))
        elif method == "item/completed":
            item = params.get("item", {})
            if isinstance(item, dict) and item.get("type") == "contextCompaction" and params.get("turnId") == history.turn:
                history.reset("native contextCompaction item", compacted=True)
    return history


def configured_limit(workspace: Path, model: str | None, home: Path | None = None) -> tuple[int | None, str | None]:
    """Disk candidates are advisory and never promoted to effective runtime policy."""
    config = {}
    source = None
    for path in ((home or codex_home()) / "config.toml", workspace / ".codex/config.toml"):
        if not path.exists():
            continue
        try:
            data = tomllib.loads(read_bytes(path).decode())
        except (OSError, ValueError, WeatherError):
            continue
        for key in ("model", "model_auto_compact_token_limit", "model_auto_compact_token_limit_scope"):
            if key in data:
                config[key] = data[key]
                if key == "model_auto_compact_token_limit":
                    source = str(path)
    limit = config.get("model_auto_compact_token_limit")
    if (type(limit) is int and limit > 0 and config.get("model_auto_compact_token_limit_scope") == "total"
            and model is not None and config.get("model") == model):
        return limit, "disk candidate: " + str(source)
    return None, None


def collect_weather(workspace: Path, thread: str, *, transcript: Path | None = None,
                    events: Path | None = None, compact_limit: int | None = None,
                    max_age: float = 120, now: float | None = None, cache_root: Path | None = None,
                    hook_event: dict | None = None) -> dict:
    if transcript is not None and events is not None:
        raise WeatherError("Choose a transcript or public event replay")
    if events:
        history = read_notifications(events, thread)
        collection = None
        limit, source = None, None  # local config does not describe a captured remote session
    else:
        from .weather_cache import cached_rollout
        history, collection = cached_rollout(transcript or find_transcript(thread), thread, workspace,
                                             cache_root=cache_root, hook_event=hook_event)
        limit, source = configured_limit(workspace, history.model)
    if compact_limit is not None:
        limit, source = compact_limit, "explicit advisory target"
    report = history.report(limit, limit_source=source, max_age=max_age, now=now)
    if collection is not None:
        report["collection"] = collection
        if collection["catching_up"]:
            report.update(stale=True, turns_left=None, outlook=[], throughput=None)
    return report
