"""Native child-session evidence using an ephemeral, loopback-only fixture."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import subprocess

from .checkpoints import Workflow
from .desktop_adapter import JsonRpc
from .models import utc_now
from .native_acceptance import Fixture, engine_args, read_hook_rows, runtime_sources, snapshot, suite_marker
from .storage import WeatherError, atomic_json, identifier, read_json


class IsolationFixture(Fixture):
    """Do not use prompt_cache_key for identity: children share their root's key."""
    def __init__(self, child_model: str = "gpt-5.6-sol"):
        super().__init__()
        self.root: str | None = None
        self.child: str | None = None
        self.child_model = child_model
        self.child_ready = threading.Event()
        self.errors: list[str] = []

    def fail(self, reason: str):
        self.errors.append(reason)
        raise WeatherError(reason)

    def register_child(self, child: str) -> None:
        identifier(child)
        if child == self.root or self.child not in (None, child):
            self.fail("Native spawn did not identify one distinct child")
        self.child = child
        self.child_ready.set()

    def request_metadata(self, path: str, size: int, headers) -> dict:
        thread = headers.get("thread-id")
        parent = headers.get("x-codex-parent-thread-id")
        try:
            identifier(thread)
        except (WeatherError, TypeError):
            self.fail("Fixture request lacks a bounded native thread identity")
        if self.root is None or (thread != self.root and parent != self.root) or (thread == self.root and parent):
            self.fail("Fixture request is outside the native parent/child pair")
        if self.child is not None and thread not in (self.root, self.child):
            self.fail("Fixture received an unrelated child request")
        return {**super().request_metadata(path, size, headers), "thread_id": thread,
                "parent_thread_id": parent, "ordinal": 1 + sum(row["thread_id"] == thread for row in self.requests)}

    def function(self, body: dict, name: str, args: dict, call: str) -> dict:
        namespace = None
        found = False
        for tool in body.get("tools", []):
            for member in tool.get("tools", [tool]):
                if member.get("name") == name:
                    found = True
                    namespace = tool.get("name") if "tools" in tool else None
        if not found:
            self.fail("Native parent does not expose the required collaboration tool")
        item = {"type": "function_call", "id": "fc_" + call, "call_id": call,
                "name": name, "arguments": json.dumps(args), "status": "completed"}
        if namespace:
            item["namespace"] = namespace
        return item

    def response_item(self, body: dict, request: dict) -> dict:
        thread, ordinal = request["thread_id"], request["ordinal"]
        if thread == self.root and ordinal == 1:
            return self.function(body, "spawn_agent", {"message": "Execute the isolated harmless fixture step.",
                "model": self.child_model, "fork_context": False}, "tw_spawn")
        if thread == self.root and ordinal == 2:
            targets = []
            for item in body.get("input", []):
                if item.get("type") == "function_call_output" and item.get("call_id") == "tw_spawn":
                    try:
                        target = json.loads(item["output"])["agent_id"]
                        identifier(target)
                        targets.append(target)
                    except (WeatherError, ValueError, KeyError, TypeError):
                        self.fail("Native spawn output did not identify its child")
            if len(targets) != 1 or (self.child is not None and targets[0] != self.child):
                self.fail("Native spawn output and notification disagree")
            return self.function(body, "wait_agent", {"targets": targets, "timeout_ms": 10000}, "tw_wait")
        if thread != self.root:
            if not self.child_ready.wait(timeout=3) or thread != self.child:
                self.fail("Child fixture was not bound to the native spawn notification")
            if ordinal == 1:
                # The installed engine's Responses-lite child omits tools from
                # its request but still dispatches this registered native tool.
                return {"type": "function_call", "id": "fc_tw_child", "call_id": "tw_child",
                        "name": "exec_command", "arguments": json.dumps({"cmd": "/usr/bin/true",
                            "login": False, "max_output_tokens": 32, "yield_time_ms": 1000}), "status": "completed"}
        return {"type": "message", "id": "msg_isolation", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Local fixture completed.", "annotations": []}]}

    def response_usage(self, request: dict) -> dict:
        usage = super().response_usage(request)
        if request["thread_id"] != self.root and request["ordinal"] == 1:
            # Artificial usage induces the child's automatic step compaction.
            # This is instrumentation, never a token-cost measurement.
            usage.update(input_tokens=95000, total_tokens=95010)
        return usage


def assess_isolation(case: dict, require_hooks: bool) -> dict:
    failures = []
    root, child, turn = case.get("thread_id"), case.get("child_thread_id"), case.get("child_turn_id")
    spawn = case.get("spawn", {})
    if (not root or not child or root == child or spawn.get("sender_thread_id") != root
            or spawn.get("receiver_thread_ids") != [child] or spawn.get("status") != "completed"
            or spawn.get("tool") != "spawnAgent" or spawn.get("turn_id") != case.get("turn_id")):
        failures.append("Missing matching native parent/child spawn evidence")
    child_requests = [row for row in case.get("fixture_metadata", []) if row.get("thread_id") == child]
    if not child_requests or any(row.get("parent_thread_id") != root for row in child_requests):
        failures.append("Child requests do not agree with the native parent identity")
    if any(row.get("thread_id") not in (root, child) or (row.get("thread_id") == root and row.get("parent_thread_id"))
           for row in case.get("fixture_metadata", [])):
        failures.append("Fixture metadata includes requests outside the native pair")
    for label, identity, expected_turn in (("root", root, case.get("turn_id")), ("child", child, turn)):
        terminal = case.get(label + "_terminal", {})
        if (not expected_turn or terminal.get("thread_id") != identity or terminal.get("turn_id") != expected_turn
                or terminal.get("status") != "completed" or terminal.get("error")):
            failures.append("Missing successful native terminal event for the exact " + label + " turn")
    compactions = case.get("child_compactions", [])
    if not compactions or any(row.get("thread_id") != child or row.get("turn_id") != turn for row in compactions):
        failures.append("Missing native compaction for the exact child turn")
    if case.get("controller_interrupted") or case.get("errors") or case.get("incomplete"):
        failures.append("Native child evidence was interrupted, failed or incomplete")
    if require_hooks:
        if case.get("workflow_unchanged") is not True:
            failures.append("Child control changed the enrolled parent's workflow")
        # The installed hook wire format keeps the parent session_id and puts
        # the executing child in agent_id. Native notifications use the child's
        # threadId instead; require both identities rather than rewriting either.
        rows = [row for row in case.get("hook_rows", []) if row.get("event", {}).get("session_id") == root
                and row["event"].get("agent_id") == child
                and row["event"].get("turn_id") == turn and row["event"].get("cwd") == case.get("workspace")
                and row["event"].get("hook_event_name") == "PreCompact" and row["event"].get("trigger") == "auto"]
        native = [run for run in case.get("hooks", []) if run.get("thread_id") == child and run.get("turn_id") == turn
                  and run.get("sourcePath") == case.get("hook_source") and run.get("eventName") == "preCompact"]
        if not rows or not native or any(row.get("output") != {} for row in rows) or any(run.get("status") != "completed" for run in native):
            failures.append("Missing successful pass-through of the exact child's automatic hook")
    elif case.get("hooks"):
        failures.append("The no-hook subagent control executed hooks")
    return {"status": "VERIFIED" if not failures else "NOT TESTED", "failures": failures}


def run_isolation(engine: Path, workspace: Path, require_hooks: bool) -> dict:
    workspace = workspace.resolve(strict=True)
    if require_hooks:
        suite_marker(workspace)
    fixture = IsolationFixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    case = {"name": "child-auto-pass", "workspace": str(workspace), "hook_source": str(workspace / ".codex/hooks.json"),
            "hooks": [], "child_compactions": [], "errors": [], "controller_interrupted": False,
            "child_model_label": fixture.child_model, "synthetic_usage": True}
    try:
        args = engine_args(engine, f"http://127.0.0.1:{server.server_port}/v1", True)
        if not require_hooks:
            args.extend(["-c", "hooks={}"])
        with JsonRpc(args, cwd=workspace) as rpc:
            rpc.initialize()
            root = identifier(rpc.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                "baseInstructions": "Local subagent fixture control.", "developerInstructions": "",
                "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"])
            fixture.root = root
            case["thread_id"] = root
            if require_hooks:
                workflow = Workflow(workspace, "case-child-auto-pass")
                workflow.initialize(root, "Verify child compaction leaves the enrolled root unchanged.")
                workflow.prepare(root)
                tx = read_json(workflow.path("rotation.json"))
                tx["automatic_rotation"] = True
                atomic_json(workflow.path("rotation.json"), tx)
                before = snapshot(workflow)
                registration = {"name": "child-auto-pass", "session_id": root, "workflow_id": workflow.id}
                atomic_json(workspace / "active-case.json", registration)
            case["turn_id"] = rpc.request("turn/start", {"threadId": root,
                "input": [{"type": "text", "text": "Run the local child-session fixture."}]})["turn"]["id"]
            deadline = time.monotonic() + 20
            for _ in range(500):
                message = rpc.next_notification(max(0, deadline - time.monotonic()))
                method, params = message.get("method"), message.get("params", {})
                thread = params.get("threadId")
                if method == "error" and thread in (None, root, fixture.child):
                    raise WeatherError("Native child control error event")
                if thread not in (root, fixture.child) or thread is None:
                    continue
                item = params.get("item", {})
                if method == "item/completed" and item.get("type") == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
                    if case.get("spawn") or thread != root or params.get("turnId") != case["turn_id"]:
                        raise WeatherError("Duplicate or mismatched native spawn notification")
                    receivers = item.get("receiverThreadIds", [])
                    if item.get("status") != "completed" or item.get("senderThreadId") != root or len(receivers) != 1:
                        raise WeatherError("Native spawn failed to identify exactly one child")
                    child = identifier(receivers[0])
                    case.update(child_thread_id=child, spawn={"sender_thread_id": root, "receiver_thread_ids": [child],
                        "status": item["status"], "tool": item["tool"], "turn_id": params.get("turnId")})
                    if require_hooks:
                        registration["child_session_id"] = child
                        atomic_json(workspace / "active-case.json", registration)
                    fixture.register_child(child)
                elif method == "turn/started" and thread == fixture.child:
                    child_turn = params.get("turn", {}).get("id")
                    if case.get("child_turn_id") not in (None, child_turn):
                        raise WeatherError("Unexpected second child turn")
                    case["child_turn_id"] = identifier(child_turn)
                elif method == "hook/completed":
                    run = params.get("run", {})
                    case["hooks"].append({**{key: run.get(key) for key in ("eventName", "status", "sourcePath", "durationMs")},
                                          "thread_id": thread, "turn_id": params.get("turnId")})
                elif method == "item/completed" and item.get("type") == "contextCompaction" and thread == fixture.child:
                    case["child_compactions"].append({"thread_id": thread, "turn_id": params.get("turnId")})
                elif method == "turn/completed":
                    terminal = params.get("turn", {})
                    label = "root" if thread == root else "child"
                    expected = case["turn_id"] if label == "root" else case.get("child_turn_id")
                    if terminal.get("id") != expected:
                        raise WeatherError("Native terminal event has an unexpected turn identity")
                    case[label + "_terminal"] = {"thread_id": thread, "turn_id": terminal.get("id"),
                                                 "status": terminal.get("status"), "error": bool(terminal.get("error"))}
                    if case.get("root_terminal") and case.get("child_terminal"):
                        break
                if time.monotonic() >= deadline:
                    raise WeatherError("Native child control deadline reached")
            else:
                raise WeatherError("Native child control event limit reached")
            if require_hooks:
                case["workflow_unchanged"] = before == snapshot(workflow)
    except (WeatherError, OSError, ValueError, KeyError, TypeError) as exc:
        case["incomplete"] = True
        case["errors"].append(str(exc))
    finally:
        fixture.child_ready.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    case["errors"].extend(fixture.errors)
    case["fixture_metadata"] = fixture.requests
    case["fixture_requests"] = len(fixture.requests)
    if require_hooks:
        case["hook_rows"] = read_hook_rows(workspace, case["name"])
    case.update(assess_isolation(case, require_hooks))
    return case


def probe_subagent(engine: Path) -> dict:
    engine = engine.resolve(strict=True)
    try:
        version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise WeatherError("Subagent control engine version probe timed out") from exc
    if version.returncode or not version.stdout.strip():
        raise WeatherError("Subagent control engine version probe failed")
    with tempfile.TemporaryDirectory(prefix="token-weather-subagent-") as temporary:
        case = run_isolation(engine, Path(temporary), require_hooks=False)
    return {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "isolated-native",
            "kind": "no-hook-subagent-control", "engine_path": str(engine), "engine_version": version.stdout.strip(),
            "runtime_source_sha256": runtime_sources(), "status": case["status"],
            "case": case, "paid_model_requests": 0, "trust_modified": False, "production_promotion": False,
            "native_subagent_isolation": "NOT TESTED", "root_only_precompact": "NOT TESTED",
            "note": "Real child execution/compaction instrumentation only; no hooks or cancellation proof."}
