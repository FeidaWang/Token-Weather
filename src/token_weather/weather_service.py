"""Managed per-root loopback collectors. Control only a verified own instance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from .storage import WeatherError, atomic_json, identifier, lock, owned_path, read_json
from .weather_cache import data_directory
from .weather_sources import collect_weather
from .weather_view import weather_server

SERVICE_VERSION = 1
_CHILDREN: dict[str, subprocess.Popen] = {}


def _root(path: Path | None) -> Path:
    root = path.absolute() if path is not None else data_directory()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise WeatherError("Weather service directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def runtime_fingerprint() -> str:
    root = Path(__file__).resolve().parent
    names = ("weather.py", "weather_cache.py", "weather_sources.py", "weather_hook.py", "weather_view.py", "weather_service.py", "cli.py")
    value = hashlib.sha256()
    for name in names:
        value.update(name.encode())
        value.update((root / name).read_bytes())
    return value.hexdigest()


def _path(root: Path, thread: str) -> Path:
    return owned_path(root, "service-" + identifier(thread) + ".json")


def _guard(root: Path, thread: str):
    return lock(owned_path(root, "service-" + identifier(thread) + ".lock"), timeout=3)


def _read(root: Path, thread: str) -> dict | None:
    value = read_json(_path(root, thread))
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("schema_version") != SERVICE_VERSION
            or value.get("thread_id") != thread or not isinstance(value.get("workspace"), str)
            or not Path(value["workspace"]).is_absolute() or value.get("state") not in ("starting", "running", "stopping", "stopped")
            or not isinstance(value.get("instance_id"), str) or not isinstance(value.get("control_token"), str)):
        raise WeatherError("Invalid weather service registration; no process was controlled")
    identifier(value["instance_id"])
    return value


def _alive(value: dict) -> bool:
    pid = value.get("pid")
    if type(pid) is not int or pid <= 1:
        return False
    child = _CHILDREN.get(value["instance_id"])
    if child is not None:
        return child.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie can still accept signal 0 but is no longer a running collector.
    probe = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2)
    return not probe.returncode and not probe.stdout.strip().startswith("Z")


def _identity(value: dict) -> dict:
    return {key: value.get(key) for key in ("instance_id", "pid", "thread_id", "workspace", "runtime_fingerprint")}


def _remote(value: dict, stop: bool = False) -> dict:
    port = value.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise WeatherError("Weather service has not published a loopback port")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/service/" + ("stop" if stop else "status"))
    if stop:
        request.data = b"{}"
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Token-Weather-Control", value["control_token"])
        request.add_header("X-Token-Weather-Instance", value["instance_id"])
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            data = json.loads(response.read(65537))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise WeatherError("Weather service endpoint is unavailable") from exc
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in _identity(value).items()):
        raise WeatherError("Weather service identity mismatch; no process was controlled")
    return data


def _summary(value: dict | None, state: str | None = None, **extra) -> dict:
    if value is None:
        return {"state": "not-started", **extra}
    result = {key: value.get(key) for key in ("instance_id", "pid", "thread_id", "workspace", "port", "started_at", "runtime_fingerprint")}
    result.update(state=state or value["state"], **extra)
    if type(value.get("port")) is int and value["port"] > 0:
        result["url"] = f'http://127.0.0.1:{value["port"]}'
    return result  # Never return the control credential or inherited environment.


def service_status(thread: str, *, root: Path | None = None) -> dict:
    root = _root(root)
    value = _read(root, thread)
    if value is None:
        return _summary(None)
    if not _alive(value):
        return _summary(value, "stopped" if value["state"] == "stopped" else "stale", verified=False)
    try:
        remote = _remote(value)
    except WeatherError as exc:
        return _summary(value, "unreachable", verified=False, reason=str(exc))
    return _summary(value, "running", verified=True, collector=remote.get("collector"))


def service_list(*, root: Path | None = None) -> dict:
    root = _root(root)
    entries = []
    for path in sorted(root.glob("service-*.json")):
        thread = path.stem.removeprefix("service-")
        try:
            entries.append(service_status(thread, root=root))
        except WeatherError:
            entries.append({"thread_id": thread, "state": "invalid-registration"})
    return {"services": entries}


def _wait_running(root: Path, thread: str, instance: str, timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _read(root, thread)
        if value is None or value["instance_id"] != instance:
            raise WeatherError("Weather service startup registration changed")
        if value["state"] == "running":
            _remote(value)
            return value
        if value.get("pid") is not None and not _alive(value):
            raise WeatherError("Weather collector failed to start; inspect its private service log")
        time.sleep(.05)
    raise WeatherError("Weather collector startup is pending; inspect status before retrying")


def start_service(workspace: Path, thread: str, *, port: int = 0, transcript: Path | None = None,
                  events: Path | None = None, compact_limit: int | None = None, max_age: float = 120,
                  poll_interval: float = 2, root: Path | None = None, entry: Path | None = None) -> dict:
    identifier(thread)
    workspace = workspace.resolve(strict=True)
    root = _root(root)
    if type(port) is not int or not 0 <= port <= 65535 or not .2 <= poll_interval <= 30 or not 0 < max_age <= 86400:
        raise WeatherError("Invalid service port, polling interval or reading age")
    fingerprint = runtime_fingerprint()
    settings = {"transcript": str(transcript.absolute()) if transcript is not None else None,
                "events": str(events.absolute()) if events is not None else None,
                "compact_limit": compact_limit, "max_age": max_age, "poll_interval": poll_interval}
    with _guard(root, thread):
        old = _read(root, thread)
        if old is not None and _alive(old):
            if old["workspace"] != str(workspace) or old.get("settings") != settings or old.get("runtime_fingerprint") != fingerprint:
                raise WeatherError("The current root already has a collector with different settings/runtime; use weather-service restart")
            if port and old.get("port") not in (0, port):
                raise WeatherError("The current root already has a collector on another port")
            instance = old["instance_id"]
            reused = True
        else:
            # Validate exact identity and source before launching a detached process.
            collect_weather(workspace, thread, transcript=transcript, events=events,
                            compact_limit=compact_limit, max_age=max_age, cache_root=root)
            launch = (entry or Path(__file__).resolve().parents[2] / "scripts/weather.py").resolve(strict=True)
            instance = str(uuid.uuid4())
            value = {"schema_version": SERVICE_VERSION, "state": "starting", "instance_id": instance,
                     "pid": None, "thread_id": thread, "workspace": str(workspace), "port": 0,
                     "requested_port": port, "preferred_port": port or (old.get("port", 0) if old else 0),
                     "settings": settings, "runtime_fingerprint": fingerprint,
                     "control_token": secrets.token_urlsafe(32), "started_at": time.time(), "entry": str(launch)}
            atomic_json(_path(root, thread), value)
            log = owned_path(root, "service-" + thread + ".log")
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                env = dict(os.environ, TOKEN_WEATHER_DATA_DIR=str(root))
                with os.fdopen(fd, "ab", buffering=0) as output:
                    child = subprocess.Popen([sys.executable, str(launch), "--workspace", str(workspace), "--thread-id", thread,
                                              "weather-service", "_run", "--instance-id", instance],
                                             stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                             env=env, start_new_session=True)
                _CHILDREN[instance] = child
            except BaseException:
                value["state"] = "stopped"
                atomic_json(_path(root, thread), value)
                raise
            value["pid"] = child.pid
            atomic_json(_path(root, thread), value)
            reused = False
    value = _wait_running(root, thread, instance)
    return _summary(value, "running", verified=True, reused=reused)


def stop_service(thread: str, *, root: Path | None = None) -> dict:
    root = _root(root)
    with _guard(root, thread):
        value = _read(root, thread)
        if value is None or not _alive(value):
            if value is not None and value["state"] != "stopped":
                value["state"] = "stopped"
                atomic_json(_path(root, thread), value)
            return _summary(value, "stopped" if value else "not-started", already_stopped=True)
        if value["state"] != "stopping":
            _remote(value)  # Verify identity before sending the private stop credential.
            _remote(value, stop=True)
            value["state"] = "stopping"
            atomic_json(_path(root, thread), value)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = _read(root, thread)
        if current is None or current["instance_id"] != value["instance_id"]:
            raise WeatherError("Weather service registration changed during stop")
        if not _alive(current):
            if current["state"] != "stopped":
                current["state"] = "stopped"
                with _guard(root, thread):
                    if _read(root, thread)["instance_id"] == current["instance_id"]:
                        atomic_json(_path(root, thread), current)
            return _summary(current, "stopped", verified=True)
        time.sleep(.05)
    raise WeatherError("Weather service stop is pending; check status before retrying")


def run_service(workspace: Path, thread: str, instance: str) -> int:
    root = data_directory()
    value = _read(root, thread)
    if (value is None or value["instance_id"] != instance or value["workspace"] != str(workspace.resolve())
            or value["runtime_fingerprint"] != runtime_fingerprint()):
        raise WeatherError("Weather worker registration does not match its runtime/root")
    settings = value["settings"]
    stop = threading.Event()
    metrics = {"polls": 0, "last_error": None, "last_collection": None}

    def collect():
        report = collect_weather(workspace, thread,
            transcript=Path(settings["transcript"]) if settings["transcript"] else None,
            events=Path(settings["events"]) if settings["events"] else None,
            compact_limit=settings["compact_limit"], max_age=settings["max_age"], cache_root=root)
        metrics.update(polls=metrics["polls"] + 1, last_error=None, last_collection=report.get("collection"))
        return report

    def poll():
        while not stop.wait(settings["poll_interval"]):
            try:
                collect()
            except (WeatherError, OSError, ValueError, TypeError, KeyError) as exc:
                metrics["last_error"] = type(exc).__name__

    collect()
    public = {**_identity(value), "pid": os.getpid(), "collector": metrics}
    preferred = value["preferred_port"]
    try:
        server = weather_server(collect, preferred, service=lambda: public,
                                control_token=value["control_token"], on_stop=lambda: stop.set())
    except OSError:
        if value["requested_port"]:
            raise
        server = weather_server(collect, 0, service=lambda: public,
                                control_token=value["control_token"], on_stop=lambda: stop.set())
    with _guard(root, thread):
        current = _read(root, thread)
        if current is None or current["instance_id"] != instance or current["pid"] != os.getpid():
            server.server_close()
            raise WeatherError("Weather worker startup ownership changed")
        current.update(state="running", port=server.server_port)
        atomic_json(_path(root, thread), current)
    worker = threading.Thread(target=poll, daemon=True)
    worker.start()
    # Signals affect only this directly managed worker, never another PID.
    def interrupt(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=1)
        with _guard(root, thread):
            current = _read(root, thread)
            if current and current["instance_id"] == instance:
                current.update(state="stopped", stopped_at=time.time())
                atomic_json(_path(root, thread), current)
    return 0
