"""MIT-licensed token-budget MCP, local by default; optional owner-managed HTTPS proxy."""
import argparse
import ipaddress
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from budget import BudgetInputError, INPUT_SCHEMA, OUTPUT_SCHEMA, plan_context_budget

PROTOCOL = "2025-11-25"
MAX_BYTES = 16 * 1024
TOOL = {
    "name": "plan_context_budget", "title": "Plan a token budget",
    "description": "Calculate reference headroom, a next-task budget and a five-turn linear outlook from explicit numbers. Missing numbers stay unknown. No chat, filesystem, account, API or pricing access.",
    "inputSchema": INPUT_SCHEMA, "outputSchema": OUTPUT_SCHEMA,
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
}


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def no_constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=no_constant)


def handle(raw):
    if len(raw) > MAX_BYTES:
        return error(None, -32600, "Request exceeds the local size limit.")
    try:
        message = decode(raw)
    except (ValueError, UnicodeError, RecursionError):
        return error(None, -32700, "Invalid JSON.")
    if type(message) is not dict or message.get("jsonrpc") != "2.0":
        return error(None, -32600, "Expected one JSON-RPC 2.0 message; batches are not supported.")
    request_id = message.get("id")
    if "id" in message and (type(request_id) not in (int, str) or (type(request_id) is str and len(request_id) > 128)):
        return error(None, -32600, "Invalid request ID.")
    method = message.get("method")
    params = message.get("params", {})
    if "method" in message and ("result" in message or "error" in message):
        return error(request_id, -32600, "A message cannot combine a method with a result or error.")
    if not isinstance(method, str):
        # No server-to-client requests are made; ignore well-formed client responses.
        if "id" in message and ("result" in message) != ("error" in message) and "method" not in message:
            return None
        return error(request_id, -32600, "A method is required.")
    if "id" not in message:
        # Notifications never receive a JSON-RPC response, even if unrecognised.
        return None
    if type(params) is not dict:
        return error(request_id, -32602, "Parameters must be an object.")
    if method == "initialize":
        if (type(params.get("protocolVersion")) is not str or type(params.get("capabilities")) is not dict
                or type(params.get("clientInfo")) is not dict
                or type(params["clientInfo"].get("name")) is not str
                or type(params["clientInfo"].get("version")) is not str):
            return error(request_id, -32602, "Initialization requires protocolVersion, capabilities and clientInfo.")
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "token-weather-budget", "version": "1.0.0"},
                  "instructions": "Only explicit numeric budgeting. MIT open source; owner-hosted. No current chat/account access or paid API calls."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        if params.get("cursor") is not None:
            return error(request_id, -32602, "This tool list has no continuation cursor.")
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        if set(params) - {"name", "arguments", "_meta"} or params.get("name") != TOOL["name"]:
            return error(request_id, -32602, "Unknown tool or unsupported call parameters.")
        try:
            output = plan_context_budget(params.get("arguments", {}))
        except BudgetInputError as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        else:
            result = {"isError": False, "structuredContent": output,
                      "content": [{"type": "text", "text": encode(output).decode("utf-8")}]}
    else:
        return error(request_id, -32601, "Method not supported.")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve_stdio():
    while True:
        raw = sys.stdin.buffer.readline(MAX_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_BYTES:
            tail = raw
            while tail and not tail.endswith(b"\n"):
                tail = sys.stdin.buffer.readline(MAX_BYTES + 1)
            reply = error(None, -32600, "Request exceeds the local size limit.")
        else:
            reply = handle(raw)
        if reply is not None:
            sys.stdout.buffer.write(encode(reply) + b"\n")
            sys.stdout.buffer.flush()


class Handler(BaseHTTPRequestHandler):
    # One stateless response per POST, no SSE stream or business-state storage.
    server_version = "TokenWeatherBudget/1.0.0"

    def log_message(self, _format, *args):
        pass  # No request/body/access logging.

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def reply(self, status, value=None):
        body = encode(value) if value is not None else b""
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 405:
            self.send_header("Allow", "POST")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        port = self.server.server_port
        hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
        permitted_origins = {"http://" + host for host in hosts}
        public_origin = getattr(self.server, "public_origin", None)
        if public_origin:
            hosts.add(urlsplit(public_origin).netloc)
            permitted_origins.add(public_origin)
        incoming_hosts = self.headers.get_all("Host", [])
        if len(incoming_hosts) != 1 or incoming_hosts[0] not in hosts:
            self.reply(403, error(None, -32600, "Host is not explicitly allowed."))
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] not in permitted_origins):
            self.reply(403, error(None, -32600, "Origin is not explicitly allowed."))
            return False
        if self.path != "/mcp":
            self.reply(404, error(None, -32600, "Endpoint not found."))
            return False
        return True

    def do_POST(self):
        if not self.allowed():
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.reply(415, error(None, -32600, "Use application/json."))
            return
        accepts = {part.split(";", 1)[0].strip().lower() for part in self.headers.get("Accept", "").split(",")}
        if not {"application/json", "text/event-stream"} <= accepts:
            self.reply(406, error(None, -32600, "Accept must include application/json and text/event-stream."))
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.reply(400, error(None, -32600, "Use one bounded Content-Length; transfer encoding is unsupported."))
            return
        if len(lengths[0]) > 6 or int(lengths[0]) > MAX_BYTES:
            self.reply(413, error(None, -32600, "Request exceeds the local size limit."))
            return
        raw = self.rfile.read(int(lengths[0]))
        if len(raw) != int(lengths[0]):
            self.reply(400, error(None, -32600, "Incomplete request."))
            return
        versions = self.headers.get_all("MCP-Protocol-Version", [])
        try:
            initial = decode(raw).get("method") == "initialize"
        except (ValueError, UnicodeError, RecursionError, AttributeError):
            initial = False
        if len(versions) > 1 or (versions and versions != [PROTOCOL]) or (not versions and not initial):
            self.reply(400, error(None, -32600, "Use the negotiated MCP-Protocol-Version: " + PROTOCOL))
            return
        output = handle(raw)
        self.reply(202 if output is None else 200, output)

    def do_GET(self):
        if self.allowed():
            self.reply(405)

    do_DELETE = do_GET
    do_OPTIONS = do_GET


