"""Read-only recovery diagnostics, separate from live acceptance evidence."""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import socket
import stat

from .checkpoints import Workflow
from .desktop_probe import probe_desktop
from .models import utc_now
from .owner_session import validate_owner_socket
from .storage import WeatherError, identifier, read_json


def required_owner_contract() -> dict:
    """Describe the existing reader contract; this is never a capability grant."""
    from .continuation import POLICY_FIELDS
    return {
        "schema_version": 1, "kind": "required-owner-contract",
        "implemented_by_current_public_rpcs": False,
        "endpoint": {
            "existing_desktop_owner": True, "exact_loaded_root_workspace": True,
            "same_connection_usage_and_control": True,
            "desktop_owner_attestation": "Required in addition to loaded-root matching",
        },
        "baseline": {
            "reader": "reader(session, request, timeout)",
            "acceptance_scope": "desktop-live-baseline",
            "response_fields": ["schema_version", "acceptance_scope", "binding", "observed_at",
                                "complete", "source", "successor"],
            "binding": ["schema_version", "request_id", "connection_id", "transport_id", "engine_version", "workspace",
                        "revision", "source", "successor", "context", "issued_at", "expires_at"],
            "complete": {key: True for key in ("descendants", "tools", "external_operations", "settings", "turns")},
            "source_fields": ["settings", "members", "items", "background", "external_operations"],
            "settings_fields": [*POLICY_FIELDS.values(), "cwd"],
            "member_fields": ["thread_id", "parent_thread_id", "turn_id", "started", "coverage_complete", "terminal"],
            "terminal_fields": ["status", "error", "observed_at"],
            "item_fields": ["thread_id", "turn_id", "item_id", "type", "status", "process_id", "result_failed", "finished"],
            "successor_fields": ["thread_id", "parent_thread_id", "forked_from_id", "settings", "turn_ids",
                                 "turn_id", "started", "status", "error", "items", "background", "external_operations"],
            "requirements": [
                "Authoritative live descendant tree, including unloaded and ephemeral members",
                "Explicit external-operation inventory; null means unknown",
                "Full effective policy and provenance, including late attachment",
                "Exact turn starts, terminals and tools with complete lifecycle coverage",
                "Atomic snapshot and subscription boundary; no events lost at attachment or reconnect",
                "Echo the fresh request binding and reject connection/revision/transaction drift",
                "Bind successor only to persisted create/submit receipts; never guess an identity",
            ],
            "max_bytes": 262144, "max_descendants": 32, "max_items": 128, "max_seconds": 5,
            "validator": "token_weather.live_baseline.restore_live_baseline",
            "disk_import_allowed": False,
        },
        "control_authorization": {
            "acceptance_scope": "desktop-live-control",
            "owner_binding": ["connection_id", "thread_id", "workspace", "engine_version", "expires_at"],
            "max_seconds": 30,
            "required_live_test_capabilities": ["same_live_server", "prompt_ownership_guard", "tool_ownership_guard",
                                                "complete_live_baseline"],
            "required_capability_values": {"value": True, "status": "VERIFIED", "evidence_kind": "live-test"},
        },
        "other_live_gates": ["prompt_ownership_guard", "tool_ownership_guard", "exact_policy_preservation",
                             "desktop_live_attachment", "native_bootstrap_acknowledgement"],
        "insufficient_evidence": ["stored history", "exhausted loaded list", "empty background list",
                                  "interrupt reply", "GUI visibility", "independent daemon"],
        "production_promotion": False,
    }


