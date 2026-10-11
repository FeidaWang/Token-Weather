"""Bounded read-only owner state from the installed, supported 0.160.0 RPCs.

Stored ancestry and an exhausted loaded list do not prove a complete live tree.
This provider deliberately cannot authorize quiescence or a complete baseline.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import subprocess
import time

from .continuation import POLICY_FIELDS, effective_policy
from .models import utc_now
from .owner_session import background_inventory, open_owner_session
from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError, canonical_json, identifier

SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
READ_METHODS = ("thread/loaded/list", "thread/read", "thread/list", "thread/backgroundTerminals/list")
LIMITATIONS = [
    "Stored ancestry excludes ephemeral, deleted and unindexed descendants; live tree completeness is unknown.",
    "Read-only metadata calls do not subscribe to or backfill exact turn/item lifecycle events.",
    "No supported RPC enumerates all detached external operations or proves their absence.",
    "Desktop attachment and scheduling guards require independent live verification.",
]


def bounded_timeout(timeout: float) -> float:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
        raise WeatherError("Owner state requires a finite timeout greater than zero and at most 5 seconds")
    return timeout


def runtime_status(value) -> dict:
    if not isinstance(value, dict) or value.get("type") not in ("idle", "active", "notLoaded", "systemError"):
        raise WeatherError("Unknown owner runtime status")
    result = {"type": value["type"]}
    if value["type"] == "active":
        flags = value.get("activeFlags")
        if (not isinstance(flags, list) or len(flags) > 2
                or any(not isinstance(flag, str) or flag not in ("waitingOnApproval", "waitingOnUserInput") for flag in flags)
                or len(set(flags)) != len(flags)):
            raise WeatherError("Unknown owner activity flag")
        result["active_flags"] = list(flags)
    return result


class SupportedOwnerStateProvider:
    """Discovery on one OwnerSession; no saved evidence or control promotion.

    snapshot() returns a fresh independent reduction. descendant_reader() is
    compatible with the existing controller but always incomplete. baseline_reader()
    rejects rather than filling missing coverage with stored history or defaults.
    """
    def __init__(self, session):
        self.session = session

    def _binding(self) -> dict:
        session = self.session
        process = getattr(session.rpc, "process", None)
        if (not session.owner_matched or session.owner_engine_version != ENGINE_VERSION
                or not session.observer.connected or getattr(session.rpc, "closed", False)
                or process is not None and process.poll() is not None):
            raise WeatherError("A connected, version-matched exact owner is required for state discovery")
        transport = getattr(getattr(session.rpc, "correlation", None), "transport_id", None)
        identifier(transport)
        return {"connection_id": session.connection_id, "transport_id": transport,
                "revision": session.event_revision, "observer_revision": session.observer.revision,
                "thread_id": session.thread, "turn_id": session.observer.turn,
                "workspace": str(session.workspace), "engine_version": ENGINE_VERSION}

    def _request(self, method, params, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WeatherError("Owner state snapshot deadline expired")
        response = self.session.rpc.request(method, params, timeout=remaining)
        if not isinstance(response, dict):
            raise WeatherError("Malformed owner state RPC response")
        return response

    def _pages(self, method, params, deadline, maximum):
        cursor, cursors, rows = None, set(), []
        for _ in range(10):
            page_params = {**params, "limit": 100}
            if cursor is not None:
                page_params["cursor"] = cursor
            page = self._request(method, page_params, deadline)
            data = page.get("data")
            if not isinstance(data, list) or len(data) > 100 or len(rows) + len(data) > maximum:
                raise WeatherError("Malformed or oversized owner state inventory")
            rows.extend(data)
            # Omission is not a proof that pagination has completed.
            if "nextCursor" not in page:
                raise WeatherError("Owner state inventory lacks an explicit pagination boundary")
            cursor = page["nextCursor"]
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
                raise WeatherError("Invalid or repeated owner state cursor")
            cursors.add(cursor)
        raise WeatherError("Owner state inventory exceeded the bounded page limit")

    def _loaded(self, deadline):
        rows = self._pages("thread/loaded/list", {}, deadline, 1000)
        for thread in rows:
            identifier(thread)
        if len(set(rows)) != len(rows):
            raise WeatherError("Duplicate loaded owner thread")
        if self.session.thread not in rows:
            raise WeatherError("Exact source root is no longer loaded on this owner")
        return set(rows)

    def _stored(self, deadline):
        result = {}
        for archived in (False, True):
            # All source kinds are explicit: the default excludes subagents.
            # The DB-only flag prevents the server's scan-and-repair fallback.
            rows = self._pages("thread/list", {"ancestorThreadId": self.session.thread,
                "sourceKinds": SOURCE_KINDS[:], "archived": archived, "useStateDbOnly": True}, deadline, 32)
            for row in rows:
                if not isinstance(row, dict) or "parentThreadId" not in row:
                    raise WeatherError("Stored descendant lacks exact parent identity")
                thread, parent = identifier(row.get("id")), identifier(row["parentThreadId"])
                if thread == self.session.thread or thread in result or len(result) >= 32:
                    raise WeatherError("Duplicate, source or oversized stored descendant result")
                result[thread] = {"parent_thread_id": parent, "archived": archived}
        for thread in result:
            seen, current = set(), thread
            while current != self.session.thread:
                if current in seen or current not in result:
                    raise WeatherError("Stored ancestry filter returned a cyclic or disconnected tree")
                seen.add(current)
                current = result[current]["parent_thread_id"]
        return result

    def _metadata(self, thread, deadline):
        response = self._request("thread/read", {"threadId": thread, "includeTurns": False}, deadline)
        row = response.get("thread")
        if not isinstance(row, dict) or row.get("id") != thread or "parentThreadId" not in row:
            raise WeatherError("Live metadata does not identify the exact requested thread/parent")
        parent = row["parentThreadId"]
        if parent is not None:
            identifier(parent)
        status = runtime_status(row.get("status"))
        if status["type"] == "notLoaded":
            raise WeatherError("Loaded owner member became unloaded during inspection")
        if thread == self.session.thread:
            cwd = row.get("cwd")
            if (parent is not None or not isinstance(cwd, str) or not Path(cwd).is_absolute()
                    or Path(cwd).resolve() != self.session.workspace):
                raise WeatherError("Live source must be an exact root in the bound workspace")
        # Drop preview/name/path, instructions and any unexpectedly included turns.
        return {"parent_thread_id": parent, "status": status}

    def _background(self, thread, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WeatherError("Owner state snapshot deadline expired")
        return background_inventory(self.session.rpc, thread, remaining)

    def snapshot(self, timeout=5) -> dict:
        timeout = bounded_timeout(timeout)
        session = self.session
        session.drain()
        binding, issued, started = self._binding(), time.time(), time.monotonic()
        deadline = started + timeout
        loaded = self._loaded(deadline)
        root = self._metadata(session.thread, deadline)
        stored, stored_query_exhausted = {}, True
        try:
            stored = self._stored(deadline)
        except WeatherError as exc:
            # Only an explicit unsupported-method error permits partial discovery.
            # Invalid filters, permissions, disconnects and malformed pages fail.
            if str(exc) != "RPC thread/list rejected: -32601":
                raise
            stored_query_exhausted = False
        observed_members = copy.deepcopy(session.observer.members)
        descendants = sorted((set(observed_members) | set(stored)) - {session.thread})
        if len(descendants) > 32:
            raise WeatherError("Combined owner descendant evidence exceeds its bounded limit")
        members = {session.thread: {**root, "parent_known": True, "loaded": True, "stored": False, "observed": True}}
        backgrounds = {session.thread: self._background(session.thread, deadline)}
        for thread in descendants:
            entry = {"parent_thread_id": stored.get(thread, {}).get("parent_thread_id"),
                     "parent_known": thread in stored, "status": None, "loaded": thread in loaded, "stored": thread in stored,
                     "observed": thread in observed_members}
            if thread in stored:
                entry["archived"] = stored[thread]["archived"]
            if thread in loaded:
                metadata = self._metadata(thread, deadline)
                if thread in stored and metadata["parent_thread_id"] != entry["parent_thread_id"]:
                    raise WeatherError("Stored and live descendant parent identities disagree")
                if metadata["parent_thread_id"] not in {session.thread, *descendants}:
                    raise WeatherError("Loaded descendant has an unrelated or unknown parent")
                entry.update(metadata)
                entry["parent_known"] = True
                backgrounds[thread] = self._background(thread, deadline)
            else:
                backgrounds[thread] = None  # Unloaded is not a zero-process proof.
            members[thread] = entry
        for thread in descendants:
            seen, current = set(), thread
            while current != session.thread and current is not None:
                if current in seen or current not in members:
                    raise WeatherError("Combined live descendant topology is cyclic or disconnected")
                seen.add(current)
                current = members[current]["parent_thread_id"]
        if self._loaded(deadline) != loaded:
            raise WeatherError("Loaded membership changed during owner state inspection")
        settings = None
        if session.source_settings is not None:
            effective_policy(session.source_settings, session.workspace)
            settings = {key: copy.deepcopy(session.source_settings[key]) for key in (*POLICY_FIELDS.values(), "cwd")}
        items = [{"thread_id": thread, "turn_id": turn, "item_id": item, **copy.deepcopy(row)}
                 for (thread, turn, item), row in session.observer.items.items()]
        session.drain()
        if self._binding() != binding or time.monotonic() >= deadline or not issued <= time.time() < issued + timeout:
            raise WeatherError("Owner state changed, disconnected or expired during acquisition")
        result = {"schema_version": 1, "kind": "supported-owner-state", "acceptance_scope": "owner-state-discovery",
                  "binding": binding, "observed_at": time.time(), "expires_at": issued + timeout, "atomic": False,
                  "owner_scope": session.observer.scope, "production_promotion": False,
                  "complete": {"descendants": False, "tools": False,
                      "external_operations": False, "settings": False, "turns": False},
                  "coverage": {"loaded_query_exhausted": True, "stored_query_exhausted": stored_query_exhausted,
                      "known_member_backgrounds": all(value is not None for value in backgrounds.values()),
                      "observed_source_settings": settings is not None,
                      "observed_lifecycles": all(value["started"] and value["coverage_complete"]
                          for value in observed_members.values()) and set(observed_members) == set(members)},
                  "members": members, "observed_members": observed_members, "observed_items": items,
                  "source_settings": settings, "background": backgrounds, "external_operations": None,
                  "limitations": LIMITATIONS[:]}
        if len(canonical_json(result)) > 262_144:
            raise WeatherError("Owner state exceeds the bounded metadata budget")
        return result

    def descendant_reader(self, session, timeout):
        if session is not self.session:
            raise WeatherError("Owner state provider belongs to another session")
        snapshot = self.snapshot(timeout)
        descendants = sorted(set(snapshot["members"]) - {session.thread})
        if set(descendants) != set(session.observer.members) - {session.thread}:
            raise WeatherError("Additional stored descendants require complete live baseline coverage")
        return {"connection_id": session.connection_id, "revision": session.observer.revision,
                "descendants": descendants, "external_operations": None, "complete": False}

    def baseline_reader(self, session, request, timeout):
        if session is not self.session:
            raise WeatherError("Owner state provider belongs to another session")
        bounded_timeout(timeout)
        raise WeatherError("Supported owner RPCs cannot prove complete live descendants, external operations or lifecycle baseline")


def probe_owner_state(engine: Path, thread: str, turn: str, workspace: Path,
                      seconds=0, socket_path: Path | None = None, *, transport: str = "proxy") -> dict:
    identifier(thread); identifier(turn)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 30:
        raise WeatherError("Owner state observation must be finite and between zero and 30 seconds")
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "owner-state-discovery",
              "status": "NOT TESTED", "target_thread": thread, "target_turn": turn,
              "workspace": str(workspace.resolve(strict=True)), "engine_path": str(engine.resolve(strict=True)),
              "transport": transport, "snapshot": None, "complete_live_state": False,
              "production_promotion": False, "model_requests": 0, "started_server": False,
              "submitted_turn": False, "interrupted_turn": False, "trust_modified": False}
    try:
        with open_owner_session(engine, thread, turn, workspace, socket_path, transport=transport) as session:
            deadline = time.monotonic() + seconds
            for _ in range(500):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    message = session.rpc.next_notification(remaining)
                except WeatherError as exc:
                    if "timed out" not in str(exc):
                        raise
                    break
                session.consume(message)
            else:
                raise WeatherError("Owner state observation exceeded the event limit")
            report["snapshot"] = SupportedOwnerStateProvider(session).snapshot()
            report["status"] = "VERIFIED"
            report["reason"] = "Read-only partial state acquisition succeeded; complete live state remains unsupported."
    except (WeatherError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Do not publish RPC bodies, stderr or private socket details in reports.
        report["reason"] = "Exact owner state acquisition failed; complete live state remains unknown."
        report["reason_code"] = ("owner-unavailable" if "disconnected" in str(exc)
                                 else "runtime-mismatch" if "protocol" in str(exc)
                                 else "owner-state-unavailable")
    return report
