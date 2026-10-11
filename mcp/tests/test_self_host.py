import http.client
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import PROTOCOL, configured_server, encode, validated_public_origin


class SelfHostingTests(unittest.TestCase):
    def test_defaults_stay_local_and_external_binding_requires_origin(self):
        with configured_server() as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertIsNone(server.public_origin)
        with self.assertRaises(ValueError):
            configured_server(bind="0.0.0.0")

    def test_invalid_origins_fail_before_binding(self):
        for origin in ["http://mcp.example.org", "https://localhost", "https://127.0.0.1", "https://10.0.0.1", "https://user:secret@mcp.example.org", "https://mcp.example.org/mcp", "https://mcp.example.org?key=secret", "https://mcp.example.org#fragment", "https://mcp.example.org:99999"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validated_public_origin(origin)

    def test_exact_public_host_origin_and_version_over_local_proxy_simulation(self):
        # This is a literal-loopback simulation, not a deployment or TLS test.
        server = configured_server(public_origin="https://mcp.example.org")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            for host, origin, expected in [("mcp.example.org", "https://mcp.example.org", 200), ("evil.example.org", "https://mcp.example.org", 403), ("mcp.example.org", "https://evil.example.org", 403), ("mcp.example.org", None, 200)]:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                headers = {"Host": host, "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
                if origin is not None:
                    headers["Origin"] = origin
                message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "plan_context_budget", "arguments": {"reference_limit_tokens": 100000, "used_tokens": 65000}}}
                try:
                    connection.request("POST", "/mcp", body=encode(message), headers=headers)
                    response = connection.getresponse(); body = response.read()
                    self.assertEqual(response.status, expected)
                    if expected == 200:
                        self.assertEqual(json.loads(body)["result"]["structuredContent"]["current"]["remaining_tokens"], 35000)
                finally:
                    connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_remote_config_helper_validates_and_never_overwrites(self):
        helper = Path(__file__).resolve().parents[2] / "scripts/configure-remote.py"
        spec = importlib.util.spec_from_file_location("remote_config", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for invalid in ["http://mcp.acme.test/mcp", "https://127.0.0.1/mcp", "https://example.org/mcp", "https://mcp.acme.test/mcp?key=secret"]:
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                module.remote_url(invalid)
        # acme.test is only a test string; no DNS/network request is performed.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "remote.json"
            args = [sys.executable, "-B", str(helper), "--url", "https://mcp.acme.test/mcp", "--output", str(output)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            config = json.loads(output.read_text())
            self.assertEqual(config["mcpServers"]["context-budget"]["type"], "streamable-http")
            before = output.read_bytes()
            repeated = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(output.read_bytes(), before)
