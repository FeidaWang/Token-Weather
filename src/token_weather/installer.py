from __future__ import annotations

import copy
from pathlib import Path
import shlex
import uuid
import sys
import subprocess

from .storage import WeatherError, owned_path, read_json, atomic_json, atomic_write, read_bytes, digest, lock


def hook_configuration(workspace: Path, workflow: str, interpreter: Path, launcher: Path) -> dict:
    args = [str(interpreter), str(launcher), "--workspace", str(workspace), "--workflow", workflow, "hook"]
    return {"hooks": {event: [{**({"matcher": "^auto$"} if event == "PreCompact" else {}),
                              "hooks": [{"type": "command", "command": shlex.join(args), "timeout": 2}]}]
                      for event in ("PreCompact", "UserPromptSubmit", "PreToolUse")}}


def install(workspace: Path, workflow: str, interpreter: Path, launcher: Path,
            apply: bool = False, register_hooks: bool = False, schema_evidence: dict | None = None) -> dict:
    codex_dir = owned_path(workspace, ".codex")
    hooks_path = owned_path(codex_dir, "hooks.json")
    receipt_path = owned_path(codex_dir, "token-weather-install.json")
    addition = hook_configuration(workspace, workflow, interpreter, launcher)
    existing = read_json(hooks_path, {"hooks": {}})
    if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
        raise WeatherError("Existing hooks configuration has an unknown format")
    merged = copy.deepcopy(existing)
    merged.setdefault("hooks", {})
    entries = []
    for event, groups in addition["hooks"].items():
        target = merged["hooks"].setdefault(event, [])
        if not isinstance(target, list):
            raise WeatherError("Unknown hook matcher format")
        for group in groups:
            if group not in target:
                target.append(group)
                entries.append({"event": event, "group": group})
    plan = {"dry_run": not apply, "hooks_requested": register_hooks, "hook_configuration": addition,
            "automatic_rotation": False, "skill": "skills/token-weather-checkpoint/SKILL.md",
            "trust": "Review hooks through normal Codex trust UI after registration"}
    if not apply:
        return plan
    if not register_hooks:
        return {**plan, "applied": False, "reason": "Source launcher and skill are already usable; hook registration requires its validated schema evidence"}
    expected_digest = digest(__import__("json").dumps(addition, sort_keys=True, separators=(",", ":")).encode())
    if not schema_evidence or schema_evidence.get("parser_accepted") is not True or schema_evidence.get("config_sha256") != expected_digest:
        raise WeatherError("Hook configuration has no matching installed-runtime parser evidence; keep using the checkpoint CLI")
    engine_path = schema_evidence.get("engine_path")
    engine_version = schema_evidence.get("engine_version")
    if not engine_path or not engine_version:
        raise WeatherError("Hook schema evidence must pin the installed engine path and version")
    try:
        version = subprocess.run([engine_path, "--version"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WeatherError("Schema evidence engine is unavailable") from exc
    if version != engine_version:
        raise WeatherError("Installed engine changed; invalidate hook schema evidence")
    # Refuse probable duplicate sources rather than silently enabling two handlers.
    config_path = owned_path(codex_dir, "config.toml")
    if config_path.exists() and "token-weather" in read_bytes(config_path).decode():
        raise WeatherError("An inline Token Weather hook may already be registered; reconcile it before installing")
    codex_dir.mkdir(exist_ok=True, mode=0o700)
    with lock(owned_path(codex_dir, "token-weather-install.lock")):
        if receipt_path.exists():
            return {**plan, "applied": False, "reason": "Already installed; use uninstall before changing registration"}
        backup_dir = owned_path(codex_dir, "token-weather-backups")
        backup_dir.mkdir(exist_ok=True, mode=0o700)
        backup = owned_path(backup_dir, uuid.uuid4().hex + ".json")
        atomic_json(backup, existing)
        # Persist ownership before mutation so a crashed installation is removable.
        atomic_json(receipt_path, {"schema_version": 1, "backup": str(backup), "entries": entries,
                                  "launcher": str(launcher), "interpreter": str(interpreter), "workflow": workflow})
        atomic_json(hooks_path, merged)
    return {**plan, "applied": True, "backup": str(backup)}


def uninstall(workspace: Path, dry_run: bool = True) -> dict:
    codex_dir = owned_path(workspace, ".codex")
    receipt_path = owned_path(codex_dir, "token-weather-install.json")
    receipt = read_json(receipt_path)
    if not receipt:
        return {"dry_run": dry_run, "removed": [], "reason": "No project-owned hook registration"}
    plan = {"dry_run": dry_run, "removed": receipt["entries"], "preserved": ["handover ledger", "generations", "worktree", "configuration backups"]}
    if dry_run:
        return plan
    with lock(owned_path(codex_dir, "token-weather-install.lock")):
        hooks_path = owned_path(codex_dir, "hooks.json")
        data = read_json(hooks_path, {"hooks": {}})
        for entry in receipt["entries"]:
            groups = data.get("hooks", {}).get(entry["event"], [])
            data["hooks"][entry["event"]] = [g for g in groups if g != entry["group"]]
        atomic_json(hooks_path, data)
        receipt_path.unlink()
    return plan
