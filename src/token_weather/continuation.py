"""Bounded continuation ticks over one proven owner, never a background daemon.

Live attachment and occupancy validators are explicit providers. There is no
production provider in this release; discovery/fixture JSON cannot enable it.
"""
from __future__ import annotations

import copy
from pathlib import Path
import shlex
import time

from .capabilities import mode
from .coordinator import Coordinator
from .desktop_probe import inspect_loaded_owner
from .models import utc_now
from .renderer import budget, plain
from .remote_reconciliation import RemoteReconciler, owner_binding
from .runtime_usage import ENGINE_VERSION
from .storage import WeatherError, atomic_json, canonical_json, digest, identifier


POLICY_FIELDS = {"model": "model", "model_provider": "modelProvider",
                 "model_reasoning_effort": "effort", "approval_policy": "approvalPolicy",
                 "approvals_reviewer": "approvalsReviewer", "sandbox_policy": "sandboxPolicy",
                 "active_permission_profile": "activePermissionProfile"}
SANDBOX_MODES = {"readOnly": "read-only", "workspaceWrite": "workspace-write",
                 "dangerFullAccess": "danger-full-access"}


def effective_policy(settings: dict, workspace: Path) -> dict:
    if not isinstance(settings, dict) or set(POLICY_FIELDS.values()) - set(settings):
        raise WeatherError("Complete live model and permission settings are required")
    cwd = settings.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or Path(cwd).resolve() != workspace:
        raise WeatherError("Live policy workspace mismatch")
    values = {key: copy.deepcopy(settings[wire]) for key, wire in POLICY_FIELDS.items()}
    for key in ("model", "model_provider", "model_reasoning_effort", "approvals_reviewer"):
        value = values[key]
        if not isinstance(value, str) or not value or len(value.encode()) > 256 or value in ("unknown", "unresolved"):
            raise WeatherError("Exact live model/provider/effort/reviewer is unresolved")
    approval = values["approval_policy"]
    if (isinstance(approval, str) and approval not in ("untrusted", "on-request", "never")
            or not isinstance(approval, (str, dict)) or not approval):
        raise WeatherError("Effective approval policy is unresolved")
    if isinstance(approval, dict):
        granular = approval.get("granular")
        if (set(approval) != {"granular"} or not isinstance(granular, dict)
                or {"mcp_elicitations", "rules", "sandbox_approval"} - set(granular)
                or set(granular) - {"mcp_elicitations", "rules", "sandbox_approval", "request_permissions", "skill_approval"}
                or any(type(value) is not bool for value in granular.values())):
            raise WeatherError("Malformed effective granular approval policy")
    if values["approvals_reviewer"] not in ("user", "auto_review", "guardian_subagent"):
        raise WeatherError("Unsupported effective approvals reviewer")
    sandbox = values["sandbox_policy"]
    if not isinstance(sandbox, dict) or sandbox.get("type") not in SANDBOX_MODES:
        raise WeatherError("This continuation adapter cannot preserve the effective sandbox")
    required = {"dangerFullAccess": {"type"}, "readOnly": {"type", "networkAccess"},
                "workspaceWrite": {"type", "networkAccess", "writableRoots", "excludeSlashTmp", "excludeTmpdirEnvVar"}}[sandbox["type"]]
    if set(sandbox) != required or any(type(value) is not bool for key, value in sandbox.items()
                                                      if key not in ("type", "writableRoots")):
        raise WeatherError("Complete effective sandbox details are required")
    if sandbox["type"] == "workspaceWrite":
        roots = sandbox["writableRoots"]
        if not isinstance(roots, list) or len(roots) > 32 or any(not isinstance(path, str) or not Path(path).is_absolute() for path in roots):
            raise WeatherError("Invalid effective writable roots")
    profile = values["active_permission_profile"]
    if profile is not None and (not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
                                or not profile["id"] or set(profile) - {"id", "extends"}
                                or profile.get("extends") is not None and not isinstance(profile["extends"], str)):
        raise WeatherError("Malformed active permission profile provenance")
    values["sandbox_mode"] = SANDBOX_MODES[sandbox["type"]]
    if len(canonical_json(values)) > 16_384:
        raise WeatherError("Live policy exceeds the bounded metadata budget")
    return values


