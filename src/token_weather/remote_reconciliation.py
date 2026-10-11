"""Recover IDs from the original live RPC response, without another remote request."""
from __future__ import annotations

import copy

from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError, identifier


def owner_binding(session) -> dict:
    return {"connection_id": session.connection_id, "thread_id": session.thread,
            "turn_id": session.observer.turn, "workspace": str(session.workspace),
            "engine_version": ENGINE_VERSION}


class RemoteReconciler:
    """No history search, external report import, retry, or lease grant.

    Only evidence still held by the original live transport is accepted. After
    closing that transport, a missing receipt remains unresolved. RPC errors
    and ordinary lifecycle events never authorize a retry.
    """
    def __init__(self, coordinator, session):
        self.coordinator, self.workflow, self.session = coordinator, coordinator.workflow, session
        self.responses = {}

    def reconcile(self, *, apply: bool = False, transaction_id: str | None = None) -> dict:
        if type(apply) is not bool:
            raise WeatherError("Remote reconciliation apply must be an explicit boolean")
        self.responses = {}
        self.session.require_control()
        if (self.session.workspace != self.workflow.workspace or not self.session.owner_matched
                or self.session.owner_engine_version != ENGINE_VERSION):
            raise WeatherError("Remote reconciliation requires the proven original owner")
        # Route late wire responses into the bounded registry. Lifecycle events
        # remain with the existing observers, with all of their fail-closed rules.
        self.session.drain()
        self.session.require_control()
        with self.workflow.locked():
            self.session.drain()
            self.session.require_control()
            tx = self.coordinator.transaction()
            if (tx["source_thread"] != self.session.thread
                    or tx.get("source_turn_id") != self.session.observer.turn
                    or tx.get("continuation_version") != 1
                    or apply and transaction_id != tx["transaction_id"]):
                raise WeatherError("Original request does not bind this source/turn/transaction")
            if tx.get("create_attempt") == "ambiguous" and tx.get("submit_attempt") == "ambiguous":
                raise WeatherError("Multiple ambiguous requests have an inconsistent rotation state")
            resolved, unresolved, outcomes = [], [], {}
            for step in ("create", "submit"):
                if tx.get(f"{step}_attempt") != "ambiguous":
                    continue
                if (tx["phase"] != ("SEALED" if step == "create" else "NEW_THREAD")
                        or tx["lease"]["thread"] is not None):
                    raise WeatherError("Original request has an inconsistent phase/lease")
                pinned = self.coordinator._pinned(tx)
                request = ({"state": pinned["state"]} if step == "create" else
                           {"thread": tx["new_thread"], "prompt": self.coordinator._prompt(tx, pinned)})
                binding = self.coordinator.journal.dispatch(tx, step, request, owner_binding(self.session))
                correlation = getattr(self.session.rpc, "correlation", None)
                response = None
                if binding is not None and correlation is not None:
                    try:
                        response = correlation.response(binding)
                    except WeatherError:
                        # Request IDs can repeat on a different transport, but
                        # its UUID and in-memory reservation cannot match.
                        pass
                outcomes[step] = response["outcome"] if response is not None else "unavailable"
                if response is None or response["outcome"] != "success":
                    unresolved.append(step)
                    continue
                thread = response["thread_id"] if step == "create" else tx["new_thread"]
                # Validate even dry runs; a source ID or enrolled old root is
                # never a legitimate new creation result.
                identifier(thread)
                if thread == tx["source_thread"] or (step == "create" and thread in self.workflow.check_owner()["enrolled_root_threads"]):
                    raise WeatherError("Correlated creation result is not a fresh successor")
                self.responses[step] = copy.deepcopy(response)
                resolved.append(step)
                if apply:
                    self.session.require_control()
                    self.coordinator.journal.record_receipt(tx, step, thread, response["turn_id"])
            if tx.get("archive_attempt") == "ambiguous":
                unresolved.append("archive")
            result = {"dry_run": not apply, "transaction_id": tx["transaction_id"],
                      "status": "blocked" if unresolved else "ready" if resolved else "not-required",
                      "resolved_steps": resolved, "unresolved_steps": unresolved,
                      "response_outcomes": outcomes, "applied": False,
                      "will_create": False, "will_submit": False, "lease_transferred": False,
                      "reason": "Only the exact original live response can restore a missing receipt; no request is repeated."}
        # Receipt publication is deliberately separate from phase publication:
        # a crash between them is recoverable by the existing local reconciler.
        if apply and resolved:
            local = self.coordinator.reconcile(True, tx["transaction_id"], self.session.thread)
            result["applied"] = local["applied"]
        return result
