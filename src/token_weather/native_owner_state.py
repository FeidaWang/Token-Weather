"""Installed RPC cross-check; isolated fixed responses never prove Desktop ownership."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from .desktop_adapter import JsonRpc
from .models import utc_now
from .native_acceptance import engine_args, runtime_sources
from .native_continuation import TextFixture
from .owner_session import OwnerSession
from .owner_state import READ_METHODS, SupportedOwnerStateProvider
from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError


def probe_native_owner_state(engine: Path) -> dict:
    engine = engine.resolve(strict=True)
    version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    if version != ENGINE_VERSION:
        raise WeatherError("Installed engine does not match the owner-state protocol")
    report = {"schema_version": 1, "generated_at": utc_now(), "kind": "supported-owner-state-primitives",
              "acceptance_scope": "isolated-native", "status": "NOT TESTED", "engine_version": version,
              "runtime_source_sha256": runtime_sources(), "paid_model_requests": 0, "trust_modified": False,
              "production_promotion": False, "same_desktop_owner": "NOT TESTED", "complete_live_state": False,
              "hooks_executed": False, "ephemeral_roots": True, "state_rpc_methods": [], "snapshot": None}
    report["descendant_topology"] = "NOT TESTED; root-only native fixture, nonempty ancestry is offline-tested"
    fixture = TextFixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with tempfile.TemporaryDirectory(prefix="token-weather-owner-state-") as temporary:
            workspace = Path(temporary).resolve()
            preserved = workspace / "uncommitted.txt"
            preserved.write_text("Preserve local work.\n")
            args = engine_args(engine, f"http://127.0.0.1:{server.server_port}/v1", True) + ["-c", "hooks={}"]
            with JsonRpc(args, cwd=workspace) as rpc:
                handshake = rpc.initialize(experimental=True)
                started = rpc.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                    "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                    "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "read-only",
                    "config": {"model_reasoning_effort": "low"}})
                thread = started["thread"]["id"]
                turn = rpc.request("turn/start", {"threadId": thread,
                    "input": [{"type": "text", "text": "Complete the fixed local fixture."}]})["turn"]["id"]
                session = OwnerSession(rpc, thread, turn, workspace, version,
                                       scope="isolated-native", handshake=handshake)
                session.discover()
                deadline = time.monotonic() + 15
                for _ in range(500):
                    message = rpc.next_notification(max(0, deadline - time.monotonic()))
                    if message.get("method") == "hook/completed":
                        report["hooks_executed"] = True
                    session.consume(message)
                    if session.observer.report()["terminal_event"]:
                        break
                else:
                    raise WeatherError("Native owner-state fixture exceeded its event limit")
                report["exact_terminal_observed"] = session.observer.report()["source_terminal"]["status"] == "completed"
                # A terminal may precede the trailing idle-status notification.
                # Observe that tail on the same connection before a stable read.
                for _ in range(100):
                    try:
                        message = rpc.next_notification(.2)
                    except WeatherError as exc:
                        if "timed out" not in str(exc):
                            raise
                        break
                    session.consume(message)
                else:
                    raise WeatherError("Native terminal tail exceeded its event limit")
                original_request = rpc.request
                def tracked(method, params, timeout=5, **kwargs):
                    if method not in READ_METHODS:
                        raise WeatherError("Owner-state provider attempted a non-read-only RPC")
                    report["state_rpc_methods"].append(method)
                    return original_request(method, params, timeout, **kwargs)
                rpc.request = tracked
                report["state_notifications"] = []
                def state_event(message):
                    if ("id" not in message and isinstance(message.get("method"), str)
                            and len(message["method"]) <= 96 and len(report["state_notifications"]) < 100):
                        report["state_notifications"].append(message["method"])
                session.add_listener(state_event)
                provider = SupportedOwnerStateProvider(session)
                # A delayed engine/plugin discovery notification can invalidate
                # the first read. Retry only this explicitly read-only probe,
                # on the same connection, at most three times in five seconds.
                snapshot_deadline = time.monotonic() + 5
                report["snapshot_attempts"] = 0
                for _ in range(3):
                    report["snapshot_attempts"] += 1
                    try:
                        report["snapshot"] = provider.snapshot(snapshot_deadline - time.monotonic())
                        break
                    except WeatherError as exc:
                        if str(exc) != "Owner state changed, disconnected or expired during acquisition":
                            raise
                if report["snapshot"] is None:
                    raise WeatherError("Native state could not acquire a stable snapshot within its read-only retry bound")
                # This probe verifies one coherent acquisition. The reader's
                # subsequent-read race/refusal behavior is covered offline.
                report["incomplete_inventory_preserved"] = (report["snapshot"]["complete"]["descendants"] is False
                    and report["snapshot"]["external_operations"] is None)
                report["baseline_refused"] = False
                try:
                    provider.baseline_reader(session, {}, 5)
                except WeatherError:
                    report["baseline_refused"] = True
                report["files_preserved"] = preserved.read_text() == "Preserve local work.\n"
                report["local_fixture_requests"] = len(fixture.requests)
                snapshot = report["snapshot"]
                if (not report["exact_terminal_observed"] or report["hooks_executed"]
                        or not report["files_preserved"] or not report["incomplete_inventory_preserved"]
                        or not report["baseline_refused"] or set(report["state_rpc_methods"]) != set(READ_METHODS)
                        or snapshot["background"] != {thread: []} or snapshot["external_operations"] is not None
                        or any(snapshot["complete"][key] for key in ("descendants", "tools", "external_operations", "turns"))):
                    raise WeatherError("Native owner-state read/coverage invariants failed")
                report["status"] = "VERIFIED"
                report["reason"] = "Supported partial state reads and refusal of complete baseline verified in an isolated fixture."
    except (WeatherError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        report["reason"] = str(exc)
    finally:
        server.shutdown(); server.server_close(); serving.join(timeout=2)
    return report