def handshake_commands(workflow, tx: dict) -> dict[str, str]:
    # The live root and tool cwd have both been checked against this worktree.
    base = [str(workflow.launcher), "--workflow", workflow.id]
    if tx.get("new_thread"):
        base.extend(["--thread-id", tx["new_thread"]])
    return {"bootstrap": shlex.join(base + ["bootstrap", "--generation", str(tx["generation"])]),
            "acknowledge": shlex.join(base + ["acknowledge", "--generation", str(tx["generation"]),
                                            "--manifest-sha256", tx["manifest_sha256"]]),
            "status": shlex.join(base + ["status", "--json"])}


def continuation_prompt(workflow, tx: dict, pinned: dict) -> str:
    commands = handshake_commands(workflow, tx)
    state = pinned["state"]
    last = state["milestones"][-1]["summary"] if state["milestones"] else "None recorded."
    text = f"""cwd: {workflow.workspace}
Run each once, login:false: {commands['bootstrap']}
Read ACTIVE, then: {commands['acknowledge']}
Await controller lease before edits. Use status --json with the same launcher/workflow, login:false.
Last: {plain(last)}
Next: {plain(state['next_action'])}
Keep rules/policies/files/history. Reuse evidence; reconcile unknown effects.
"""
    return budget(text, 300, "continuation prompt")


def allow_handshake_tool(workflow, tx: dict, event: dict) -> bool:
    """Only exact, shell-quoted commands; no broad shell/read permission."""
    if (tx.get("continuation_version") != 1 or tx.get("new_thread") != event.get("session_id")
            or tx.get("submit_attempt") not in ("ambiguous", "confirmed")):
        return False
    turn = event.get("turn_id")
    if not isinstance(turn, str) or not turn or (tx.get("submit_attempt") == "confirmed" and turn != tx.get("turn_id")):
        return False
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    name = event.get("tool_name")
    command = tool_input.get("cmd") if name == "exec_command" else tool_input.get("command")
    commands = handshake_commands(workflow, tx).values()
    if name in ("exec_command", "shell_command"):
        allowed = {"cmd" if name == "exec_command" else "command", "login", "workdir",
                   "max_output_tokens", "yield_time_ms", "timeout_ms"}
        return (set(tool_input) <= allowed and tool_input.get("login") is False
                and tool_input.get("workdir", str(workflow.workspace)) == str(workflow.workspace)
                and isinstance(command, str) and command in commands)
    return False


