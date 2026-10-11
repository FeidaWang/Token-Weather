"""Exercise the installed private-socket proxy in a disposable local service.

The service is a child of this test, never the Desktop daemon. Its fixed local
response and discovery results cannot authorize production control or rotation.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .models import utc_now
from .desktop_probe import probe_desktop
from .native_acceptance import engine_args, runtime_sources
from .native_continuation import TextFixture
from .owner_session import OwnerSession, open_owner_session, owner_version, proxy_args
from .owner_state import READ_METHODS, SupportedOwnerStateProvider
from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError, identifier
from .unix_websocket import UnixWebSocketRpc


@contextmanager
def isolated_listener(engine: Path, workspace: Path, fixture_url: str, lifecycle: dict):
    # macOS sockaddr_un paths are short. A resolved /tmp also avoids the
    # platform's /var -> /private/var symlink in the socket security checks.
    with tempfile.TemporaryDirectory(prefix="tw-owner-", dir=Path("/tmp").resolve(strict=True)) as temporary:
        published_path = Path(temporary) / "owner.sock"
        socket_path = None
        args = engine_args(engine, fixture_url, True)
        args.remove("--stdio")
        args.extend(["--listen", "unix://" + str(published_path), "-c", "hooks={}"])
        process = subprocess.Popen(args, cwd=workspace, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lifecycle["started_isolated_server"] = True
        try:
            deadline = time.monotonic() + 5
            while not published_path.exists():
                if process.poll() is not None:
                    raise WeatherError("Isolated Unix listener exited before readiness")
                if time.monotonic() >= deadline:
                    raise WeatherError("Isolated Unix listener readiness timed out")
                time.sleep(.02)
            # 0.160.0 publishes an alias to its owned generation socket. Resolve
            # only the exact link this test requested in its private directory.
            # The production factory still rejects symlinks supplied by callers.
            lifecycle["published_socket_alias"] = published_path.is_symlink()
            socket_path = Path(os.readlink(published_path)) if published_path.is_symlink() else published_path
            if not socket_path.is_absolute():
                raise WeatherError("Isolated listener published a relative endpoint")
            info = socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise WeatherError("Isolated listener did not create its owned Unix socket")
            proxy_args(engine, socket_path)  # Apply the production path checks.
            lifecycle["socket_permissions_verified"] = True
            yield socket_path, process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if process.stdin:
                process.stdin.close()
            lifecycle["listener_stopped"] = process.poll() is not None
            lifecycle["socket_removed"] = socket_path is not None and not socket_path.exists()


def refuse(action, reason: str, expected: str) -> bool:
    try:
        action()
    except WeatherError as exc:
        if str(exc) != expected:
            raise WeatherError("Native refusal did not match its intended identity or permission check") from None
        return True
    raise WeatherError(reason)


def read_snapshot(session, methods: list[str]) -> tuple[dict, int]:
    original = session.rpc.request
    def tracked(method, params, timeout=5, **kwargs):
        if method not in READ_METHODS:
            raise WeatherError("Native owner reader attempted a modifying RPC")
        methods.append(method)
        return original(method, params, timeout, **kwargs)
    session.rpc.request = tracked
    try:
        provider = SupportedOwnerStateProvider(session)
        deadline = time.monotonic() + 5
        for attempt in range(1, 4):
            try:
                return provider.snapshot(deadline - time.monotonic()), attempt
            except WeatherError as exc:
                if str(exc) != "Owner state changed, disconnected or expired during acquisition":
                    raise
        raise WeatherError("Isolated socket state did not stabilize within the read-only retry bound")
    finally:
        session.rpc.request = original


def assess_transport(report: dict) -> None:
    required = ("creator_terminal_observed", "exact_owner_matched", "reconnected_owner_matched",
                "distinct_connections", "wrong_root_refused", "wrong_workspace_refused",
                "control_refused", "baseline_refused", "closed_connection_invalidated",
                "proxy_close_preserved_owner", "late_lifecycle_unknown", "incomplete_state_preserved",
                "files_preserved", "listener_stopped", "socket_removed", "socket_permissions_verified",
                "discovery_entry_verified")
    if (any(report.get(key) is not True for key in required)
            or report.get("hooks_executed") is not False
            or report.get("local_fixture_requests") != 1
            or report.get("paid_model_requests") != 0
            or report.get("trust_modified") is not False
            or report.get("production_promotion") is not False
            or set(report.get("state_rpc_methods", [])) != set(READ_METHODS)):
        raise WeatherError("Native private-socket transport invariants failed")


def probe_native_owner_transport(engine: Path) -> dict:
    engine = engine.resolve(strict=True)
    version = subprocess.run([str(engine), "--version"], capture_output=True, text=True,
                             timeout=5, check=True).stdout.strip()
    if version != ENGINE_VERSION:
        raise WeatherError("Installed engine does not match the owner transport protocol")
    report = {"schema_version": 1, "generated_at": utc_now(), "kind": "private-socket-owner-transport",
              "acceptance_scope": "isolated-native", "status": "NOT TESTED", "engine_version": version,
              "runtime_source_sha256": runtime_sources(), "transport": "private Unix WebSocket",
              "paid_model_requests": 0, "trust_modified": False, "production_promotion": False,
              "same_desktop_owner": "NOT TESTED", "complete_live_state": False,
              "started_desktop_daemon": False, "ephemeral_roots": True, "hooks_executed": False,
              "started_isolated_server": False, "listener_stopped": False, "socket_removed": False,
              "state_rpc_methods": [], "snapshot": None, "reason_code": "listener-startup"}
    fixture = TextFixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with tempfile.TemporaryDirectory(prefix="token-weather-owner-transport-") as temporary:
            workspace = Path(temporary).resolve(strict=True)
            preserved = workspace / "uncommitted.txt"
            preserved.write_text("Preserve local work.\n")
            wrong_workspace = workspace / "other-workspace"
            wrong_workspace.mkdir()
            with isolated_listener(engine, workspace, f"http://127.0.0.1:{server.server_port}/v1", report) as (socket_path, listener):
                report["reason_code"] = "creator-handshake"
                with UnixWebSocketRpc(socket_path) as creator:
                    handshake = creator.initialize(experimental=True)
                    if owner_version(handshake) != ENGINE_VERSION:
                        raise WeatherError("Isolated listener handshake did not match the installed version")
                    report["reason_code"] = "fixture-creation"
                    thread = identifier(creator.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                        "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                        "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "read-only",
                        "config": {"model_reasoning_effort": "low"}})["thread"]["id"])
                    turn = identifier(creator.request("turn/start", {"threadId": thread,
                        "input": [{"type": "text", "text": "Complete the fixed socket fixture."}]})["turn"]["id"])
                    observed = OwnerSession(creator, thread, turn, workspace, version,
                                            scope="isolated-native", handshake=handshake)
                    observed.discover()
                    deadline = time.monotonic() + 15
                    for _ in range(500):
                        message = creator.next_notification(max(0, deadline - time.monotonic()))
                        if message.get("method") == "hook/completed":
                            report["hooks_executed"] = True
                        observed.consume(message)
                        if observed.observer.report()["terminal_event"]:
                            break
                    else:
                        raise WeatherError("Isolated socket fixture exceeded its event limit")
                    report["creator_terminal_observed"] = observed.observer.report()["source_terminal"]["status"] == "completed"
                    report["reason_code"] = "discovery-entry"
                    entry = probe_desktop(engine, thread, workspace, socket_path, transport="unix-websocket", turn=turn)
                    report["discovery_entry"] = {key: entry[key] for key in
                        ("status", "reason_code", "ownership_candidate", "production_ready", "production_promotion",
                         "target_turn_verified", "same_live_server", "continuation_blockers", "rpc_methods")}
                    report["discovery_entry_verified"] = (entry["status"] == "VERIFIED"
                        and entry["ownership_candidate"] is True and entry["production_ready"] is False
                        and entry["production_promotion"] is False and entry["target_turn_verified"] is False
                        and entry["same_live_server"] == "NOT TESTED"
                        and "desktop_owner_attestation" in entry["continuation_blockers"]
                        and "complete_live_baseline" in entry["continuation_blockers"]
                        and set(entry["rpc_methods"]) == {"initialize", *READ_METHODS}
                        and entry["owner_state"]["external_operations"] is None
                        and all(value is False for value in entry["owner_state"]["complete"].values()))
                    report["reason_code"] = "owner-discovery"
                    with open_owner_session(engine, thread, turn, workspace, socket_path, transport="unix-websocket") as owner:
                        report["exact_owner_matched"] = (owner.owner_matched and owner.owner_engine_version == version)
                        report["control_refused"] = refuse(owner.require_control, "Discovery unexpectedly authorized control",
                            "Current live owner/control proof is missing, disconnected or expired")
                        provider = SupportedOwnerStateProvider(owner)
                        report["baseline_refused"] = refuse(lambda: provider.baseline_reader(owner, {}, 5),
                            "Partial state unexpectedly supplied a complete baseline",
                            "Supported owner RPCs cannot prove complete live descendants, external operations or lifecycle baseline")
                        report["reason_code"] = "partial-state-read"
                        report["snapshot"], report["snapshot_attempts"] = read_snapshot(owner, report["state_rpc_methods"])
                        snapshot = report["snapshot"]
                        report["incomplete_state_preserved"] = (snapshot["external_operations"] is None
                            and all(value is False for value in snapshot["complete"].values())
                            and snapshot["background"] == {thread: []})
                        # Creator lifecycle is never copied into another connection.
                        report["late_lifecycle_unknown"] = not owner.observer.report()["terminal_event"]
                        first_connection = owner.connection_id
                    report["closed_connection_invalidated"] = not owner.observer.connected
                    report["proxy_close_preserved_owner"] = (listener.poll() is None
                        and creator.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"]["id"] == thread)
                    report["reason_code"] = "reconnection"
                    with open_owner_session(engine, thread, turn, workspace, socket_path, transport="unix-websocket") as reconnected:
                        report["reconnected_owner_matched"] = reconnected.owner_matched
                        report["distinct_connections"] = reconnected.connection_id != first_connection
                    def inspect_target(target, directory):
                        with open_owner_session(engine, target, turn, directory, socket_path, transport="unix-websocket"):
                            pass
                    report["reason_code"] = "identity-controls"
                    report["wrong_root_refused"] = refuse(lambda: inspect_target(str(uuid.uuid4()), workspace),
                        "Unloaded root unexpectedly matched the owner", "Exact source root/workspace is not loaded on this connection")
                    report["wrong_workspace_refused"] = refuse(lambda: inspect_target(thread, wrong_workspace),
                        "Different workspace unexpectedly matched the owner", "Exact source root/workspace is not loaded on this connection")
                    if creator.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"]["id"] != thread:
                        raise WeatherError("Identity controls interrupted the isolated owner")
                    report["files_preserved"] = preserved.read_text() == "Preserve local work.\n"
                    report["local_fixture_requests"] = len(fixture.requests)
            report["listener_stopped"] = listener.poll() is not None
            report["socket_removed"] = not socket_path.exists()
        report["reason_code"] = "invariant-check"
        assess_transport(report)
        report.update(status="VERIFIED", reason_code="verified", reason="Private socket, exact root discovery and reconnection verified in a disposable service; Desktop ownership and complete baseline remain unverified.")
    except (WeatherError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        # No native error bodies, userAgent, command text or credentials in reports.
        report["reason"] = "Isolated private-socket owner transport verification failed."
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    return report