def local_server(port=0):
    return HTTPServer(("127.0.0.1", port), Handler)


def validated_public_origin(value):
    if not value:
        return None
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or not parts.netloc.isascii() or any(c.isspace() for c in parts.netloc)):
        raise ValueError("Public origin must be one HTTPS origin with no credentials, path, query or fragment.")
    # Force validation of ports and forbid private/local addresses.
    port = parts.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid public origin port.")
    hostname = parts.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("Public origin must not be local.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            raise ValueError("Use a public DNS name.") from None
    else:
        if not address.is_global:
            raise ValueError("Public origin must not be a private or loopback address.")
    return "https://" + parts.netloc.lower()


def configured_server(port=0, bind="127.0.0.1", public_origin=None):
    origin = validated_public_origin(public_origin)
    if bind not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("Bind must be 127.0.0.1 or 0.0.0.0.")
    if bind == "0.0.0.0" and origin is None:
        raise ValueError("External binding requires an explicit public HTTPS origin and a TLS reverse proxy.")
    server = HTTPServer((bind, port), Handler)
    server.public_origin = origin
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--port", type=int, default=0, help="HTTP port; 0 selects an ephemeral port.")
    parser.add_argument("--bind", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1", help="Keep loopback unless behind your own TLS proxy.")
    parser.add_argument("--public-origin", default=os.environ.get("TOKEN_WEATHER_PUBLIC_ORIGIN"), help="Exact HTTPS origin for your own reverse proxy.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    if args.transport == "stdio":
        serve_stdio()
    else:
        try:
            httpd = configured_server(args.port, args.bind, args.public_origin)
        except ValueError as exc:
            parser.error(str(exc))
        with httpd:
            print("Token Weather MCP listening on " + args.bind + ":" + str(httpd.server_port) + " (Ctrl+C to stop)", file=sys.stderr, flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
