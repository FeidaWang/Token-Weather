"""Existing owner transport and gated source-turn control; never start a daemon."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import re
import stat
import subprocess
import time
import uuid
import copy
import math

from .desktop_adapter import JsonRpc
from .desktop_probe import inspect_loaded_owner, inspect_root_metadata
from .models import utc_now
from .runtime_control import QuiescenceObserver, label
from .runtime_usage import ENGINE_VERSION, UsageObserver
from .storage import WeatherError, identifier


def owner_version(handshake: dict | None) -> str | None:
    agent = handshake.get("userAgent") if isinstance(handshake, dict) else None
    # Keep only the version. Platform, terminal and client strings are discarded.
    if isinstance(agent, str) and re.match(r"^[A-Za-z0-9 _.-]{1,80}/0\.160\.0(?: |$)", agent):
        return ENGINE_VERSION
    return None


def proxy_args(engine: Path, socket_path: Path | None = None) -> list[str]:
    args = [str(engine), "app-server", "proxy"]
    if socket_path is not None:
        validate_owner_socket(socket_path)
        args.extend(["--sock", str(socket_path)])
    return args


def validate_owner_socket(socket_path: Path) -> None:
    if not socket_path.is_absolute() or socket_path.is_symlink():
        raise WeatherError("Owner socket must be an absolute, non-symlink Unix socket")
    info = socket_path.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WeatherError("Owner socket must belong to this user and deny group/other access")
    parent_info = socket_path.parent.stat()
    if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077:
        raise WeatherError("Owner socket directory must be private and belong to this user")
    # Sticky shared ancestors (such as /private/tmp) cannot rename another
    # user's private directory; unprotected writable ancestors can.
    for parent in socket_path.parents:
        parent_mode = parent.stat().st_mode
        if parent.is_symlink() or (parent_mode & 0o022 and not parent_mode & stat.S_ISVTX):
            raise WeatherError("Owner socket has an unsafe parent directory")


def background_inventory(rpc: JsonRpc, thread: str, timeout: float = 5) -> list[str]:
    identifier(thread)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise WeatherError("Background inventory requires a finite positive timeout of at most 30 seconds")
    deadline, cursor, seen = time.monotonic() + timeout, None, set()
    processes = []
    for _ in range(10):
        params = {"threadId": thread, "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WeatherError("Background terminal inventory deadline expired")
        page = rpc.request("thread/backgroundTerminals/list", params, timeout=remaining)
        if not isinstance(page, dict) or "nextCursor" not in page:
            raise WeatherError("Background terminal inventory lacks an explicit pagination boundary")
        rows = page.get("data")
        if not isinstance(rows, list) or len(rows) > 100:
            raise WeatherError("Malformed background terminal inventory")
        for row in rows:
            if not isinstance(row, dict):
                raise WeatherError("Malformed background terminal entry")
            process = label(row.get("processId"))
            if process in processes or len(processes) >= 128:
                raise WeatherError("Duplicate or oversized background terminal inventory")
            processes.append(process)
        cursor = page.get("nextCursor")
        if cursor is None:
            return processes
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise WeatherError("Invalid background terminal cursor")
        seen.add(cursor)
    raise WeatherError("Background terminal inventory exceeded the bounded page limit")


class OwnerSession:
    """One initialized transport shared by usage, lifecycle and controller RPC.

    Construction only records discovery. Live control additionally requires
    release evidence bound to this connection, exact root and runtime. No saved
    discovery/replay/fixture report can satisfy that gate.
    """
    def __init__(self, rpc: JsonRpc, thread: str, turn: str, workspace: Path,
                 engine_version: str, *, scope: str = "desktop-control-discovery", handshake: dict | None = None):
        self.rpc, self.thread, self.workspace = rpc, identifier(thread), workspace.resolve(strict=True)
        self.connection_id = str(uuid.uuid4())
        self.observer = QuiescenceObserver(thread, turn, self.workspace, engine_version, self.connection_id, scope)
        self.usage = UsageObserver(thread, self.workspace, engine_version,
                                   "isolated-native" if scope == "isolated-native" else "desktop-usage-discovery")
        self.owner_matched = False
        self.owner_engine_version = owner_version(handshake)
        self.control_expires = 0
        self.owner_summary = None
        self.source_settings = None
        self.listeners = []
        self.event_revision = 0
        self.baseline_authorized = False
        self.live_baseline = None

    def discover(self, timeout: float = 5) -> dict:
        self.owner_matched = False
        self.owner_summary = None
        deadline = time.monotonic() + timeout
        owner = inspect_loaded_owner(self.rpc, self.thread, self.workspace, timeout)
        if not owner.get("ownership_candidate"):
            raise WeatherError("Exact source root/workspace is not loaded on this connection")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WeatherError("Owner discovery deadline expired")
        metadata = self.rpc.request("thread/read", {"threadId": self.thread, "includeTurns": False}, timeout=remaining)
        if not inspect_root_metadata(metadata, self.thread, self.workspace)["ownership_candidate"]:
            raise WeatherError("Exact source root/workspace changed during owner discovery")
        self.usage.observe_thread(metadata)
        self.owner_matched = True
        self.owner_summary = owner
        return owner

    def authorize_control(self, evidence: dict) -> None:
        binding = evidence.get("owner_binding", {})
        expected = {"connection_id": self.connection_id, "thread_id": self.thread,
                    "workspace": str(self.workspace), "engine_version": ENGINE_VERSION}
        expires = binding.get("expires_at")
        if (not self.owner_matched or self.owner_engine_version != ENGINE_VERSION
                or self.observer.scope != "desktop-control-discovery"
                or evidence.get("acceptance_scope") != "desktop-live-control"
                or any(binding.get(key) != value for key, value in expected.items())
                or type(expires) not in (int, float) or not time.time() < expires <= time.time() + 30):
            raise WeatherError("Control proof does not bind this live owner connection/root/runtime")
        for name in ("same_live_server", "prompt_ownership_guard", "tool_ownership_guard"):
            row = evidence.get("capabilities", {}).get(name, {})
            if row.get("value") is not True or row.get("status") != "VERIFIED" or row.get("evidence_kind") != "live-test":
                raise WeatherError("Owner control and source scheduling guards have not passed live verification")
        self.control_expires = expires
        self.live_baseline = None
        baseline = evidence.get("capabilities", {}).get("complete_live_baseline", {})
        self.baseline_authorized = (baseline.get("value") is True and baseline.get("status") == "VERIFIED"
                                    and baseline.get("evidence_kind") == "live-test")

    def require_control(self) -> None:
        process = getattr(self.rpc, "process", None)
        if getattr(self.rpc, "closed", False) or process is not None and process.poll() is not None:
            self.observer.invalidate("Owner proxy exited")
        if not self.owner_matched or not self.observer.connected or time.time() >= self.control_expires:
            raise WeatherError("Current live owner/control proof is missing, disconnected or expired")

    def consume(self, message: dict) -> None:
        if isinstance(message, dict) and "id" not in message and isinstance(message.get("params"), dict):
            # Includes successor/settings events ignored by the source observer.
            self.event_revision += 1
        self.observer.consume(message)
        self.usage.consume(message)
        if ("id" not in message and message.get("method") == "thread/settings/updated"
                and isinstance(message.get("params"), dict) and message["params"].get("threadId") == self.thread):
            from .continuation import effective_policy, POLICY_FIELDS
            settings = message["params"].get("threadSettings")
            try:
                effective_policy(settings, self.workspace)
            except WeatherError:
                # Incomplete permission provenance blocks continuation, not the
                # independently supported usage/lifecycle discovery adapters.
                self.source_settings = None
            else:
                self.source_settings = {key: copy.deepcopy(settings[key]) for key in (*POLICY_FIELDS.values(), "cwd")}
        for listener in tuple(self.listeners):
            listener(message)

    def restore_baseline(self, reader, **kwargs):
        from .live_baseline import restore_live_baseline
        try:
            return restore_live_baseline(self, reader, **kwargs)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise WeatherError("Malformed complete live baseline evidence") from exc

    def inspect_state(self, timeout: float = 5) -> dict:
        """Read supported partial state; never authorize a baseline or control."""
        from .owner_state import SupportedOwnerStateProvider
        return SupportedOwnerStateProvider(self).snapshot(timeout)

    def add_listener(self, listener) -> None:
        if listener not in self.listeners:
            if len(self.listeners) >= 4:
                raise WeatherError("Owner observer listener limit reached")
            self.listeners.append(listener)

    def remove_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def drain(self) -> None:
        try:
            for _ in range(500):
                message = self.rpc.poll_notification()
                if message is None:
                    return
                self.consume(message)
            raise WeatherError("Owner event drain exceeded the bounded limit")
        except (WeatherError, OSError):
            self.observer.invalidate("Owner connection lost or event evidence incomplete")
            raise

    def interrupt(self) -> dict:
        self.require_control()
        self.drain()
        self.require_control()
        if not self.observer.connected:
            raise WeatherError("Source turn changed before interruption")
        if self.observer.report()["terminal_event"]:
            return {"requested": False, "terminal_already_observed": True}
        if self.observer.interrupt_attempt != "not-started":
            raise WeatherError("An interrupt was already attempted; observe its terminal event without retrying")
        self.observer.interrupt_attempt = "ambiguous"
        try:
            reply = self.rpc.request("turn/interrupt", {"threadId": self.thread, "turnId": self.observer.turn})
        except (WeatherError, OSError):
            self.observer.invalidate("Interrupt reply is ambiguous; wait on a newly verified owner connection")
            raise
        if reply != {}:
            raise WeatherError("Malformed interrupt acknowledgement; wait for terminal reconciliation")
        self.observer.interrupt_attempt = "acknowledged"
        return {"requested": True, "acknowledged": True, "quiescent": False}

    def inspect_inventory(self, descendant_reader=None, timeout: float = 5) -> None:
        """Read background terminals; missing descendant enumeration stays unknown."""
        self.drain()
        revision = self.observer.revision
        members = list(self.observer.members)
        deadline = time.monotonic() + timeout
        backgrounds = {thread: background_inventory(self.rpc, thread, max(0, deadline - time.monotonic())) for thread in members}
        descendants = [thread for thread in members if thread != self.thread]
        complete = False
        external_operations = None
        if descendant_reader is not None:
            self.require_control()
            # A future supported live-owner provider must return a complete
            # bounded enumeration, not merely the observed event receivers.
            inventory = descendant_reader(self, max(0, deadline - time.monotonic()))
            if (not isinstance(inventory, dict)
                    or set(inventory) != {"connection_id", "revision", "descendants", "external_operations", "complete"}
                    or inventory["connection_id"] != self.connection_id or inventory["revision"] != revision):
                raise WeatherError("Live inventory provider must bind descendants and detached work to this connection/revision")
            descendants, external_operations, complete = inventory["descendants"], inventory["external_operations"], inventory["complete"]
        self.drain()  # RPCs may have queued lifecycle changes during inspection.
        self.observer.bind_inventory(backgrounds, descendants, revision=revision,
                                     connection_id=self.connection_id, complete=complete, external_operations=external_operations)

    def wait(self, seconds: float = 5, descendant_reader=None) -> dict:
        if type(seconds) not in (int, float) or not 0 < seconds <= 30:
            raise WeatherError("Source terminal wait must be greater than zero and at most 30 seconds")
        deadline = time.monotonic() + seconds
        try:
            for _ in range(500):
                report = self.observer.report()
                if not report["connected"]:
                    break
                if report["terminal_event"] and all(m["terminal"] for m in report["members"].values()) and not report["active_operations"]:
                    self.inspect_inventory(descendant_reader, max(0, deadline - time.monotonic()))
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.consume(self.rpc.next_notification(remaining))
            else:
                self.observer.invalidate("Source terminal wait event limit exceeded")
        except (WeatherError, OSError) as exc:
            # Timeout retains unknown evidence; a disconnect never proves idle.
            if "timed out" not in str(exc):
                self.observer.invalidate("Owner connection or inventory observation failed")
        return self.observer.report()


@contextmanager
def open_owner_transport(engine: Path, workspace: Path, socket_path: Path | None = None, *, transport: str = "proxy",
                         on_request=None):
    """Version-check an existing endpoint, without claiming root/control proof."""
    if transport not in ("proxy", "unix-websocket") or transport == "unix-websocket" and socket_path is None:
        raise WeatherError("Unix WebSocket owner transport requires an explicit private socket")
    engine, workspace = engine.resolve(strict=True), workspace.resolve(strict=True)
    version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    if version != ENGINE_VERSION:
        raise WeatherError("Installed engine does not match the control adapter's 0.160.0 protocol")
    if transport == "unix-websocket":
        from .unix_websocket import UnixWebSocketRpc
        client = UnixWebSocketRpc(socket_path)
    else:
        client = JsonRpc(proxy_args(engine, socket_path), cwd=workspace)
    with client as rpc:
        original = rpc.request
        if on_request is not None:
            def tracked(method, params, timeout=5, **kwargs):
                on_request(method)
                return original(method, params, timeout=timeout, **kwargs)
            rpc.request = tracked
        handshake = rpc.initialize(experimental=True)  # Read-only background inventory needs this capability.
        if owner_version(handshake) != ENGINE_VERSION:
            raise WeatherError("Existing owner handshake does not identify the pinned 0.160.0 runtime")
        yield rpc, version, handshake


@contextmanager
def open_owner_session(engine: Path, thread: str, turn: str, workspace: Path, socket_path: Path | None = None,
                       *, transport: str = "proxy"):
    """Reuse this one session for P2 observations and P4 source control."""
    identifier(thread)
    identifier(turn)
    with open_owner_transport(engine, workspace, socket_path, transport=transport) as (rpc, version, handshake):
        session = OwnerSession(rpc, thread, turn, workspace, version, handshake=handshake)
        try:
            session.discover()
            yield session
        finally:
            session.observer.invalidate("Owner session closed")


def probe_quiescence(engine: Path, thread: str, turn: str, workspace: Path,
                     seconds: float = 3, socket_path: Path | None = None, *, transport: str = "proxy") -> dict:
    identifier(thread)
    identifier(turn)
    if type(seconds) not in (int, float) or not 0 < seconds <= 30:
        raise WeatherError("Control discovery duration must be greater than zero and at most 30 seconds")
    engine, workspace = engine.resolve(strict=True), workspace.resolve(strict=True)
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "desktop-control-discovery",
              "status": "NOT TESTED", "engine_path": str(engine), "target_thread": thread, "target_turn": turn,
              "workspace": str(workspace), "transport": transport, "production_promotion": False,
              "same_live_server": "NOT TESTED", "model_requests": 0, "started_server": False,
              "submitted_turn": False, "interrupted_turn": False, "trust_modified": False}
    session = None
    try:
        with open_owner_session(engine, thread, turn, workspace, socket_path, transport=transport) as session:
            report.update(session.owner_summary)
            report["owner_engine_version"] = session.owner_engine_version
            report["quiescence_observation"] = session.wait(seconds)
            report["usage_observation"] = session.usage.report()
            report["status"] = ("VERIFIED" if report["quiescence_observation"]["terminal_event"]
                                and report["quiescence_observation"]["connected"] else "NOT TESTED")
            report["reason"] = "Terminal discovery only; complete descendants, control, source guards and Desktop attachment remain unverified."
    except (WeatherError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        if session is not None:
            session.observer.invalidate("Owner discovery failed")
            report["quiescence_observation"] = session.observer.report()
        report["reason"] = str(exc)
    return report
