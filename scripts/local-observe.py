#!/usr/bin/env python3
"""Read a precisely bound local Codex root; optionally refresh local advice."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from local_context import local_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", help="Exact current root ID from the host; default CODEX_THREAD_ID")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task-tokens", type=int)
    parser.add_argument("--output-reserve", type=int)
    parser.add_argument("--watch", action="store_true", help="Refresh read-only observations; Ctrl+C stops. No model/effort/chat mutations.")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.interval <= 3600:
        parser.error("Interval must be between 1 and 3600 seconds")
    arguments = {"workspace": str(args.workspace.resolve())}
    if args.thread:
        arguments["thread_id"] = args.thread
    if args.task_tokens is not None:
        arguments["planned_increase_tokens"] = args.task_tokens
    if args.output_reserve is not None:
        arguments["reserved_output_tokens"] = args.output_reserve
    try:
        while True:
            try:
                output = local_context(arguments)
            except Exception:
                print(json.dumps({"available": False, "reason": "Verify exact root/workspace, compatible local records and Python 3.11+. No control action was performed."}), flush=True)
                return 2
            print(json.dumps(output, separators=(",", ":")), flush=True)
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
