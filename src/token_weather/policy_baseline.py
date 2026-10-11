"""Explicit live policy seeding into a distinct workflow; no disk proof import."""
from __future__ import annotations

import copy
import uuid

from .checkpoints import Workflow
from .continuation import POLICY_FIELDS, effective_policy
from .live_baseline import capture_initial_policy
from .models import State, utc_now
from .storage import WeatherError, canonical_json, digest, identifier, read_json


def policy_baseline_plan(source: Workflow, target_id: str, thread: str) -> dict:
    """Read-only diagnosis. It never captures policy, enrolls or grants a lease."""
    identifier(target_id)
    identifier(thread)
    if target_id == source.id:
        raise WeatherError("A policy baseline needs a distinct workflow ID")
    pinned = source.bootstrap(verify_evidence=False)
    tx = read_json(source.path("rotation.json"))
    missing = sorted(set(POLICY_FIELDS) - set(pinned["state"]["policies"]))
    target = Workflow(source.workspace, target_id, source.launcher)
    return {"schema_version": 1, "kind": "policy-baseline-plan", "status": "BLOCKED",
            "source_workflow": source.id, "target_workflow": target_id,
            "generation": pinned["generation"], "manifest_sha256": pinned["manifest_sha256"],
            "lease_epoch": tx["lease"]["epoch"], "writer_matched": tx["lease"]["thread"] == thread,
            "missing_policy_fields": missing, "target_exists": target.root.exists(),
            "required": ["Current same-owner control and complete-live-baseline authorization",
                         "Fresh atomic complete snapshot with effective execution policy",
                         "Source writer, lease epoch, generation and manifest compare-and-swap",
                         "New workflow ID; preserve the original workflow and history"],
            "disk_import_allowed": False, "automatic_rotation": False, "production_promotion": False}


def initialize_policy_workflow(source: Workflow, target_id: str, session, reader, *, thread: str,
                               expected_generation: int, epoch: int, key: str, timeout: float = 5) -> dict:
    """Clone a pinned active state under its first authoritative live policy.

    The source is unchanged. New history begins with a pinned origin reference;
    original ledger rows remain in their original workflow. Automatic mode stays
    disabled. A partial local publication is retained for explicit recovery.
    """
    identifier(target_id)
    identifier(thread)
    identifier(key)
    if (target_id == source.id or type(expected_generation) is not int or expected_generation < 1
            or type(epoch) is not int or epoch < 1):
        raise WeatherError("Policy seeding requires a new ID and exact positive generation/epoch")
    if session.thread != thread or session.workspace != source.workspace:
        raise WeatherError("Policy owner does not match the current writer/workspace")
    with source.locked():
        tx = source.assert_writer(thread, epoch)
        if (tx["phase"] not in ("OBSERVING", "ACKNOWLEDGED", "RETIRED")
                or source.path("pending-publication.json").exists()):
            raise WeatherError("Finish or recover the existing publication/rotation before policy seeding")
        pinned = source.bootstrap()
        if pinned["generation"] != expected_generation:
            raise WeatherError("Policy seed generation compare-and-swap failed")
        old = pinned["state"]
        if not set(POLICY_FIELDS) - set(old["policies"]):
            raise WeatherError("The source already has a complete immutable policy baseline")
        if source.load_state(replay=True).to_dict() != old:
            raise WeatherError("Unpublished ledger changes must be recovered before policy seeding")
        target = Workflow(source.workspace, target_id, source.launcher)
        # Reject collisions before invoking a live provider. Exact retries are
        # checked again after fresh proof; they never republish the target.
        owner = target.check_owner() if target.path("ownership.json").exists() else None
        if owner is None and target.root.exists():
            raise WeatherError("Target directory already exists without an initialization receipt; choose a new ID")
        fixed = {"schema_version": 1, "kind": "live-policy-seed", "key": key,
                 "source_workflow": source.id, "source_thread": thread, "source_lease_epoch": epoch,
                 "source_generation": pinned["generation"], "source_manifest_sha256": pinned["manifest_sha256"],
                 "source_ledger_high_water": old["ledger_high_water"],
                 "source_state_sha256": digest(canonical_json(old))}
        if owner is not None:
            origin = owner.get("policy_origin", {})
            if any(origin.get(name) != value for name, value in fixed.items()):
                raise WeatherError("Target belongs to another initialization; choose a new workflow ID")

        def validate_source():
            current_tx = source.assert_writer(thread, epoch)
            if (source.current() != {"generation": pinned["generation"], "manifest_sha256": pinned["manifest_sha256"]}
                    or canonical_json(current_tx) != canonical_json(tx)
                    or source.path("pending-publication.json").exists()
                    or source.load_state(replay=True).to_dict() != old):
                raise WeatherError("Source lease, generation or transaction changed during live policy capture")

        context = {"workflow_id": source.id, "transaction_id": str(uuid.uuid4()),
                   "generation": pinned["generation"], "manifest_sha256": pinned["manifest_sha256"]}
        baseline = capture_initial_policy(session, reader, context=context, timeout=timeout, validate=validate_source)
        policy = effective_policy(session.source_settings, source.workspace)
        if any(name in old["policies"] and old["policies"][name] != value for name, value in policy.items()):
            raise WeatherError("Live policy conflicts with the source's existing immutable policy fields")

        def validate_publication():
            session.drain()
            baseline.current(session, context, None, None)
            if effective_policy(session.source_settings, source.workspace) != policy:
                raise WeatherError("Effective policy changed before seed publication")
            validate_source()

        validate_publication()
        policy_hash = digest(canonical_json(policy))
        if owner is not None:
            if owner["policy_origin"].get("policy_sha256") != policy_hash:
                raise WeatherError("A retry cannot replace the target's initial policy")
            first = target.bootstrap(1)
            if any(first["state"]["policies"].get(name) != value for name, value in policy.items()):
                raise WeatherError("Target policy does not match its pinned origin")
            validate_publication()
            return {**target.current(), "workflow_id": target_id, "duplicate": True,
                    "automatic_rotation": False, "production_promotion": False, "source_unchanged": True}
        state = State.from_dict(copy.deepcopy(old))
        state.workflow_id, state.source_thread = target_id, thread
        state.policies = {**state.policies, **policy}
        state.ledger_high_water, state.usage, state.updated_at = 0, {}, utc_now()
        origin = {**fixed, "policy_sha256": policy_hash, "captured_at": utc_now()}
        result = target._initialize(thread, state, origin=origin, validate=validate_publication)
        return {**result, "workflow_id": target_id, "duplicate": False,
                "automatic_rotation": False, "production_promotion": False, "source_unchanged": True}
