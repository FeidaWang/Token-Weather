"""Inspect and register a project hook bridge without changing Codex trust."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import time
import uuid

from .storage import WeatherError, atomic_json, atomic_write, lock, owned_path, read_bytes, read_json

EVENTS = ("SessionStart", "PostToolUse", "Stop", "PostCompact")
NATIVE_EVENTS = {name: name[0].lower() + name[1:] for name in EVENTS}


def inspect_native_hooks(workspace: Path, engine: Path) -> dict:
    """Use a short lived, read-only app-server for the host's active hook list."""
    from .desktop_adapter import JsonRpc

    workspace, engine = workspace.resolve(strict=True), engine.resolve(strict=True)
    if not engine.is_file():
        raise WeatherError("Codex engine must be an existing executable file")
    with JsonRpc([str(engine), "app-server", "--stdio", "--strict-config"], cwd=workspace) as rpc:
        rpc.initialize()
        response = rpc.request("hooks/list", {"cwds": [str(workspace)]})
    entries = [row for row in response.get("data", []) if isinstance(row, dict) and row.get("cwd") == str(workspace)]
    if len(entries) != 1 or entries[0].get("errors"):
        raise WeatherError("Native hook list is incomplete; bridge registration was preserved")
    found = []
    for hook in entries[0].get("hooks", []):
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if (hook.get("eventName") in NATIVE_EVENTS.values() and isinstance(command, str)
                and "scripts/weather.py" in command and "weather-hook" in command):
            found.append({key: hook.get(key) for key in ("eventName", "source", "sourcePath", "enabled", "trustStatus", "command")})
    return {"status": "verified", "engine": str(engine), "weather_hooks": found,
            "trust_modified": False, "hooks_executed": False, "model_requests": 0}


def _remove_owned(config: dict, owned: dict) -> None:
    for event, handler in owned.items():
        if event not in EVENTS or not isinstance(handler, dict):
            raise WeatherError("Invalid bridge ownership record")
        groups = config["hooks"].get(event, [])
        # The bridge adds one unfiltered, single-handler group. Remove only one
        # owned occurrence; identical definitions elsewhere belong to the user.
        for index in range(len(groups) - 1, -1, -1):
            group = groups[index]
            if group.get("matcher") is None and group.get("hooks") == [handler]:
                groups.pop(index)
                break
        if not groups:
            config["hooks"].pop(event, None)


