"""Isolated native continuation primitives, with explicitly simulated gates.

No Desktop ownership/visibility or hook guard claim is produced. Both roots
run fixed local text responses; bootstrap and acknowledgement are controller
side simulations. Production workflow state and trust are untouched.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from .capabilities import CAPABILITY_NAMES
from .checkpoints import Workflow
from .continuation import ContinuationController, effective_policy
from .coordinator import Coordinator
from .desktop_adapter import JsonRpc
from .models import utc_now
from .native_acceptance import Fixture, engine_args, runtime_sources
from .owner_session import OwnerSession
from .remote_reconciliation import RemoteReconciler
from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError, atomic_json, digest, canonical_json


class TextFixture(Fixture):
    def response_item(self, body: dict, request: dict) -> dict:
        return {"type": "message", "id": "msg_continuation", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Local continuation primitive completed.", "annotations": []}]}


def probe_continuation(engine: Path, *, receipt_fault: str | None = None) -> dict:
    if receipt_fault not in (None, "create", "submit"):
        raise WeatherError("Unknown isolated receipt failure boundary")
    engine = engine.resolve(strict=True)
    try:
        version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise WeatherError("Cannot identify the installed continuation engine") from exc
    if version != ENGINE_VERSION:
        raise WeatherError("Installed engine does not match the continuation protocol")
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "isolated-native",
              "kind": "no-hook-continuation-primitives", "status": "NOT TESTED", "engine_path": str(engine),
              "engine_version": version, "production_promotion": False, "paid_model_requests": 0,
              "ephemeral_roots": True,
              "trust_modified": False, "hooks_executed": False, "runtime_source_sha256": runtime_sources(),
              "simulated": ["live release capabilities", "source scheduling guards", "complete inventory provider",
                            "Desktop live attachment", "successor bootstrap/acknowledgement"],
              "same_desktop_owner": "NOT TESTED", "desktop_attachment": "NOT TESTED",
              "native_bootstrap_acknowledgement": "NOT TESTED"}
    fixture = TextFixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    calls = []
    controller = None
    try:
        with tempfile.TemporaryDirectory(prefix="token-weather-continuation-") as temporary:
            workspace = Path(temporary).resolve()
            preserved = workspace / "dirty-worktree.txt"
            preserved.write_text("Preserve uncommitted work.\n")
            args = engine_args(engine, f"http://127.0.0.1:{server.server_port}/v1", True) + ["-c", "hooks={}"]
            with JsonRpc(args, cwd=workspace) as rpc:
                handshake = rpc.initialize(experimental=True)
                original_request = rpc.request
                def tracked(method, params, timeout=5, **kwargs):
                    calls.append(method)  # No prompts, parameters or responses persisted.
                    if method == "thread/start": params = {**params, "ephemeral": True}
                    return original_request(method, params, timeout, **kwargs)
                rpc.request = tracked
                started = rpc.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                    "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                    "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "read-only",
                    "config": {"model_reasoning_effort": "low"}})
                thread = started["thread"]["id"]
                source_turn = rpc.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Run local source fixture."}]})["turn"]["id"]
                session = OwnerSession(rpc, thread, source_turn, workspace, version, handshake=handshake)
                session.discover()
                # All proof labels below are local simulation inputs. They are
                # never returned as evidence and cannot enable a production run.
                capabilities = {name: {"value": True, "status": "VERIFIED", "evidence_kind": "live-test"} for name in CAPABILITY_NAMES}
                source_settings = {**started, "effort": started["reasoningEffort"], "sandboxPolicy": started["sandbox"]}
                source_policy = effective_policy(source_settings, workspace)
                session.source_settings = source_settings
                binding = {"connection_id": session.connection_id, "thread_id": thread, "turn_id": source_turn,
                           "workspace": str(workspace), "engine_version": version, "expires_at": time.time() + 25,
                           "policy_sha256": digest(canonical_json(source_policy))}
                session.authorize_control({"acceptance_scope": "desktop-live-control", "capabilities": capabilities, "owner_binding": binding})
                deadline = time.monotonic() + 15
                for _ in range(500):
                    message = rpc.next_notification(max(0, deadline - time.monotonic()))
                    if message.get("method") == "hook/completed": report["hooks_executed"] = True
                    session.consume(message)
                    if session.observer.report()["terminal_event"]: break
                else: raise WeatherError("Native source terminal event limit exceeded")
                if session.observer.report()["source_terminal"]["status"] != "completed":
                    raise WeatherError("Native source did not complete successfully")
                wf = Workflow(workspace, "fixture")
                wf.initialize(thread, "Continue the isolated primitive fixture.", source_policy)
                wf.prepare(thread)
                coordinator = Coordinator(wf)
                tx = coordinator.transaction(); tx["automatic_rotation"] = True
                atomic_json(wf.path("rotation.json"), tx)
                coordinator.request_rotation(source_turn)
                def inventory(owner, timeout):
                    # Fixed text fixture creates no children/tools/external work.
                    return {"connection_id": owner.connection_id, "revision": owner.observer.revision,
                            "descendants": [], "external_operations": [], "complete": True}
                def attachment(owner, successor, turn):
                    return {"acceptance_scope": "desktop-live-attachment", "connection_id": owner.connection_id,
                            "thread_id": successor, "turn_id": turn, "workspace": str(workspace),
                            "attached_live": True, "visible": True, "duplicate_execution": False, "expires_at": time.time()+3}
                controller = ContinuationController(wf, session, {"acceptance_scope": "desktop-live-continuation",
                    "capabilities": capabilities, "owner_binding": binding}, descendant_reader=inventory, attachment_reader=attachment)
                if receipt_fault is not None:
                    original_receipt = controller.coordinator.journal.record_receipt
                    fault_injected = False
                    def fail_receipt(tx, step, successor, turn=None):
                        nonlocal fault_injected
                        if step == receipt_fault and not fault_injected:
                            fault_injected = True
                            raise OSError("Isolated fixture receipt publication failure")
                        return original_receipt(tx, step, successor, turn)
                    controller.coordinator.journal.record_receipt = fail_receipt
                    try:
                        controller.advance(seconds=5)
                    except OSError:
                        if not fault_injected: raise
                    if not fault_injected:
                        raise WeatherError("Native fixture did not reach its receipt failure boundary")
                    ambiguous = coordinator.transaction()
                    calls_before = len(calls)
                    files_before = {name: wf.path(name).read_bytes() for name in
                                    ("current.json", "rotation.json", "ownership.json", "ledger.jsonl")}
                    observed = RemoteReconciler(controller.coordinator, session).reconcile()
                    files_after = {name: wf.path(name).read_bytes() for name in files_before}
                    report["remote_reconciliation"] = {"receipt_fault": receipt_fault,
                        "fault_injected": fault_injected, "original_response_matched": observed["resolved_steps"] == [receipt_fault],
                        "response_outcome": observed["response_outcomes"].get(receipt_fault),
                        "original_dispatch_saved": bool(ambiguous.get(f"{receipt_fault}_dispatch_sha256")),
                        "dry_run_files_unchanged": files_before == files_after,
                        "dry_run_remote_calls": len(calls) - calls_before, "lease_before_apply": ambiguous["lease"]["thread"]}
                    if (observed["status"] != "ready" or observed["resolved_steps"] != [receipt_fault]
                            or files_before != files_after or len(calls) != calls_before
                            or ambiguous["lease"]["thread"] is not None):
                        raise WeatherError("Original native response did not safely reconcile the missing receipt")
                first = controller.advance(seconds=5)
                tx = coordinator.transaction()
                bootstrap = wf.bootstrap(tx["generation"])
                pending = coordinator.acknowledge(tx["new_thread"], bootstrap["generation"], bootstrap["manifest_sha256"])
                report["pending_ack_has_lease"] = pending["lease"]["thread"] is not None
                deadline = time.monotonic() + 15
                for _ in range(500):
                    if controller.adapter.turn_status == "completed": break
                    message = rpc.next_notification(max(0, deadline - time.monotonic()))
                    if message.get("method") == "hook/completed": report["hooks_executed"] = True
                    session.consume(message)
                else: raise WeatherError("Native successor terminal event limit exceeded")
                final = controller.advance(seconds=5)
                again = controller.advance(seconds=5)
                report.update(source_thread=thread, source_turn=source_turn, successor_thread=tx["new_thread"],
                    successor_turn=tx["turn_id"], source_terminal=True, successor_terminal=controller.adapter.turn_status == "completed",
                    root_creations=calls.count("thread/start"), turn_submissions=calls.count("turn/start"),
                    controller_interrupts=calls.count("turn/interrupt"), first_status=first["status"],
                    final_phase=final["phase"], repeated_tick_status=again["status"],
                    policy_preserved=controller.adapter.settings == source_policy,
                    dirty_worktree_preserved=preserved.read_text() == "Preserve uncommitted work.\n",
                    simulated_ack_lease_granted=final["lease"]["thread"] == tx["new_thread"])
                if (first["status"] == "awaiting-acknowledgement" and final["status"] == "complete"
                        and report["successor_terminal"] and report["policy_preserved"] and not report["pending_ack_has_lease"]
                        and report["dirty_worktree_preserved"] and not report["hooks_executed"]
                        and report["root_creations"] == 2 and report["turn_submissions"] == 2
                        and len(fixture.requests) == 2 and report["runtime_source_sha256"] == runtime_sources()):
                    report["status"] = "VERIFIED"
    except (WeatherError, OSError, ValueError, KeyError, TypeError) as exc:
        report["reason"] = str(exc)
    finally:
        if controller: controller.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    report["fixture_requests"] = fixture.requests
    return report


def probe_reconciliation(engine: Path) -> dict:
    """Two native original-response cases; release providers remain simulations."""
    cases = [probe_continuation(engine, receipt_fault=step) for step in ("create", "submit")]
    return {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "isolated-native",
            "kind": "original-rpc-response-reconciliation", "status": "VERIFIED" if all(row["status"] == "VERIFIED" for row in cases) else "NOT TESTED",
            "production_promotion": False, "paid_model_requests": 0,
            "trust_modified": False, "production_workflow_modified": False,
            "runtime_source_sha256": runtime_sources(), "cases": cases,
            "same_desktop_owner": "NOT TESTED", "reconnect_request_lookup": "UNSUPPORTED",
            "late_wire_response": "Covered by offline pipe transport tests; native cases inject receipt publication failure."}
