"""Request-bound, reduced live snapshots; no saved-report import or history search.

The reader is a separately verified owner provider. Public thread/read and an
empty background list cannot implement its completeness contract on their own.
"""
from __future__ import annotations

import copy
import math
import time
import uuid

from .runtime_control import QuiescenceObserver, ITEM_STATUSES, TERMINAL, label
from .runtime_usage import ENGINE_VERSION, UsageObserver, TOOL_ITEMS
from .storage import WeatherError, canonical_json, identifier


def fields(value, expected, description):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise WeatherError(f"Incomplete or unexpected {description} fields")


def stamp(value, earliest, latest):
    if type(value) not in (int, float) or not math.isfinite(value) or not earliest <= value <= latest:
        raise WeatherError("Invalid live baseline timestamp")
    return value


def transport(session):
    return identifier(session.rpc.correlation.transport_id)


def policy(settings, workspace):
    from .continuation import effective_policy, POLICY_FIELDS
    fields(settings, (*POLICY_FIELDS.values(), "cwd"), "baseline policy")
    return effective_policy(settings, workspace)


def items(rows, members):
    if not isinstance(rows, list) or len(rows) > 128:
        raise WeatherError("Baseline requires a bounded complete tool inventory")
    reduced = {}
    for row in rows:
        fields(row, ("thread_id", "turn_id", "item_id", "type", "status", "process_id", "result_failed", "finished"), "baseline tool")
        thread, turn = identifier(row["thread_id"]), identifier(row["turn_id"])
        key = (thread, turn, label(row["item_id"]))
        kind, status = row["type"], row["status"]
        if (thread not in members or turn != members[thread]["turn_id"] or key in reduced
                or not isinstance(kind, str) or kind not in ITEM_STATUSES or status not in ITEM_STATUSES[kind]
                or type(row["finished"]) is not bool or type(row["result_failed"]) is not bool
                or row["finished"] != (status != "inProgress")
                or row["process_id"] is not None and kind != "commandExecution"):
            raise WeatherError("Inconsistent live baseline tool coverage")
        if row["process_id"] is not None:
            label(row["process_id"])
        reduced[key] = {key: copy.deepcopy(row[key]) for key in ("type", "status", "process_id", "result_failed", "finished")}
    return reduced


