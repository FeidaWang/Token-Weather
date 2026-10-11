"""Read-only supported Desktop transport probe; presence is not control proof."""
from __future__ import annotations

from pathlib import Path
import math
import subprocess
import time

from .desktop_adapter import JsonRpc
from .models import utc_now
from .storage import WeatherError, identifier


def inspect_root_metadata(response: dict, thread: str, workspace: Path) -> dict:
    """Reduce exact live root identity; omitted parent/status is not evidence."""
    metadata = response.get("thread") if isinstance(response, dict) else None
    if not isinstance(metadata, dict):
        raise WeatherError("Malformed thread metadata")
    cwd, status = metadata.get("cwd"), metadata.get("status")
    matched = isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).resolve() == workspace.resolve()
    root = "parentThreadId" in metadata and metadata["parentThreadId"] is None
    loaded = isinstance(status, dict) and status.get("type") in ("idle", "active")
    result = {"target_identity_matches": metadata.get("id") == thread,
              "workspace_matches": matched, "root_identity_matches": root,
              "target_runtime_loaded": loaded}
    return {**result, "ownership_candidate": all(result.values())}


def inspect_loaded_owner(rpc: JsonRpc, thread: str, workspace: Path, timeout: float = 8) -> dict:
    identifier(thread)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise WeatherError("Owner discovery requires a finite positive timeout of at most 30 seconds")
    deadline = time.monotonic() + timeout
    cursor = None
    seen, loaded = set(), set()
    for _ in range(10):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WeatherError("Owner discovery deadline expired")
        page = rpc.request("thread/loaded/list", params, timeout=remaining)
        if not isinstance(page, dict):
            raise WeatherError("Malformed loaded-thread inventory")
        data = page.get("data")
        if not isinstance(data, list) or len(data) > 100 or any(not isinstance(item, str) for item in data):
            raise WeatherError("Malformed loaded-thread inventory")
        for item in data:
            identifier(item)
            if item in loaded:
                raise WeatherError("Duplicate loaded-thread identity")
            loaded.add(item)
        # The pinned loaded-list schema permits omission when the cursor is
        # None. Exhausting this list is only lookup evidence, never a live tree.
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
            raise WeatherError("Invalid or repeated loaded-thread cursor")
        seen.add(cursor)
    else:
        raise WeatherError("Loaded-thread inventory exceeded the bounded page limit")
    summary = {"target_loaded": thread in loaded, "ownership_candidate": False,
               "loaded_entries_inspected": len(loaded), "loaded_inventory_exhausted": True}
    if thread not in loaded:
        return summary
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WeatherError("Owner discovery deadline expired")
    # Read only the exact loaded target, never turns or unrelated metadata.
    response = rpc.request("thread/read", {"threadId": thread, "includeTurns": False}, timeout=remaining)
    return {**summary, **inspect_root_metadata(response, thread, workspace)}


def probe_desktop(engine: Path, thread: str, workspace: Path, socket_path: Path | None = None,
                  *, transport: str = "proxy", turn: str | None = None) -> dict:
    from .owner_session import open_owner_transport, OwnerSession
    identifier(thread)
    if turn is not None:
        identifier(turn)
    engine = engine.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "desktop-transport-discovery",
              "engine_path": str(engine), "target_thread": thread, "workspace": str(workspace),
              "transport": transport, "socket_path": str(socket_path) if socket_path is not None else None,
              "target_turn": turn, "target_turn_verified": False,
              "status": "NOT TESTED", "same_live_server": "NOT TESTED", "desktop_visibility": "NOT TESTED",
              "production_promotion": False, "production_ready": False,
              "continuation_blockers": ["owner_connection", "exact_loaded_root", "desktop_owner_attestation", "exact_turn_lifecycle",
                  "complete_live_baseline", "complete_descendants_and_external_operations",
                  "prompt_ownership_guard", "tool_ownership_guard", "exact_policy_preservation",
                  "desktop_live_attachment", "native_bootstrap_acknowledgement"],
              "ownership_candidate": False, "rpc_methods": [], "model_requests": 0,
              "started_server": False, "reconfigured_server": False, "submitted_turn": False,
              "interrupted_turn": False, "trust_modified": False}
    stage = "owner_connection"
    session = None
    def record_method(method):
        if method not in report["rpc_methods"]:
            report["rpc_methods"].append(method)
    try:
        with open_owner_transport(engine, workspace, socket_path, transport=transport,
                                  on_request=record_method) as (rpc, version, handshake):
            report["engine_version"] = report["owner_engine_version"] = version
            report["continuation_blockers"].remove("owner_connection")
            stage = "exact_loaded_root"
            if turn is None:
                observation = inspect_loaded_owner(rpc, thread, workspace)
            else:
                session = OwnerSession(rpc, thread, turn, workspace, version, handshake=handshake)
                observation = session.discover()
            report.update(observation)
            if not observation.get("ownership_candidate"):
                report.update(reason_code="exact-root-mismatch", reason="The exact live root/parent/workspace does not match this endpoint.")
                return report
            report["continuation_blockers"].remove("exact_loaded_root")
            if session is not None:
                stage = "partial_owner_state"
                report["owner_state"] = session.inspect_state()
            report["status"] = "VERIFIED"  # Transport discovery only, never automatic-mode evidence.
            report["reason_code"] = "owner-discovery-only"
            report["reason"] = "Exact loaded-root discovery completed; Desktop ownership attestation and complete continuation remain unverified."
    except (WeatherError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Native error bodies and command output are never persisted.
        report["reason_code"] = {"owner_connection": "owner-connection-unavailable",
            "exact_loaded_root": "exact-root-unverified", "partial_owner_state": "partial-state-unavailable"}[stage]
        report["reason"] = "Read-only owner discovery could not verify the required connection or state."
    finally:
        if session is not None:
            session.observer.invalidate("Owner discovery connection closed")
    return report