class ContinuationAdapter:
    """Exact owner, resolved policies and expiring Desktop attachment proof."""
    def __init__(self, controller, attachment_reader):
        self.controller, self.session = controller, controller.session
        self.workflow = controller.workflow
        self.attachment_reader = attachment_reader
        self.successor = None
        self.settings = None
        self.turn = None
        self.turn_status = None
        self.turn_started = False
        self.invalid = False
        self.creation_policy = None
        self.pending_request = None
        self.creation_pending = controller.coordinator.transaction().get("create_attempt") == "ambiguous"
        self.pending_baselines = {}

    def consume(self, message: dict) -> None:
        if "id" in message or not isinstance(message.get("params"), dict):
            return
        params, method = message["params"], message.get("method")
        thread_id = params.get("threadId")
        if thread_id != self.successor:
            if self.creation_pending and self.successor is None and thread_id != self.session.thread:
                self._pending_baseline(thread_id, method, params)
            return
        if method == "thread/settings/updated":
            try:
                self.settings = effective_policy(params.get("threadSettings"), self.workflow.workspace)
            except WeatherError:
                self.invalid = True
        elif method in ("turn/started", "turn/completed"):
            turn = params.get("turn", {})
            if not isinstance(turn, dict):
                self.invalid = True
                raise WeatherError("Malformed successor turn notification")
            try:
                identity = identifier(turn.get("id"))
            except WeatherError:
                self.invalid = True
                raise
            if self.turn not in (None, identity):
                self.invalid = True
            if method == "turn/started":
                if self.turn_status == "completed": self.invalid = True
                self.turn_started = True
            elif not self.turn_started:
                self.invalid = True
            self.turn, self.turn_status = identity, turn.get("status")
            if self.turn_status not in ("inProgress", "completed") or turn.get("error"):
                self.invalid = True
        elif method in ("model/rerouted", "error"):
            self.invalid = True

    def _pending_baseline(self, thread, method: str, params: dict) -> None:
        if method not in ("thread/settings/updated", "turn/started", "turn/completed", "model/rerouted", "error"):
            return
        try:
            identifier(thread)
        except WeatherError:
            return
        if thread not in self.pending_baselines:
            if len(self.pending_baselines) >= 32:
                self.invalid = True
                return
            self.pending_baselines[thread] = {}
        row = self.pending_baselines[thread]
        if method == "thread/settings/updated":
            try:
                row["settings"] = effective_policy(params.get("threadSettings"), self.workflow.workspace)
            except WeatherError:
                row["invalid"] = True
        else:
            # Activity observed before correlation is not an idle baseline.
            row["invalid"] = True

    def _restore_pending_baseline(self) -> None:
        self.creation_pending = False
        row = self.pending_baselines.get(self.successor, {})
        self.pending_baselines.clear()
        if row.get("invalid"):
            self.invalid = True
        if "settings" in row:
            self.settings = row["settings"]

    def preflight(self, tx: dict, step: str, pinned: dict) -> None:
        self.controller.require_live()
        self.session.drain()
        if self.controller.baseline_reader is not None:
            self.controller.ensure_baseline(tx)
        self._validate_responses(tx, pinned)
        policy = self.controller.source_policy(pinned)
        self.session.inspect_inventory(self.controller.descendant_reader)
        if not self.session.observer.report()["quiescent"] or tx.get("source_turn_id") != self.session.observer.turn:
            raise WeatherError("Source stopped proof is incomplete or changed before continuation")
        self.controller.require_live()
        self.controller.source_policy(pinned)
        if step == "create":
            continuation_prompt(self.workflow, tx, pinned)  # Reject oversize before any remote request.
            if self.attachment_reader is None:
                raise WeatherError("A verified live Desktop attachment provider is required")
            return
        self.successor = identifier(tx["new_thread"])
        loaded = inspect_loaded_owner(self.session.rpc, self.successor, self.workflow.workspace)
        if not loaded.get("ownership_candidate"):
            raise WeatherError("Successor is not loaded on this exact owner connection")
        response = self.session.rpc.request("thread/read", {"threadId": self.successor, "includeTurns": False})
        self.session.drain()
        thread = response.get("thread", {})
        if (thread.get("id") != self.successor or self.successor == tx["source_thread"]
                or "parentThreadId" not in thread or thread["parentThreadId"] is not None
                or "forkedFromId" not in thread or thread["forkedFromId"] is not None
                or thread.get("model") != policy["model"] or thread.get("modelProvider") != policy["model_provider"]
                or thread.get("reasoningEffort") != policy["model_reasoning_effort"]):
            raise WeatherError("Successor root, workspace or effective model identity mismatch")
        if self.invalid or self.settings != policy:
            raise WeatherError("Fresh successor settings do not preserve the full source policy")
        if step == "submit" and (self.turn is not None or thread.get("status") != {"type": "idle"}):
            raise WeatherError("Successor already has execution; initialization cannot be sent")
        if step == "ack" and (not self.turn_started or self.turn != tx.get("turn_id") or self.turn_status not in ("inProgress", "completed")):
            raise WeatherError("Successor acknowledgement lacks the exact successfully submitted native turn")
        if thread.get("status", {}).get("type") not in ("idle", "active"):
            raise WeatherError("Successor is in an unknown or failed runtime state")
        if self.creation_policy is not None and self.creation_policy != policy:
            raise WeatherError("Creation response did not preserve the source policies")
        if self.attachment_reader is None:
            raise WeatherError("A verified live Desktop attachment provider is required")
        proof = self.attachment_reader(self.session, self.successor, tx.get("turn_id"))
        expected = {"acceptance_scope": "desktop-live-attachment", "connection_id": self.session.connection_id,
                    "thread_id": self.successor, "turn_id": tx.get("turn_id"),
                    "workspace": str(self.workflow.workspace), "attached_live": True,
                    "visible": True, "duplicate_execution": False}
        expires = proof.get("expires_at") if isinstance(proof, dict) else None
        if (not isinstance(proof, dict) or set(proof) != set(expected) | {"expires_at"}
                or any(proof.get(key) != value for key, value in expected.items())
                or any(type(proof[key]) is not bool for key in ("attached_live", "visible", "duplicate_execution"))
                or type(expires) not in (int, float) or not time.time() < expires <= time.time() + 5):
            raise WeatherError("Desktop attachment does not prove this exact live successor without duplicate execution")
        self.session.drain()
        self.controller.require_live()
        self._validate_responses(tx, pinned)
        self.controller.source_policy(pinned)
        if time.time() >= expires:
            raise WeatherError("Desktop attachment evidence expired before continuation commit")
        if self.invalid or self.settings != policy or not self.session.observer.report()["quiescent"]:
            raise WeatherError("Owner or successor evidence changed during attachment validation")
        if step == "submit" and self.turn is not None:
            raise WeatherError("Successor started during validation; do not send again")
        if step == "ack" and self.turn != tx.get("turn_id"):
            raise WeatherError("Successor turn changed during acknowledgement validation")

    def _validate_responses(self, tx: dict, pinned: dict) -> None:
        for step in ("create", "submit"):
            if tx.get(f"{step}_attempt") != "confirmed" or tx.get(f"{step}_dispatch_sha256") is None:
                continue
            request = ({"state": pinned["state"]} if step == "create" else
                       {"thread": tx["new_thread"], "prompt": self.controller.coordinator._prompt(tx, pinned)})
            binding = self.controller.coordinator.journal.dispatch(tx, step, request, owner_binding(self.session))
            if binding is None:
                # Only a confirmed durable receipt plus a new complete live
                # baseline can bridge connections. Missing receipts stay ambiguous.
                receipt = self.controller.coordinator.journal.receipt(tx, step, request)
                if (receipt is None or receipt["thread_id"] != tx["new_thread"]
                        or step == "submit" and receipt["turn_id"] != tx["turn_id"]):
                    raise WeatherError("Reconnected continuation lacks a matching confirmed receipt")
                baseline = self.session.live_baseline
                if baseline is None:
                    raise WeatherError("Original response baseline is not bound to this live connection")
                baseline.current(self.session, self.controller.baseline_context(tx), tx["new_thread"], tx.get("turn_id"))
                continue
            response = self.session.rpc.correlation.response(binding)
            if (response is None or response["outcome"] != "success"
                    or step == "create" and response["thread_id"] != tx["new_thread"]
                    or step == "submit" and response["turn_id"] != tx["turn_id"]):
                raise WeatherError("Original response evidence became unavailable or conflicting")

    def create_root(self, state: dict) -> str:
        policy = state["policies"]
        params = {"cwd": str(self.workflow.workspace), "model": policy["model"],
                  "modelProvider": policy["model_provider"], "approvalPolicy": policy["approval_policy"],
                  "approvalsReviewer": policy["approvals_reviewer"],
                  "config": {"model_reasoning_effort": policy["model_reasoning_effort"]}}
        if policy["active_permission_profile"] is not None:
            params["permissions"] = policy["active_permission_profile"]["id"]
        else:
            params["sandbox"] = policy["sandbox_mode"]
        result = self._request("create", "thread/start", params)
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise WeatherError("Malformed thread/start response; preserve the ambiguous creation")
        thread = identifier(result.get("thread", {}).get("id"))
        self.successor = thread
        # The ID is returned before policy validation so Coordinator first
        # persists the creation receipt. A rejected policy cannot cause recreate.
        try:
            root = result["thread"]
            if ("parentThreadId" not in root or root["parentThreadId"] is not None
                    or "forkedFromId" not in root or root["forkedFromId"] is not None
                    or root.get("cwd") != str(self.workflow.workspace)):
                raise WeatherError("Creation response is not the exact fresh worktree root")
            resolved = {**result, "effort": result.get("reasoningEffort"), "sandboxPolicy": result.get("sandbox")}
            # Profile provenance is experimental in 0.160.0. Absence stays
            # unknown; only an explicit field can establish a null profile.
            self.creation_policy = effective_policy(resolved, self.workflow.workspace)
            self.settings = copy.deepcopy(self.creation_policy)
        except WeatherError:
            self.invalid = True
        self._restore_pending_baseline()
        return thread

    def start_turn(self, thread: str, prompt: str) -> dict:
        return self._request("submit", "turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}]})

    def bind_rotation(self, tx: dict, step: str) -> None:
        self.pending_request = (tx, step)
        if step == "create":
            self.creation_pending = True

    def _request(self, step: str, method: str, params: dict) -> dict:
        if self.pending_request is None or self.pending_request[1] != step:
            raise WeatherError("Continuation request lacks its durable rotation intent")
        tx = self.pending_request[0]
        self.pending_request = None
        if not callable(getattr(self.session.rpc, "request_correlated", None)):
            raise WeatherError("Owner transport cannot correlate the original request")
        def before_send(binding):
            self.controller.require_live()
            tx[f"{step}_dispatch_sha256"] = self.controller.coordinator.journal.record_dispatch(
                tx, step, owner_binding(self.session), binding)
            atomic_json(self.workflow.path("rotation.json"), tx)
        return self.session.rpc.request_correlated(method, params, before_send)

    def restore_creation(self, response: dict) -> None:
        self.successor = identifier(response["thread_id"])
        policy = response.get("creation_policy")
        if policy is None or self.creation_policy is not None and self.creation_policy != policy:
            self.invalid = True
        else:
            self.creation_policy = copy.deepcopy(policy)
            self.settings = copy.deepcopy(policy)
        self._restore_pending_baseline()


class ContinuationController:
    def __init__(self, workflow, session, report: dict, *, attachment_reader=None, descendant_reader=None, baseline_reader=None):
        self.workflow, self.session, self.report = workflow, session, copy.deepcopy(report)
        self.coordinator = Coordinator(workflow, self.report)
        self.descendant_reader = descendant_reader
        self.baseline_reader = baseline_reader
        self.adapter = ContinuationAdapter(self, attachment_reader)
        session.add_listener(self.adapter.consume)

    def close(self):
        self.session.remove_listener(self.adapter.consume)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def require_live(self, *, policy=None):
        self.session.require_control()
        binding = self.report.get("owner_binding", {})
        expected = {"connection_id": self.session.connection_id, "thread_id": self.session.thread,
                    "turn_id": self.session.observer.turn, "workspace": str(self.workflow.workspace),
                    "engine_version": ENGINE_VERSION,
                    "policy_sha256": digest(canonical_json(policy if policy is not None else effective_policy(self.session.source_settings, self.workflow.workspace)))}
        expires = binding.get("expires_at")
        if (mode(self.report) != "automatic" or self.report.get("acceptance_scope") != "desktop-live-continuation"
                or self.session.workspace != self.workflow.workspace
                or any(binding.get(key) != value for key, value in expected.items())
                or type(expires) not in (int, float) or not time.time() < expires <= time.time() + 30):
            raise WeatherError("Automatic continuation requires current same-owner live release evidence")
        tx = self.coordinator.transaction()
        if not tx["automatic_rotation"] or tx["source_thread"] != self.session.thread:
            raise WeatherError("Continuation is disabled or bound to another source root")

    def source_policy(self, pinned: dict) -> dict:
        policy = effective_policy(self.session.source_settings, self.workflow.workspace)
        expected = pinned["state"].get("policies", {})
        if any(expected.get(key) != value for key, value in policy.items()):
            raise WeatherError("Published policy snapshot differs from the live source; continuation cannot change immutable policies")
        return policy

    def baseline_context(self, tx: dict) -> dict:
        return {"workflow_id": self.workflow.id, "transaction_id": tx["transaction_id"],
                "generation": tx["generation"], "manifest_sha256": tx["manifest_sha256"]}

    def published_policy(self, pinned: dict) -> dict:
        values = pinned["state"].get("policies", {})
        return effective_policy({"cwd": str(self.workflow.workspace),
                                 **{wire: values[key] for key, wire in POLICY_FIELDS.items() if key in values}}, self.workflow.workspace)

    def restore_baseline(self, *, timeout: float = 5):
        """Read live evidence only for receipt-confirmed IDs; never find new IDs."""
        tx = self.coordinator.transaction()
        pinned = self.coordinator._pinned(tx)
        expected = self.published_policy(pinned)
        self.require_live(policy=expected if self.session.source_settings is None else None)
        if self.session.source_settings is not None:
            self.source_policy(pinned)
        gate = self.report.get("capabilities", {}).get("complete_live_baseline", {})
        source_turn = tx.get("source_turn_id")
        if source_turn is None and tx["phase"] == "QUIESCING":
            source_turn = tx.get("event_id")
        if (self.baseline_reader is None or gate.get("value") is not True or gate.get("status") != "VERIFIED"
                or gate.get("evidence_kind") != "live-test" or self.adapter.invalid
                or source_turn != self.session.observer.turn
                or tx["phase"] not in ("QUIESCING", "SEALED", "NEW_THREAD")):
            raise WeatherError("Continuation has no verified, consistent complete baseline recovery path")
        if any(tx.get(f"{step}_attempt") == "ambiguous" for step in ("create", "submit", "archive")):
            raise WeatherError("Live baseline cannot resolve an unreceipted remote request")
        successor, turn = tx.get("new_thread"), tx.get("turn_id")
        for step in ("create", "submit"):
            if tx.get(f"{step}_attempt") != "confirmed":
                continue
            request = {"state": pinned["state"]} if step == "create" else {"thread": successor, "prompt": self.coordinator._prompt(tx, pinned)}
            receipt = self.coordinator.journal.receipt(tx, step, request)
            if (receipt is None or receipt["thread_id"] != successor or step == "submit" and receipt["turn_id"] != turn):
                raise WeatherError("Baseline targets must match the sealed confirmed receipts")
            self.coordinator.journal.dispatch(tx, step, request, owner_binding(self.session))
        if ((successor is not None and tx.get("create_attempt") != "confirmed")
                or (turn is not None and tx.get("submit_attempt") != "confirmed")):
            raise WeatherError("Baseline target has no confirmed creation/submission")
        if (self.adapter.turn not in (None, turn)
                or self.adapter.settings is not None and self.adapter.settings != expected
                or self.adapter.creation_policy is not None and self.adapter.creation_policy != expected):
            raise WeatherError("Baseline cannot erase observed successor activity or policy drift")
        pending = self.adapter.pending_baselines.get(successor, {})
        if pending.get("invalid") or "settings" in pending and pending["settings"] != expected:
            raise WeatherError("Pending successor activity contradicts baseline recovery")
        # Bind the receipt-known target before acquisition so events arriving
        # inside the provider call retain their usual sticky conflict handling.
        self.adapter.successor = successor
        def validate():
            if self.coordinator.transaction() != tx:
                raise WeatherError("Rotation changed during baseline recovery")
            self.coordinator._pinned(tx)
            self.require_live(policy=expected if self.session.source_settings is None else None)
            if self.adapter.invalid:
                raise WeatherError("Successor activity invalidated baseline recovery")
        baseline = self.session.restore_baseline(self.baseline_reader, expected_policy=expected,
            context=self.baseline_context(tx), successor=successor, successor_turn=turn, timeout=timeout, validate=validate)
        restored = baseline.current(self.session, self.baseline_context(tx), successor, turn)
        self.adapter.successor = successor
        if restored is not None:
            self.adapter.settings = restored["settings"]
            self.adapter.turn, self.adapter.turn_status = restored["turn_id"], restored["status"]
            self.adapter.turn_started = restored["started"]
            self.adapter._restore_pending_baseline()
        return baseline

    def ensure_baseline(self, tx: dict):
        gate = self.report.get("capabilities", {}).get("complete_live_baseline", {})
        if gate.get("value") is not True or gate.get("status") != "VERIFIED" or gate.get("evidence_kind") != "live-test":
            raise WeatherError("Complete live baseline release evidence is missing")
        if self.session.live_baseline is not None:
            try:
                self.session.live_baseline.current(self.session, self.baseline_context(tx), tx.get("new_thread"), tx.get("turn_id"))
                if tx.get("new_thread") is None or self.adapter.settings is not None:
                    return
            except WeatherError:
                pass
        self.restore_baseline()

    def advance(self, *, seconds: float = 5, safe_milestone: bool = False,
                unknown_effects: list[str] | None = None, final_update: dict | None = None) -> dict:
        """One bounded tick. Repeated ticks resume; no retry on missing receipts."""
        if type(seconds) not in (int, float) or not 0 < seconds <= 30:
            raise WeatherError("Continuation tick must be greater than zero and at most 30 seconds")
        if type(safe_milestone) is not bool:
            raise WeatherError("Safe milestone must be an explicit boolean")
        if unknown_effects is not None:
            if not isinstance(unknown_effects, list) or len(unknown_effects) > 128:
                raise WeatherError("Unknown effects must be a bounded explicit list")
            for effect in unknown_effects: identifier(effect)
            if len(set(unknown_effects)) != len(unknown_effects):
                raise WeatherError("Unknown effects must have distinct operation IDs")
        tx = self.coordinator.transaction()
        if tx["phase"] in ("ACKNOWLEDGED", "RETIRED"):
            return self.result("complete")
        if self.session.source_settings is None and self.baseline_reader is not None:
            self.require_live(policy=self.published_policy(self.coordinator._pinned(tx)))
        else:
            self.require_live()
        if any(tx.get(f"{step}_attempt") == "ambiguous" for step in ("create", "submit", "archive")):
            reconciled = self.coordinator.reconcile(True, tx["transaction_id"], self.session.thread)
            if reconciled["status"] == "blocked":
                remote = RemoteReconciler(self.coordinator, self.session)
                reconciled = remote.reconcile(apply=True, transaction_id=tx["transaction_id"])
                if "create" in remote.responses:
                    self.adapter.restore_creation(remote.responses["create"])
                if reconciled["status"] == "blocked":
                    return self.result("blocked", "Original correlated response is unavailable; no request is repeated.")
            tx = self.coordinator.transaction()
        self.adapter.successor = tx.get("new_thread")
        self.session.drain()
        if self.baseline_reader is not None and tx["phase"] in ("QUIESCING", "SEALED", "NEW_THREAD"):
            self.ensure_baseline(tx)
        self.source_policy(self.workflow.bootstrap())
        if tx["phase"] in ("OBSERVING", "PREPARING", "READY"):
            observation = self.session.usage.report()
            action = observation["rotation_reason"]["action"]
            if action not in ("prepare", "rotate-at-safe-milestone"):
                return self.result("waiting", observation["rotation_reason"]["reason"])
            self.workflow.prepare(self.session.thread)
            if action != "rotate-at-safe-milestone" or not safe_milestone:
                return self.result("prepared", "Waiting for a safe milestone and validated occupancy.")
            occupancy = self.report.get("occupancy_binding", {})
            cap = self.report.get("capabilities", {}).get("reliable_active_occupancy", {})
            expires = occupancy.get("expires_at")
            expected = {"connection_id": self.session.connection_id, "thread_id": self.session.thread,
                        "turn_id": self.session.observer.turn, "model_fingerprint": observation["model_fingerprint"],
                        "context_epoch": observation["context_epoch"], "revision": observation["revision"]}
            if (any(occupancy.get(key) != value for key, value in expected.items())
                    or cap.get("value") is not True or cap.get("status") != "VERIFIED" or cap.get("evidence_kind") != "live-test"
                    or type(expires) not in (int, float) or not time.time() < expires <= time.time() + 5):
                return self.result("prepared", "Active occupancy proof is missing or stale; source is not interrupted.")
            self.coordinator.request_rotation(self.session.observer.turn)
            with self.workflow.locked():
                tx = self.coordinator.transaction()
                tx["trigger_kind"] = "watermark"
                atomic_json(self.workflow.path("rotation.json"), tx)
        with self.workflow.locked():
            tx = self.coordinator.transaction()
            if tx["phase"] in ("QUIESCING", "SEALED", "NEW_THREAD"):
                if tx.get("continuation_version") not in (None, 1):
                    raise WeatherError("Unsupported continuation transaction version")
                tx["continuation_version"] = 1
                atomic_json(self.workflow.path("rotation.json"), tx)
        if tx["phase"] == "QUIESCING":
            stopped = self.coordinator.quiesce(self.session, seconds,
                interrupt=tx.get("trigger_kind") == "watermark", descendant_reader=self.descendant_reader)
            if not stopped["observation"]["quiescent"]:
                return self.result("waiting", "Exact source terminal and complete idle inventory are required.")
            if unknown_effects is None and (stopped["observation"]["unknown_effects"]
                    or any(row["status"] == "unknown" for row in self.workflow.load_state().operations)):
                return self.result("waiting", "Unknown effects require explicit recovery tasks before seal.")
            self.coordinator.seal_quiescent(self.session, unknown_effects or [], final_update)
        tx = self.coordinator.transaction()
        if tx["phase"] in ("SEALED", "NEW_THREAD"):
            self.adapter.successor = tx.get("new_thread")
            self.coordinator.rotate(self.adapter)
            self.session.drain()
            tx = self.coordinator.transaction()
            if tx.get("successor_ack"):
                self.coordinator.acknowledge(tx["new_thread"], tx["generation"], tx["manifest_sha256"], adapter=self.adapter)
                return self.result("complete")
            return self.result("awaiting-acknowledgement", "Successor must bootstrap and request the pinned generation acknowledgement.")
        return self.result("waiting")

    def result(self, status: str, reason: str | None = None) -> dict:
        tx = self.coordinator.transaction()
        return {"schema_version": 1, "generated_at": utc_now(), "status": status, "phase": tx["phase"],
                "transaction_id": tx["transaction_id"], "generation": tx["generation"],
                "source_thread": tx["source_thread"], "successor_thread": tx["new_thread"],
                "turn_id": tx.get("turn_id"), "lease": tx["lease"], "reason": reason}


def continuation_plan(workflow, report: dict) -> dict:
    """Read-only public entry point; no owner connection or evidence import."""
    coordinator = Coordinator(workflow, report)
    tx = coordinator.transaction()
    pointer = workflow.current()
    if tx.get("generation") is not None and (tx["generation"] != pointer["generation"] or tx.get("manifest_sha256") != pointer["manifest_sha256"]):
        raise WeatherError("Continuation plan does not match the published generation")
    # Plans consume no external evidence and cannot claim execution readiness.
    workflow.bootstrap(tx.get("generation"), verify_evidence=False)
    pending = [step for step in ("create", "submit", "archive") if tx.get(f"{step}_attempt") == "ambiguous"]
    return {"schema_version": 1, "dry_run": True, "mode": mode(report), "phase": tx["phase"],
            "transaction_id": tx["transaction_id"], "generation": tx["generation"],
            "automatic_enabled": tx["automatic_rotation"], "unresolved_steps": pending,
            "successor_thread": tx["new_thread"], "acknowledgement_requested": bool(tx.get("successor_ack")),
            "will_create": False, "will_submit": False, "will_interrupt": False, "lease_transferred": False,
            "production_ready": False,
            "checkpoint_evidence_validated": False,
            "required_live_providers": ["same owner and source guards", "complete descendant/external inventory",
                                        "exact live Desktop successor attachment", "resolved effective source/successor policies"],
            "reason": "Production live providers remain unverified; this command only describes the durable transaction."}
