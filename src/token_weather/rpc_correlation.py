"""Bounded, connection-local response evidence; never retain request/transcript text."""
from __future__ import annotations

import copy
from pathlib import Path
import uuid

from .storage import WeatherError, canonical_json, digest, identifier

METHODS = {"create": "thread/start", "submit": "turn/start"}
MAX_REQUESTS = 64


class ResponseCorrelator:
    def __init__(self):
        self.transport_id = str(uuid.uuid4())
        self.records = {}

    def reserve(self, request_id: int, method: str, params: dict) -> dict:
        if method not in METHODS.values() or type(request_id) is not int or request_id < 1:
            raise WeatherError("Unsupported correlated request")
        if request_id in self.records or len(self.records) >= MAX_REQUESTS:
            raise WeatherError("Correlation registry is full or request ID was reused; no dispatch")
        binding = {"schema_version": 1, "transport_id": self.transport_id, "request_id": request_id,
                   "method": method, "params_sha256": digest(canonical_json(params))}
        self.records[request_id] = {"binding": binding, "response": None, "dispatched": False,
                                    "workspace": params.get("cwd") if method == "thread/start" else None}
        return copy.deepcopy(binding)

    def dispatched(self, binding: dict) -> None:
        self._record(binding)["dispatched"] = True

    def cancel(self, binding: dict) -> None:
        record = self._record(binding)
        if record["dispatched"]:
            raise WeatherError("Cannot forget a possibly dispatched request")
        del self.records[binding["request_id"]]

    def _record(self, binding: dict) -> dict:
        request_id = binding.get("request_id") if isinstance(binding, dict) else None
        record = self.records.get(request_id) if type(request_id) is int else None
        if record is None or canonical_json(record["binding"]) != canonical_json(binding):
            raise WeatherError("Request correlation does not belong to this original transport")
        return record

    def capture(self, message: dict) -> None:
        request_id = message.get("id")
        # A server request is never a response, even when its ID collides.
        if "method" in message or type(request_id) is not int or request_id not in self.records:
            return
        record = self.records[request_id]
        if not record["dispatched"]:
            return
        reduced = {"outcome": "invalid"}
        if (not set(message) - {"id", "jsonrpc", "result", "error"}
                and ("jsonrpc" not in message or message["jsonrpc"] == "2.0")
                and ("result" in message) != ("error" in message)):
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict) and type(error.get("code")) is int:
                    reduced = {"outcome": "rejected", "error_code": error["code"]}
            else:
                reduced = self._success(record, message["result"])
        old = record["response"]
        if old is not None and old != reduced:
            reduced = {"outcome": "invalid"}  # Conflicts remain sticky.
        record["response"] = reduced

    def _success(self, record: dict, result) -> dict:
        if not isinstance(result, dict):
            return {"outcome": "invalid"}
        creation = record["binding"]["method"] == "thread/start"
        value = result.get("thread" if creation else "turn")
        try:
            identity = identifier(value.get("id") if isinstance(value, dict) else None)
        except WeatherError:
            return {"outcome": "invalid"}
        reduced = {"outcome": "success", "thread_id": identity if creation else None,
                   "turn_id": None if creation else identity}
        if creation:
            # Preserve only fully validated policy metadata for an in-memory
            # continuation baseline. Failed policy validation still retains the
            # created ID, but can never authorize sending to it.
            reduced["creation_policy"] = None
            try:
                from .continuation import effective_policy
                workspace = record["workspace"]
                if (not isinstance(workspace, str) or not Path(workspace).is_absolute()
                        or value.get("cwd") != workspace
                        or "parentThreadId" not in value or value["parentThreadId"] is not None
                        or "forkedFromId" not in value or value["forkedFromId"] is not None):
                    raise WeatherError("Creation response is not a fresh worktree root")
                policy = {**result, "effort": result.get("reasoningEffort"), "sandboxPolicy": result.get("sandbox")}
                reduced["creation_policy"] = effective_policy(policy, Path(workspace).resolve())
            except WeatherError:
                pass
        return reduced

    def response(self, binding: dict) -> dict | None:
        record = self._record(binding)
        if not record["dispatched"]:
            return None
        return copy.deepcopy(record["response"])

    def clear(self) -> None:
        self.records.clear()
