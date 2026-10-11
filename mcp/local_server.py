"""Local stdio MCP: numeric planning plus explicit local Codex observations.

Never import this entry point into an HTTP server or container deployment.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from budget import BudgetInputError, count_schema
from server import MAX_BYTES, TOOL, encode, error, handle

LOCAL_TOOL = {
    "name": "inspect_local_context", "title": "Read local Codex context estimates",
    "description": "Local stdio only. Read the exact requested Codex root's compatible local numeric events, calculate a budget and update advisory task/output recommendations. No private ChatGPT access, no model/effort/chat control, no account or paid API call.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "thread_id": {"type": "string", "maxLength": 128, "description": "Exact root ID from the current host. Omit only when CODEX_THREAD_ID is correctly bound."},
        "workspace": {"type": "string", "description": "Exact absolute workspace; omit only when TOKEN_WEATHER_WORKSPACE is bound."},
        "planned_increase_tokens": count_schema(), "reserved_output_tokens": count_schema(),
    }},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
}


def local_handle(raw):
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return handle(raw)
    if (type(message) is dict and message.get("jsonrpc") == "2.0" and "id" in message
            and message.get("method") == "tools/call" and type(message.get("params")) is dict
            and message["params"].get("name") == LOCAL_TOOL["name"]):
        # Let the common parser check size, unique keys and the RPC envelope first.
        checked = handle(raw)
        if checked and "error" in checked and checked["error"]["code"] != -32602:
            return checked
        if set(message["params"]) - {"name", "arguments", "_meta"}:
            return error(message["id"], -32602, "Unsupported local call parameters.")
        try:
            from local_context import local_context
            result = local_context(message["params"].get("arguments", {}))
        except BudgetInputError as exc:
            output = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        except Exception:
            # Local paths, IDs or contents are never echoed in operational errors.
            output = {"isError": True, "content": [{"type": "text", "text": "Local observation unavailable: verify the exact root/workspace, compatible records and Python runtime."}]}
        else:
            output = {"isError": False, "structuredContent": result, "content": [{"type": "text", "text": encode(result).decode()}]}
        return {"jsonrpc": "2.0", "id": message["id"], "result": output}
    reply = handle(raw)
    if reply and "result" in reply and isinstance(message, dict):
        if message.get("method") == "tools/list":
            reply["result"]["tools"] = [TOOL, LOCAL_TOOL]
        elif message.get("method") == "initialize":
            reply["result"]["serverInfo"]["name"] = "token-weather-local"
    return reply


def main():
    if sys.version_info < (3, 11):
        candidates = [os.environ.get("TOKEN_WEATHER_PYTHON")] + [shutil.which(name) for name in ("python3.13", "python3.12", "python3.11")]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and Path(candidate).resolve() != Path(sys.executable).resolve():
                os.execv(candidate, [candidate, "-B", str(Path(__file__).resolve())])
    while True:
        raw = sys.stdin.buffer.readline(MAX_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = sys.stdin.buffer.readline(MAX_BYTES + 1)
            reply = error(None, -32600, "Request exceeds the local size limit.")
        else:
            reply = local_handle(raw)
        if reply is not None:
            sys.stdout.buffer.write(encode(reply) + b"\n")
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
