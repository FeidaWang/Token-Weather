"""Read-only usage discovery on the existing proxy; never starts a model turn."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import time

from .accounting import Inputs, Occupancy, resolve, watermarks
from .desktop_adapter import JsonRpc
from .desktop_probe import inspect_loaded_owner
from .models import utc_now
from .runtime_usage import ENGINE_VERSION, SCOPES, UsageObserver
from .storage import WeatherError, digest, canonical_json, identifier


def probe_usage(engine: Path, thread: str, workspace: Path, seconds: float = 3) -> dict:
    identifier(thread)
    if not 0 < seconds <= 10:
        raise WeatherError("Usage observation duration must be greater than zero and at most 10 seconds")
    engine = engine.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "desktop-usage-discovery",
              "engine_path": str(engine), "target_thread": thread, "workspace": str(workspace),
              "status": "NOT TESTED", "production_promotion": False, "rpc_methods": [],
              "model_requests": 0, "started_server": False, "submitted_turn": False,
              "interrupted_turn": False, "trust_modified": False, "usage_observation": None}
    observer = None
    try:
        version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        if version != ENGINE_VERSION:
            raise WeatherError("Installed engine does not match the usage adapter's 0.160.0 protocol")
        with JsonRpc([str(engine), "app-server", "proxy"], cwd=workspace) as rpc:
            report["rpc_methods"].append("initialize")
            rpc.initialize()
            report["rpc_methods"].append("thread/loaded/list")
            owner = inspect_loaded_owner(rpc, thread, workspace)
            report.update(owner)
            if owner.get("target_loaded"):
                report["rpc_methods"].append("thread/read")
            if not owner.get("ownership_candidate"):
                raise WeatherError("Exact root/workspace is not loaded on the existing proxy")
            # Refresh only the matched target. Stored metadata is configured state,
            # not per-request execution telemetry or controller ownership proof.
            report["rpc_methods"].append("thread/read")
            metadata = rpc.request("thread/read", {"threadId": thread, "includeTurns": False})
            observer = UsageObserver(thread, workspace, version, "desktop-usage-discovery")
            observer.observe_thread(metadata)
            report["rpc_methods"].append("config/read")
            observer.observe_config(rpc.request("config/read", {"cwd": str(workspace), "includeLayers": False}))
            deadline = time.monotonic() + seconds
            for _ in range(500):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    message = rpc.next_notification(remaining)
                except WeatherError as exc:
                    if "timed out" in str(exc):
                        break  # No spontaneous usage is a valid unknown observation.
                    raise
                observer.consume(message)
            else:
                raise WeatherError("Usage event stream exceeded the bounded observation limit")
            report["status"] = "VERIFIED" if observer.usage_events else "NOT TESTED"
            report["reason"] = ("Matched-root usage observed; active occupancy and live controller ownership remain unverified."
                                if observer.usage_events else "No matched-root usage notification arrived; an idle connection is not a usage measurement.")
    except (WeatherError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        report["reason"] = str(exc)
    if observer is not None:
        report["usage_observation"] = observer.report()
    return report


def status_observation(report: dict, thread: str, workspace: Path) -> dict:
    """Recompute saved evidence at read time, without importing capabilities."""
    if not isinstance(report, dict):
        raise WeatherError("Invalid saved usage report")
    if "usage_observation" in report:
        if report.get("status") != "VERIFIED":
            raise WeatherError("Usage probe is incomplete; its saved observation cannot drive status")
        report = report["usage_observation"]
    if (not isinstance(report, dict) or report.get("schema_version") != 1
            or report.get("kind") != "runtime-usage-observation" or report.get("engine_version") != ENGINE_VERSION
            or report.get("acceptance_scope") not in SCOPES or report.get("thread_id") != thread
            or report.get("workspace") != str(workspace.resolve()) or report.get("production_promotion") is not False):
        raise WeatherError("Saved usage does not match the current workflow root/workspace/protocol")
    settings = report.get("settings")
    if not isinstance(settings, dict) or set(settings) != {"model", "provider", "effort", "workspace"}:
        raise WeatherError("Invalid saved root settings")
    verifier = UsageObserver(thread, workspace, ENGINE_VERSION)
    verifier.observe_settings({"model": settings["model"], "modelProvider": settings["provider"],
                              "effort": settings["effort"], "cwd": settings["workspace"]}, "saved-report")
    if digest(canonical_json(settings)) != report.get("model_fingerprint"):
        raise WeatherError("Saved usage fingerprint does not match its settings")
    data = report.get("observation")
    if not isinstance(data, dict) or set(data) != {"limits", "occupancy"}:
        raise WeatherError("Missing typed runtime observation")
    try:
        inputs = replace(Inputs(**data["limits"]), runtime_matched=False)
        original = Occupancy(**data["occupancy"])
        if original.model_fingerprint != report["model_fingerprint"]:
            raise WeatherError("Saved occupancy belongs to a different model")
        if original.observed_at is not None and original.observed_at > time.time() + 1:
            raise WeatherError("Saved usage has a future receipt time")
        occupancy = replace(original, exact_active=None, reasoning_accounted=False).estimate()
    except (TypeError, ValueError) as exc:
        raise WeatherError("Invalid typed saved usage") from exc
    calculated = resolve(inputs, occupancy["estimate"])
    marks = watermarks(calculated["end_boundary"] if not calculated["unknown_inputs"] else None)
    return {"accounting": calculated, "occupancy": occupancy, "watermarks": marks,
            "rotation_reason": {"action": "checkpoint", "reason": "Saved runtime evidence is an estimate; live reasoning/growth and owner validation remain unresolved"},
            "runtime_observation": {"acceptance_scope": report["acceptance_scope"], "thread_id": thread,
                                    "turn_id": report.get("turn_id"), "model_fingerprint": report["model_fingerprint"],
                                    "context_epoch": report.get("context_epoch"), "settings": settings,
                                    "production_promotion": False}}
