"""Read-only local probes. Documentation/schema presence is not a live test."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
import tomllib

from .models import utc_now
from .storage import atomic_json, digest, canonical_json, read_json, WeatherError

CAPABILITY_NAMES = (
    "trusted_hook_execution", "automatic_precompact_cancellation", "root_only_precompact", "current_thread_usage",
    "reliable_active_occupancy", "model_window_resolution", "compaction_scope_resolution",
    "fallback_buffer_resolution", "post_turn_limit_resolution", "same_live_server",
    "clean_root_creation", "turn_submission", "desktop_visibility", "quiescence",
    "generation_acknowledgement", "deep_link_workspace", "deep_link_prefill",
    "reversible_archive", "archive_descendant_safety", "prompt_ownership_guard", "tool_ownership_guard", "exact_policy_preservation", "complete_live_baseline",
)
CONFIG_KEYS = ("model", "model_provider", "model_reasoning_effort", "model_context_window",
               "model_auto_compact_token_limit", "model_auto_compact_token_limit_scope",
               "model_post_turn_compact_threshold_percent", "sandbox_mode", "approval_policy", "approvals_reviewer")
MODEL_KEYS = ("slug", "context_window", "max_context_window", "effective_context_window_percent", "auto_compact_token_limit", "comp_hash")


def dependency_fingerprints(inputs: dict) -> dict:
    workspace = Path(inputs["workspace"])
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    hooks = {}
    for path in (workspace / ".codex/hooks.json", home / "hooks.json"):
        try:
            hooks[str(path)] = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        except OSError:
            hooks[str(path)] = "missing"
    values = inputs["on_disk_config"]["values"]
    sections = {"runtime": {k: inputs[k] for k in ("engine", "desktop", "python")},
                "configuration": inputs["on_disk_config"], "hooks": hooks,
                "model": {k: values.get(k) for k in CONFIG_KEYS if k.startswith("model")},
                "thread": inputs["root_thread"], "workspace": inputs["workspace"]}
    return {key: digest(canonical_json(value)) for key, value in sections.items()}


def capability_dependencies(name: str) -> tuple[str, ...]:
    if name.startswith("deep_link"):
        return ("runtime", "workspace")
    if "hook" in name or "precompact" in name or "ownership_guard" in name:
        return ("runtime", "configuration", "hooks", "workspace", "thread")
    if name in ("model_window_resolution", "compaction_scope_resolution", "fallback_buffer_resolution", "post_turn_limit_resolution", "current_thread_usage", "reliable_active_occupancy", "exact_policy_preservation"):
        return ("runtime", "configuration", "model", "thread")
    return ("runtime", "configuration", "thread", "workspace")


def run_bounded(args: list[str], timeout: float = 6) -> dict:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"returncode": r.returncode, "stdout": r.stdout[:2_000_000], "stderr": r.stderr[:4096]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": None, "stdout": "", "error": type(exc).__name__}


def mode(report: dict) -> str:
    if (report.get("production_promotion") is False or report.get("acceptance_scope") in
            ("isolated-native", "desktop-transport-discovery", "desktop-control-discovery", "runtime-control-replay", "desktop-live-baseline")):
        return "checkpoint-only"
    caps = report.get("capabilities", {})
    def verified(key: str) -> bool:
        value = caps.get(key, {})
        return value.get("value") is True and value.get("status") == "VERIFIED" and value.get("evidence_kind") == "live-test"
    automatic = ("trusted_hook_execution", "automatic_precompact_cancellation", "root_only_precompact", "same_live_server", "clean_root_creation",
                 "turn_submission", "desktop_visibility", "quiescence", "generation_acknowledgement", "prompt_ownership_guard", "tool_ownership_guard", "exact_policy_preservation")
    if all(verified(k) for k in automatic):
        return "automatic"
    if all(verified(k) for k in ("trusted_hook_execution", "automatic_precompact_cancellation", "root_only_precompact", "deep_link_workspace", "deep_link_prefill")):
        return "prepared"
    return "checkpoint-only"


def disk_config(workspace: Path) -> dict:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    result = {"values": {}, "sources": [], "effective": False}
    # These are declarations only; project trust, profiles, MDM and live overrides are unknown.
    for file in (home / "config.toml", workspace / ".codex" / "config.toml"):
        if file.is_file() and not file.is_symlink() and file.stat().st_size <= 1_048_576:
            try:
                data = tomllib.loads(file.read_text())
                allowed = {key: data[key] for key in CONFIG_KEYS if key in data}
                if isinstance(data.get("features"), dict) and "hooks" in data["features"]:
                    allowed["hooks_enabled_declaration"] = data["features"]["hooks"]
                result["sources"].append({"path": str(file), "allowlist_sha256": digest(canonical_json(allowed)), "values": allowed})
                result["values"].update(allowed)
            except (OSError, ValueError):
                result["sources"].append({"path": str(file), "error": "unreadable/invalid TOML"})
    return result


def discover(workspace: Path, engine_override: str | None = None) -> dict:
    bundles = [Path("/Applications/Codex.app"), Path("/Applications/ChatGPT.app"), Path.home() / "Applications/Codex.app"]
    bundle = next((p for p in bundles if (p / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex").is_file()), None)
    plist = {}
    if bundle:
        try:
            with (bundle / "Contents/Info.plist").open("rb") as stream:
                p = plistlib.load(stream)
            plist = {key: p.get(key) for key in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion")}
        except (OSError, ValueError):
            pass
    engine = Path(engine_override).resolve() if engine_override else (bundle / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex" if bundle else None)
    exe = {"path": str(engine) if engine else None}
    if engine and engine.is_file():
        st = engine.stat()
        exe.update(size=st.st_size, mtime_ns=st.st_mtime_ns, bundled=bool(bundle and engine.is_relative_to(bundle)))
    path_cli = shutil.which("codex")
    return {"workspace": str(workspace.resolve()), "root_thread": os.environ.get("CODEX_THREAD_ID"),
            "desktop": {"bundle": str(bundle) if bundle else None, **plist}, "engine": exe,
            "path_cli": str(Path(path_cli).resolve()) if path_cli else None,
            "python": {"executable": str(Path(sys.executable).resolve()), "version": sys.version.split()[0]},
            "on_disk_config": disk_config(workspace)}


def doctor(workspace: Path, output: Path | None = None, engine_override: str | None = None, refresh: bool = False) -> dict:
    inputs = discover(workspace, engine_override)
    dependencies = dependency_fingerprints(inputs)
    fingerprint = digest(canonical_json({"inputs": inputs, "dependencies": dependencies}))
    cached = read_json(output) if output else None
    if not refresh and cached and cached.get("fingerprint") == fingerprint and time.time() - cached.get("probed_at_epoch", 0) < 300:
        return {**cached, "cache_reused": True}
    caps = {name: {"value": None, "status": "NOT TESTED", "evidence_kind": None, "reason": "No bounded live test has established this capability"} for name in CAPABILITY_NAMES}
    report = {"schema_version": 1, "generated_at": utc_now(), "probed_at_epoch": time.time(), "fingerprint": fingerprint,
              "runtime": inputs, "capabilities": caps, "evidence": [], "mode": "checkpoint-only", "cache_reused": False,
              "dependency_fingerprints": dependencies, "invalidated_capabilities": []}
    if cached:
        previous = cached.get("dependency_fingerprints", {})
        for name in CAPABILITY_NAMES:
            unchanged = all(previous.get(k) == dependencies[k] for k in capability_dependencies(name))
            row = cached.get("capabilities", {}).get(name)
            observed = row.get("observed_epoch", cached.get("probed_at_epoch", 0)) if row else 0
            if unchanged and row and time.time() - observed < 300:
                caps[name] = {**row, "reused_evidence": True, "observed_epoch": observed}
            elif row and row.get("value") is not None:
                report["invalidated_capabilities"].append(name)
    engine = inputs["engine"]["path"]
    if engine:
        help_result = run_bounded([engine, "--help"])
        if help_result["returncode"] == 0:
            report["runtime"]["engine"]["version"] = run_bounded([engine, "--version"])["stdout"].strip()
            report["evidence"].append({"kind": "diagnostic", "finding": "Consulted the bundled executable's help and version; these are separate from PATH CLI"})
            if "doctor" in help_result["stdout"]:
                subhelp = run_bounded([engine, "doctor", "--help"])
                if "--json" in subhelp["stdout"]:
                    probe = run_bounded([engine, "doctor", "--json"], timeout=12)
                    try:
                        native = json.loads(probe["stdout"])
                        checks = native.get("checks", {})
                        # No credentials, auth summaries, network environments or unrelated sessions.
                        selected = {}
                        for key in ("app_server.status", "desktop.app.version", "desktop.app_server.handshake", "runtime.provenance"):
                            row = checks.get(key, {})
                            selected[key] = {"status": row.get("status"), "summary": row.get("summary")}
                        report["native_diagnostics"] = selected
                        if checks.get("app_server.status", {}).get("details", {}).get("status") == "not running":
                            caps["same_live_server"] = {"value": False, "status": "UNSUPPORTED", "evidence_kind": "diagnostic", "observed_epoch": time.time(), "reason": "Default shared control server is not running; desktop handshake does not establish live thread ownership"}
                        commit = checks.get("runtime.provenance", {}).get("details", {}).get("commit")
                        report["runtime"]["engine"]["commit"] = commit or "unknown"
                    except (ValueError, AttributeError):
                        report["evidence"].append({"kind": "diagnostic", "finding": "Native redacted doctor unavailable; no account/auth data persisted"})
            if "debug" in help_result["stdout"]:
                debug_help = run_bounded([engine, "debug", "models", "--help"])
                if "--bundled" in debug_help["stdout"]:
                    catalog = run_bounded([engine, "debug", "models", "--bundled"])
                    try:
                        data = json.loads(catalog["stdout"])
                        rows = data if isinstance(data, list) else data.get("models", [])
                        candidate = inputs["on_disk_config"]["values"].get("model")
                        report["bundled_model_candidate"] = next(({k: row.get(k) for k in MODEL_KEYS} for row in rows if row.get("slug") == candidate), None)
                        report["model_metadata_note"] = "Bundled candidate only. Active server metadata, overrides and reasoning accounting are NOT TESTED."
                    except (ValueError, AttributeError):
                        report["bundled_model_candidate"] = None
    report["mode"] = mode(report)
    if output:
        atomic_json(output, report)
    return report
