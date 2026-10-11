from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
import copy

from .storage import WeatherError, identifier, canonical_json, MAX_JSON


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class State:
    workflow_id: str
    workspace: str
    source_thread: str
    objective: str
    policies: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    ledger_high_water: int = 0
    requirements: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    milestones: list[dict[str, Any]] = field(default_factory=list)
    next_action: str = "Record the next concrete action."
    followups: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    pending_inputs: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> State:
        if value.get("schema_version") != 1:
            raise WeatherError("Unsupported state schema")
        return cls(**value)


UPDATE_FIELDS = {"requirements", "constraints", "decisions", "milestone", "next_action", "followups", "blockers", "pending_inputs", "operations", "evidence", "usage", "handled_inputs", "resolved_blockers"}


def text_value(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 16_384:
        raise WeatherError(f"{name} must be a nonempty string under 16 KiB")
    return value


def validate_update(update: Any) -> dict[str, Any]:
    if not isinstance(update, dict) or set(update) - UPDATE_FIELDS:
        raise WeatherError("Unknown checkpoint fields")
    if len(canonical_json(update)) > 32_768:
        raise WeatherError("Checkpoint delta exceeds 32 KiB")
    for name in ("constraints", "decisions", "followups", "blockers", "handled_inputs", "resolved_blockers"):
        if name in update:
            if not isinstance(update[name], list):
                raise WeatherError(f"{name} must be an array")
            for value in update[name]:
                text_value(value, name)
    if len(update.get("followups", [])) > 2:
        raise WeatherError("At most two nearest follow-ups")
    if "next_action" in update:
        text_value(update["next_action"], "next_action")
    record_fields = {
        "requirements": ({"id", "text", "hard"}, {"id", "text"}),
        "pending_inputs": ({"id", "text"}, {"id", "text"}),
        "operations": ({"id", "summary", "status", "non_idempotent", "evidence"}, {"id", "summary", "status"}),
        "evidence": ({"id", "summary", "path", "sha256", "verified_at", "kind", "cache_id"}, {"id", "summary"}),
    }
    for name, (allowed, required) in record_fields.items():
        if name not in update:
            continue
        if not isinstance(update[name], list):
            raise WeatherError(f"{name} must be an array")
        for row in update[name]:
            if not isinstance(row, dict) or set(row) - allowed or not required <= set(row):
                raise WeatherError(f"Invalid {name} record")
            identifier(row["id"])
            for k in required - {"id"}:
                text_value(row[k], k)
            if "hard" in row and type(row["hard"]) is not bool:
                raise WeatherError("hard must be a boolean")
            if name == "operations":
                if row["status"] not in ("running", "unknown", "completed", "failed"):
                    raise WeatherError("Invalid operation status")
                if "non_idempotent" in row and type(row["non_idempotent"]) is not bool:
                    raise WeatherError("non_idempotent must be a boolean")
    if "milestone" in update:
        row = update["milestone"]
        if not isinstance(row, dict) or set(row) - {"id", "summary", "status", "result", "evidence"} or not {"id", "summary", "status"} <= set(row):
            raise WeatherError("Invalid milestone")
        identifier(row["id"])
        text_value(row["summary"], "milestone summary")
        if row["status"] not in ("completed", "incomplete", "failed"):
            raise WeatherError("Invalid milestone status")
        if "result" in row:
            text_value(row["result"], "milestone result")
        if "evidence" in row and (not isinstance(row["evidence"], list) or any(not isinstance(x, str) for x in row["evidence"])):
            raise WeatherError("Milestone evidence must be an array of IDs")
    if "usage" in update:
        usage = update["usage"]
        if not isinstance(usage, dict) or set(usage) - {"limits", "occupancy", "predicted_burst", "fingerprint"}:
            raise WeatherError("usage accepts limits, occupancy, predicted_burst and fingerprint; lifetime counters are excluded")
        from .accounting import Inputs, Occupancy
        try:
            Inputs(**usage.get("limits", {}))
            Occupancy(**usage.get("occupancy", {})).estimate()
        except (TypeError, ValueError) as exc:
            raise WeatherError("Invalid typed usage observation") from exc
        burst = usage.get("predicted_burst", 0)
        if type(burst) is not int or burst < 0:
            raise WeatherError("predicted_burst must be a nonnegative integer")
    return copy.deepcopy(update)


def merge_records(existing: list[dict], incoming: list[dict], immutable: bool = False) -> list[dict]:
    result = copy.deepcopy(existing)
    by_id = {r["id"]: i for i, r in enumerate(result)}
    for row in incoming:
        if row["id"] in by_id:
            i = by_id[row["id"]]
            if immutable and any(result[i].get(key) != value for key, value in row.items()):
                raise WeatherError("Requirement IDs are immutable; add an explicit replacement decision and a new ID")
            result[i].update(copy.deepcopy(row))
        else:
            by_id[row["id"]] = len(result)
            result.append(copy.deepcopy(row))
    return result


def apply_update(state: State, update: dict[str, Any], sequence: int, timestamp: str) -> State:
    update = validate_update(update)
    result = copy.deepcopy(state)
    for name in ("constraints", "decisions", "blockers"):
        values = getattr(result, name)
        for value in update.get(name, []):
            if value not in values:
                values.append(value)
    for name in ("requirements", "pending_inputs", "operations", "evidence"):
        setattr(result, name, merge_records(getattr(result, name), update.get(name, []), immutable=name in ("requirements", "pending_inputs")))
    if "milestone" in update:
        result.milestones = merge_records(result.milestones, [update["milestone"]])
    for name in ("next_action", "followups", "usage"):
        if name in update:
            setattr(result, name, update[name])
    handled = set(update.get("handled_inputs", []))
    known_inputs = {x["id"] for x in result.pending_inputs}
    if handled - known_inputs:
        raise WeatherError("Cannot handle an input that was not recorded")
    for row in result.pending_inputs:
        if row["id"] in handled:
            row["handled"] = True
    result.blockers = [x for x in result.blockers if x not in update.get("resolved_blockers", [])]
    result.ledger_high_water = sequence
    result.updated_at = timestamp
    if len(canonical_json(result.to_dict())) > MAX_JSON:
        raise WeatherError("Canonical state exceeds 256 KiB; create indexed requirement/evidence records")
    return result
