"""Advisory plugin hooks. They never block tools or start a continuation."""
from __future__ import annotations

import os
from pathlib import Path

from .storage import WeatherError, atomic_json, identifier, lock, owned_path, read_json
from .weather_sources import collect_weather

EVENTS = {"SessionStart", "PostToolUse", "Stop", "PostCompact"}


def handle_weather(event: dict, *, data_root: Path | None = None) -> dict:
    if not isinstance(event, dict) or event.get("hook_event_name") not in EVENTS:
        return {}
    thread = identifier(event.get("session_id"))
    # Subagent hook session_id may be the parent's: agent_id must also match.
    if event.get("agent_id") not in (None, thread) or event.get("agent_type") is not None:
        return {}
    cwd, transcript = event.get("cwd"), event.get("transcript_path")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not isinstance(transcript, str):
        return {}
    workspace = Path(cwd).resolve(strict=True)
    root = data_root or Path(os.environ.get("PLUGIN_DATA", workspace / ".codex/token-weather"))
    root = root.absolute()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise WeatherError("Weather data directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = owned_path(root, thread + ".json")
    with lock(owned_path(root, thread + ".lock"), timeout=.5):
        previous = read_json(target, {})
        report = collect_weather(workspace, thread, transcript=Path(transcript),
                                 cache_root=data_root, hook_event=event)
        atomic_json(target, report)
    old_rank = previous.get("level", {}).get("rank", -1) if isinstance(previous, dict) else -1
    rank = report["level"]["rank"]
    if not report["stale"] and rank in (3, 4) and rank > old_rank:
        return {"systemMessage": f'Token Weather {report["level"]["icon"]} {report["level"]["name"]}: Estimated chat space is about {report["fraction"]:.0%} of the reference limit.'}
    return {}
