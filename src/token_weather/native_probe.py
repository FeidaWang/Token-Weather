"""Inspect native hook metadata without running hooks, turns or trust writes."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile

from .desktop_adapter import JsonRpc
from .installer import hook_configuration
from .models import utc_now
from .storage import WeatherError, digest, identifier

EVENT_NAMES = {"PreCompact": "preCompact", "UserPromptSubmit": "userPromptSubmit", "PreToolUse": "preToolUse"}


def toml_value(value) -> str:
    """Encode the small hook configuration as one TOML override, never shell code."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(key, ensure_ascii=False) + " = " + toml_value(item) for key, item in value.items()) + "}"
    raise WeatherError("Unsupported hook override value")


def assess_hooks(response: dict, configuration: dict, cwd: Path, source: str) -> dict:
    expected = Counter()
    commands = set()
    for event, groups in configuration["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                commands.add(handler["command"])
                expected[(EVENT_NAMES[event], handler["type"], handler["command"],
                          group.get("matcher"), handler["timeout"], False)] += 1
    entries = [entry for entry in response.get("data", []) if entry.get("cwd") == str(cwd)]
    found = Counter()
    metadata = []
    errors = sum(len(entry.get("errors", [])) for entry in entries)
    for entry in entries:
        for hook in entry.get("hooks", []):
            if hook.get("source") != source or hook.get("command") not in commands:
                continue
            found[(hook.get("eventName"), hook.get("handlerType"), hook.get("command"),
                   hook.get("matcher"), hook.get("timeoutSec"), hook.get("async", False))] += 1
            metadata.append({key: hook.get(key) for key in (
                "eventName", "handlerType", "command", "matcher", "timeoutSec", "async",
                "source", "sourcePath", "enabled", "isManaged", "currentHash", "trustStatus")})
    accepted = len(entries) == 1 and errors == 0 and found == expected
    # A matching command from another project layer is not this project's proof.
    if source == "project":
        accepted = accepted and all(hook["sourcePath"] == str(cwd / ".codex/hooks.json")
                                    for hook in metadata)
    return {"parser_accepted": accepted, "status": "VERIFIED" if accepted else "NOT TESTED",
            "expected_hooks": sum(expected.values()), "recognized_hooks": sum(found.values()),
            "error_count": errors, "hooks": metadata,
            "all_hooks_trusted": accepted and all(hook["trustStatus"] == "trusted" for hook in metadata),
            "all_hooks_enabled": accepted and all(hook["enabled"] is True for hook in metadata)}


def probe_hooks(engine: Path, workspace: Path, workflow: str, interpreter: Path, launcher: Path,
                project: bool = False) -> dict:
    identifier(workflow)
    engine = engine.resolve(strict=True)
    configuration = hook_configuration(workspace, workflow, interpreter, launcher)
    try:
        version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired as exc:
        raise WeatherError("Installed engine version probe timed out") from exc
    if version.returncode or not version.stdout.strip():
        raise WeatherError("Installed engine version probe failed")
    args = [str(engine), "app-server", "--stdio", "--strict-config"]
    # Overrides exercise the installed parser without registering or trusting hooks.
    # Project mode instead observes the actual active project layer as-is.
    if not project:
        args += ["-c", "hooks=" + toml_value(configuration["hooks"])]
    with tempfile.TemporaryDirectory(prefix="token-weather-parser-") as temporary:
        cwd = workspace if project else Path(temporary).resolve()
        with JsonRpc(args, cwd=cwd) as rpc:
            rpc.initialize()
            response = rpc.request("hooks/list", {"cwds": [str(cwd)]})
    result = assess_hooks(response, configuration, cwd, "project" if project else "sessionFlags")
    return {"schema_version": 1, "generated_at": utc_now(), "engine_path": str(engine),
            "engine_version": version.stdout.strip(), "workspace": str(workspace), "workflow_id": workflow,
            "config_sha256": digest(json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()),
            "configuration_source": "project" if project else "sessionFlags", **result,
            "rpc_methods": ["initialize", "hooks/list"], "model_requests": 0,
            "hooks_executed": False, "trust_modified": False, "same_desktop_server": False,
            "automatic_precompact_cancellation": "NOT TESTED",
            "note": "Parser recognition and trust metadata only; execution, cancellation and Desktop ownership require separate live tests."}
