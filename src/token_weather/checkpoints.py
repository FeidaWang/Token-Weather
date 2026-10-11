from __future__ import annotations

import os
from pathlib import Path
import shutil
import uuid
from typing import Any

from .ledger import Ledger
from .models import State, apply_update, validate_update, utc_now
from .renderer import active_section, init_prompt, heading
from .storage import (WeatherError, owned_path, read_json, read_bytes, atomic_json,
                      atomic_write, canonical_json, digest, identifier, lock, sync_dir)


class Workflow:
    def __init__(self, workspace: Path, workflow_id: str, launcher: Path | None = None):
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise WeatherError("Workspace must be a directory")
        self.id = identifier(workflow_id)
        base = owned_path(self.workspace, ".codex", "handover")
        self.root = owned_path(base, self.id)
        self.launcher = launcher or Path(__file__).resolve().parents[2] / "bin" / "token-weather"
        self.ledger = Ledger(self.root)

    def path(self, *parts: str) -> Path:
        return owned_path(self.root, *parts)

    def identity(self) -> dict:
        st = self.workspace.stat()
        return {"realpath": str(self.workspace), "device": st.st_dev, "inode": st.st_ino}

    def check_owner(self) -> dict:
        owner = read_json(self.path("ownership.json"))
        if owner is None or owner.get("worktree") != self.identity() or owner.get("workflow_id") != self.id:
            raise WeatherError("Workflow ownership/worktree identity mismatch")
        return owner

    def locked(self, timeout: float = 2.0):
        self.check_owner()
        return lock(self.path("workflow.lock"), timeout)

    def initialize(self, thread: str, objective: str, policies: dict | None = None) -> dict:
        return self._initialize(thread, State(self.id, str(self.workspace), thread, objective, policies or {}))

    def _initialize(self, thread: str, state: State, *, origin: dict | None = None, validate=None) -> dict:
        """Initialize a new workflow; a live policy seed supplies its final validator."""
        identifier(thread)
        if not state.objective.strip():
            raise WeatherError("Objective is required")
        if (state.workflow_id != self.id or state.workspace != str(self.workspace)
                or state.source_thread != thread or state.ledger_high_water != 0):
            raise WeatherError("Initial state identity/high-water mismatch")
        # Refuse oversized seeds before creating files or enrolling a root.
        active_section(state)
        init_prompt(state, 1, self.launcher)
        if validate is not None:
            validate()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock(self.path("workflow.lock")):
            if self.path("ownership.json").exists():
                raise WeatherError("Workflow already exists; resume it without reinitializing")
            owner = {"schema_version": 1, "workflow_id": self.id, "worktree": self.identity(), "source_thread": thread,
                     "enrolled_root_threads": [thread], "created_at": utc_now()}
            if origin is not None:
                owner["policy_origin"] = origin
            if validate is not None:
                validate()
            atomic_json(self.path("ownership.json"), owner)
            atomic_json(self.path("rotation.json"), {"schema_version": 1, "phase": "OBSERVING", "source_thread": thread,
                        "new_thread": None, "generation": None, "transaction_id": None,
                        "acknowledged": False, "lease": {"thread": thread, "epoch": 1},
                        "automatic_rotation": False, "unknown_effects": [], "active_operations": [], "terminal_evidence": None})
            payload = {"initial_state": state.to_dict()}
            if origin is not None:
                payload["policy_origin"] = origin
            self.ledger.append("init", state.objective, "init", payload)
            state.ledger_high_water = 1
            return self._publish(state, expected_generation=0, validate=validate)

    def current(self) -> dict:
        self.check_owner()
        value = read_json(self.path("current.json"))
        if not value or type(value.get("generation")) is not int or value["generation"] < 1:
            raise WeatherError("No published generation; run explicit prepare recovery")
        return value

    def bootstrap(self, generation: int | None = None, verify_evidence: bool = True) -> dict:
        """Pin one generation; never scan the transcript or lifetime ledger."""
        pointer = self.current()
        number = pointer["generation"] if generation is None else generation
        if type(number) is not int or number < 1:
            raise WeatherError("Invalid generation")
        folder = self.path("generations", str(number))
        manifest_raw = read_bytes(owned_path(folder, "manifest.json"), 16_384)
        if number == pointer["generation"] and digest(manifest_raw) != pointer["manifest_sha256"]:
            raise WeatherError("Published manifest digest mismatch")
        import json
        manifest = json.loads(manifest_raw)
        owner = self.check_owner()
        if (manifest.get("schema_version") != 1 or manifest.get("generation") != number
                or manifest.get("workspace") != str(self.workspace) or manifest.get("workflow_id") != self.id
                or manifest.get("source_thread") not in owner.get("enrolled_root_threads", [owner["source_thread"]])):
            raise WeatherError("Manifest identity mismatch")
        expected_files = {"state.json", "active.md", "init-prompt.md", "evidence.json"}
        if set(manifest.get("files", {})) != expected_files:
            raise WeatherError("Manifest bootstrap file set mismatch")
        contents = {}
        for name, expected in manifest["files"].items():
            raw = read_bytes(owned_path(folder, name))
            if digest(raw) != expected:
                raise WeatherError(f"Bootstrap digest mismatch: {name}")
            contents[name] = raw
        state = State.from_dict(json.loads(contents["state.json"]))
        if (state.workflow_id != self.id or state.workspace != str(self.workspace)
                or state.source_thread != manifest["source_thread"]):
            raise WeatherError("State/manifest identity mismatch")
        if state.ledger_high_water != manifest["ledger_high_water"] or digest(contents["active.md"]) != manifest["active_state_digest"]:
            raise WeatherError("Bootstrap high-water/digest mismatch")
        references = json.loads(contents["evidence.json"])
        if verify_evidence:
            for ref in references:
                if ref.get("cache_id"):
                    from .verification_cache import VerificationCache
                    reused = VerificationCache(self.root).verify(ref["cache_id"])
                    if not reused["valid"]:
                        raise WeatherError(f"Stale verification evidence {ref['id']}: {reused['reason']}")
                if ref.get("path") and ref.get("sha256"):
                    p = Path(ref["path"])
                    if not p.is_absolute():
                        p = self.workspace / p
                    if not p.resolve().is_relative_to(self.workspace):
                        raise WeatherError("Bootstrap evidence must be inside the enrolled worktree")
                    # Check only evidence explicitly pinned for consumption.
                    if digest(read_bytes(p, 4 * 1024 * 1024)) != ref["sha256"]:
                        raise WeatherError(f"Stale evidence: {ref['id']}")
        return {"generation": number, "manifest_sha256": digest(manifest_raw), "manifest": manifest,
                "active": contents["active.md"].decode(), "init_prompt": contents["init-prompt.md"].decode(),
                "evidence": references, "state": state.to_dict()}

    def load_state(self, replay: bool = False) -> State:
        try:
            state = State.from_dict(self.bootstrap(verify_evidence=False)["state"])
        except WeatherError:
            if not replay or self.path("current.json").exists():
                raise
            first = next(self.ledger.rows(), None)
            if not first or first["kind"] != "init":
                raise WeatherError("No recoverable initial state")
            state = State.from_dict(first["payload"]["initial_state"])
        if replay:
            for row in self.ledger.rows(after=state.ledger_high_water):
                if row["kind"] == "checkpoint":
                    state = apply_update(state, row["payload"]["update"], row["sequence"], row["timestamp"])
                else:
                    state.ledger_high_water = row["sequence"]
        return state

    def assert_writer(self, thread: str, epoch: int | None = None) -> dict:
        rotation = read_json(self.path("rotation.json"))
        if any(rotation.get(f"{step}_attempt") == "ambiguous" for step in ("create", "submit", "archive")):
            raise WeatherError("Ambiguous API result; reconcile before writing or preparing another rotation")
        lease = rotation["lease"]
        if lease["thread"] != thread or (epoch is not None and epoch != lease["epoch"]):
            raise WeatherError("Write lease belongs to another thread/epoch")
        if rotation["phase"] not in ("OBSERVING", "PREPARING", "READY", "ACKNOWLEDGED", "RETIRED"):
            raise WeatherError("Workflow is quiescing/sealed; reconcile and acknowledge before writing")
        return rotation

    def checkpoint(self, update: dict, key: str, thread: str, expected_generation: int | None = None, epoch: int | None = None) -> dict:
        update = validate_update(update)
        identifier(key)
        with self.locked():
            rotation = self.assert_writer(thread, epoch)
            current = self.current()["generation"]
            if expected_generation is not None and expected_generation != current:
                raise WeatherError("Generation compare-and-swap failed")
            state = self.load_state(replay=True)
            state.source_thread = thread
            # Check budgets and semantic validation before committing any delta.
            preview = apply_update(state, update, state.ledger_high_water + 1, utc_now())
            active_section(preview)
            init_prompt(preview, current + 1, self.launcher)
            # O(1) critical-hook freshness marker; ledger append alone is not publication.
            atomic_json(self.path("pending-publication.json"), {"key": key, "previous_generation": current})
            row = self.ledger.append("checkpoint", update.get("milestone", {}).get("summary", update.get("next_action", "State checkpoint")), key, {"update": update})
            if row["sequence"] <= state.ledger_high_water:
                published = self.load_state()
                if state.to_dict() != published.to_dict():
                    result = self._publish(state, expected_generation=current)
                    self.path("pending-publication.json").unlink()
                    sync_dir(self.root)
                    return {**result, "duplicate": True, "recovered": True}
                self.path("pending-publication.json").unlink()
                sync_dir(self.root)
                return {"generation": current, "duplicate": True}
            state = apply_update(state, update, row["sequence"], row["timestamp"])
            result = self._publish(state, expected_generation=current)
            self.path("pending-publication.json").unlink()
            sync_dir(self.root)
            # A READY checkpoint changed: its previous rotation preparation is stale.
            if rotation["phase"] in ("PREPARING", "READY"):
                rotation.update(phase="OBSERVING", generation=None, transaction_id=None)
                atomic_json(self.path("rotation.json"), rotation)
            return result

    def prepare(self, thread: str) -> dict:
        with self.locked():
            rotation = self.assert_writer(thread)
            if rotation["phase"] == "READY" and rotation["generation"] == self.current()["generation"] and not self.path("pending-publication.json").exists():
                self.bootstrap()
                self._repair_materializations()
                return {"phase": "READY", "generation": rotation["generation"], "transaction_id": rotation["transaction_id"]}
            if rotation["phase"] in ("ACKNOWLEDGED", "RETIRED"):
                # The same workflow keeps its entire chronology across root rotations.
                history = self.path("rotations")
                history.mkdir(mode=0o700, exist_ok=True)
                saved = self.path("rotations", identifier(rotation["transaction_id"]) + ".json")
                if not saved.exists():
                    atomic_json(saved, rotation)
                rotation.update(phase="OBSERVING", source_thread=thread, new_thread=None, generation=None,
                                transaction_id=None, acknowledged=False, terminal_evidence=None,
                                active_operations=[], unknown_effects=[])
                for key in ("create_attempt", "submit_attempt", "archive_attempt", "event_id", "turn_id",
                            "create_intent_sha256", "submit_intent_sha256", "sealed_update_sha256", "reconciled_at",
                            "source_turn_id", "stop_attempt", "quiescence", "create_dispatch_sha256", "submit_dispatch_sha256"):
                    rotation.pop(key, None)
                for key in ("continuation_version", "trigger_kind", "successor_ack"):
                    rotation.pop(key, None)
            rotation.update(phase="PREPARING", transaction_id=rotation.get("transaction_id") or str(uuid.uuid4()))
            atomic_json(self.path("rotation.json"), rotation)
            current = read_json(self.path("current.json"), {"generation": 0})["generation"]
            state = self.load_state(replay=True)
            if current == 0 or state.to_dict() != self.load_state().to_dict():
                self._publish(state, expected_generation=current)
            self.bootstrap()  # One bounded relevant evidence validation before ready.
            self._repair_materializations()
            if self.path("pending-publication.json").exists():
                self.path("pending-publication.json").unlink()
                sync_dir(self.root)
            generation = self.current()["generation"]
            if rotation["phase"] != "READY" or rotation.get("generation") != generation:
                rotation.update(phase="READY", generation=generation, prepared_at=utc_now(),
                                manifest_sha256=self.current()["manifest_sha256"])
                atomic_json(self.path("rotation.json"), rotation)
            return {"phase": "READY", "generation": generation, "transaction_id": rotation["transaction_id"]}

    def _repair_materializations(self) -> None:
        published = self.bootstrap(verify_evidence=False)
        state = State.from_dict(published["state"])
        atomic_json(self.path("state.json"), published["state"])
        atomic_write(self.path("init-prompt.md"), published["init_prompt"].encode())
        self._materialize(state, published["generation"], published["active"])

    def _publish(self, state: State, expected_generation: int, *, validate=None) -> dict:
        current = read_json(self.path("current.json"), {"generation": 0})
        if current["generation"] != expected_generation:
            raise WeatherError("Generation compare-and-swap failed")
        # Orphan generations are retained and skipped, never overwritten.
        generation = expected_generation + 1
        while self.path("generations", str(generation)).exists():
            generation += 1
        active = active_section(state)
        prompt = init_prompt(state, generation, self.launcher)
        content = {"state.json": canonical_json(state.to_dict()), "active.md": active.encode(),
                   "init-prompt.md": prompt.encode(), "evidence.json": canonical_json(state.evidence)}
        parent = self.path("generations")
        parent.mkdir(mode=0o700, exist_ok=True)
        temporary = self.path("generations", ".tw-" + uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        for name, raw in content.items():
            atomic_write(owned_path(temporary, name), raw)
        manifest = {"schema_version": 1, "workflow_id": self.id, "generation": generation,
                    "workspace": str(self.workspace), "source_thread": state.source_thread,
                    "active_state_digest": digest(content["active.md"]), "ledger_high_water": state.ledger_high_water,
                    "files": {name: digest(raw) for name, raw in content.items()}, "created_at": utc_now()}
        manifest_bytes = canonical_json(manifest)
        atomic_write(owned_path(temporary, "manifest.json"), manifest_bytes)
        sync_dir(temporary)
        final = self.path("generations", str(generation))
        os.rename(temporary, final)
        sync_dir(parent)
        # Sole transactional commit point. Convenience files are materializations.
        pointer = {"generation": generation, "manifest_sha256": digest(manifest_bytes)}
        if validate is not None:
            validate()
        atomic_json(self.path("current.json"), pointer)
        atomic_write(self.path("state.json"), content["state.json"])
        atomic_write(self.path("init-prompt.md"), content["init-prompt.md"])
        self._materialize(state, generation, active)
        return {**pointer, "ledger_high_water": state.ledger_high_water}

    def _materialize(self, state: State, generation: int, active: str) -> None:
        # Incremental chronology; rebuild only following a materialization crash.
        chronology = self.path("chronology.md")
        meta = read_json(self.path("chronology-index.json"), {"sequence": 0, "bytes": 0})
        valid = chronology.exists() and chronology.stat().st_size == meta["bytes"]
        if not valid:
            atomic_write(chronology, b"")
            meta = {"sequence": 0, "bytes": 0}
        fd = os.open(chronology, os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "ab") as out:
                for row in self.ledger.rows(after=meta["sequence"]):
                    if row["sequence"] > state.ledger_high_water:
                        break
                    out.write(Ledger.render_row(row).encode())
                    meta["sequence"] = row["sequence"]
                out.flush()
                os.fsync(out.fileno())
        finally:
            pass
        meta["bytes"] = chronology.stat().st_size
        atomic_json(self.path("chronology-index.json"), meta)
        # Stream the lifetime materialization; it is never returned by bootstrap.
        temporary = self.path(".tw-handover-" + uuid.uuid4().hex)
        with temporary.open("xb") as out:
            out.write((heading(state, generation) + active + "\n## Complete chronological activity ledger — load selectively\n\n").encode())
            with chronology.open("rb") as source:
                shutil.copyfileobj(source, out, length=65_536)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path("handover.md"))
        sync_dir(self.root)

    def ledger_entries(self, numbers: list[int]) -> list[dict]:
        requested = set(numbers)
        if len(requested) > 20:
            raise WeatherError("Request at most 20 specific ledger rows")
        return [row for row in self.ledger.rows() if row["sequence"] in requested]
