from __future__ import annotations

from pathlib import Path
import copy

from .capabilities import mode
from .checkpoints import Workflow
from .desktop_adapter import prepared_link
from .models import utc_now, text_value
from .rotation_journal import RotationJournal
from .storage import WeatherError, atomic_json, read_json, identifier, digest, canonical_json

PHASES = ("OBSERVING", "PREPARING", "READY", "QUIESCING", "SEALED", "NEW_THREAD", "ACKNOWLEDGED", "RETIRED")


class Coordinator:
    def __init__(self, workflow: Workflow, report: dict | None = None):
        self.workflow = workflow
        self.report = report or {}
        self.journal = RotationJournal(workflow)

    def transaction(self) -> dict:
        self.workflow.check_owner()
        return read_json(self.workflow.path("rotation.json"))

    def dry_run(self) -> dict:
        tx = self.transaction()
        bootstrap = self.workflow.bootstrap()
        return {"dry_run": True, "mode": mode(self.report), "phase": tx["phase"], "generation": bootstrap["generation"],
                "workspace": str(self.workflow.workspace), "source_thread": tx["source_thread"],
                "next_step": "Prepare, verify quiescence, then open a fresh composer" if mode(self.report) != "automatic" else "Continue the durable rotation transaction",
                "prepared_link": prepared_link(self.workflow.workspace, self.workflow.id, bootstrap["generation"], self.workflow.launcher),
                "automatic_enabled": tx["automatic_rotation"], "will_submit": False,
                "policies": bootstrap["state"]["policies"], "unknown_effects": tx["unknown_effects"]}

    def request_rotation(self, event_id: str) -> dict:
        with self.workflow.locked(timeout=0.1):
            tx = self.transaction()
            if tx["phase"] == "QUIESCING" and tx.get("event_id") == event_id:
                return tx
            if tx["phase"] != "READY" or tx["generation"] != self.workflow.current()["generation"]:
                raise WeatherError("No current ready generation")
            tx.update(phase="QUIESCING", event_id=event_id, requested_at=utc_now())
            atomic_json(self.workflow.path("rotation.json"), tx)
            return tx

    def quiesce(self, session, seconds: float = 5, *, interrupt: bool = False, descendant_reader=None) -> dict:
        """Pin one owner/turn; persist uncertainty before requesting interruption."""
        if type(seconds) not in (int, float) or not 0 < seconds <= 30:
            raise WeatherError("Source terminal wait must be greater than zero and at most 30 seconds")
        session.require_control()
        if session.workspace != self.workflow.workspace:
            raise WeatherError("Owner session workspace differs from the rotation")
        with self.workflow.locked():
            tx = self.transaction()
            self._pinned(tx)
            if tx["phase"] != "QUIESCING" or tx["source_thread"] != session.thread:
                raise WeatherError("Source owner must be quiescing the exact current transaction")
            if tx.get("source_turn_id") not in (None, session.observer.turn):
                raise WeatherError("Quiescence cannot switch the source turn")
            if tx.get("event_id") != session.observer.turn:
                raise WeatherError("Rotation trigger does not match the observed source turn")
            tx["source_turn_id"] = session.observer.turn
            attempt = tx.get("stop_attempt", "not-started")
            if attempt != "not-started":
                session.observer.interrupt_attempt = attempt
            if interrupt and attempt == "not-started":
                tx["stop_attempt"] = "ambiguous"
                atomic_json(self.workflow.path("rotation.json"), tx)
                # A failed reply or failed following local write leaves a
                # durable attempted stop. A restarted controller only waits.
                reply = session.interrupt()
                tx["stop_attempt"] = "acknowledged" if reply["requested"] else "terminal-observed"
            atomic_json(self.workflow.path("rotation.json"), tx)
        observation = session.wait(seconds, descendant_reader)
        return {"transaction_id": tx["transaction_id"], "phase": "QUIESCING", "stop_attempt": tx.get("stop_attempt", "not-started"),
                "observation": observation, "sealed": False, "lease_transferred": False}

    def seal_quiescent(self, session, unknown_effects: list[str], final_update: dict | None = None) -> dict:
        """Use current in-memory owner evidence; never import a saved report."""
        session.require_control()
        session.drain()
        observed = session.observer.report()
        if not observed["quiescent"] or set(observed["unknown_effects"]) - set(unknown_effects):
            raise WeatherError("Live source quiescence or explicit recovery effects are incomplete")
        if session.workspace != self.workflow.workspace:
            raise WeatherError("Owner session workspace differs from the rotation")
        if observed["unknown_effects"]:
            # Keep runtime uncertainty in the next generation's ACTIVE state,
            # not only the controller's transaction file.
            final_update = copy.deepcopy(final_update or {})
            operations = {row["id"]: row for row in final_update.get("operations", [])}
            for operation in observed["unknown_effects"]:
                operations[operation] = {"id": operation, "status": "unknown", "non_idempotent": True,
                    "summary": "Runtime tool failed or was interrupted; inspect its effects before any retry."}
            final_update["operations"] = list(operations.values())
        # The evidence is checked while holding the same lock as seal, so a
        # changed transaction/generation cannot consume an old stop snapshot.
        with self.workflow.locked():
            session.require_control()
            session.drain()
            current_observation = session.observer.report()
            if not current_observation["quiescent"] or current_observation["revision"] != observed["revision"]:
                raise WeatherError("Source evidence changed or expired while acquiring the seal lock")
            tx = self.transaction()
            self._pinned(tx)
            if (tx["source_thread"] != session.thread or tx["phase"] not in ("QUIESCING", "SEALED")
                    or tx.get("source_turn_id") != session.observer.turn):
                raise WeatherError("Quiescence does not match this rotation's source turn")
            evidence = "owner-terminal:" + digest(canonical_json(observed))
            # seal takes the workflow lock itself; use its locked body below.
            return self._seal_locked(session.thread, evidence, [], unknown_effects, final_update,
                                     quiescence=observed)

    def seal(self, thread: str, terminal_evidence: str, active_operations: list[str], unknown_effects: list[str], final_update: dict | None = None) -> dict:
        if not terminal_evidence or active_operations:
            raise WeatherError("A terminal event and no active mutating tools/subagents are required")
        with self.workflow.locked():
            return self._seal_locked(thread, terminal_evidence, active_operations, unknown_effects, final_update)

    def _seal_locked(self, thread, terminal_evidence, active_operations, unknown_effects, final_update, quiescence=None) -> dict:
        tx = self.transaction()
        if thread != tx["source_thread"] or tx["phase"] not in ("QUIESCING", "SEALED"):
            raise WeatherError("Rotation has not stopped the source thread")
        update_hash = digest(canonical_json(final_update)) if final_update else None
        if tx["phase"] == "SEALED":
            if (tx["terminal_evidence"] != terminal_evidence
                    or set(tx["unknown_effects"]) != set(unknown_effects)
                    or tx.get("sealed_update_sha256") != update_hash):
                raise WeatherError("Sealed evidence changed; do not reseal a completed transaction")
            return tx
        state = self.workflow.load_state()
        if final_update:
            from .models import apply_update
            from .renderer import active_section, init_prompt
            preview = apply_update(state, final_update, state.ledger_high_water + 1, utc_now())
            active_section(preview)
            init_prompt(preview, self.workflow.current()["generation"] + 1, self.workflow.launcher)
        else:
            preview = state
        if any(x["status"] == "running" for x in preview.operations):
            raise WeatherError("Recorded tools/subagents are still running")
        unresolved = [x["id"] for x in preview.operations if x["status"] == "unknown"]
        if set(unresolved) - set(unknown_effects):
            raise WeatherError("Every unknown effect must be recorded as a recovery task")
        if final_update:
            row = self.workflow.ledger.append("checkpoint", "Recorded final known effects before seal", "seal-" + tx["transaction_id"], {"update": final_update})
            state = apply_update(state, final_update, row["sequence"], row["timestamp"])
            published = self.workflow._publish(state, expected_generation=self.workflow.current()["generation"])
            tx.update(generation=published["generation"], manifest_sha256=published["manifest_sha256"])
        tx.update(phase="SEALED", terminal_evidence=terminal_evidence, active_operations=active_operations,
                  unknown_effects=unknown_effects, sealed_update_sha256=update_hash,
                  lease={"thread": None, "epoch": tx["lease"]["epoch"] + 1})
        if quiescence is not None:
            tx["quiescence"] = quiescence
        atomic_json(self.workflow.path("rotation.json"), tx)
        return tx

    def rotate(self, adapter) -> dict:
        if mode(self.report) != "automatic":
            raise WeatherError("Automatic rotation is unavailable; use rotate --dry-run or open-prepared")
        with self.workflow.locked():
            tx = self.transaction()
            if not tx["automatic_rotation"]:
                raise WeatherError("Automatic rotation is disabled")
            if tx.get("create_attempt") == "ambiguous" or tx.get("submit_attempt") == "ambiguous":
                raise WeatherError("Ambiguous API result; reconcile thread/turn IDs before any retry")
            if tx["phase"] == "SEALED":
                pinned = self._pinned(tx)
                if tx.get("continuation_version") == 1:
                    adapter.preflight(tx, "create", pinned)
                tx["create_intent_sha256"] = self.journal.record_intent(tx, "create", {"state": pinned["state"]})
                # Persist uncertainty BEFORE a potentially non-idempotent call.
                tx["create_attempt"] = "ambiguous"
                atomic_json(self.workflow.path("rotation.json"), tx)
                if tx.get("continuation_version") == 1:
                    adapter.bind_rotation(tx, "create")
                thread = identifier(adapter.create_root(pinned["state"]))
                self.journal.record_receipt(tx, "create", thread)
                tx.update(phase="NEW_THREAD", new_thread=thread, create_attempt="confirmed", submit_attempt="not-started")
                atomic_json(self.workflow.path("rotation.json"), tx)
            if tx["phase"] == "NEW_THREAD" and tx["submit_attempt"] == "not-started":
                pinned = self._pinned(tx)
                if tx.get("continuation_version") == 1:
                    adapter.preflight(tx, "submit", pinned)
                    # Guard the successor before its first turn. Handshake
                    # commands are the sole pre-lease tool exception.
                    owner = self.workflow.check_owner()
                    if tx["new_thread"] not in owner["enrolled_root_threads"]:
                        owner["enrolled_root_threads"].append(tx["new_thread"])
                        atomic_json(self.workflow.path("ownership.json"), owner)
                prompt = self._prompt(tx, pinned)
                tx["submit_intent_sha256"] = self.journal.record_intent(tx, "submit", {"thread": tx["new_thread"], "prompt": prompt})
                tx["submit_attempt"] = "ambiguous"
                atomic_json(self.workflow.path("rotation.json"), tx)
                if tx.get("continuation_version") == 1:
                    adapter.bind_rotation(tx, "submit")
                response = adapter.start_turn(tx["new_thread"], prompt)
                if not isinstance(response, dict) or not isinstance(response.get("turn"), dict):
                    raise WeatherError("Malformed turn/start reply; reconcile before continuing")
                turn_id = identifier(response.get("turn", {}).get("id"))
                self.journal.record_receipt(tx, "submit", tx["new_thread"], turn_id)
                tx.update(submit_attempt="confirmed", turn_id=turn_id)
                atomic_json(self.workflow.path("rotation.json"), tx)
            return tx

    def _prompt(self, tx: dict, pinned: dict) -> str:
        if tx.get("continuation_version") == 1:
            from .continuation import continuation_prompt
            return continuation_prompt(self.workflow, tx, pinned)
        return pinned["init_prompt"]

    def _pinned(self, tx: dict) -> dict:
        pointer = self.workflow.current()
        if (pointer["generation"] != tx["generation"]
                or pointer["manifest_sha256"] != tx["manifest_sha256"]):
            raise WeatherError("Rotation no longer matches the published generation")
        return self.workflow.bootstrap(tx["generation"])

    def reconcile(self, apply: bool = False, transaction_id: str | None = None, thread: str | None = None) -> dict:
        """Restore only confirmed local receipts; never create, submit, or return a lease."""
        with self.workflow.locked():
            original = self.transaction()
            if apply and (not transaction_id or transaction_id != original["transaction_id"] or thread != original["source_thread"]):
                raise WeatherError("Apply requires the current transaction ID and source controller identity")
            tx = copy.deepcopy(original)
            resolved, unresolved = [], []
            if tx.get("create_attempt") == "ambiguous" and tx.get("submit_attempt") == "ambiguous":
                raise WeatherError("Multiple ambiguous requests have an inconsistent rotation state")
            for step in ("create", "submit"):
                if tx.get(f"{step}_attempt") != "ambiguous":
                    continue
                expected_phase = "SEALED" if step == "create" else "NEW_THREAD"
                if tx["phase"] != expected_phase or tx["lease"]["thread"] is not None:
                    raise WeatherError("Ambiguous request has an inconsistent rotation/lease state")
                pinned = self._pinned(tx)
                request = {"state": pinned["state"]} if step == "create" else {"thread": tx["new_thread"], "prompt": self._prompt(tx, pinned)}
                receipt = self.journal.receipt(tx, step, request)
                if receipt is None:
                    unresolved.append(step)
                    continue
                if step == "create":
                    tx.update(phase="NEW_THREAD", new_thread=receipt["thread_id"],
                              create_attempt="confirmed", submit_attempt="not-started")
                else:
                    tx.update(submit_attempt="confirmed", turn_id=receipt["turn_id"])
                resolved.append(step)
            if tx.get("archive_attempt") == "ambiguous":
                unresolved.append("archive")
            if apply and resolved:
                tx["reconciled_at"] = utc_now()
                atomic_json(self.workflow.path("rotation.json"), tx)
            return {"dry_run": not apply, "transaction_id": original["transaction_id"],
                    "phase": original["phase"], "proposed_phase": tx["phase"],
                    "status": "blocked" if unresolved else "ready" if resolved else "not-required",
                    "resolved_steps": resolved, "unresolved_steps": unresolved,
                    "applied": bool(apply and resolved), "will_create": False, "will_submit": False,
                    "lease_transferred": False,
                    "reason": "No durable receipt; verify the original live request. Absence cannot authorize retry or source recovery."
                              if unresolved else "Only matching durable receipts can restore local transaction state."}

    def recover(self, thread: str, terminal_evidence: str, reason: str) -> dict:
        identifier(thread)
        text_value(terminal_evidence, "terminal evidence")
        text_value(reason, "recovery reason")
        with self.workflow.locked():
            tx = self.transaction()
            if thread != tx["source_thread"]:
                raise WeatherError("Recovery must be performed by the source root")
            if tx["new_thread"] is not None or tx.get("create_attempt") in ("ambiguous", "confirmed"):
                raise WeatherError("A successor may exist; reconcile before returning the source lease")
            if any(tx.get(f"{step}_attempt") == "ambiguous" for step in ("submit", "archive")):
                raise WeatherError("Ambiguous API result; source recovery cannot clear it")
            if tx["phase"] not in ("OBSERVING", "READY", "QUIESCING", "SEALED"):
                raise WeatherError("This rotation phase cannot return the source lease")
            if any(row["status"] == "running" for row in self.workflow.load_state().operations):
                raise WeatherError("Recorded tools/subagents are still running")
            epoch = tx["lease"]["epoch"] + 1
            if tx.get("transaction_id"):
                saved = self.workflow.path("rotations", identifier(tx["transaction_id"]) + ".json")
                if not saved.exists():
                    atomic_json(saved, {**tx, "recovery_reason": reason, "recovered_at": utc_now()})
            self.workflow.ledger.append("recovery", reason, "recovery-" + str(epoch), {"terminal_evidence": terminal_evidence})
            tx.update(phase="OBSERVING", terminal_evidence=terminal_evidence, recovery_reason=reason,
                      generation=None, transaction_id=None,
                      lease={"thread": thread, "epoch": epoch}, automatic_rotation=False)
            for key in ("create_attempt", "submit_attempt", "archive_attempt", "create_intent_sha256",
                        "submit_intent_sha256", "sealed_update_sha256", "reconciled_at", "turn_id", "manifest_sha256", "event_id",
                        "source_turn_id", "stop_attempt", "quiescence", "create_dispatch_sha256", "submit_dispatch_sha256"):
                tx.pop(key, None)
            for key in ("continuation_version", "trigger_kind", "successor_ack"):
                tx.pop(key, None)
            atomic_json(self.workflow.path("rotation.json"), tx)
            return tx

    def bind_prepared_successor(self, thread: str, workspace: Path, terminal_evidence: str) -> dict:
        """Manual fallback: explicit root/workspace and idle confirmation, no submission."""
        identifier(thread)
        if workspace.resolve() != self.workflow.workspace:
            raise WeatherError("Successor workspace mismatch")
        with self.workflow.locked():
            tx = self.transaction()
            if tx["phase"] != "SEALED" or tx["terminal_evidence"] != terminal_evidence:
                raise WeatherError("Seal the old thread before binding the fresh root")
            if tx.get("create_attempt") == "ambiguous":
                raise WeatherError("Creation result is ambiguous; reconcile before manual binding")
            if thread == tx["source_thread"]:
                raise WeatherError("Successor must be a new root thread")
            tx.update(phase="NEW_THREAD", new_thread=thread, create_attempt="manual-confirmed", submit_attempt="manual")
            for key in ("continuation_version", "trigger_kind", "successor_ack"):
                tx.pop(key, None)
            atomic_json(self.workflow.path("rotation.json"), tx)
            return tx

    def acknowledge(self, thread: str, generation: int, manifest_sha256: str, *, adapter=None) -> dict:
        with self.workflow.locked():
            tx = self.transaction()
            pinned = self.workflow.bootstrap(generation)
            if generation != tx["generation"] or pinned["manifest_sha256"] != manifest_sha256 or tx.get("manifest_sha256") != manifest_sha256:
                raise WeatherError("Wrong generation/manifest acknowledgement")
            if tx["phase"] == "ACKNOWLEDGED" and tx["new_thread"] == thread:
                return tx
            if tx["phase"] != "NEW_THREAD" or tx["new_thread"] != thread or tx["lease"]["thread"] is not None:
                raise WeatherError("Successor identity or lease state mismatch")
            if tx.get("create_attempt") == "ambiguous" or tx.get("submit_attempt") not in ("confirmed", "manual"):
                raise WeatherError("Successor submission must be reconciled before acknowledgement")
            if tx.get("submit_attempt") == "confirmed":
                identifier(tx.get("turn_id"))
            if tx.get("continuation_version") == 1:
                request = {"thread_id": thread, "turn_id": tx["turn_id"], "generation": generation,
                           "manifest_sha256": manifest_sha256, "transaction_id": tx["transaction_id"]}
                if adapter is None:
                    if tx.get("successor_ack") not in (None, request):
                        raise WeatherError("Conflicting successor acknowledgement request")
                    tx["successor_ack"] = request
                    atomic_json(self.workflow.path("rotation.json"), tx)
                    return tx
                if tx.get("successor_ack") != request:
                    raise WeatherError("Pinned successor acknowledgement request is required before lease transfer")
                self._pinned(tx)
                adapter.preflight(tx, "ack", pinned)
            tx.update(phase="ACKNOWLEDGED", acknowledged=True, acknowledged_at=utc_now(),
                      lease={"thread": thread, "epoch": tx["lease"]["epoch"] + 1}, superseded_thread=tx["source_thread"])
            tx.setdefault("successors", {})[tx["source_thread"]] = thread
            owner = self.workflow.check_owner()
            if thread not in owner.setdefault("enrolled_root_threads", [owner["source_thread"]]):
                owner["enrolled_root_threads"].append(thread)
            atomic_json(self.workflow.path("ownership.json"), owner)
            atomic_json(self.workflow.path("rotation.json"), tx)
            return tx

    def retire(self, dry_run: bool = True, adapter=None) -> dict:
        tx = self.transaction()
        caps = self.report.get("capabilities", {})
        safe = all(caps.get(k, {}).get("value") is True and caps[k].get("status") == "VERIFIED" and caps[k].get("evidence_kind") == "live-test"
                   for k in ("reversible_archive", "archive_descendant_safety"))
        eligible = bool(tx["acknowledged"] and tx["phase"] in ("ACKNOWLEDGED", "RETIRED") and safe)
        plan = {"dry_run": dry_run, "eligible": eligible, "thread_id": tx["source_thread"],
                "action": "reversible archive" if eligible else "skip archive", "reason": "Acknowledgement and descendant safety required"}
        if dry_run or not eligible:
            return plan
        with self.workflow.locked():
            tx = self.transaction()
            if tx["phase"] == "RETIRED":
                return tx
            if tx.get("archive_attempt") == "ambiguous":
                raise WeatherError("Archive result ambiguous; reconcile before retrying")
            tx["archive_attempt"] = "ambiguous"
            atomic_json(self.workflow.path("rotation.json"), tx)
            adapter.archive(tx["source_thread"])
            tx.update(phase="RETIRED", archive_attempt="confirmed")
            atomic_json(self.workflow.path("rotation.json"), tx)
            return tx
