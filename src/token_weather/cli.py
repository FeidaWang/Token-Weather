from __future__ import annotations

import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import shutil
import sys

from .accounting import Inputs, Occupancy, resolve, watermarks, warning
from .capabilities import doctor, mode
from .checkpoints import Workflow
from .coordinator import Coordinator
from .desktop_adapter import open_prepared
from .hook_adapter import handle, stdin_event
from .installer import install, uninstall
from .metrics import measure
from .native_probe import probe_hooks
from .renderer import estimated_tokens
from .storage import WeatherError, read_json, read_bytes, atomic_json, atomic_write


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="token-weather", description="Token Weather for Codex — context weather and durable checkpoints")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--workflow", default="default")
    p.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("weather", "weather-serve"):
        weather = sub.add_parser(name, help="Read current-root context weather without enrolling a workflow")
        source = weather.add_mutually_exclusive_group()
        source.add_argument("--transcript", type=Path, help="Exact root transcript; versioned local fallback")
        source.add_argument("--events", type=Path, help="Bounded JSONL public app-server notification capture")
        weather.add_argument("--compact-limit", type=int, help="Explicit advisory total-context target")
        weather.add_argument("--max-age", type=float, default=120, help="Seconds before predictions are suppressed")
        if name == "weather":
            weather.add_argument("--json", action="store_true")
            weather.add_argument("--compact", action="store_true")
            weather.add_argument("--html", type=Path, help="Export an offline interactive snapshot")
            weather.add_argument("--output", type=Path, help="Save numeric-only JSON")
        else:
            weather.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an available port")
    sub.add_parser("weather-hook", help="Read an advisory plugin hook event from stdin")
    setup = sub.add_parser("weather-plugin-setup", help="Register or remove a project bridge to the installed plugin's weather hooks")
    setup.add_argument("--plugin-root", type=Path, default=Path(__file__).resolve().parents[2])
    setup.add_argument("--engine", type=Path, help="Codex CLI for read-only hooks/list preflight; defaults to the CLI on PATH")
    setup.add_argument("--check", action="store_true", help="Inspect registration and native hook status without writing project files")
    setup.add_argument("--remove", action="store_true")
    service = sub.add_parser("weather-service", help="Start, reuse, inspect and stop a managed current-root collector")
    actions = service.add_subparsers(dest="service_action", required=True)
    for action in ("start", "restart"):
        start = actions.add_parser(action)
        start.add_argument("--port", type=int, default=0)
        inputs = start.add_mutually_exclusive_group()
        inputs.add_argument("--transcript", type=Path)
        inputs.add_argument("--events", type=Path)
        start.add_argument("--compact-limit", type=int)
        start.add_argument("--max-age", type=float, default=120)
        start.add_argument("--poll-interval", type=float, default=2)
    for action in ("status", "stop", "list"):
        actions.add_parser(action)
    run = actions.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("--instance-id", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--output", type=Path)
    d.add_argument("--engine")
    d.add_argument("--refresh", action="store_true")
    d.add_argument("--json", action="store_true")
    init = sub.add_parser("init")
    init.add_argument("--objective", required=True)
    init.add_argument("--policies", type=Path)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--usage-report", type=Path, help="Read saved matching-root usage evidence; never enables automatic mode")
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--update", type=Path, required=True, help="JSON delta file, or - for stdin")
    checkpoint.add_argument("--key", required=True)
    checkpoint.add_argument("--expected-generation", type=int)
    checkpoint.add_argument("--lease-epoch", type=int)
    sub.add_parser("prepare")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("--dry-run", action="store_true", required=True, help="Live automatic adapter remains gated")
    sub.add_parser("plan-continuation", help="Describe the continuation transaction without connecting, sending or moving a lease")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--generation", type=int)
    bootstrap.add_argument("--ledger", type=int, nargs="*", default=[])
    bootstrap.add_argument("--json", action="store_true")
    ack = sub.add_parser("acknowledge")
    ack.add_argument("--generation", type=int, required=True)
    ack.add_argument("--manifest-sha256", required=True)
    retire = sub.add_parser("retire")
    retire.add_argument("--dry-run", action="store_true", required=True)
    un = sub.add_parser("uninstall")
    un.add_argument("--dry-run", action="store_true")
    ins = sub.add_parser("install")
    ins.add_argument("--dry-run", action="store_true")
    ins.add_argument("--apply", action="store_true")
    ins.add_argument("--register-hooks", action="store_true")
    ins.add_argument("--schema-evidence", type=Path)
    probe = sub.add_parser("probe-hooks", help="Inspect native parser/trust metadata without executing hooks")
    probe.add_argument("--engine", type=Path, required=True)
    probe.add_argument("--output", type=Path)
    probe.add_argument("--project", action="store_true", help="Observe actual project hooks without overrides")
    native_prepare = sub.add_parser("prepare-native-acceptance", help="Create a new disposable hook acceptance workspace")
    native_prepare.add_argument("--directory", type=Path, required=True)
    native_prepare.add_argument("--suite", choices=("compaction", "execution-guards"), default="compaction")
    baseline_plan = sub.add_parser("plan-policy-baseline", help="Plan a distinct live-policy workflow without capturing policy or writing files")
    baseline_plan.add_argument("--new-workflow", required=True)
    native = sub.add_parser("native-acceptance", help="Run isolated native gate cases only after normal hook trust review")
    native.add_argument("--engine", type=Path, required=True)
    native.add_argument("--test-workspace", type=Path, required=True)
    native.add_argument("--output", type=Path)
    preflight = sub.add_parser("native-acceptance-preflight", help="Diagnose disposable project/hook readiness without running native turns")
    preflight.add_argument("--engine", type=Path, required=True)
    preflight.add_argument("--test-workspace", type=Path, required=True)
    preflight.add_argument("--output", type=Path)
    fixture = sub.add_parser("probe-native-fixture", help="Exercise local compaction instrumentation with no configured hooks")
    fixture.add_argument("--engine", type=Path, required=True)
    fixture.add_argument("--output", type=Path)
    native_usage = sub.add_parser("probe-native-usage", help="Validate usage mapping/reset with a no-hook loopback fixture")
    native_usage.add_argument("--engine", type=Path, required=True)
    native_usage.add_argument("--output", type=Path)
    native_continuation = sub.add_parser("probe-native-continuation", help="Verify isolated native creation/submission with simulated release/attachment/ack gates")
    native_continuation.add_argument("--engine", type=Path, required=True)
    native_continuation.add_argument("--output", type=Path)
    native_reconciliation = sub.add_parser("probe-native-reconciliation", help="Verify original native response correlation after isolated create/submit receipt failures")
    native_reconciliation.add_argument("--engine", type=Path, required=True)
    native_reconciliation.add_argument("--output", type=Path)
    child = sub.add_parser("probe-native-subagent", help="Observe native child execution/compaction with a local no-hook fixture")
    child.add_argument("--engine", type=Path, required=True)
    child.add_argument("--output", type=Path)
    desktop = sub.add_parser("probe-desktop", help="Read the existing control transport without starting or interrupting turns")
    desktop.add_argument("--engine", type=Path, required=True)
    desktop.add_argument("--socket", type=Path, help="Explicit canonical private socket of an existing owner")
    desktop.add_argument("--transport", choices=("proxy", "unix-websocket"), default="proxy")
    desktop.add_argument("--turn-id", help="Exact source turn; also read partial live state without claiming lifecycle coverage")
    desktop.add_argument("--output", type=Path)
    readiness = sub.add_parser("preflight-owner", help="Diagnose workflow binding, stale owner endpoint and required live contract without changing a lease")
    readiness.add_argument("--engine", type=Path, required=True)
    readiness.add_argument("--turn-id")
    readiness.add_argument("--socket", type=Path, help="Explicit canonical private socket of the existing Desktop owner")
    readiness.add_argument("--transport", choices=("proxy", "unix-websocket"), default="proxy")
    readiness.add_argument("--output", type=Path)
    usage = sub.add_parser("probe-usage", help="Observe current-root usage through the existing proxy without submitting turns")
    usage.add_argument("--engine", type=Path, required=True)
    usage.add_argument("--seconds", type=float, default=3)
    usage.add_argument("--output", type=Path)
    replay = sub.add_parser("replay-usage", help="Reduce bounded recorded runtime events to a typed estimate")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--output", type=Path)
    control = sub.add_parser("probe-quiescence", help="Read exact source terminal/background evidence from an existing owner proxy")
    control.add_argument("--engine", type=Path, required=True)
    control.add_argument("--turn-id", required=True)
    control.add_argument("--socket", type=Path, help="Existing user-owned private Unix socket; never starts a server")
    control.add_argument("--transport", choices=("proxy", "unix-websocket"), default="proxy")
    control.add_argument("--seconds", type=float, default=3)
    control.add_argument("--output", type=Path)
    owner_state = sub.add_parser("probe-owner-state", help="Read bounded partial owner state and explicit coverage gaps")
    owner_state.add_argument("--engine", type=Path, required=True)
    owner_state.add_argument("--turn-id", required=True)
    owner_state.add_argument("--socket", type=Path, help="Existing user-owned private Unix socket")
    owner_state.add_argument("--transport", choices=("proxy", "unix-websocket"), default="proxy")
    owner_state.add_argument("--seconds", type=float, default=0)
    owner_state.add_argument("--output", type=Path)
    native_state = sub.add_parser("probe-native-owner-state", help="Validate supported read-only state RPCs in an isolated local fixture")
    native_state.add_argument("--engine", type=Path, required=True)
    native_state.add_argument("--output", type=Path)
    native_transport = sub.add_parser("probe-native-owner-transport", help="Verify private-socket owner discovery in a disposable local service")
    native_transport.add_argument("--engine", type=Path, required=True)
    native_transport.add_argument("--output", type=Path)
    control_replay = sub.add_parser("replay-control", help="Reduce bounded recorded lifecycle events without authorizing live control")
    control_replay.add_argument("--input", type=Path, required=True)
    control_replay.add_argument("--output", type=Path)
    native_control = sub.add_parser("probe-native-control", help="Validate exact terminal/tool tracking with a no-hook loopback fixture")
    native_control.add_argument("--engine", type=Path, required=True)
    native_control.add_argument("--output", type=Path)
    sub.add_parser("hook")
    sub.add_parser("open-prepared")
    seal = sub.add_parser("seal")
    seal.add_argument("--terminal-evidence", required=True)
    seal.add_argument("--unknown-effect", action="append", default=[])
    seal.add_argument("--final-update", type=Path)
    seal.add_argument("--manual-idle-confirmed", action="store_true", help="Explicitly confirm source turn/tools have stopped; not a native capability proof")
    bind = sub.add_parser("bind-successor")
    bind.add_argument("--new-thread", required=True)
    bind.add_argument("--terminal-evidence", required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("--terminal-evidence", required=True)
    recover.add_argument("--reason", required=True)
    reconcile = sub.add_parser("reconcile", help="Inspect durable rotation receipts without creating or sending; default is dry-run")
    reconcile.add_argument("--apply", action="store_true", help="Restore only confirmed local transaction state")
    reconcile.add_argument("--transaction-id", help="Required with --apply; pins the exact transaction")
    return p


def output(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def require_thread(args) -> str:
    if not args.thread_id:
        raise WeatherError("Pass --thread-id or run inside an enrolled Codex root thread")
    return args.thread_id


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "weather-hook":
            from .weather_hook import handle_weather
            print(json.dumps(handle_weather(stdin_event()), ensure_ascii=False))
            return 0
        workspace = args.workspace.resolve(strict=True)
        if args.command == "weather-plugin-setup":
            from .weather_setup import inspect_native_hooks, project_setup
            if args.check and args.remove:
                raise WeatherError("Choose --check or --remove")
            if not (args.plugin_root / "hooks/hooks.json").is_file():
                raise WeatherError("This plugin package has no bundled lifecycle hooks; use manual weather readings")
            native = None
            issue = None
            if not args.remove:
                engine = args.engine or (Path(found) if (found := shutil.which("codex")) else None)
                if engine is None:
                    issue = "Codex CLI was not found; supply --engine for a native hooks/list check"
                else:
                    try:
                        native = inspect_native_hooks(workspace, engine)
                    except (WeatherError, OSError) as exc:
                        issue = str(exc)
            if issue and not args.check:
                raise WeatherError("Hook registration requires a successful native check: " + issue)
            result = project_setup(workspace, args.plugin_root, remove=args.remove, check_only=args.check, native=native)
            if issue:
                result.update(native_check="unavailable", native_error=issue,
                              next_step="Resolve native hook inspection before registering; no project files were written.")
            output(result)
            return 0
        if args.command == "weather-service":
            from .weather_service import run_service, service_list, service_status, start_service, stop_service
            if args.service_action == "list":
                output(service_list())
                return 0
            thread = require_thread(args)
            if args.service_action == "_run":
                return run_service(workspace, thread, args.instance_id)
            if args.service_action == "status":
                output(service_status(thread))
            elif args.service_action == "stop":
                output(stop_service(thread))
            else:
                if args.service_action == "restart":
                    stop_service(thread)
                output(start_service(workspace, thread, port=args.port, transcript=args.transcript, events=args.events,
                                     compact_limit=args.compact_limit, max_age=args.max_age, poll_interval=args.poll_interval))
            return 0
        if args.command in ("weather", "weather-serve"):
            from .weather import terminal_weather
            from .weather_sources import collect_weather
            from .weather_view import weather_html, weather_server
            thread = require_thread(args)
            def collect():
                return collect_weather(workspace, thread, transcript=args.transcript, events=args.events,
                                       compact_limit=args.compact_limit, max_age=args.max_age)
            if args.command == "weather-serve":
                collect()  # validate before announcing a usable URL
                server = weather_server(collect, args.port)
                print(f"Token Weather: http://127.0.0.1:{server.server_port}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
                return 0
            result = collect()
            if args.output:
                atomic_json(args.output, result)
            if args.html:
                atomic_write(args.html, weather_html(result).encode())
            if args.json:
                output(result)
            else:
                print(terminal_weather(result, args.compact))
                if args.html:
                    print("Weather snapshot:", args.html.resolve())
            return 0
        if args.command == "plan-policy-baseline":
            from .policy_baseline import policy_baseline_plan
            output(policy_baseline_plan(Workflow(workspace, args.workflow), args.new_workflow, require_thread(args)))
            return 2
        if args.command == "preflight-owner":
            from .owner_readiness import preflight_owner
            result = preflight_owner(args.engine, require_thread(args), workspace, args.workflow,
                                     turn=args.turn_id, socket_path=args.socket, transport=args.transport)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 2  # Diagnostics cannot authorize production continuation.
        if args.command == "probe-native-owner-transport":
            from .native_owner_transport import probe_native_owner_transport
            result = probe_native_owner_transport(args.engine)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "probe-owner-state":
            from .owner_state import probe_owner_state
            result = probe_owner_state(args.engine, require_thread(args), args.turn_id, workspace, args.seconds, args.socket, transport=args.transport)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "probe-native-owner-state":
            from .native_owner_state import probe_native_owner_state
            result = probe_native_owner_state(args.engine)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "replay-control":
            from .runtime_control import replay_control
            result = replay_control(read_json(args.input))
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0
        if args.command == "probe-quiescence":
            from .owner_session import probe_quiescence
            result = probe_quiescence(args.engine, require_thread(args), args.turn_id, workspace, args.seconds, args.socket, transport=args.transport)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "replay-usage":
            from .runtime_usage import replay_usage
            result = replay_usage(read_json(args.input))
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0
        if args.command == "probe-usage":
            from .usage_probe import probe_usage
            result = probe_usage(args.engine, require_thread(args), workspace, args.seconds)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "prepare-native-acceptance":
            from .native_acceptance import prepare_suite
            output(prepare_suite(args.directory, suite_type=args.suite))
            return 0
        if args.command in ("native-acceptance", "native-acceptance-preflight"):
            from .native_acceptance import run_suite, preflight_suite
            result = (run_suite if args.command == "native-acceptance" else preflight_suite)(args.engine, args.test_workspace)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "probe-desktop":
            from .desktop_probe import probe_desktop
            result = probe_desktop(args.engine, require_thread(args), workspace, args.socket,
                                   transport=args.transport, turn=args.turn_id)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command in ("probe-native-continuation", "probe-native-reconciliation"):
            from .native_continuation import probe_continuation, probe_reconciliation
            result = (probe_continuation if args.command == "probe-native-continuation" else probe_reconciliation)(args.engine)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command in ("probe-native-fixture", "probe-native-usage", "probe-native-control"):
            from .native_acceptance import probe_fixture
            result = probe_fixture(args.engine, collect_usage=args.command == "probe-native-usage", collect_control=args.command == "probe-native-control")
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "probe-native-subagent":
            from .native_isolation import probe_subagent
            result = probe_subagent(args.engine)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0 if result["status"] == "VERIFIED" else 2
        if args.command == "doctor":
            result = doctor(workspace, args.output, args.engine, args.refresh)
            if args.json:
                output(result)
            else:
                print(f'Token Weather mode: {result["mode"]}\nDesktop: {result["runtime"]["desktop"]}\nEngine: {result["runtime"]["engine"]}\nCapabilities: true/false/null values are evidence-backed; null means NOT TESTED.')
            return 0
        if args.command == "probe-hooks":
            result = probe_hooks(args.engine, workspace, args.workflow, Path(sys.executable).resolve(),
                                 Path(sys.argv[0]).resolve(), args.project)
            if args.output:
                atomic_json(args.output, result)
            output(result)
            return 0
        if args.command in ("install", "uninstall"):
            if args.command == "uninstall":
                output(uninstall(workspace, args.dry_run))
            else:
                output(install(workspace, args.workflow, Path(sys.executable).resolve(), Path(sys.argv[0]).resolve(),
                               apply=args.apply and not args.dry_run, register_hooks=args.register_hooks,
                               schema_evidence=read_json(args.schema_evidence) if args.schema_evidence else None))
            return 0
        workflow = Workflow(workspace, args.workflow)
        if args.command == "init":
            output(workflow.initialize(require_thread(args), args.objective, read_json(args.policies, {}) if args.policies else {}))
            return 0
        report = read_json(workflow.path("capabilities.json"), {})
        coordinator = Coordinator(workflow, report)
        if args.command == "status":
            bootstrap = workflow.bootstrap(verify_evidence=False)
            state = bootstrap["state"]
            observations = state.get("usage", {})
            limits_data = observations.get("limits", {})
            inputs = Inputs(**limits_data)
            usage_data = observations.get("occupancy", {})
            allowed = {f.name for f in fields(Occupancy)}
            usage = Occupancy(**{k: v for k, v in usage_data.items() if k in allowed}).estimate()
            calculated = resolve(inputs, usage["estimate"])
            marks = watermarks(calculated["end_boundary"] if not calculated["unknown_inputs"] else None)
            result = {"workflow_id": workflow.id, "workspace": str(workspace), "generation": bootstrap["generation"],
                      "mode": mode(report), "phase": coordinator.transaction()["phase"], "lease": coordinator.transaction()["lease"],
                      "next_action": state["next_action"], "accounting": calculated, "occupancy": usage,
                      "watermarks": marks, "rotation_reason": warning(usage, marks, observations.get("predicted_burst", 0)),
                      "bootstrap_estimated_tokens": estimated_tokens(bootstrap["active"]),
                      "init_prompt_estimated_tokens": estimated_tokens(bootstrap["init_prompt"]), "token_sizing": "UTF-8 bytes / 3 heuristic; not native tokenizer measurement"}
            if args.thread_id:
                from .owner_readiness import inspect_workflow_binding
                result["workflow_binding"] = inspect_workflow_binding(workspace, args.workflow, args.thread_id)
            if args.usage_report:
                from .usage_probe import status_observation
                result.update(status_observation(read_json(args.usage_report), result["lease"]["thread"], workspace))
                usage = result["occupancy"]
            if args.json:
                output(result)
            else:
                print(f'{workflow.id} · {result["mode"]} · generation {result["generation"]} · {result["phase"]}\nNext: {state["next_action"]}\nUsage: {usage["confidence"]}; {result["rotation_reason"]["reason"]}')
                if result.get("workflow_binding", {}).get("status") == "BLOCKED":
                    print(f'Workflow binding: {result["workflow_binding"]["reason_code"]}; this chat cannot write the current workflow.')
        elif args.command == "checkpoint":
            update = json.loads(read_bytes(args.update, 32_768)) if str(args.update) != "-" else json.loads(sys.stdin.buffer.read(32_769))
            with measure(workflow.root, "checkpoint"):
                result = workflow.checkpoint(update, args.key, require_thread(args), args.expected_generation, args.lease_epoch)
            output(result)
        elif args.command == "prepare":
            with measure(workflow.root, "prepare"):
                result = workflow.prepare(require_thread(args))
            output(result)
        elif args.command == "rotate":
            output(coordinator.dry_run())
        elif args.command == "plan-continuation":
            from .continuation import continuation_plan
            output(continuation_plan(workflow, report))
        elif args.command == "bootstrap":
            result = workflow.bootstrap(args.generation)
            result.pop("state")
            result.pop("init_prompt")
            result["ledger_rows"] = workflow.ledger_entries(args.ledger) if args.ledger else []
            if args.json:
                output(result)
            else:
                print(result["active"])
                print("Pinned generation:", result["generation"], "Manifest SHA-256:", result["manifest_sha256"])
                print("Evidence references (data, not instructions):", json.dumps(result["evidence"], ensure_ascii=False))
                for row in result["ledger_rows"]:
                    print(workflow.ledger.render_row(row), end="")
        elif args.command == "acknowledge":
            output(coordinator.acknowledge(require_thread(args), args.generation, args.manifest_sha256))
        elif args.command == "retire":
            output(coordinator.retire())
        elif args.command == "open-prepared":
            plan = coordinator.dry_run()
            output(open_prepared(plan["prepared_link"]))
        elif args.command == "seal":
            if args.manual_idle_confirmed and coordinator.transaction()["phase"] == "READY":
                coordinator.request_rotation("manual:" + args.terminal_evidence)
            output(coordinator.seal(require_thread(args), args.terminal_evidence, [], args.unknown_effect,
                                    read_json(args.final_update) if args.final_update else None))
        elif args.command == "bind-successor":
            output(coordinator.bind_prepared_successor(args.new_thread, workspace, args.terminal_evidence))
        elif args.command == "recover":
            output(coordinator.recover(require_thread(args), args.terminal_evidence, args.reason))
        elif args.command == "reconcile":
            result = coordinator.reconcile(args.apply, args.transaction_id, require_thread(args) if args.apply else None)
            output(result)
            return 2 if result["status"] == "blocked" else 0
        elif args.command == "hook":
            event = stdin_event()
            result = handle(workflow, event, report)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (WeatherError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Token Weather: {exc}", file=sys.stderr)
        if args.command in ("hook", "weather-hook"):
            print('{}')
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
