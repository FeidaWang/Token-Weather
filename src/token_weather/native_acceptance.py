"""Isolated native acceptance. Test bootstrap evidence never enables production.

New project hooks require normal runtime trust review. This module does not write
trust, start a Desktop daemon, submit to Desktop, or call a paid model.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import tempfile
import time
import uuid

from .checkpoints import Workflow
from .desktop_adapter import JsonRpc
from .hook_adapter import handle
from .models import utc_now
from .native_probe import assess_hooks, toml_value
from .storage import (WeatherError, atomic_json, append_json, canonical_json,
                      digest, identifier, lock, owned_path, read_json, read_bytes)

ROOT = Path(__file__).resolve().parents[2]
MARKER = "native-acceptance.json"
CASES = ("root-auto-stop", "disabled-auto", "unrelated-root-auto", "corrupt-auto", "manual-pass", "child-auto-pass")
TRACKED = ("current.json", "rotation.json", "ownership.json", "ledger.jsonl")
ATTEMPT = "run-attempt.json"


def runtime_sources() -> dict:
    """Pin the helper and its local imports, including the production gate."""
    paths = [ROOT / "scripts/native-acceptance-hook.py", *sorted((ROOT / "src/token_weather").glob("*.py"))]
    return {str(path.relative_to(ROOT)): digest(read_bytes(path)) for path in paths}


def configuration(workspace: Path, interpreter: Path, helper: Path, suite_type: str = "compaction") -> dict:
    command = shlex.join([str(interpreter), str(helper), "--suite", str(workspace)])
    if suite_type == "execution-guards":
        return {"hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": 2}]}]
                          for event in ("UserPromptSubmit", "PreToolUse")}}
    return {"hooks": {"PreCompact": [{"matcher": "^(auto|manual)$", "hooks": [
        {"type": "command", "command": command, "timeout": 2}]}]}}


def prepare_suite(directory: Path, interpreter: Path | None = None, *, suite_type: str = "compaction") -> dict:
    if suite_type not in ("compaction", "execution-guards"):
        raise WeatherError("Unknown native acceptance suite type")
    directory = directory.absolute()
    if directory.exists() or directory.is_symlink():
        raise WeatherError("Acceptance workspace must be a new directory")
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    workspace = directory.resolve(strict=True)
    interpreter = (interpreter or Path(sys.executable)).resolve(strict=True)
    helper = ROOT / "scripts/native-acceptance-hook.py"
    config = configuration(workspace, interpreter, helper, suite_type)
    atomic_json(owned_path(workspace, ".codex", "hooks.json"), config)
    marker = {"schema_version": 2, "kind": "isolated-native-acceptance", "workspace": str(workspace),
              "suite_id": str(uuid.uuid4()), "created_at": utc_now(),
              "configuration": config, "config_sha256": digest(canonical_json(config)),
              "helper_sha256": digest(read_bytes(helper)),
              "runtime_source_sha256": runtime_sources(),
              "test_capabilities_simulated": True, "production_promotion": False}
    if suite_type != "compaction":
        marker["suite_type"] = suite_type
    atomic_json(workspace / MARKER, marker)
    return {"workspace": str(workspace), "config_sha256": marker["config_sha256"],
            "trust_modified": False, "automatic_rotation": False,
            "suite_type": suite_type,
            "next_action": "Review only this disposable workspace's exact hooks through the bundled Codex /hooks flow; then run native-acceptance."}


def suite_marker(workspace: Path) -> dict:
    if workspace.is_symlink():
        raise WeatherError("Acceptance workspace cannot be a symlink")
    workspace = workspace.resolve(strict=True)
    marker = read_json(workspace / MARKER)
    if (not isinstance(marker, dict) or marker.get("kind") != "isolated-native-acceptance"
            or marker.get("workspace") != str(workspace) or marker.get("production_promotion") is not False):
        raise WeatherError("Not an isolated native acceptance workspace")
    config = read_json(owned_path(workspace, ".codex", "hooks.json"))
    if marker.get("suite_type", "compaction") not in ("compaction", "execution-guards"):
        raise WeatherError("Unknown acceptance suite type")
    if config != marker.get("configuration") or digest(canonical_json(config)) != marker.get("config_sha256"):
        raise WeatherError("Acceptance hook definition changed; prepare and review a fresh suite")
    if marker.get("helper_sha256") != digest(read_bytes(ROOT / "scripts/native-acceptance-hook.py")):
        raise WeatherError("Acceptance helper changed; prepare a fresh suite")
    if marker.get("schema_version") != 2 or marker.get("runtime_source_sha256") != runtime_sources():
        raise WeatherError("Acceptance runtime sources changed or were not pinned; prepare and review a fresh suite")
    return marker


def assess_readiness(proof: dict, config: dict, requirements: dict, workspace: Path) -> dict:
    """Reduce native metadata to a bounded diagnostic without storing raw config."""
    layers = config.get("layers")
    matches = [layer for layer in layers or [] if isinstance(layer, dict)
               and layer.get("name", {}).get("type") == "project"
               and layer["name"].get("dotCodexFolder") == str(workspace / ".codex")]
    layer_state = "unknown"
    if len(matches) == 1:
        layer_state = "disabled" if matches[0].get("disabledReason") else "active"
    managed_only = (requirements.get("requirements") or {}).get("allowManagedHooksOnly")
    if managed_only is True:
        code, reason = "managed-hooks-only", "Managed policy permits only managed hooks; this disposable project hook cannot run."
    elif layer_state == "disabled":
        code, reason = "project-layer-disabled", "The disposable project's config layer is disabled. Review project trust in the bundled Codex CLI before reviewing /hooks."
    elif not proof["parser_accepted"]:
        code = "hook-definition-mismatch" if proof["recognized_hooks"] else "hook-not-discovered"
        reason = "Native discovery did not recognize exactly the disposable hook definition and source path."
    elif not proof["all_hooks_enabled"]:
        code, reason = "hook-disabled", "The disposable hook is disabled; inspect this project's /hooks entry."
    elif not proof["all_hooks_trusted"]:
        code, reason = "hook-review-required", "Review and trust the exact disposable hook definition through /hooks."
    else:
        code, reason = "ready", "The exact disposable hook is recognized, enabled and trusted; native cases may run."
    return {"ready": code == "ready", "reason_code": code, "reason": reason,
            "project_layer": {"state": layer_state, "matching_layers": len(matches)},
            "allow_managed_hooks_only": managed_only if type(managed_only) is bool else None}


def preflight_suite(engine: Path, workspace: Path) -> dict:
    """Read-only readiness check. Never execute hooks or create native threads."""
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "isolated-native",
              "kind": "native-acceptance-preflight", "status": "NOT TESTED", "ready": False,
              "reason_code": "invalid-suite", "rpc_methods": [], "model_requests": 0,
              "hooks_executed": False, "trust_modified": False, "production_promotion": False,
              "same_desktop_server": False}
    try:
        marker = suite_marker(workspace)
        workspace = workspace.resolve(strict=True)
        engine = engine.resolve(strict=True)
        report.update(workspace=str(workspace), engine_path=str(engine), suite_id=marker["suite_id"],
                      config_sha256=marker["config_sha256"], runtime_source_sha256=marker["runtime_source_sha256"])
        report["review"] = {"cwd": str(workspace), "hook_source": str(workspace / ".codex/hooks.json"),
                            "launch_argv": [str(engine), "-C", str(workspace)], "hook_command": "/hooks",
                            "expected_events": list(marker["configuration"]["hooks"]),
                            "expected_matcher": "" if marker.get("suite_type") == "execution-guards" else "^(auto|manual)$"}
        if (workspace / ATTEMPT).exists() or (workspace / "active-case.json").exists():
            report.update(reason_code="suite-already-started", reason="This suite has already started; prepare a fresh workspace to avoid repeating ambiguous native effects.")
            return report
        report["reason_code"] = "engine-probe-failed"
        version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=3)
        if version.returncode or not version.stdout.strip():
            raise WeatherError("Engine version probe failed")
        report["engine_version"] = version.stdout.strip()
        report["reason_code"] = "metadata-probe-failed"
        with JsonRpc([str(engine), "app-server", "--stdio", "--strict-config"], cwd=workspace) as rpc:
            report["rpc_methods"].append("initialize")
            rpc.initialize()
            report["rpc_methods"].append("config/read")
            config = rpc.request("config/read", {"cwd": str(workspace), "includeLayers": True})
            report["rpc_methods"].append("configRequirements/read")
            requirements = rpc.request("configRequirements/read", {})
            report["rpc_methods"].append("hooks/list")
            metadata = rpc.request("hooks/list", {"cwds": [str(workspace)]})
        proof = assess_hooks(metadata, marker["configuration"], workspace, "project")
        report["hook_preflight"] = proof
        report.update(assess_readiness(proof, config, requirements, workspace))
        # Metadata must not be used after local sources/config changed during RPC.
        code = report["reason_code"]
        report["reason_code"] = "suite-changed"
        suite_marker(workspace)
        report["reason_code"] = code
        report["status"] = "VERIFIED" if report["ready"] else "NOT TESTED"
    except (WeatherError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        report.update(ready=False, status="NOT TESTED", reason=str(exc))
    return report


def snapshot(workflow: Workflow) -> dict:
    return {name: digest(read_bytes(workflow.path(name), 4 * 1024 * 1024)) for name in TRACKED}


def isolated_gate_report() -> dict:
    # The test must reach the cancellation branch before its native semantics can
    # be established. This deliberate bootstrap fixture is restricted to test
    # workflows; the resulting evidence remains isolated and is never imported.
    return {"acceptance_scope": "isolated-native", "test_capabilities_simulated": True,
            "capabilities": {key: {"value": True, "status": "VERIFIED", "evidence_kind": "live-test"}
                             for key in ("trusted_hook_execution", "automatic_precompact_cancellation", "root_only_precompact")}}


def run_suite_hook(workspace: Path, event: dict) -> dict:
    """Run the production gate on a test workflow; never record prompt/tool data."""
    marker = suite_marker(workspace)
    if marker.get("suite_type") == "execution-guards":
        from .native_guards import run_guard_hook
        return run_guard_hook(workspace, event)
    workspace = workspace.resolve(strict=True)
    registration = read_json(owned_path(workspace, "active-case.json"), {})
    name = registration.get("name")
    if (name not in CASES or registration.get("workflow_id") != "case-" + name
            or not isinstance(registration.get("session_id"), str)):
        return {}
    sessions = {registration["session_id"]}
    if name == "child-auto-pass" and isinstance(registration.get("child_session_id"), str):
        sessions.add(registration["child_session_id"])
    if event.get("cwd") != str(workspace) or event.get("session_id") not in sessions:
        return {}
    workflow = Workflow(workspace, registration["workflow_id"])
    output = handle(workflow, event, isolated_gate_report())
    safe_event = {key: event.get(key) for key in (
        "hook_event_name", "session_id", "cwd", "turn_id", "trigger", "agent_id", "is_subagent", "parent_thread_id")}
    with lock(owned_path(workspace, "evidence.lock"), timeout=0.1):
        append_json(owned_path(workspace, "hook-events.jsonl"), {"case": name, "event": safe_event, "output": output,
                                                             "output_at_ns": time.monotonic_ns()})
    return output


def read_hook_rows(workspace: Path, name: str) -> list[dict]:
    raw = read_bytes(owned_path(workspace, "hook-events.jsonl"), 256 * 1024) if (workspace / "hook-events.jsonl").exists() else b""
    return [row for line in raw.splitlines() if (row := json.loads(line)).get("case") == name]


def assess_case(case: dict) -> dict:
    """Strict evidence checks: observing a hook or interrupting it is insufficient."""
    if case.get("name") == "child-auto-pass":
        from .native_isolation import assess_isolation
        return assess_isolation(case, require_hooks=True)
    errors = []
    rows = [row for row in case.get("hook_rows", [])
            if row.get("event", {}).get("session_id") == case.get("thread_id")
            and row["event"].get("hook_event_name") == "PreCompact"
            and row["event"].get("trigger") == ("manual" if case.get("name") == "manual-pass" else "auto")
            and row["event"].get("cwd") == case.get("workspace")
            and row["event"].get("turn_id") == case.get("compaction_turn_id", case.get("turn_id"))]
    native = [run for run in case.get("hooks", []) if run.get("eventName") == "preCompact"
              and run.get("sourcePath") == case.get("hook_source")
              and run.get("turn_id") == case.get("compaction_turn_id", case.get("turn_id"))]
    if not rows or not native:
        errors.append("Missing matching native hook completion or recorded hook output")
    if not case.get("terminal_event") or not case.get("terminal_identity_matches"):
        errors.append("Missing terminal event for the exact test thread/turn")
    if case.get("errors") or case.get("incomplete"):
        errors.append("Native evidence contains errors or is incomplete")
    if case.get("name") not in CASES or case.get("turn_status") not in ("completed", "interrupted"):
        errors.append("Unknown case or unsuccessful native terminal state")
    stopped = any(row.get("output", {}).get("continue") is False for row in rows)
    if case.get("name") == "root-auto-stop":
        if not stopped or not any(run.get("status") == "stopped" for run in native):
            errors.append("The native engine did not report the hook as stopped")
        if case.get("controller_interrupted") or case.get("turn_status") != "interrupted":
            errors.append("Cancellation must terminate without a controller interrupt")
        if case.get("compactions", 0) or case.get("requests_after_stop") != 0:
            errors.append("Compaction or a model request occurred after the stop gate")
        if case.get("phase") != "QUIESCING" or not case.get("transaction_matches_turn"):
            errors.append("No durable rotation request for this turn")
        if not case.get("generation_unchanged") or not case.get("lease_unchanged"):
            errors.append("Gate changed generation or transferred the source lease")
    else:
        if stopped or any(run.get("status") != "completed" for run in native):
            errors.append("A pass-through case was blocked or failed")
        if not case.get("compactions", 0):
            errors.append("No native compaction was observed in the control case")
        if not case.get("workflow_unchanged"):
            errors.append("Pass-through changed workflow state")
    return {"status": "VERIFIED" if not errors else "NOT TESTED", "failures": errors}


class Fixture:
    """Bounded, credential-free Responses fixture. Never saves request bodies."""
    def __init__(self):
        self.requests: list[dict] = []
        self.guard = threading.Lock()

    def request_metadata(self, path: str, size: int, headers) -> dict:
        return {"path": path, "body_bytes": size, "received_at_ns": time.monotonic_ns()}

    def response_item(self, body: dict, request: dict) -> dict:
        item = {"type": "message", "id": "msg_acceptance", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Isolated native acceptance completed.", "annotations": []}]}
        names = [member.get("name") for tool in body.get("tools", []) for member in tool.get("tools", [tool])]
        tool_name = next((name for name in ("exec_command", "shell_command", "shell") if name in names), None)
        if request is self.requests[0] and tool_name:
            args = {"cmd": "/usr/bin/true", "login": False, "max_output_tokens": 32, "yield_time_ms": 1000}
            if tool_name == "shell_command":
                args = {"command": "/usr/bin/true", "timeout_ms": 1000}
            elif tool_name == "shell":
                args = {"command": ["/usr/bin/true"], "timeout_ms": 1000}
            item = {"type": "function_call", "id": "fc_acceptance", "call_id": "call_acceptance",
                    "name": tool_name, "arguments": json.dumps(args), "status": "completed"}
        return item

    def response_usage(self, request: dict) -> dict:
        return {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}

    def handler(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    self.connection.settimeout(3)
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000 or self.path not in ("/v1/responses", "/v1/responses/compact"):
                        self.send_error(400)
                        return
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        self.send_error(400)
                        return
                    with fixture.guard:
                        if len(fixture.requests) >= 8:
                            self.send_error(429)
                            return
                        request = fixture.request_metadata(self.path, size, self.headers)
                        fixture.requests.append(request)
                    item = fixture.response_item(body, request)
                    usage = fixture.response_usage(request)
                    if self.path.endswith("/compact"):
                        payload = json.dumps({"id": "cmp_acceptance", "object": "response.compaction",
                            "output": [{"type": "compaction", "id": "cmp_item", "encrypted_content": "local-fixture"}],
                            "usage": usage}).encode()
                        content_type = "application/json"
                    else:
                        events = [{"type": "response.created", "response": {"id": "resp_acceptance", "status": "in_progress"}},
                                  {"type": "response.output_item.added", "output_index": 0, "item": item},
                                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                                  {"type": "response.completed", "response": {"id": "resp_acceptance", "status": "completed", "output": [item], "usage": usage}}]
                        payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                        content_type = "text/event-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (OSError, ValueError, TypeError, WeatherError):
                    self.close_connection = True
        return Handler


def engine_args(engine: Path, fixture_url: str, manual: bool) -> list[str]:
    provider = {"name": "Token Weather acceptance fixture", "base_url": fixture_url,
                "wire_api": "responses", "requires_openai_auth": False, "supports_websockets": False,
                "request_max_retries": 0, "stream_max_retries": 0, "stream_idle_timeout_ms": 3000}
    overrides = {"model_provider": "token_weather_acceptance", "model_providers.token_weather_acceptance": provider,
                 "model": "token-weather-local-fixture", "model_context_window": 100_000,
                 "model_auto_compact_token_limit": 90_000 if manual else 1,
                 "mcp_servers": {}, "features.code_mode": False, "memories.use_memories": False}
    args = [str(engine), "app-server", "--stdio", "--strict-config"]
    for key, value in overrides.items():
        args.extend(["-c", key + "=" + toml_value(value)])
    return args


def run_case(engine: Path, workspace: Path, name: str) -> dict:
    if name == "child-auto-pass":
        from .native_isolation import run_isolation
        return run_isolation(engine, workspace, require_hooks=True)
    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    case = {"name": name, "workspace": str(workspace), "hook_source": str(workspace / ".codex/hooks.json"),
            "hooks": [], "compactions": 0, "errors": [], "terminal_event": False,
            "terminal_identity_matches": False, "controller_interrupted": False}
    try:
        with JsonRpc(engine_args(engine, f"http://127.0.0.1:{server.server_port}/v1", name == "manual-pass"), cwd=workspace) as rpc:
            rpc.initialize()
            result = rpc.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                "baseInstructions": "Isolated deterministic acceptance.", "developerInstructions": "",
                "approvalPolicy": "never", "sandbox": "read-only"}, timeout=10)
            thread = identifier(result["thread"]["id"])
            case["thread_id"] = thread
            wf = Workflow(workspace, "case-" + name)
            enrolled = "unrelated-enrolled-root" if name == "unrelated-root-auto" else thread
            wf.initialize(enrolled, "Verify native gate semantics in this disposable workspace.")
            wf.prepare(enrolled)
            tx = read_json(wf.path("rotation.json"))
            tx["automatic_rotation"] = name != "disabled-auto"
            atomic_json(wf.path("rotation.json"), tx)
            if name == "corrupt-auto":
                from .storage import atomic_write
                atomic_write(wf.path("generations", str(wf.current()["generation"]), "active.md"), b"invalid test generation\n")
            before = snapshot(wf)
            atomic_json(workspace / "active-case.json", {"name": name, "session_id": thread, "workflow_id": wf.id})
            turn = rpc.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Run the isolated acceptance case."}]}, timeout=10)
            turn_id = turn["turn"]["id"]
            case["turn_id"] = turn_id
            manual_started = False
            deadline = time.monotonic() + 15
            for _ in range(500):
                message = rpc.next_notification(max(0, deadline - time.monotonic()))
                method, params = message.get("method"), message.get("params", {})
                if params.get("threadId") != thread:
                    continue
                if method == "hook/completed":
                    run = params.get("run", {})
                    summary = {key: run.get(key) for key in ("eventName", "status", "sourcePath", "durationMs")}
                    summary["turn_id"] = params.get("turnId")
                    case["hooks"].append(summary)
                elif method == "turn/started" and manual_started:
                    turn_id = params.get("turn", {}).get("id")
                    case["compaction_turn_id"] = turn_id
                elif method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction":
                    case["compactions"] += 1
                    # Controls may otherwise repeatedly compact at limit=1.
                    # Never interrupt the cancellation case to manufacture proof.
                    if name not in ("root-auto-stop", "manual-pass") and not case["controller_interrupted"]:
                        case["controller_interrupted"] = True
                        rpc.request("turn/interrupt", {"threadId": thread, "turnId": turn_id})
                elif method == "error":
                    case["errors"].append("Native error event")
                elif method == "turn/completed":
                    terminal = params.get("turn", {})
                    if not turn_id or terminal.get("id") != turn_id:
                        continue
                    if name == "manual-pass" and not manual_started:
                        if terminal.get("status") != "completed" or terminal.get("error"):
                            raise WeatherError("Manual compaction seed turn failed")
                        rpc.request("thread/compact/start", {"threadId": thread})
                        manual_started = True
                        # The native manual compaction has its own turn identity.
                        turn_id = None
                        continue
                    case["terminal_event"] = True
                    case["terminal_identity_matches"] = terminal.get("id") == turn_id
                    case["turn_status"] = terminal.get("status")
                    if terminal.get("error"):
                        case["errors"].append("Native terminal error")
                    break
                if time.monotonic() >= deadline:
                    raise WeatherError("Native acceptance case deadline reached")
            else:
                raise WeatherError("Native acceptance event limit reached")
            after = snapshot(wf)
            current_tx = read_json(wf.path("rotation.json"))
            case.update(phase=current_tx["phase"], transaction_matches_turn=current_tx.get("event_id") == case["turn_id"],
                        workflow_unchanged=before == after,
                        generation_unchanged=before["current.json"] == after["current.json"],
                        lease_unchanged=current_tx["lease"] == tx["lease"])
    except (WeatherError, OSError, ValueError, KeyError, TypeError) as exc:
        case["incomplete"] = True
        case["errors"].append(str(exc))
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    case["fixture_requests"] = len(fixture.requests)
    case["fixture_metadata"] = fixture.requests
    case["hook_rows"] = read_hook_rows(workspace, name)
    stops = [row["output_at_ns"] for row in case["hook_rows"]
             if row.get("output", {}).get("continue") is False and type(row.get("output_at_ns")) is int]
    case["requests_after_stop"] = sum(request["received_at_ns"] > min(stops) for request in fixture.requests) if stops else None
    case.update(assess_case(case))
    return case


def run_suite(engine: Path, workspace: Path) -> dict:
    marker = suite_marker(workspace)
    if marker.get("suite_type") == "execution-guards":
        from .native_guards import run_guard_suite
        return run_guard_suite(engine, workspace)
    workspace = workspace.resolve(strict=True)
    engine = engine.resolve(strict=True)
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "isolated-native",
              "engine_path": str(engine), "workspace": str(workspace), "suite_id": marker["suite_id"], "config_sha256": marker["config_sha256"],
              "runtime_source_sha256": {name: digest(read_bytes(ROOT / "src/token_weather" / name))
                                        for name in ("native_acceptance.py", "native_isolation.py", "hook_adapter.py", "coordinator.py", "desktop_adapter.py")},
              "test_capabilities_simulated": True, "trust_modified": False, "production_promotion": False,
              "paid_model_requests": 0, "same_desktop_server": False, "cases": [],
              "automatic_precompact_cancellation": "NOT TESTED", "root_only_precompact": "NOT TESTED",
              "native_subagent_isolation": "NOT TESTED", "status": "NOT TESTED"}
    try:
        with lock(workspace / "suite.lock", timeout=0.1):
            if (workspace / ATTEMPT).exists() or (workspace / "active-case.json").exists():
                raise WeatherError("Suite has already started; use a fresh suite to avoid mixing evidence")
            readiness = preflight_suite(engine, workspace)
            report["preflight"] = readiness
            for key in ("engine_version", "hook_preflight", "reason_code", "reason"):
                if key in readiness:
                    report[key] = readiness[key]
            if not readiness["ready"]:
                return report
            attempt = {"suite_id": marker["suite_id"], "started_at": utc_now(), "status": "started", "cases_started": []}
            # Persist before the first native thread/start, including ambiguous failures.
            atomic_json(workspace / ATTEMPT, attempt)
            for name in CASES:
                attempt["cases_started"].append(name)
                atomic_json(workspace / ATTEMPT, attempt)
                report["cases"].append(run_case(engine, workspace, name))
            passed = {case["name"] for case in report["cases"] if case["status"] == "VERIFIED"}
            if {"root-auto-stop", "disabled-auto", "corrupt-auto"} <= passed:
                report["automatic_precompact_cancellation"] = "VERIFIED"
            report["enrolled_vs_unrelated_root"] = "VERIFIED" if {"root-auto-stop", "unrelated-root-auto"} <= passed else "NOT TESTED"
            if {"root-auto-stop", "child-auto-pass"} <= passed:
                report["native_subagent_isolation"] = "VERIFIED"
                report["root_only_precompact"] = "VERIFIED"
            report["status"] = "VERIFIED" if passed == set(CASES) else "NOT TESTED"
            report["reason_code"] = "verified" if report["status"] == "VERIFIED" else "native-cases-incomplete"
            report["reason"] = "All listed root/control/subagent cases passed." if report["status"] == "VERIFIED" else "One or more native cases did not meet the evidence requirements; inspect each case's failures."
            report["note"] = "Suite status covers only these isolated native cases. Owning Desktop integration remains untested; this report cannot enable production automatic mode."
            attempt.update(status="finished", finished_at=utc_now(), result=report["status"])
            atomic_json(workspace / ATTEMPT, attempt)
    except (WeatherError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        report["reason"] = str(exc)
        report["reason_code"] = "native-run-failed"
    return report


def probe_fixture(engine: Path, collect_usage: bool = False, collect_control: bool = False) -> dict:
    """Prove compaction instrumentation without requiring any hook execution.

    No hooks are configured in this disposable process. This validates the
    control fixture only and cannot establish native cancellation or isolation.
    """
    engine = engine.resolve(strict=True)
    if collect_usage or collect_control:
        from .runtime_usage import ENGINE_VERSION, UsageObserver
        try:
            version = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise WeatherError("Cannot identify the installed usage-probe engine") from exc
        if version != ENGINE_VERSION:
            raise WeatherError("Installed engine does not match the usage adapter's protocol")
    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    report = {"schema_version": 1, "generated_at": utc_now(), "acceptance_scope": "isolated-native",
              "kind": "no-hook-fixture-control", "engine_path": str(engine), "status": "NOT TESTED",
              "paid_model_requests": 0, "trust_modified": False, "production_promotion": False,
              "hooks_executed": False, "compactions": 0, "terminal_event": False,
              "controller_interrupted": False}
    observer = None
    control = None
    if collect_usage:
        report.update(kind="no-hook-usage-control", engine_version=version, usage_samples=[], runtime_source_sha256=runtime_sources())
    if collect_control:
        report.update(kind="no-hook-terminal-control", engine_version=version, runtime_source_sha256=runtime_sources())
    try:
        with tempfile.TemporaryDirectory(prefix="token-weather-fixture-") as temporary:
            workspace = Path(temporary).resolve()
            args = engine_args(engine, f"http://127.0.0.1:{server.server_port}/v1", False)
            args.extend(["-c", "hooks={}"])
            with JsonRpc(args, cwd=workspace) as rpc:
                if collect_control:
                    rpc.initialize(experimental=True)
                else:
                    rpc.initialize()
                started = rpc.request("thread/start", {"cwd": str(workspace), "ephemeral": True,
                    "model": "token-weather-local-fixture", "modelProvider": "token_weather_acceptance",
                    "baseInstructions": "Local fixture control.", "developerInstructions": "",
                    "approvalPolicy": "never", "sandbox": "read-only"})
                thread = started["thread"]["id"]
                if collect_usage:
                    observer = UsageObserver(thread, workspace, version, "isolated-native")
                    observer.observe_thread(started, "thread/start resolved response")
                    observer.observe_config(rpc.request("config/read", {"cwd": str(workspace), "includeLayers": False}))
                turn = rpc.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Run fixture control."}]})["turn"]["id"]
                if collect_control:
                    from .runtime_control import QuiescenceObserver
                    control = QuiescenceObserver(thread, turn, workspace, version, "isolated-fixture", "isolated-native")
                report.update(thread_id=thread, turn_id=turn)
                deadline = time.monotonic() + 15
                for _ in range(500):
                    message = rpc.next_notification(max(0, deadline - time.monotonic()))
                    method, params = message.get("method"), message.get("params", {})
                    if params.get("threadId") != thread:
                        continue
                    if control is not None:
                        control.consume(message)
                    if observer is not None:
                        observer.consume(message)
                        if method == "thread/tokenUsage/updated":
                            if len(report["usage_samples"]) >= 8:
                                raise WeatherError("Native usage fixture exceeded its sample bound")
                            report["usage_samples"].append(observer.report())
                    if method == "hook/completed":
                        report["hooks_executed"] = True
                    elif method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction":
                        report["compactions"] += 1
                        if not report["controller_interrupted"]:
                            report["controller_interrupted"] = True
                            if control is not None:
                                control.interrupt_attempt = "ambiguous"
                            rpc.request("turn/interrupt", {"threadId": thread, "turnId": turn})
                            if control is not None:
                                control.interrupt_attempt = "acknowledged"
                                report["interrupt_ack_was_quiescent"] = control.report()["quiescent"]
                    elif method == "turn/completed" and params.get("turn", {}).get("id") == turn:
                        report["terminal_event"] = True
                        report["turn_status"] = params["turn"]["status"]
                        if params["turn"].get("error"):
                            raise WeatherError("Fixture control terminal error")
                        break
                    elif method == "error":
                        raise WeatherError("Fixture control error event")
                if (report["compactions"] and report["terminal_event"] and not report["hooks_executed"]
                        and report.get("turn_status") in ("completed", "interrupted")):
                    report["status"] = "VERIFIED"
                if observer is not None:
                    if report["runtime_source_sha256"] != runtime_sources():
                        raise WeatherError("Usage fixture sources changed during observation")
                    report["usage_observation"] = observer.report()
                    report["usage_adapter_verified"] = (observer.usage_events > 0 and observer.compactions > 0
                        and any(sample["last_request_usage"]["input_tokens"] == 100
                                and sample["last_request_usage"]["total_tokens"] == 110 for sample in report["usage_samples"]))
                    if not report["usage_adapter_verified"]:
                        report["status"] = "NOT TESTED"
                if control is not None:
                    from .owner_session import background_inventory
                    for _ in range(500):
                        pending = rpc.poll_notification()
                        if pending is None:
                            break
                        control.consume(pending)
                    else:
                        raise WeatherError("Native terminal control drain exceeded its event limit")
                    revision = control.revision
                    backgrounds = {thread: background_inventory(rpc, thread)}
                    # This fixture creates one clean ephemeral root, contains no
                    # child spawn responses and uses only /usr/bin/true. Its
                    # complete inventory claim is restricted to isolated-native.
                    control.bind_inventory(backgrounds, [], revision=revision,
                                           connection_id=control.connection_id, complete=True, external_operations=[])
                    report["quiescence_observation"] = control.report()
                    report["control_adapter_verified"] = (control.report()["quiescent"]
                        and report.get("interrupt_ack_was_quiescent") is False
                        and bool(control.items) and report["runtime_source_sha256"] == runtime_sources())
                    if not report["control_adapter_verified"]:
                        report["status"] = "NOT TESTED"
    except (WeatherError, OSError, ValueError, KeyError, TypeError) as exc:
        report["reason"] = str(exc)
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    report["fixture_requests"] = fixture.requests
    return report
