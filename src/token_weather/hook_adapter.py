"""Critical path: local bounded files only, no model/network/controller waits."""
from __future__ import annotations

from pathlib import Path
import json
import sys

from .checkpoints import Workflow
from .coordinator import Coordinator
from .storage import WeatherError, read_json, identifier, atomic_json


def handle(workflow: Workflow, event: dict, report: dict) -> dict:
    try:
        owner = workflow.check_owner()
        tx = read_json(workflow.path("rotation.json"))
        cwd = event.get("cwd")
        if (not isinstance(cwd, str) or not Path(cwd).is_absolute()
                or Path(cwd).resolve() != workflow.workspace
                or event.get("session_id") not in owner.get("enrolled_root_threads", [owner["source_thread"]])
                or event.get("agent_id") or event.get("is_subagent") or event.get("parent_thread_id")):
            return {}
        name = event.get("hook_event_name")
        successor = tx.get("successors", {}).get(event["session_id"])
        if name == "UserPromptSubmit" and successor:
            return {"decision": "block", "reason": f'Workflow is owned by successor {successor}; use the controller recovery override if needed.'}
        if (name == "UserPromptSubmit" and event["session_id"] == tx["source_thread"]
                and tx["phase"] in ("QUIESCING", "SEALED", "NEW_THREAD")):
            return {"decision": "block", "reason": "The source workflow is quiescing/sealed; finish or explicitly recover the current handover before submitting another turn."}
        if (name == "UserPromptSubmit" and tx.get("continuation_version") == 1
                and event["session_id"] == tx.get("new_thread") and tx["phase"] == "NEW_THREAD"):
            from .continuation import continuation_prompt
            turn = event.get("turn_id")
            initial_turn = (isinstance(turn, str) and bool(turn) and
                            (tx.get("submit_attempt") == "ambiguous" or tx.get("submit_attempt") == "confirmed" and turn == tx.get("turn_id")))
            if (initial_turn and event.get("prompt") == continuation_prompt(
                    workflow, tx, workflow.bootstrap(tx["generation"], verify_evidence=False))):
                return {}
            return {"decision": "block", "reason": "Successor initialization is pending; wait for the controller write lease."}
        if name == "PreToolUse" and (tx["lease"]["thread"] != event["session_id"] or tx["phase"] in ("QUIESCING", "SEALED", "NEW_THREAD")):
            from .continuation import allow_handshake_tool
            if allow_handshake_tool(workflow, tx, event):
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "Workflow write lease is sealed/transferred; reconcile through the controller."}}
        if name != "PreCompact" or event.get("trigger") != "auto" or event.get("session_id") != tx["source_thread"]:
            return {}
        caps = report.get("capabilities", {})
        verified = all(caps.get(k, {}).get("value") is True and caps[k].get("status") == "VERIFIED" and caps[k].get("evidence_kind") == "live-test"
                       for k in ("trusted_hook_execution", "automatic_precompact_cancellation", "root_only_precompact"))
        if not tx["automatic_rotation"] or not verified:
            return {"systemMessage": "Token Weather saved checkpoints; automatic interception is disabled or unverified."}
        if workflow.path("pending-publication.json").exists():
            raise WeatherError("A semantic checkpoint was not completely published; allow native compaction and recover it outside the hook")
        turn = event.get("turn_id")
        if not isinstance(turn, str) or not turn:
            return {}
        current = workflow.current()
        if tx["generation"] != current["generation"]:
            raise WeatherError("Prepared generation is stale")
        # Hash just the small prepared bootstrap. Evidence was checked in prepare.
        workflow.bootstrap(tx["generation"], verify_evidence=False)
        Coordinator(workflow, report).request_rotation(turn)
        return {"continue": False, "stopReason": "Handover checkpoint committed; rotating to a fresh conversation."}
    except (WeatherError, OSError, ValueError, KeyError) as exc:
        print(f"Token Weather: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"systemMessage": "Handover gate unavailable; keeping the last good generation and allowing native compaction."}


def stdin_event() -> dict:
    raw = sys.stdin.buffer.read(65_537)
    if len(raw) > 65_536:
        raise WeatherError("Hook input exceeds 64 KiB")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise WeatherError("Hook input must be one JSON object")
    return result