def inspect_workflow_binding(workspace: Path, workflow_id: str, thread: str) -> dict:
    """Read under an existing shared lock, without creating or repairing files."""
    identifier(thread)
    workflow = Workflow(workspace, workflow_id)
    result = {"workflow_id": workflow.id, "current_thread": thread, "status": "BLOCKED",
              "lease_matches_current_thread": False, "current_thread_enrolled": False,
              "lease_transferred": False}
    if not workflow.root.exists():
        return {**result, "reason_code": "workflow-not-initialized"}
    descriptor = None
    try:
        descriptor = os.open(workflow.path("workflow.lock"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WeatherError("Invalid workflow lock")
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        owner = workflow.check_owner()
        tx = read_json(workflow.path("rotation.json"))
        pointer = workflow.current()
        if (not isinstance(tx, dict) or tx.get("schema_version") != 1
                or not isinstance(tx.get("lease"), dict)):
            raise WeatherError("Invalid workflow transaction")
        source, holder, epoch = tx.get("source_thread"), tx["lease"].get("thread"), tx["lease"].get("epoch")
        enrolled = owner.get("enrolled_root_threads", [owner.get("source_thread")])
        if (not isinstance(enrolled, list) or not enrolled or len(set(enrolled)) != len(enrolled)
                or source not in enrolled or holder is not None and holder not in enrolled
                or type(epoch) is not int or epoch < 1):
            raise WeatherError("Invalid workflow root or lease identity")
        for item in enrolled:
            identifier(item)
        from .coordinator import PHASES
        if tx.get("phase") not in PHASES:
            raise WeatherError("Invalid workflow phase")
        unresolved = [step for step in ("create", "submit", "archive") if tx.get(f"{step}_attempt") == "ambiguous"]
        writer = thread == holder and thread in enrolled
        result.update(source_thread=source, lease={"thread": holder, "epoch": epoch}, phase=tx["phase"],
                      generation=pointer["generation"], transaction_id=tx.get("transaction_id"),
                      current_thread_enrolled=thread in enrolled, lease_matches_current_thread=writer,
                      unresolved_requests=unresolved)
        if unresolved:
            result["reason_code"] = "unresolved-remote-request"
        elif not writer:
            result["reason_code"] = "workflow-lease-mismatch"
        else:
            try:
                workflow.assert_writer(thread, epoch)
            except WeatherError:
                result["reason_code"] = "workflow-writes-paused"
            else:
                result.update(status="MATCHED", reason_code="workflow-writer-matched")
        return result
    except BlockingIOError:
        return {**result, "reason_code": "workflow-busy"}
    except (WeatherError, OSError, TypeError, ValueError, KeyError, AttributeError):
        return {**result, "reason_code": "workflow-state-unavailable"}
    finally:
        if descriptor is not None:
            os.close(descriptor)  # Releases only our shared lock.


def inspect_socket_endpoint(path: Path, *, explicit: bool) -> dict:
    """Distinguish stale/missing endpoints; connection success proves no ownership."""
    result = {"path": str(path), "explicit": explicit, "is_symlink": path.is_symlink(),
              "status": "unavailable", "ownership_verified": False}
    try:
        if explicit:
            validate_owner_socket(path)
        info = path.stat()
        result.update(target_exists=True, target_is_socket=stat.S_ISSOCK(info.st_mode))
        if not result["target_is_socket"]:
            return {**result, "status": "not-a-socket"}
        # Resolve the well-known publication link for diagnosis only. It is
        # never passed to the explicit transport or used to discover endpoints.
        validate_owner_socket(path.resolve(strict=True))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
        result["status"] = "connectable-unverified"
    except FileNotFoundError:
        result.update(status="missing", target_exists=False)
    except WeatherError:
        result["status"] = "unsafe-or-noncanonical"
    except OSError as exc:
        result["status"] = {errno.ECONNREFUSED: "connection-refused", errno.EACCES: "permission-denied",
                            errno.ETIMEDOUT: "timeout"}.get(exc.errno, "timeout" if isinstance(exc, TimeoutError) else "unavailable")
    return result


def preflight_owner(engine: Path, thread: str, workspace: Path, workflow_id: str, *,
                    turn: str | None = None, socket_path: Path | None = None, transport: str = "proxy") -> dict:
    """Aggregate independent blockers without claiming to implement missing APIs."""
    identifier(thread)
    if turn is not None:
        identifier(turn)
    if transport not in ("proxy", "unix-websocket") or transport == "unix-websocket" and socket_path is None:
        raise WeatherError("Unix WebSocket owner transport requires an explicit private socket")
    workspace = workspace.resolve(strict=True)
    binding = inspect_workflow_binding(workspace, workflow_id, thread)
    default_home = Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    selected = socket_path if socket_path is not None else default_home / "app-server-control/app-server-control.sock"
    endpoint = inspect_socket_endpoint(selected, explicit=socket_path is not None)
    discovery = probe_desktop(engine, thread, workspace, socket_path, transport=transport, turn=turn)
    blockers = list(discovery["continuation_blockers"])
    if binding["status"] != "MATCHED":
        blockers.insert(0, binding["reason_code"])
    # Do not use a discovered candidate, or this report on disk, to fabricate
    # the complete reader/guards/attachment that must pass real live tests.
    remedies = []
    if binding["status"] != "MATCHED":
        remedies.append({"code": binding["reason_code"], "action":
            "Continue from the existing lease holder and perform the normal seal/bind/bootstrap/acknowledge handover, or initialize a distinct checkpoint-only workflow for this chat. Never rewrite the existing lease."})
    if "owner_connection" in blockers:
        remedies.append({"code": "owner-endpoint-required", "action":
            "Obtain an explicit canonical private endpoint from the Desktop process owning this live root. A stdio-only owner needs a Desktop-supported transport integration; restarting a separate daemon does not attach it."})
    remedies.append({"code": "live-contract-required", "action":
        "Implement and natively verify the required owner contract and remaining live gates. Public partial-state RPCs cannot certify completeness."})
    return {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "owner-readiness-diagnostic",
            "status": "BLOCKED" if blockers else "NOT TESTED", "workspace": str(workspace),
            "target_thread": thread, "target_turn": turn, "workflow_binding": binding,
            "endpoint": endpoint, "owner_discovery": discovery,
            "blockers": blockers, "remediation": remedies, "required_contract": required_owner_contract(),
            "production_ready": False, "production_promotion": False,
            "started_server": False, "reconfigured_server": False, "lease_transferred": False,
            "submitted_turn": False, "interrupted_turn": False, "model_requests": 0}