def source_observer(session, value, observed_at):
    fields(value, ("settings", "members", "items", "background", "external_operations"), "source baseline")
    policy(value["settings"], session.workspace)
    rows = value["members"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 33:
        raise WeatherError("Baseline requires the root and at most 32 descendants")
    observer = QuiescenceObserver(session.thread, session.observer.turn, session.workspace,
                                  ENGINE_VERSION, session.connection_id, session.observer.scope)
    members, parents = {}, {}
    for row in rows:
        fields(row, ("thread_id", "parent_thread_id", "turn_id", "started", "coverage_complete", "terminal"), "baseline member")
        thread, turn = identifier(row["thread_id"]), identifier(row["turn_id"])
        if thread in members or row["started"] is not True or row["coverage_complete"] is not True:
            raise WeatherError("Duplicate or incomplete baseline lifecycle coverage")
        parent = row["parent_thread_id"]
        if parent is not None:
            identifier(parent)
        terminal = row["terminal"]
        if terminal is not None:
            fields(terminal, ("status", "error", "observed_at"), "baseline terminal")
            if terminal["status"] not in TERMINAL or type(terminal["error"]) is not bool:
                raise WeatherError("Invalid baseline terminal evidence")
            stamp(terminal["observed_at"], 0.000001, observed_at)
        members[thread] = {"turn_id": turn, "started": True, "coverage_complete": True, "terminal": copy.deepcopy(terminal)}
        parents[thread] = parent
    if (session.thread not in members or parents[session.thread] is not None
            or members[session.thread]["turn_id"] != session.observer.turn):
        raise WeatherError("Baseline does not identify the exact source root/turn")
    for thread in members:
        seen, current = set(), thread
        while current != session.thread:
            if current in seen or current not in parents or parents[current] is None:
                raise WeatherError("Baseline descendant topology is incomplete or cyclic")
            seen.add(current)
            current = parents[current]
    reduced = items(value["items"], members)
    # A refresh cannot forget already observed descendants, tools or failures.
    for thread, previous in session.observer.members.items():
        current = members.get(thread)
        if (current is None or previous["turn_id"] not in (None, current["turn_id"])
                or previous["terminal"] is not None and (current["terminal"] is None
                    or any(previous["terminal"][key] != current["terminal"][key] for key in ("status", "error")))):
            raise WeatherError("Baseline contradicts or omits observed source activity")
        if previous["terminal"] is not None:
            current["terminal"]["observed_at"] = min(previous["terminal"]["observed_at"], current["terminal"]["observed_at"])
    for key, previous in session.observer.items.items():
        if (key not in reduced or reduced[key]["type"] != previous["type"]
                or previous["process_id"] is not None and reduced[key]["process_id"] != previous["process_id"]
                or previous["finished"] and reduced[key] != previous):
            raise WeatherError("Baseline cannot discard observed tool effects")
    observer.members, observer.items = members, reduced
    observer.revision = session.observer.revision + 1
    observer.received_at = observed_at
    observer.interrupt_attempt = session.observer.interrupt_attempt
    observer.bind_inventory(value["background"], list(set(members) - {session.thread}),
                            revision=observer.revision, connection_id=session.connection_id, complete=True,
                            external_operations=value["external_operations"])
    return observer


def successor_state(session, value, request):
    target = request["successor"]
    if target is None:
        if value is not None:
            raise WeatherError("Baseline cannot discover or guess a successor")
        return None
    fields(value, ("thread_id", "parent_thread_id", "forked_from_id", "settings", "turn_ids",
                   "turn_id", "started", "status", "error", "items", "background", "external_operations"), "successor baseline")
    if (value["thread_id"] != target["thread_id"] or value["thread_id"] == session.thread
            or value["parent_thread_id"] is not None or value["forked_from_id"] is not None
            or value["turn_id"] != target["turn_id"] or value["error"] is not False
            or value["background"] != [] or value["external_operations"] != []):
        raise WeatherError("Baseline successor identity or detached activity mismatch")
    resolved = policy(value["settings"], session.workspace)
    turn = target["turn_id"]
    if turn is None:
        if value["turn_ids"] != [] or value["started"] is not False or value["status"] != "idle" or value["items"] != []:
            raise WeatherError("Unsubmitted successor has prior execution")
    else:
        if value["turn_ids"] != [turn] or value["started"] is not True or value["status"] not in ("inProgress", "completed"):
            raise WeatherError("Baseline lacks the exact sole submitted native turn")
        rows = items(value["items"], {target["thread_id"]: {"turn_id": turn}})
        if any(row["type"] != "commandExecution" or row["result_failed"] or row["status"] in ("failed", "declined") for row in rows.values()):
            raise WeatherError("Successor has uncontrolled or failed tool effects")
    return {"thread_id": target["thread_id"], "settings": resolved, "turn_id": turn,
            "started": value["started"], "status": value["status"]}


class LiveBaseline:
    """Memory-only evidence bound to one request and uninterrupted connection."""
    def __init__(self, request, successor, expires, deadline):
        self._request, self._successor = copy.deepcopy(request), copy.deepcopy(successor)
        self._expires, self._deadline = expires, deadline

    def current(self, session, context, successor, turn):
        session.require_control()
        request = self._request
        if (not session.baseline_authorized or request["connection_id"] != session.connection_id or request["transport_id"] != transport(session)
                or request["revision"] != session.event_revision or request["context"] != context
                or request["successor"] != (None if successor is None else {"thread_id": successor, "turn_id": turn})
                or not request["issued_at"] <= time.time() < self._expires or time.monotonic() >= self._deadline):
            raise WeatherError("Live baseline is stale or belongs to another connection/transaction")
        return copy.deepcopy(self._successor)


def restore_live_baseline(session, reader, *, expected_policy, context, successor=None, successor_turn=None, timeout=5, validate=None):
    if not isinstance(expected_policy, dict) or not expected_policy:
        raise WeatherError("Restoration requires the existing immutable policy")
    return _acquire_baseline(session, reader, expected_policy=expected_policy, context=context,
                            successor=successor, successor_turn=successor_turn, timeout=timeout, validate=validate)


def capture_initial_policy(session, reader, *, context, timeout=5, validate=None):
    """Capture a first policy for a distinct workflow, with all existing live gates.

    This cannot restore a successor or overwrite a published policy. The caller
    must validate its source lease/generation again at the publication boundary.
    """
    return _acquire_baseline(session, reader, expected_policy=None, context=context,
                            successor=None, successor_turn=None, timeout=timeout, validate=validate)


def _acquire_baseline(session, reader, *, expected_policy, context, successor, successor_turn, timeout, validate):
    """Validate the whole candidate before replacing any session evidence."""
    if not callable(reader) or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
        raise WeatherError("Live baseline requires a callable provider and a bounded 0–5 second timeout")
    session.require_control()
    if not session.baseline_authorized or session.observer.scope != "desktop-control-discovery":
        raise WeatherError("Complete live baseline provider has not passed this owner's live release gate")
    fields(context, ("workflow_id", "transaction_id", "generation", "manifest_sha256"), "baseline transaction")
    identifier(context["workflow_id"]); identifier(context["transaction_id"])
    if (type(context["generation"]) is not int or context["generation"] < 1
            or not isinstance(context["manifest_sha256"], str) or len(context["manifest_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in context["manifest_sha256"])):
        raise WeatherError("Invalid live baseline transaction generation/digest")
    if successor is not None:
        identifier(successor)
        if successor == session.thread:
            raise WeatherError("Baseline successor cannot be the source root")
    if successor_turn is not None:
        identifier(successor_turn)
        if successor is None:
            raise WeatherError("Baseline turn requires a receipted successor")
    session.drain()
    session.require_control()
    issued, started = time.time(), time.monotonic()
    request = {"schema_version": 1, "request_id": str(uuid.uuid4()), "connection_id": session.connection_id,
               "transport_id": transport(session), "engine_version": ENGINE_VERSION,
               "workspace": str(session.workspace), "revision": session.event_revision,
               "source": {"thread_id": session.thread, "turn_id": session.observer.turn},
               "successor": None if successor is None else {"thread_id": successor, "turn_id": successor_turn},
               "context": copy.deepcopy(context), "issued_at": issued, "expires_at": issued + timeout}
    # No global provider registration, no disk import and no fallback RPC scan.
    value = reader(session, copy.deepcopy(request), timeout)
    session.drain()
    session.require_control()
    now = time.time()
    if (session.event_revision != request["revision"] or transport(session) != request["transport_id"]
            or not issued <= now < request["expires_at"] or time.monotonic() >= started + timeout):
        raise WeatherError("Owner activity or connection changed during baseline acquisition")
    fields(value, ("schema_version", "acceptance_scope", "binding", "observed_at", "complete", "source", "successor"), "live baseline")
    if len(canonical_json(value)) > 262_144:
        raise WeatherError("Live baseline exceeds the bounded metadata budget")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["acceptance_scope"] != "desktop-live-baseline"
            or canonical_json(value["binding"]) != canonical_json(request)):
        raise WeatherError("Baseline does not bind this exact live snapshot request")
    fields(value["complete"], ("descendants", "tools", "external_operations", "settings", "turns"), "baseline completeness")
    if any(flag is not True for flag in value["complete"].values()):
        raise WeatherError("Complete live baseline coverage is required")
    observed = stamp(value["observed_at"], issued, now)
    observer = source_observer(session, value["source"], observed)
    successor_value = successor_state(session, value["successor"], request)
    source_policy = policy(value["source"]["settings"], session.workspace)
    if expected_policy is None and session.source_settings is not None:
        if policy(session.source_settings, session.workspace) != source_policy:
            raise WeatherError("Initial policy capture contradicts observed effective settings")
    if expected_policy is not None and (source_policy != expected_policy
            or successor_value is not None and successor_value["settings"] != expected_policy):
        raise WeatherError("Baseline cannot change immutable source/successor policy")
    # Reconnection never revives cached occupancy, numeric limits or growth.
    usage = UsageObserver(session.thread, session.workspace, ENGINE_VERSION, session.usage.scope)
    usage.observe_settings(value["source"]["settings"], "complete-live-baseline")
    usage.epoch, usage.revision = session.usage.epoch + 1, session.usage.revision + 1
    usage.turn = observer.turn
    usage.seen_turns.append(observer.turn)
    usage.turn_active = observer.members[session.thread]["terminal"] is None
    usage.pending_tools = {key[2]: key[1] for key, row in observer.items.items()
                           if key[0] == session.thread and row["type"] in TOOL_ITEMS and not row["finished"]}
    usage.appended_unknown = bool(usage.pending_tools)
    if validate is not None:
        validate()
    session.require_control()
    if (session.event_revision != request["revision"] or transport(session) != request["transport_id"]
            or not issued <= time.time() < request["expires_at"] or time.monotonic() >= started + timeout):
        raise WeatherError("Live baseline expired or changed before publication")
    baseline = LiveBaseline(request, successor_value, request["expires_at"], started + timeout)
    session.observer, session.usage = observer, usage
    session.source_settings = copy.deepcopy(value["source"]["settings"])
    session.live_baseline = baseline
    return baseline
