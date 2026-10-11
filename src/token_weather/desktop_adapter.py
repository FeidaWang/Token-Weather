"""Explicit adapters. An isolated app-server is never the Desktop thread owner."""
from __future__ import annotations

import json
import copy
import os
from pathlib import Path
import selectors
import subprocess
import time
from urllib.parse import urlencode, quote

from .storage import WeatherError
from .rpc_correlation import ResponseCorrelator


def prepared_link(workspace: Path, workflow_id: str, generation: int, launcher: Path) -> str:
    if not workspace.is_absolute() or not workspace.is_dir():
        raise WeatherError("Deep-link workspace must be an existing absolute directory")
    # Only identifiers/local paths enter the URL. Full task text and secrets stay on disk.
    import shlex
    command = shlex.join([str(launcher), "--workspace", str(workspace), "--workflow", workflow_id, "bootstrap", "--generation", str(generation)])
    prompt = f"Continue workflow {workflow_id}, generation {generation}. First run {command}. Read its active state once, then acknowledge and wait for the controller write lease."
    return "codex://new?" + urlencode({"path": str(workspace), "prompt": prompt}, quote_via=quote)


def open_prepared(url: str) -> dict:
    if not url.startswith("codex://new?"):
        raise WeatherError("Unsupported prepared link")
    try:
        result = subprocess.run(["/usr/bin/open", url], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WeatherError("Desktop open failed/ambiguous; inspect the existing composer before retrying") from exc
    if result.returncode:
        raise WeatherError("Desktop rejected the prepared link")
    return {"opened": True, "submitted": False, "workspace_verified": False,
            "note": "OS dispatch succeeded; inspect workspace/composer before sending"}


class JsonRpc:
    """Bounded stdio transport with no background thread/model invocation."""
    def __init__(self, args: list[str], env: dict | None = None, cwd: Path | None = None):
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, cwd=cwd, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.counter = 0
        self.notifications: list[dict] = []
        self.correlation = ResponseCorrelator()

    def write(self, message: dict) -> None:
        if self.process.poll() is not None:
            raise WeatherError("App-server transport exited")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request_correlated(self, method: str, params: dict, before_send, timeout: float = 5) -> dict:
        """Persist the exact wire binding before any possibly effective write."""
        return self.request(method, params, timeout, before_send=before_send)

    def request(self, method: str, params: dict, timeout: float = 5, *, before_send=None) -> dict:
        self.counter += 1
        request_id = self.counter
        if before_send is not None:
            params = copy.deepcopy(params)
            binding = self.correlation.reserve(request_id, method, params)
            try:
                before_send(binding)
            except BaseException:
                self.correlation.cancel(binding)
                raise
            # A partial/failed write is still potentially effective remotely.
            self.correlation.dispatched(binding)
        self.write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self._read_message(max(0, deadline - time.monotonic()))
            self.correlation.capture(response)
            if type(response.get("id")) is int and response["id"] == request_id and "method" not in response:
                if "error" in response:
                    error = response["error"]
                    code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
                    raise WeatherError(f"RPC {method} rejected: {code}")
                if "result" not in response:
                    raise WeatherError(f"Malformed RPC {method} response; reconcile before retrying")
                if before_send is not None and self.correlation.response(binding).get("outcome") != "success":
                    raise WeatherError(f"Invalid correlated RPC {method} response; reconcile before retrying")
                return response["result"]
            if "method" in response and "id" in response:
                self._deny_request(response)
            elif "method" in response:
                if len(self.notifications) >= 100:
                    raise WeatherError("App-server notification limit reached; evidence is incomplete")
                self.notifications.append(response)
        raise WeatherError(f"RPC {method} timed out; reconcile before retrying")

    def _deny_request(self, message: dict) -> None:
        self.write({"id": message["id"], "error": {"code": -32601,
                   "message": "Diagnostic client does not grant approvals"}})

    def _read_message(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise WeatherError("Invalid app-server message") from exc
                if not isinstance(message, dict):
                    raise WeatherError("App-server message must be an object")
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise WeatherError("App-server event timed out; evidence is incomplete")
            chunk = os.read(self.process.stdout.fileno(), 65_536)
            if not chunk:
                raise WeatherError("App-server disconnected; reconcile before retrying")
            self.buffer += chunk
            if len(self.buffer) > 2_000_000:
                raise WeatherError("Oversized app-server response")

    def next_notification(self, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            message = self.notifications.pop(0) if self.notifications else self._read_message(max(0, deadline - time.monotonic()))
            self.correlation.capture(message)
            if "method" in message and "id" in message:
                self._deny_request(message)
            elif "method" in message:
                return message
            if time.monotonic() >= deadline:
                raise WeatherError("App-server notification timed out; evidence is incomplete")

    def poll_notification(self) -> dict | None:
        """Drain already available evidence without waiting or dropping fragments."""
        for _ in range(100):
            if self.notifications:
                message = self.notifications.pop(0)
            else:
                if b"\n" not in self.buffer and self.selector.select(0):
                    chunk = os.read(self.process.stdout.fileno(), 65_536)
                    if not chunk:
                        raise WeatherError("App-server disconnected; evidence is incomplete")
                    self.buffer += chunk
                    if len(self.buffer) > 2_000_000:
                        raise WeatherError("Oversized app-server response")
                if b"\n" not in self.buffer:
                    return None
                message = self._read_message(0)
            self.correlation.capture(message)
            if "method" in message and "id" in message:
                self._deny_request(message)
            elif "method" in message:
                return message
        raise WeatherError("App-server message limit reached; evidence is incomplete")

    def initialize(self, experimental: bool = False) -> dict:
        result = self.request("initialize", {"clientInfo": {"name": "token_weather", "version": "0.1.0"}, "capabilities": {"experimentalApi": experimental}})
        self.write({"method": "initialized", "params": {}})
        return result

    def close(self) -> None:
        self.correlation.clear()
        self.selector.close()
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LiveDesktopAdapter:
    """Legacy placeholder; live rotation now requires ContinuationController."""
    def __init__(self, rpc: JsonRpc, report: dict, workspace: Path):
        from .capabilities import mode
        if mode(report) != "automatic":
            raise WeatherError("Automatic Desktop adapter has not passed its live release gates")
        self.rpc, self.workspace = rpc, workspace

    def create_root(self, state: dict) -> str:
        raise WeatherError("Use ContinuationController with a proven owner and fresh successor policy/attachment checks")

    def start_turn(self, thread: str, prompt: str) -> dict:
        raise WeatherError("Use the durable ContinuationController for single initialization submission")

    def archive(self, thread: str) -> dict:
        raise WeatherError("Live reversible archive and descendant safety remain unverified")
