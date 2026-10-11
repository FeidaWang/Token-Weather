"""Native prompt/tool lease guards in a fresh trusted, disposable workspace."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import threading
import time

from .checkpoints import Workflow
from .continuation import continuation_prompt, handshake_commands
from .hook_adapter import handle
from .models import utc_now
from .native_acceptance import (ATTEMPT, Fixture, engine_args, preflight_suite, read_hook_rows,
                                snapshot, suite_marker)
from .desktop_adapter import JsonRpc
from .storage import WeatherError, append_json, atomic_json, digest, identifier, lock, owned_path, read_json


GUARD_CASES = ("prompt-quiescing", "prompt-sealed", "prompt-transferred", "source-tool-deny",
               "unrelated-tool-pass", "successor-command-deny", "successor-login-deny",
               "successor-wrong-turn-deny", "successor-handshake")


def run_guard_hook(workspace: Path, event: dict) -> dict:
    workspace = workspace.resolve(strict=True)
    registration = read_json(owned_path(workspace, "active-case.json"), {})
    name = registration.get("name")
    if (name not in GUARD_CASES or registration.get("workflow_id") != "case-" + name
            or event.get("session_id") != registration.get("session_id")
            or event.get("cwd") != str(workspace)):
        return {}
    workflow = Workflow(workspace, registration["workflow_id"])
    output = handle(workflow, event, {})
    # Commands/prompts and tool results stay out of the evidence log.
    safe = {key: event.get(key) for key in ("hook_event_name", "session_id", "cwd", "turn_id",
                                           "agent_id", "parent_thread_id", "tool_name")}
    tool_input = event.get("tool_input")
    if event.get("hook_event_name") == "PreToolUse" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        argv = []
        if isinstance(command, str):
            try:
                argv = shlex.split(command)
            except ValueError:
                pass
        tx = read_json(workflow.path("rotation.json"))
        expected = (list(handshake_commands(workflow, tx).values())
                    if type(tx.get("generation")) is int and isinstance(tx.get("manifest_sha256"), str) else [])
        safe["tool_shape"] = {"keys": sorted(key for key in tool_input if isinstance(key, str) and len(key) <= 64)[:32],
            "command_type": type(command).__name__, "login": tool_input.get("login") if type(tool_input.get("login")) is bool else None,
            "workdir_present": "workdir" in tool_input, "workdir_matches": tool_input.get("workdir") == str(workspace),
            "command_matches": command in expected, "argv_count": len(argv),
            "shell": argv[0] if argv and argv[0] in ("/bin/zsh", "/bin/bash", "/bin/sh") else None,
            "shell_mode": argv[1] if len(argv) > 1 and argv[1] in ("-c", "-lc") else None,
            "shell_body_matches": len(argv) == 3 and argv[2] in expected}
    with lock(owned_path(workspace, "evidence.lock"), timeout=.1):
        append_json(owned_path(workspace, "hook-events.jsonl"),
                    {"case": name, "event": safe, "output": output, "output_at_ns": time.monotonic_ns()})
    return output


class GuardFixture(Fixture):
    def __init__(self, workspace: Path, name: str, workflow: Workflow, thread: str):
        super().__init__()
        self.workspace, self.name, self.workflow, self.thread = workspace, name, workflow, thread
        self.effect = workspace / (name + ".effect")
        self.before = None
        self.turn = None

    def response_item(self, body: dict, request: dict) -> dict:
        index = self.requests.index(request)
        if index == 0:
            rows = [row for row in read_hook_rows(self.workspace, self.name)
                    if row["event"].get("hook_event_name") == "UserPromptSubmit"]
            if len(rows) != 1:
                raise WeatherError("Missing exact native initial prompt hook")
            self.turn = identifier(rows[0]["event"]["turn_id"])
            tx = read_json(self.workflow.path("rotation.json"))
            if self.name == "source-tool-deny":
                tx.update(phase="SEALED", lease={"thread": None, "epoch": tx["lease"]["epoch"]})
                atomic_json(self.workflow.path("rotation.json"), tx)
            elif self.name.startswith("successor-"):
                tx.update(submit_attempt="confirmed", turn_id=self.turn if self.name != "successor-wrong-turn-deny" else "other-turn")
                atomic_json(self.workflow.path("rotation.json"), tx)
            self.before = snapshot(self.workflow)
        commands = handshake_commands(self.workflow, read_json(self.workflow.path("rotation.json")))
        sequence = list(commands.values()) if self.name == "successor-handshake" else [
            commands["status"] if self.name in ("successor-login-deny", "successor-wrong-turn-deny")
            else "/usr/bin/printf guard > " + shlex.quote(str(self.effect))]
        if index >= len(sequence):
            return {"type": "message", "id": "msg_guard", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "Native guard case completed.", "annotations": []}]}
        names = [member.get("name") for tool in body.get("tools", []) for member in tool.get("tools", [tool])]
        tool = next((name for name in ("exec_command", "shell_command") if name in names), None)
        if tool is None:
            raise WeatherError("Native fixture has no supported command tool")
        args = {"cmd" if tool == "exec_command" else "command": sequence[index],
                "login": self.name == "successor-login-deny", "workdir": str(self.workspace), "max_output_tokens": 2048}
        if tool == "shell_command":
            args.pop("max_output_tokens")
            args["timeout_ms"] = 5000
        return {"type": "function_call", "id": "fc_guard_" + str(index), "call_id": "call_guard_" + str(index),
                "name": tool, "arguments": json.dumps(args), "status": "completed"}


def assess_guard_case(case: dict) -> dict:
    errors = []
    name = case.get("name")
    prompt = isinstance(name, str) and name.startswith("prompt-")
    handshake = name == "successor-handshake"
    passed_tool = name == "unrelated-tool-pass" or handshake
    event = "UserPromptSubmit" if prompt else "PreToolUse"
    native_event = "userPromptSubmit" if prompt else "preToolUse"
    rows = [row for row in case.get("hook_rows", []) if
            row.get("event", {}).get("hook_event_name") == event
            and row["event"].get("session_id") == case.get("thread_id")
            and row["event"].get("turn_id") == case.get("turn_id")
            and row["event"].get("cwd") == case.get("workspace")
            and not row["event"].get("agent_id") and not row["event"].get("parent_thread_id")]
    native = [run for run in case.get("hooks", []) if run.get("eventName") == native_event
              and run.get("sourcePath") == case.get("hook_source") and run.get("turn_id") == case.get("turn_id")
              and run.get("status") in (("completed",) if passed_tool else ("completed", "blocked", "stopped"))]
    expected = 3 if handshake else 1
    if len(rows) != expected or len(native) != expected:
        errors.append("Missing exact prompt/tool hook outputs and native successful completions")
    if (name not in GUARD_CASES or case.get("errors") or case.get("incomplete")
            or not case.get("terminal_identity_matches") or not case.get("terminal_event")
            or case.get("turn_status") not in ("completed", "interrupted") or case.get("controller_interrupted") is not False):
        errors.append("Exact native terminal evidence is missing or inconsistent")
    if case.get("generation_unchanged") is not True or case.get("lease_unchanged") is not True:
        errors.append("Guard must preserve the pinned generation and write lease")
    tools = case.get("tools", [])
    if prompt:
        if not rows or any(row.get("output", {}).get("decision") != "block" for row in rows):
            errors.append("The production prompt guard did not block")
        if case.get("fixture_requests") != 0 or tools or case.get("effect_exists") is not False:
            errors.append("A rejected prompt reached the model or a command effect")
    elif not passed_tool:
        if not rows or any(row.get("output", {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny" for row in rows):
            errors.append("The production tool guard did not deny")
        if case.get("fixture_requests") != 2 or case.get("effect_exists") is not False or any(tool.get("exit_code") == 0 for tool in tools):
            errors.append("A denied command ran or the expected fixture exchange is missing")
    else:
        if any(row.get("output") != {} for row in rows):
            errors.append("Allowed command did not pass the production guard")
        if (case.get("fixture_requests") != expected + 1 or len(tools) != expected
                or any(tool.get("exit_code") != 0 or tool.get("status") != "completed" for tool in tools)):
            errors.append("Allowed command lacks successful native execution evidence")
        if handshake:
            if case.get("bootstrap_matches_pin") is not True or case.get("ack_matches_pin") is not True or case.get("phase") != "NEW_THREAD":
                errors.append("Pinned bootstrap/ack did not execute or a lease was granted prematurely")
        elif case.get("effect_exists") is not True or case.get("effect_contents_match") is not True:
            errors.append("The unrelated-root command effect was not observed")
    if case.get("workflow_unchanged") is not True and not handshake:
        errors.append("A guard unexpectedly mutated the workflow")
    return {"status": "VERIFIED" if not errors else "NOT TESTED", "failures": errors}


def run_guard_case(engine: Path, workspace: Path, name: str) -> dict:
    case = {"name": name, "workspace": str(workspace), "hook_source": str(workspace / ".codex/hooks.json"),
            "hooks": [], "tools": [], "errors": [], "terminal_event": False, "controller_interrupted": False}
    server = serving = fixture = None
    try:
        # Start the endpoint before the engine; the workflow is bound after native creation.
        holder = {}
        class DispatchFixture(Fixture):
            def response_item(self, body, request):
                actual = holder["fixture"]
                actual.requests = self.requests
                return actual.response_item(body, request)
        dispatch = DispatchFixture()
        server = ThreadingHTTPServer(("127.0.0.1", 0), dispatch.handler())
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        with JsonRpc(engine_args(engine, f"http://127.0.0.1:{server.server_port}/v1", True), cwd=workspace) as rpc:
            rpc.initialize()
            result = rpc.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                "baseInstructions": "Isolated deterministic guard acceptance.", "developerInstructions": "",
                "approvalPolicy": "never", "sandbox": "workspace-write"}, timeout=10)
            thread = identifier(result["thread"]["id"])
            case["thread_id"] = thread
            successor = name.startswith("successor-")
            enrolled = "isolated-source" if successor or name == "unrelated-tool-pass" else thread
            wf = Workflow(workspace, "case-" + name)
            wf.initialize(enrolled, "Verify lease guard.")
            wf.prepare(enrolled)
            tx = read_json(wf.path("rotation.json"))
            tx["phase"] = "OBSERVING"
            if name in ("prompt-quiescing", "prompt-sealed"):
                tx["phase"] = "QUIESCING" if name == "prompt-quiescing" else "SEALED"
            if name == "prompt-transferred":
                tx["successors"] = {thread: "isolated-successor"}
                tx["lease"]["thread"] = "isolated-successor"
            if successor:
                owner = wf.check_owner()
                owner["enrolled_root_threads"].append(thread)
                atomic_json(wf.path("ownership.json"), owner)
                tx.update(phase="NEW_THREAD", new_thread=thread, continuation_version=1, submit_attempt="ambiguous",
                          lease={"thread": None, "epoch": tx["lease"]["epoch"]})
            atomic_json(wf.path("rotation.json"), tx)
            fixture = GuardFixture(workspace, name, wf, thread)
            holder["fixture"] = fixture
            before = snapshot(wf)
            atomic_json(workspace / "active-case.json", {"name": name, "session_id": thread, "workflow_id": wf.id})
            prompt = continuation_prompt(wf, tx, wf.bootstrap()) if successor else "Run the isolated guard case."
            turn = rpc.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}]}, timeout=10)["turn"]["id"]
            case["turn_id"] = turn
            deadline = time.monotonic() + 20
            for _ in range(500):
                message = rpc.next_notification(max(0, deadline - time.monotonic()))
                method, params = message.get("method"), message.get("params", {})
                if params.get("threadId") != thread:
                    continue
                if method == "hook/completed":
                    run = params.get("run", {})
                    case["hooks"].append({**{key: run.get(key) for key in ("eventName", "status", "sourcePath")},
                                          "turn_id": params.get("turnId")})
                elif method == "item/completed" and params.get("turnId") == turn:
                    item = params.get("item", {})
                    if item.get("type") == "commandExecution":
                        text = item.get("aggregatedOutput") or ""
                        case["tools"].append({"item_id": item.get("id"), "status": item.get("status"),
                                              "exit_code": item.get("exitCode"), "output_sha256": digest(text.encode())})
                        if name == "successor-handshake" and len(case["tools"]) == 1:
                            case["bootstrap_matches_pin"] = (f"Pinned generation: {tx['generation']}" in text
                                                              and tx["manifest_sha256"] in text)
                elif method == "error":
                    case["errors"].append("Native error event")
                elif method == "turn/completed" and params.get("turn", {}).get("id") == turn:
                    terminal = params["turn"]
                    case.update(terminal_event=True, terminal_identity_matches=True, turn_status=terminal.get("status"))
                    if terminal.get("error"):
                        case["errors"].append("Native terminal error")
                    break
            else:
                raise WeatherError("Native guard event limit reached")
            actual_before = fixture.before or before
            after, final = snapshot(wf), read_json(wf.path("rotation.json"))
            case.update(workflow_unchanged=actual_before == after, generation_unchanged=actual_before["current.json"] == after["current.json"],
                        lease_unchanged=final["lease"] == ({"thread": None, "epoch": tx["lease"]["epoch"]} if name == "source-tool-deny" else tx["lease"]),
                        phase=final["phase"], fixture_requests=len(dispatch.requests), effect_exists=fixture.effect.exists(),
                        effect_contents_match=fixture.effect.read_bytes() == b"guard" if fixture.effect.exists() else False)
            if name == "successor-handshake":
                case["ack_matches_pin"] = final.get("successor_ack") == {"thread_id": thread, "turn_id": turn,
                    "generation": tx["generation"], "manifest_sha256": tx["manifest_sha256"], "transaction_id": tx["transaction_id"]}
    except (WeatherError, OSError, ValueError, KeyError, TypeError) as exc:
        case.update(incomplete=True)
        case["errors"].append(str(exc))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if serving is not None:
            serving.join(timeout=2)
    case["hook_rows"] = read_hook_rows(workspace, name)
    case.update(assess_guard_case(case))
    return case


def run_guard_suite(engine: Path, workspace: Path) -> dict:
    marker = suite_marker(workspace)
    workspace, engine = workspace.resolve(strict=True), engine.resolve(strict=True)
    report = {"schema_version": 1, "kind": "native-execution-guards", "generated_at": utc_now(),
              "acceptance_scope": "isolated-native", "suite_id": marker["suite_id"], "workspace": str(workspace),
              "runtime_source_sha256": marker["runtime_source_sha256"], "status": "NOT TESTED", "cases": [],
              "simulated": ["Workflow rotation phases/enrollment", "Create/submit receipts for successor tool tests",
                            "Live release/inventory/attachment and final controller acknowledgement are not exercised"],
              "prompt_ownership_guard": "NOT TESTED", "tool_ownership_guard": "NOT TESTED",
              "native_bootstrap_ack_request": "NOT TESTED", "production_promotion": False,
              "same_desktop_server": False, "trust_modified": False, "paid_model_requests": 0}
    with lock(workspace / "suite.lock", timeout=.1):
        if (workspace / ATTEMPT).exists() or (workspace / "active-case.json").exists():
            raise WeatherError("Suite has already started; prepare a fresh workspace")
        report["preflight"] = preflight_suite(engine, workspace)
        if report["preflight"].get("engine_version") != "codex-cli 0.160.0":
            report.update(reason_code="engine-version-mismatch", reason="Native guard cases require the pinned 0.160.0 engine")
            return report
        if not report["preflight"]["ready"]:
            report["reason_code"] = report["preflight"]["reason_code"]
            report["reason"] = report["preflight"]["reason"]
            return report
        attempt = {"suite_id": marker["suite_id"], "started_at": utc_now(), "status": "started", "cases_started": []}
        atomic_json(workspace / ATTEMPT, attempt)
        for name in GUARD_CASES:
            attempt["cases_started"].append(name)
            atomic_json(workspace / ATTEMPT, attempt)
            report["cases"].append(run_guard_case(engine, workspace, name))
        passed = {case["name"] for case in report["cases"] if case["status"] == "VERIFIED"}
        for field, needed in (("prompt_ownership_guard", set(GUARD_CASES[:3])),
                              ("tool_ownership_guard", set(GUARD_CASES[3:])),
                              ("native_bootstrap_ack_request", {"successor-handshake"})):
            if needed <= passed:
                report[field] = "VERIFIED"
        report["status"] = "VERIFIED" if passed == set(GUARD_CASES) else "NOT TESTED"
        report["reason_code"] = "verified" if report["status"] == "VERIFIED" else "native-cases-incomplete"
        attempt.update(status="finished", finished_at=utc_now(), result=report["status"])
        atomic_json(workspace / ATTEMPT, attempt)
    return report
