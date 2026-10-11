#!/usr/bin/env python3
"""Run an actual temporary local HTTP backend + verified TLS reverse proxy.

No cloud, public listener, model API, trust-store change, Docker or package install.
"""
import argparse
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "2025-11-25"
TEST_HOST = "mcp.native-deployment.test"
ORIGIN = "https://" + TEST_HOST


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Route only to literal loopback; verify the actual test DNS SAN in TLS.
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=TEST_HOST)


def run():
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("An existing openssl executable is needed; nothing was installed")
    process = None
    proxy = None
    thread = None
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="token-weather-native-tls-") as directory:
        base = Path(directory).resolve()
        cert, key = base / "test-cert.pem", base / "test-key.pem"
        command = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                   "-keyout", str(key), "-out", str(cert), "-subj", "/CN=" + TEST_HOST,
                   "-addext", "subjectAltName=DNS:" + TEST_HOST]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        backend = [sys.executable, "-B", str(ROOT / "mcp/server.py"), "--transport", "http",
                   "--bind", "127.0.0.1", "--port", "0", "--public-origin", ORIGIN]
        process = subprocess.Popen(backend, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True)
        try:
            if not select.select([process.stderr], [], [], 5)[0]:
                raise RuntimeError("Backend did not become ready")
            line = process.stderr.readline()
            matched = re.search(r"127\.0\.0\.1:(\d+)", line)
            if not matched:
                raise RuntimeError("Backend startup failed")
            backend_port = int(matched[1])

            class Proxy(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 16384:
                        self.send_error(413); return
                    body = self.rfile.read(length)
                    connection = http.client.HTTPConnection("127.0.0.1", backend_port, timeout=3)
                    try:
                        headers = {key: value for key, value in self.headers.items() if key.lower() not in ("connection", "content-length")}
                        connection.request("POST", self.path, body, headers)
                        response = connection.getresponse(); content = response.read()
                        self.send_response(response.status)
                        for name, value in response.getheaders():
                            if name.lower() in ("content-type", "cache-control"):
                                self.send_header(name, value)
                        self.send_header("Content-Length", str(len(content)))
                        self.end_headers(); self.wfile.write(content)
                    finally:
                        connection.close()

            proxy = HTTPServer(("127.0.0.1", 0), Proxy)
            tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_server.load_cert_chain(cert, key)
            proxy.socket = tls_server.wrap_socket(proxy.socket, server_side=True)
            thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            # This certificate is trusted only in this test's client context.
            client_context = ssl.create_default_context(cafile=str(cert))
            assert client_context.check_hostname and client_context.verify_mode == ssl.CERT_REQUIRED

            def post(message, *, origin=ORIGIN, version=PROTOCOL):
                client = LocalHTTPS(TEST_HOST, proxy.server_port, context=client_context, timeout=3)
                headers = {"Host": TEST_HOST, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
                if origin is not None:
                    headers["Origin"] = origin
                if version is not None:
                    headers["MCP-Protocol-Version"] = version
                try:
                    client.request("POST", "/mcp", json.dumps(message), headers)
                    response = client.getresponse(); raw = response.read()
                    return response.status, json.loads(raw) if raw else None
                finally:
                    client.close()

            status, response = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "native-local-test", "version": "1.0.0"},
            }}, version=None)
            assert status == 200 and response["result"]["protocolVersion"] == PROTOCOL
            outcomes.append("TLS hostname/certificate validation and initialize passed")
            assert post({"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
            status, response = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert status == 200 and [tool["name"] for tool in response["result"]["tools"]] == ["plan_context_budget"]
            outcomes.append("Remote inventory excludes local observation/control")
            cases = json.loads((ROOT / "mcp/examples/cases.json").read_text())
            for index, case in enumerate(cases, 3):
                request = {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                    "name": "plan_context_budget", "arguments": case["arguments"],
                }}
                status, response = post(request)
                assert status == 200 and response["result"]["isError"] == case.get("isError", False)
                if not case.get("isError"):
                    for field, expected in case["expected"].items():
                        value = response["result"]["structuredContent"]
                        for part in field.split("."):
                            value = value[int(part)] if isinstance(value, list) else value[part]
                        assert value == expected
                for marker in case.get("must_not_echo", []):
                    assert marker not in json.dumps(response)
                outcomes.append(case["name"] + ": passed over real TLS proxy")
            assert post({"jsonrpc": "2.0", "id": 20, "method": "ping"}, origin="https://untrusted.invalid")[0] == 403
            assert post({"jsonrpc": "2.0", "id": 21, "method": "ping"}, version="unsupported")[0] == 400
            outcomes.extend(("Untrusted origin rejected", "Unsupported protocol rejected"))
        finally:
            if proxy:
                proxy.shutdown(); proxy.server_close()
            if thread:
                thread.join(timeout=3)
            if process:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
                process.stderr.close()
    return {"version": "1.0.0", "passed": True, "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "Actual native Python subprocess behind actual TLS reverse proxy on loopback only",
            "tls_certificate_and_hostname_verified": True, "outcomes": outcomes,
            "test_processes_stopped": process.poll() is not None and not thread.is_alive(),
            "temporary_keys_deleted": True, "system_trust_store_changed": False,
            "public_mcp_deployed": False, "cloud_resources_created": False, "paid_api_calls": 0,
            "not_verified": ["Docker/Caddy images", "Public DNS/domain/TLS provider", "Existing Desktop model/effort/chat control", "ChatGPT remote connection"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = run()
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
