"""Bounded lifecycle evidence. An interrupt acknowledgement is never idle proof."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import time

from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError, identifier, digest, canonical_json

SCOPES = ("desktop-control-discovery", "runtime-control-replay", "isolated-native")
TERMINAL = {"completed", "interrupted", "failed"}
TOOL_TYPES = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}
ITEM_STATUSES = {"commandExecution": {"inProgress", "completed", "failed", "declined"},
                 "fileChange": {"inProgress", "completed", "failed", "declined"},
                 "mcpToolCall": {"inProgress", "completed", "failed"},
                 "dynamicToolCall": {"inProgress", "completed", "failed"},
                 "collabAgentToolCall": TERMINAL | {"inProgress"}}
PASSIVE_TYPES = {"userMessage", "hookPrompt", "agentMessage", "functionCallOutput", "plan", "reasoning",
                 "webSearch", "imageView", "sleep", "imageGeneration", "enteredReviewMode", "exitedReviewMode",
                 "contextCompaction", "subAgentActivity"}


def label(value) -> str:
    # Runtime item/process identifiers are opaque, not filesystem identifiers.
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise WeatherError("Invalid runtime control label")
    return value


class QuiescenceObserver:
    """One exact source turn and its known descendants on one ordered connection.

    Public lifecycle events cannot enumerate pre-existing children or detached
    work. A separate complete inventory must be bound at the current revision.
    Only allowlisted IDs/statuses survive reduction; no tool arguments or text.
    """
    def __init__(self, thread: str, turn: str, workspace: Path, engine_version: str,
                 connection_id: str, scope: str = "desktop-control-discovery"):
        if engine_version != ENGINE_VERSION or scope not in SCOPES:
            raise WeatherError("Control observer requires the pinned 0.160.0 protocol and a known scope")
        self.thread, self.turn = identifier(thread), identifier(turn)
        self.workspace = workspace.resolve(strict=True)
        self.connection_id, self.scope = identifier(connection_id), scope
        self.revision = 0
        self.received_at = None
        self.connected = True
        self.errors: list[str] = []
        self.members = {thread: self._member(turn)}
        self.items: dict[tuple[str, str, str], dict] = {}
        self.inventory = None
        self.interrupt_attempt = "not-started"

    @staticmethod
    def _member(turn=None) -> dict:
        return {"turn_id": turn, "started": False, "coverage_complete": True, "terminal": None}

    def invalidate(self, reason: str) -> None:
        self.connected = False
        self.inventory = None
        if len(self.errors) < 16 and reason not in self.errors:
            self.errors.append(reason)

    def _child(self, child: str) -> None:
        identifier(child)
        if child == self.thread:
            raise WeatherError("A descendant cannot be the source root")
        if child not in self.members:
            if len(self.members) >= 33:
                raise WeatherError("Control descendant limit exceeded; evidence is incomplete")
            self.members[child] = self._member()

    def consume(self, message: dict, observed_at: float | None = None) -> bool:
        try:
            return self._consume(message, observed_at)
        except (WeatherError, KeyError, TypeError, ValueError):
            self.invalidate("Malformed or inconsistent lifecycle evidence")
            raise

    def _consume(self, message: dict, observed_at: float | None) -> bool:
        if not self.connected:
            raise WeatherError("Control evidence is disconnected or invalid")
        if not isinstance(message, dict):
            raise WeatherError("Control event must be an object")
        if "id" in message or not isinstance(message.get("params"), dict):
            return False
        params, method = message["params"], message.get("method")
        thread = params.get("threadId")
        if thread not in self.members:
            return False
        member = self.members[thread]
        if method not in ("turn/started", "turn/completed", "item/started", "item/completed", "error"):
            return False
        turn_data = params.get("turn", {}) if method.startswith("turn/") else {}
        if not isinstance(turn_data, dict):
            raise WeatherError("Malformed control turn")
        turn = turn_data.get("id") if method.startswith("turn/") else params.get("turnId")
        if method == "error":
            if turn in (None, member["turn_id"]):
                self.invalidate("Runtime error; operation effects require reconciliation")
            return False
        identifier(turn)
        if member["turn_id"] is not None and turn != member["turn_id"]:
            if method == "turn/started":
                # Do not silently switch away from the source transaction or
                # forget an earlier child's outstanding operations.
                self.invalidate("A tracked thread started another turn")
            return False
        stamp = time.time() if observed_at is None else observed_at
        if (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0
                or stamp > time.time() + 1 or (self.received_at is not None and stamp < self.received_at)):
            raise WeatherError("Invalid or out-of-order control receipt time")
        self.received_at = stamp
        self.revision += 1
        self.inventory = None
        member["turn_id"] = turn
        if method == "turn/started":
            if turn_data.get("status") != "inProgress" or member["terminal"] is not None:
                raise WeatherError("Invalid turn start after terminal evidence")
            member["started"] = True
            return True
        if not member["started"]:
            member["coverage_complete"] = False
        if method == "turn/completed":
            status = turn_data.get("status")
            if status not in TERMINAL:
                raise WeatherError("Nonterminal turn/completed status")
            if member["terminal"] is not None and member["terminal"]["status"] != status:
                raise WeatherError("Conflicting terminal events")
            member["terminal"] = {"status": status, "error": bool(turn_data.get("error")), "observed_at": stamp}
            return True
        item = params.get("item")
        if not isinstance(item, dict):
            raise WeatherError("Malformed control item")
        kind, item_id = item.get("type"), label(item.get("id"))
        if kind not in TOOL_TYPES | PASSIVE_TYPES:
            raise WeatherError("Unknown control item type; effects cannot be classified")
        if method == "item/started" and member["terminal"] is not None:
            raise WeatherError("Operation started after terminal event")
        if kind == "subAgentActivity":
            self._child(item.get("agentThreadId"))
        if kind not in TOOL_TYPES:
            return True
        key = (thread, turn, item_id)
        previous = self.items.get(key)
        if previous is None:
            if len(self.items) >= 128:
                raise WeatherError("Control item limit exceeded; evidence is incomplete")
            if method == "item/completed":
                member["coverage_complete"] = False
        elif previous["type"] != kind:
            raise WeatherError("Control item type changed")
        status = item.get("status")
        if status not in ITEM_STATUSES[kind]:
            raise WeatherError("Unknown tool lifecycle status")
        if previous and previous["finished"] and (method == "item/started" or status != previous["status"]):
            raise WeatherError("Completed operation restarted or changed status")
        if kind == "collabAgentToolCall":
            if item.get("senderThreadId") != thread:
                raise WeatherError("Collaboration sender does not match the tracked turn")
            receivers = item.get("receiverThreadIds")
            if not isinstance(receivers, list) or len(receivers) > 32:
                raise WeatherError("Invalid collaboration receivers")
            for child in receivers:
                # A wait/close state is not an exact child terminal event.
                identifier(child)
                if child not in self.members:
                    self._child(child)
        process = item.get("processId") if kind == "commandExecution" else None
        if process is not None:
            label(process)
        self.items[key] = {"type": kind, "status": status, "process_id": process,
                           "result_failed": (kind == "dynamicToolCall" and item.get("success") is False)
                                            or (kind == "mcpToolCall" and bool(item.get("error"))),
                           "finished": method == "item/completed" and status != "inProgress"}
        return True

    def bind_inventory(self, background: dict[str, list[str]], descendants: list[str], *,
                       revision: int, connection_id: str, complete: bool, external_operations: list[str] | None = None) -> None:
        """Controller-only live inventory; a saved report cannot bind evidence.

        Complete means the provider enumerated *all* descendants and detached
        work through a separately verified owner path, not just known events.
        """
        if not self.connected or connection_id != self.connection_id or revision != self.revision:
            raise WeatherError("Inventory no longer matches the live connection/revision")
        expected = set(self.members) - {self.thread}
        if (not isinstance(descendants, list) or len(descendants) > 32
                or len(set(descendants)) != len(descendants) or set(descendants) != expected
                or not isinstance(background, dict) or set(background) != set(self.members)):
            raise WeatherError("Inventory must account for every tracked root/descendant exactly once")
        reduced = {}
        for thread, processes in background.items():
            if not isinstance(processes, list) or len(processes) > 128 or len(set(processes)) != len(processes):
                raise WeatherError("Invalid background process inventory")
            reduced[thread] = [label(process) for process in processes]
        if type(complete) is not bool:
            raise WeatherError("Inventory completeness must be explicit")
        if complete and external_operations is None:
            raise WeatherError("Complete inventory must explicitly account for detached external operations")
        if external_operations is not None:
            if (not isinstance(external_operations, list) or len(external_operations) > 128
                    or len(set(external_operations)) != len(external_operations)):
                raise WeatherError("Invalid detached operation inventory")
            external_operations = [label(operation) for operation in external_operations]
        self.inventory = {"revision": revision, "complete": complete, "background": reduced,
                          "external_operations": external_operations,
                          "observed_at": time.time()}

    def report(self) -> dict:
        reasons = list(self.errors)
        source_terminal = copy.deepcopy(self.members[self.thread]["terminal"])
        if not self.connected:
            reasons.append("Live connection is unavailable")
        if source_terminal is None:
            reasons.append("Exact source terminal event has not arrived")
        if any(not m["started"] or not m["coverage_complete"] for m in self.members.values()):
            reasons.append("Lifecycle coverage began after activity or is incomplete")
        if any(m["terminal"] is None for m in self.members.values()):
            reasons.append("A tracked root/descendant has no exact terminal event")
        operation_id = lambda key: "runtime-" + digest(canonical_json(list(key)))[:24]
        active = [operation_id(key) for key, row in self.items.items() if not row["finished"]]
        if active:
            reasons.append("Tools or background command items are still running")
        unknown = [operation_id(key) for key, row in self.items.items()
                   if row["finished"] and (row["status"] in ("failed", "interrupted") or row["result_failed"])]
        if any(m["terminal"] and (m["terminal"]["status"] == "failed" or m["terminal"]["error"])
               for m in self.members.values()):
            reasons.append("Failed turn effects require explicit reconciliation")
        fresh = bool(self.inventory and 0 <= time.time() - self.inventory["observed_at"] <= 5)
        if not fresh or not self.inventory["complete"]:
            reasons.append("Complete live background/descendant inventory is missing or stale")
        if self.inventory and any(self.inventory["background"].values()):
            reasons.append("Background terminal inventory is not empty")
        if self.inventory and self.inventory["external_operations"]:
            reasons.append("Detached external operations are still running")
        return {"schema_version": 1, "kind": "runtime-quiescence-observation", "engine_version": ENGINE_VERSION,
                "acceptance_scope": self.scope, "production_promotion": False,
                "thread_id": self.thread, "turn_id": self.turn, "workspace": str(self.workspace),
                "connection_id": self.connection_id, "revision": self.revision, "connected": self.connected,
                "interrupt_attempt": self.interrupt_attempt, "terminal_event": source_terminal is not None,
                "source_terminal": source_terminal, "members": copy.deepcopy(self.members),
                "active_operations": active, "unknown_effects": unknown,
                "inventory": copy.deepcopy(self.inventory), "quiescent": not reasons, "reasons": reasons}


def replay_control(recording: dict) -> dict:
    """Read-only bounded replay; receipt times are retained, never refreshed."""
    if not isinstance(recording, dict) or recording.get("schema_version") != 1:
        raise WeatherError("Invalid control recording")
    events = recording.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= 500:
        raise WeatherError("Control recording requires 1–500 events")
    previous_time = 0
    observer = QuiescenceObserver(recording["thread_id"], recording["turn_id"], Path(recording["workspace"]),
                                  recording["engine_version"], "replay", "runtime-control-replay")
    for event in events:
        if not isinstance(event, dict) or event.get("observed_at") is None:
            raise WeatherError("Every control event needs its original receipt time")
        stamp = event["observed_at"]
        if (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0
                or stamp > time.time() + 1 or stamp < previous_time):
            raise WeatherError("Invalid or out-of-order control replay receipt time")
        previous_time = stamp
        observer.consume(event["message"], stamp)
    report = observer.report()
    report["reason"] = "Replay cannot prove current owner, complete inventory or authorize seal/interruption."
    return report