def project_setup(workspace: Path, plugin: Path, *, remove: bool = False,
                  check_only: bool = False, native: dict | None = None) -> dict:
    workspace, plugin = workspace.resolve(strict=True), plugin.resolve(strict=True)
    manifest = read_json(plugin / "plugin.json")
    entry = plugin / "scripts/weather.py"
    if manifest.get("name") != "token-weather-for-codex" or not entry.is_file() or entry.is_symlink():
        raise WeatherError("Setup requires the installed Token Weather plugin root")
    if not (plugin / "hooks/hooks.json").is_file():
        raise WeatherError("This plugin package has no bundled lifecycle hooks; use manual weather readings")
    dot = owned_path(workspace, ".codex")
    if not check_only:
        dot.mkdir(mode=0o700, exist_ok=True)
    target = owned_path(dot, "hooks.json")
    marker = owned_path(dot, "token-weather-hook-bridge.json")
    command = shlex.join(["python3", str(entry), "weather-hook"])
    desired = {event: {"type": "command", "command": command, "timeout": 5} for event in EVENTS}

    def run() -> dict:
        old_marker = read_json(marker, {})
        if not isinstance(old_marker, dict) or not isinstance(old_marker.get("handlers", {}), dict):
            raise WeatherError("Invalid bridge ownership record; project hooks were preserved")
        owned = old_marker.get("handlers", {})
        config = read_json(target, {"hooks": {}})
        if not isinstance(config, dict) or not isinstance(config.get("hooks"), dict):
            raise WeatherError("Project hooks are malformed; setup preserved them")
        original = read_bytes(target) if target.exists() else None
        for groups in config["hooks"].values():
            if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks"), list) for g in groups):
                raise WeatherError("Project hook groups are malformed; setup preserved them")

        native_rows = [] if native is None else native.get("weather_hooks", [])
        if native is not None and (native.get("status") != "verified" or not isinstance(native_rows, list)):
            raise WeatherError("Native hook inspection is incomplete; bridge registration was preserved")
        owned_commands = {handler.get("command") for handler in owned.values() if isinstance(handler, dict)}
        external = [row for row in native_rows if isinstance(row, dict) and
                    not (row.get("sourcePath") == str(target) and row.get("command") in owned_commands)]
        external_events = {row.get("eventName") for row in external}
        conflicting = bool(external)
        owned_present = bool(owned) and all(
            any(group.get("matcher") is None and group.get("hooks") == [handler]
                for group in config["hooks"].get(event, [])) for event, handler in owned.items())
        native_owned = [row for row in native_rows if isinstance(row, dict) and
                        row.get("sourcePath") == str(target) and row.get("command") in owned_commands]
        native_owned_events = {row.get("eventName") for row in native_owned}
        if conflicting:
            status = "already-provided" if external_events == set(NATIVE_EVENTS.values()) else "partial-conflict"
        elif owned:
            if not owned_present:
                status = "bridge-stale"
            elif native is None:
                status = "bridge-registered"
            elif native_owned_events != {NATIVE_EVENTS[event] for event in owned}:
                status = "bridge-not-loaded"
            elif all(row.get("enabled") is True and row.get("trustStatus") == "trusted" for row in native_owned):
                status = "bridge-trusted"
            else:
                status = "bridge-review-needed"
        elif any(desired[event] in group["hooks"] for event in EVENTS for group in config["hooks"].get(event, [])):
            status = "existing-project-hooks"
        else:
            status = "needs-registration"

        if check_only:
            if status in ("needs-registration", "bridge-stale"):
                next_step = "Register this project bridge, then review the exact definitions with /hooks."
            elif status in ("bridge-review-needed", "bridge-not-loaded"):
                next_step = "Inspect the project and hook trust in Codex /hooks; do not bypass review."
            elif status == "partial-conflict":
                next_step = "Resolve the partially active weather hooks before registering another bridge."
            else:
                next_step = "Use the existing hooks; no duplicate bridge is needed."
            return {"workspace": str(workspace), "hook_source": str(target), "plugin_root": str(plugin),
                    "plugin_version": manifest["version"], "status": status, "changed": False,
                    "registered_events": list(owned), "native_check": "verified" if native is not None else "not-run",
                    "native_hooks": [{k: row.get(k) for k in ("eventName", "source", "sourcePath", "enabled", "trustStatus")} for row in native_rows],
                    "trust_modified": False, "next_step": next_step}

        if not remove and (status == "partial-conflict" or status == "already-provided" and not owned):
            return {"workspace": str(workspace), "hook_source": str(target), "plugin_root": str(plugin),
                    "plugin_version": manifest["version"], "status": status, "changed": False,
                    "removed": False, "registered_events": list(owned), "native_check": "verified",
                    "trust_modified": False, "note": "No duplicate bridge was written; inspect /hooks."}

        _remove_owned(config, owned)
        registered = {}
        if not remove and not conflicting:
            for event, handler in desired.items():
                groups = config["hooks"].setdefault(event, [])
                if not any(handler in group["hooks"] for group in groups):
                    groups.append({"hooks": [handler]})
                    registered[event] = handler
        serialized = (json.dumps(config, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        changed = serialized != original
        if changed:
            if original is not None:
                backups = owned_path(dot, "token-weather-plugin-backups")
                backups.mkdir(mode=0o700, exist_ok=True)
                atomic_write(owned_path(backups, str(uuid.uuid4()) + ".json"), original)
            atomic_write(target, serialized)
        record = {"schema_version": 1, "plugin_name": manifest["name"], "plugin_version": manifest["version"],
                  "plugin_root": str(plugin), "handlers": registered, "registered_at": time.time()}
        if owned != registered or old_marker.get("plugin_version") != record["plugin_version"]:
            atomic_json(marker, record)
        return {"workspace": str(workspace), "hook_source": str(target), "plugin_root": str(plugin),
                "plugin_version": manifest["version"], "status": "removed" if remove else status,
                "changed": changed, "removed": remove, "registered_events": list(registered),
                "native_check": "verified" if native is not None else "not-run", "trust_modified": False,
                "note": "Review exact hook definitions through Codex /hooks; setup never writes trust."}

    if check_only:
        return run()
    with lock(owned_path(dot, "token-weather-hook-bridge.lock")):
        return run()
