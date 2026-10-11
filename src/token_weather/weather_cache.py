"""Shared incremental numeric history. No transcript text is persisted."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .runtime_usage import count
from .storage import WeatherError, atomic_json, digest, identifier, lock, owned_path, read_json
from .weather import WeatherHistory, rate_limits, timestamp
from .weather_sources import LINE_BYTES, ROLLOUT_VERSIONS, _effort, _last_usage, _stamp, codex_home

CACHE_VERSION = 2
READ_BUDGET = 8 * 1024 * 1024
ANCHOR_BYTES = 2048
SAMPLE_KEYS = {"at", "tokens", "input", "output", "cached_input", "reasoning_output", "turn", "epoch", "is_compaction", "is_turn_end"}


def data_directory() -> Path:
    path = Path(os.environ.get("TOKEN_WEATHER_DATA_DIR", codex_home() / "state/token-weather")).expanduser().absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise WeatherError("Weather data directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def encode_history(history: WeatherHistory) -> dict:
    return {"samples": history.samples, "epoch": history.epoch, "model": history.model, "effort": history.effort,
            "window": history.window, "turn": history.turn, "limits": history.limits,
            "turn_started_at": history.turn_started_at, "turn_last_usage_at": history.turn_last_usage_at,
            "turn_output_tokens": history.turn_output_tokens,
            "compactions": history.compactions, "reset_reason": history.reset_reason,
            "pending_compaction": history.pending_compaction, "has_current": history.current is not None,
            "signature": history.signature, "last_request_key_digest": history.last_request_key_digest,
            "truncated": history.truncated}


def decode_history(value: dict, thread: str, source: str) -> WeatherHistory:
    expected = set(encode_history(WeatherHistory(thread, source)))
    if not isinstance(value, dict) or set(value) != expected:
        raise WeatherError("Invalid weather history cache schema")
    h = WeatherHistory(thread, source)
    for field in ("epoch", "compactions", "turn_output_tokens"):
        setattr(h, field, count(value[field], field))
    for field in ("pending_compaction", "has_current", "truncated"):
        if type(value[field]) is not bool:
            raise WeatherError("Invalid history cache flag")
    for field in ("model", "effort", "reset_reason"):
        item = value[field]
        if item is not None and (not isinstance(item, str) or not item or len(item) > (64 if field == "effort" else 256)):
            raise WeatherError("Invalid history cache label")
        setattr(h, field, item)
    for field in ("turn_started_at", "turn_last_usage_at"):
        setattr(h, field, None if value[field] is None else timestamp(value[field]))
    if (h.turn_last_usage_at is not None and (h.turn_started_at is None or h.turn_last_usage_at < h.turn_started_at)):
        raise WeatherError("Invalid cached turn timing")
    h.window = None if value["window"] is None else count(value["window"], "window")
    if h.window == 0:
        raise WeatherError("Invalid cached window")
    h.turn = None if value["turn"] is None else identifier(value["turn"])
    samples = value["samples"]
    if not isinstance(samples, list) or len(samples) > 400:
        raise WeatherError("Invalid cached sample count")
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != SAMPLE_KEYS:
            raise WeatherError("Invalid cached sample fields")
        timestamp(sample["at"])
        for field in ("tokens", "input", "output", "cached_input", "reasoning_output", "epoch"):
            count(sample[field], field)
        if sample["epoch"] > h.epoch or sample["cached_input"] > sample["input"] or sample["reasoning_output"] > sample["output"]:
            raise WeatherError("Invalid cached sample subsets")
        if sample["turn"] is not None:
            identifier(sample["turn"])
        if type(sample["is_compaction"]) is not bool or type(sample["is_turn_end"]) is not bool:
            raise WeatherError("Invalid cached sample flags")
    h.samples = samples
    h.pending_compaction, h.truncated = value["pending_compaction"], value["truncated"]
    if value["has_current"]:
        if not samples or samples[-1]["epoch"] != h.epoch:
            raise WeatherError("Invalid cached current reading")
        h.current = samples[-1]
    for field in ("signature", "last_request_key_digest"):
        fingerprint = value[field]
        if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch("[a-f0-9]{64}", fingerprint)):
            raise WeatherError("Invalid cached deduplication fingerprint")
        setattr(h, field, fingerprint)
    limits = value["limits"]
    if not isinstance(limits, list) or len(limits) > 128:
        raise WeatherError("Invalid cached quota count")
    fields = {"bucket", "window", "used_percent", "remaining_percent", "duration_minutes", "resets_at", "observed_at"}
    for item in limits:
        if not isinstance(item, dict) or set(item) != fields:
            raise WeatherError("Invalid cached quota fields")
        if not isinstance(item["bucket"], str) or not 1 <= len(item["bucket"]) <= 96:
            raise WeatherError("Invalid cached quota label")
        if item["window"] not in ("primary", "secondary") or count(item["duration_minutes"], "duration") == 0:
            raise WeatherError("Invalid cached quota metadata")
        for field in ("used_percent", "remaining_percent"):
            if type(item[field]) not in (int, float) or not math.isfinite(item[field]) or not 0 <= item[field] <= 100:
                raise WeatherError("Invalid cached quota percent")
        if abs(item["used_percent"] + item["remaining_percent"] - 100) > .00001:
            raise WeatherError("Invalid cached quota sum")
        timestamp(item["observed_at"])
        if item["resets_at"] is not None:
            timestamp(item["resets_at"])
    h.limits = limits
    return h


def _header(stream, thread: str, workspace: Path) -> tuple[dict, bytes]:
    stream.seek(0)
    raw = stream.readline(LINE_BYTES + 1)
    if len(raw) > LINE_BYTES or not raw.endswith(b"\n"):
        raise WeatherError("Unsupported or incomplete transcript header")
    try:
        first = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise WeatherError("Unsupported transcript header") from exc
    meta = first.get("payload") if isinstance(first, dict) else None
    if (not isinstance(meta, dict) or first.get("type") != "session_meta" or meta.get("id") != thread
            or not isinstance(meta.get("cwd"), str) or not Path(meta["cwd"]).is_absolute()
            or Path(meta["cwd"]).resolve() != workspace):
        raise WeatherError("Transcript identity/workspace does not match the requested root")
    if (isinstance(meta.get("source"), dict) and "subagent" in meta["source"]) or any(meta.get(k) is not None for k in ("parent_thread_id", "parentThreadId")):
        raise WeatherError("Incremental weather requires a root transcript")
    if meta.get("cli_version") not in ROLLOUT_VERSIONS:
        raise WeatherError("Transcript format is not verified for this engine version")
    return meta, raw


def _consume(h: WeatherHistory, row: dict, pending_after: float | None) -> bool:
    """Keep only numeric measurements and bounded lifecycle/model metadata."""
    if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
        return False
    p, kind = row["payload"], row.get("type")
    if kind == "turn_context":
        h.settings(p.get("model"), effort=_effort(p))
        h.begin_turn(p.get("turn_id"), _stamp(row.get("timestamp")))
    elif kind == "compacted" or kind == "event_msg" and p.get("type") == "context_compacted":
        if not h.pending_compaction:
            h.reset("native transcript compaction", compacted=True)
    elif kind == "event_msg":
        event = p.get("type")
        if event == "task_started":
            h.settings(h.model, p.get("model_context_window"))
            h.begin_turn(p.get("turn_id"), _stamp(row.get("timestamp")))
        elif event in ("task_complete", "task_completed", "turn_aborted"):
            h.end_turn(p.get("turn_id", h.turn))
        elif event == "token_count":
            at = _stamp(row.get("timestamp"))
            if p.get("rate_limits") is not None:
                h.limits = rate_limits(p["rate_limits"], at)
            info = p.get("info")
            if info is None:
                return False
            if not isinstance(info, dict):
                raise WeatherError("Unsupported transcript token_count shape")
            if pending_after is not None and at <= pending_after:
                return False
            total = info.get("total_token_usage")
            key = total.get("total_tokens") if isinstance(total, dict) else None
            if key is not None:
                count(key, "request deduplication input")
            h.request(_last_usage(info.get("last_token_usage")), info.get("model_context_window"), at, key)
            return True
    return False


def cached_rollout(path: Path, thread: str, workspace: Path, *, cache_root: Path | None = None,
                   hook_event: dict | None = None, byte_budget: int = READ_BUDGET) -> tuple[WeatherHistory, dict]:
    identifier(thread)
    workspace = workspace.resolve(strict=True)
    path = path.absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise WeatherError("Weather transcript must not be a symlink")
    if type(byte_budget) is not int or byte_budget < LINE_BYTES + 1:
        raise WeatherError("Incremental byte budget is too small")
    root = cache_root.absolute() if cache_root is not None else data_directory()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise WeatherError("Weather cache directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = owned_path(root, "history-v2-" + thread + ".json")
    with lock(owned_path(root, "history-v2-" + thread + ".lock"), timeout=.8):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise WeatherError("Weather transcript must be a regular file")
            meta, header = _header(stream, thread, workspace)
            source = "local-rollout/" + meta["cli_version"]
            binding = {"thread_id": thread, "workspace": str(workspace), "transcript": str(path),
                       "device": status.st_dev, "inode": status.st_ino, "header_sha256": digest(header)}
            reset = None
            saved = None
            try:
                saved = read_json(cache)
                required = {"schema_version", "binding", "cursor", "discarding", "anchor_sha256", "mtime_ns", "history", "pending_after"}
                if saved is None:
                    reset = "initial scan"
                elif not isinstance(saved, dict) or set(saved) != required or saved["schema_version"] != CACHE_VERSION:
                    reset = "cache schema changed"
                elif saved["binding"] != binding:
                    reset = "transcript identity/file changed"
                else:
                    cursor = count(saved["cursor"], "cursor")
                    if cursor < len(header) or cursor > status.st_size:
                        reset = "transcript truncated"
                    elif saved["mtime_ns"] != status.st_mtime_ns and cursor == status.st_size:
                        reset = "transcript rewritten"
                    elif type(saved["discarding"]) is not bool:
                        reset = "invalid cache cursor"
                    else:
                        stream.seek(max(len(header), cursor - ANCHOR_BYTES))
                        if digest(stream.read(cursor - stream.tell())) != saved["anchor_sha256"]:
                            reset = "transcript append anchor changed"
                        else:
                            h = decode_history(saved["history"], thread, source)
                            pending_after = saved["pending_after"]
                            if pending_after is not None:
                                timestamp(pending_after)
            except (WeatherError, ValueError, TypeError, KeyError):
                reset = "invalid cache rebuilt"
            if reset is not None:
                h = WeatherHistory(thread, source)
                cursor, discarding, pending_after = len(header), False, None
            else:
                cursor, discarding = saved["cursor"], saved["discarding"]
            stream.seek(cursor)
            consumed = 0
            while cursor < status.st_size and consumed < byte_budget:
                begin = stream.tell()
                line = stream.readline(min(LINE_BYTES + 1, byte_budget - consumed, status.st_size - begin))
                if not line:
                    break
                consumed += len(line)
                if discarding:
                    cursor = stream.tell()
                    discarding = not line.endswith(b"\n")
                    continue
                if len(line) > LINE_BYTES:
                    h.truncated = True
                    cursor, discarding = stream.tell(), not line.endswith(b"\n")
                    continue
                if not line.endswith(b"\n"):
                    stream.seek(begin)
                    break  # Do not store unfinished text or commit a partial record.
                cursor = stream.tell()
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    h.truncated = True
                    continue
                if _consume(h, row, pending_after):
                    pending_after = None
            if hook_event is not None:
                if hook_event.get("hook_event_name") == "PostCompact":
                    if not h.pending_compaction and not (h.current and h.current["is_compaction"]):
                        h.reset("native PostCompact hook; awaiting next request", compacted=True)
                    h.current, h.signature, h.pending_compaction = None, None, True
                    pending_after = time.time()
                elif hook_event.get("hook_event_name") == "Stop":
                    h.end_turn(hook_event.get("turn_id", h.turn))
            stream.seek(max(len(header), cursor - ANCHOR_BYTES))
            anchor = digest(stream.read(cursor - stream.tell()))
            state = {"schema_version": CACHE_VERSION, "binding": binding, "cursor": cursor,
                     "discarding": discarding, "anchor_sha256": anchor, "mtime_ns": status.st_mtime_ns,
                     "history": encode_history(h), "pending_after": pending_after}
            if state != saved or reset is not None:
                atomic_json(cache, state)
    return h, {"cache_reused": reset is None, "reset_reason": reset, "bytes_read": consumed,
               "cursor": cursor, "file_size": status.st_size, "catching_up": cursor < status.st_size,
               "numeric_samples_retained": len(h.samples)}
