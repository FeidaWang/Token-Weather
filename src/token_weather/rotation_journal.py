"""Local RPC intents and receipts; absence never proves a remote request failed."""
from __future__ import annotations

from .storage import WeatherError, atomic_json, canonical_json, digest, identifier, read_json


class RotationJournal:
    def __init__(self, workflow):
        self.workflow = workflow

    def path(self, tx: dict, step: str, kind: str):
        if step not in ("create", "submit") or kind not in ("intent", "receipt", "dispatch"):
            raise WeatherError("Unknown rotation journal record")
        return self.workflow.path("attempts", identifier(tx["transaction_id"]), f"{step}.{kind}.json")

    def intent(self, tx: dict, step: str, request: dict) -> dict:
        return {"schema_version": 1, "workflow_id": self.workflow.id,
                "workspace": str(self.workflow.workspace), "transaction_id": tx["transaction_id"],
                "source_thread": tx["source_thread"], "generation": tx["generation"],
                "manifest_sha256": tx["manifest_sha256"], "step": step,
                "request_sha256": digest(canonical_json(request))}

    def record_intent(self, tx: dict, step: str, request: dict) -> str:
        intent = self.intent(tx, step, request)
        path = self.path(tx, step, "intent")
        existing = read_json(path)
        if existing is not None and existing != intent:
            raise WeatherError("Rotation intent changed; reconcile before continuing")
        atomic_json(path, intent)
        return digest(canonical_json(intent))

    def validate_intent(self, tx: dict, step: str, request: dict) -> str:
        expected = self.intent(tx, step, request)
        intent_hash = digest(canonical_json(expected))
        if (read_json(self.path(tx, step, "intent")) != expected
                or tx.get(f"{step}_intent_sha256") != intent_hash):
            raise WeatherError("Rotation request does not match its sealed intent")
        return intent_hash

    def record_dispatch(self, tx: dict, step: str, owner: dict, binding: dict) -> str:
        """Caller holds the rotation lock and persists the returned hash before sending."""
        from .rpc_correlation import METHODS
        if (tx.get(f"{step}_attempt") != "ambiguous" or binding.get("method") != METHODS[step]
                or type(binding.get("request_id")) is not int or binding["request_id"] < 1
                or set(binding) != {"schema_version", "transport_id", "request_id", "method", "params_sha256"}
                or type(binding["schema_version"]) is not int or binding["schema_version"] != 1):
            raise WeatherError("Invalid original request dispatch binding")
        record = {"schema_version": 1, "intent_sha256": tx[f"{step}_intent_sha256"],
                  "owner": owner, "request": binding}
        path = self.path(tx, step, "dispatch")
        if read_json(path) is not None or tx.get(f"{step}_dispatch_sha256") is not None:
            raise WeatherError("A request binding already exists; never redispatch")
        atomic_json(path, record)
        return digest(canonical_json(record))

    def dispatch(self, tx: dict, step: str, request: dict, owner: dict) -> dict | None:
        intent_hash = self.validate_intent(tx, step, request)
        record = read_json(self.path(tx, step, "dispatch"))
        if record is None:
            return None
        if (not isinstance(record, dict) or set(record) != {"schema_version", "intent_sha256", "owner", "request"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["intent_sha256"] != intent_hash
                or digest(canonical_json(record)) != tx.get(f"{step}_dispatch_sha256")):
            raise WeatherError("Original request dispatch does not match the sealed transaction")
        if record["owner"] != owner:
            return None  # A new connection cannot import the old connection's evidence.
        return record["request"]

    def record_receipt(self, tx: dict, step: str, thread: str, turn: str | None = None) -> None:
        thread = identifier(thread)
        if thread == tx["source_thread"]:
            raise WeatherError("Successor must be a fresh root")
        if step == "create" and thread in self.workflow.check_owner().get("enrolled_root_threads", []):
            raise WeatherError("Creation receipt cannot reuse a previously enrolled root")
        if step == "submit":
            identifier(turn)
            if thread != tx["new_thread"]:
                raise WeatherError("Turn receipt belongs to another successor")
        receipt = {"schema_version": 1, "intent_sha256": tx[f"{step}_intent_sha256"],
                   "thread_id": thread, "turn_id": turn}
        path = self.path(tx, step, "receipt")
        existing = read_json(path)
        if existing is not None and existing != receipt:
            raise WeatherError("Conflicting rotation receipt; reconcile before continuing")
        atomic_json(path, receipt)

    def receipt(self, tx: dict, step: str, request: dict) -> dict | None:
        """Validate both records against the sealed request, never candidate history."""
        receipt = read_json(self.path(tx, step, "receipt"))
        if receipt is None:
            return None
        intent_hash = self.validate_intent(tx, step, request)
        if (not isinstance(receipt, dict)
                or set(receipt) != {"schema_version", "intent_sha256", "thread_id", "turn_id"}
                or receipt["schema_version"] != 1 or receipt["intent_sha256"] != intent_hash):
            raise WeatherError("Rotation receipt does not match the sealed intent")
        thread = identifier(receipt["thread_id"])
        if thread == tx["source_thread"]:
            raise WeatherError("Receipt names the source root as successor")
        if step == "submit":
            identifier(receipt["turn_id"])
            if thread != tx["new_thread"]:
                raise WeatherError("Turn receipt belongs to another successor")
        elif receipt["turn_id"] is not None:
            raise WeatherError("Creation receipt cannot contain a turn")
        return receipt
