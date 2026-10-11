import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from budget import BudgetInputError, INPUT_SCHEMA, OUTPUT_SCHEMA, plan_context_budget
from server import MAX_BYTES, PROTOCOL, TOOL, encode, handle, local_server

CASES = json.loads((ROOT / "examples/cases.json").read_text())
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "offline-test", "version": "1"},
}}


def call(arguments, request_id=3):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
        "name": "plan_context_budget", "arguments": arguments,
    }}


def at(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def validate_schema(value, schema):
    """Small independent checker for just the JSON Schema subset used here.

    This is not a full JSON Schema validator or an SDK conformance claim.
    """
    if "const" in schema:
        assert type(value) is type(schema["const"]) and value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    expected = schema.get("type")
    kinds = expected if isinstance(expected, list) else [expected] if expected else []
    matches = {
        "null": value is None, "object": type(value) is dict, "array": type(value) is list,
        "string": type(value) is str, "boolean": type(value) is bool,
        "number": type(value) in (int, float),
        "integer": type(value) is int or (type(value) is float and value.is_integer()),
    }
    if kinds:
        assert any(matches[kind] for kind in kinds), (value, kinds)
    if type(value) in (int, float):
        if "minimum" in schema:
            assert value >= schema["minimum"]
        if "maximum" in schema:
            assert value <= schema["maximum"]
    if type(value) is dict:
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= set(value)
        if schema.get("additionalProperties") is False:
            assert not set(value) - set(properties)
        for key in value:
            if key in properties:
                validate_schema(value[key], properties[key])
    if type(value) is list:
        assert len(value) <= schema.get("maxItems", len(value))
        for item in value:
            validate_schema(item, schema.get("items", {}))


class CalculatorTests(unittest.TestCase):
    def check_case(self, case, result):
        self.assertEqual(result["isError"], case.get("isError", False))
        if not result["isError"]:
            output = result["structuredContent"]
            validate_schema(case["arguments"], INPUT_SCHEMA)
            validate_schema(output, OUTPUT_SCHEMA)
            self.assertEqual(json.loads(result["content"][0]["text"]), output)
            for path, expected in case["expected"].items():
                self.assertEqual(at(output, path), expected, path)
        for marker in case.get("must_not_echo", []):
            self.assertNotIn(marker, json.dumps(result))

    def test_eight_scenarios(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                self.check_case(case, handle(encode(call(case["arguments"])))["result"])

    def test_boundary_weather(self):
        for used, weather in [(0, "Clear"), (34, "Clear"), (35, "Fair"), (55, "Cloudy"), (70, "Showers"), (85, "Storm"), (100, "Storm"), (101, "Storm")]:
            with self.subTest(used=used):
                result = plan_context_budget({"reference_limit_tokens": 100, "used_tokens": used})
                self.assertEqual(result["current"]["weather"], weather)
                self.assertEqual(result["current"]["remaining_tokens"], max(0, 100-used))
                self.assertEqual(result["current"]["over_budget_tokens"], max(0, used-100))

    def test_missing_does_not_become_zero_or_inferred_usage(self):
        for arguments in [{}, {"recent_turn_tokens": [10, 20, 30]}, {"reference_limit_tokens": 100, "used_tokens": 30, "planned_increase_tokens": 10}]:
            result = plan_context_budget(arguments)
            validate_schema(result, OUTPUT_SCHEMA)
            self.assertIsNone(result["plan"]["after"]["used_tokens"])
        result = plan_context_budget({"used_tokens": None, "recent_turn_tokens": [10, 20, 30]})
        self.assertIsNone(result["current"]["used_tokens"])
        self.assertEqual(result["growth"]["forecast"], [])

    def test_explicit_zero_and_exact_fit(self):
        result = plan_context_budget({"reference_limit_tokens": 100, "used_tokens": 100, "planned_increase_tokens": 0, "reserved_output_tokens": 0})
        self.assertTrue(result["plan"]["fits_reference"])
        self.assertEqual(result["plan"]["after"]["remaining_tokens"], 0)

    def test_stable_growth_has_five_flat_steps_no_crossing_claim(self):
        result = plan_context_budget({"reference_limit_tokens": 100, "used_tokens": 30, "recent_turn_tokens": [30, 30, 30]})
        self.assertEqual(result["growth"]["tokens_per_turn"], 0)
        self.assertIsNone(result["growth"]["turns_until_reference"])
        self.assertEqual(len(result["growth"]["forecast"]), 5)
        self.assertTrue(all(p["budget"]["used_tokens"] == 30 for p in result["growth"]["forecast"]))

    def test_decrease_resets_forecast_without_claiming_compaction(self):
        result = plan_context_budget({"reference_limit_tokens": 100, "used_tokens": 30, "recent_turn_tokens": [60, 70, 10, 20, 30]})
        self.assertEqual(result["growth"]["tokens_per_turn"], 10)
        self.assertEqual(result["growth"]["intervals"], 2)
        self.assertEqual(result["notices"], ["recent_decrease_resets_growth"])
        result = plan_context_budget({"used_tokens": 10, "recent_turn_tokens": [60, 70, 10]})
        self.assertEqual(result["growth"]["forecast"], [])

    def test_sparse_and_fractional_growth(self):
        self.assertEqual(plan_context_budget({"recent_turn_tokens": [1, 2]})["growth"]["forecast"], [])
        result = plan_context_budget({"reference_limit_tokens": 10, "used_tokens": 2, "recent_turn_tokens": [1, 1, 2]})
        self.assertEqual(result["growth"]["tokens_per_turn"], 0.5)
        self.assertEqual(result["growth"]["turns_until_reference"], 16)
        self.assertEqual(result["growth"]["forecast"][0]["budget"]["used_tokens"], 2.5)

    def test_input_bounds_types_and_conflicts(self):
        bad = [
            {"used_tokens": -1}, {"used_tokens": True}, {"used_tokens": "20"}, {"used_tokens": 1.5},
            {"used_tokens": 10**12+1}, {"used_tokens": 10**1000}, {"reference_limit_tokens": 0},
            {"recent_turn_tokens": [0]*7}, {"recent_turn_tokens": [False]},
            {"used_tokens": 30, "recent_turn_tokens": [10, 20]},
            {"sources": {"used_tokens": "assumed"}}, {"used_tokens": 10, "sources": {"used_tokens": "SECRET"}},
            {"sources": {"path": "SYNTHETIC-PATH"}}, {"messages": []}, {"sources": None}, [],
        ]
        for arguments in bad:
            with self.subTest(arguments=arguments):
                with self.assertRaises(BudgetInputError):
                    plan_context_budget(arguments)

    def test_stateless_pure_calculation_no_io(self):
        args = CASES[1]["arguments"]
        before = copy.deepcopy(args)
        with patch("builtins.open", side_effect=AssertionError("Unexpected file I/O")), patch("socket.socket", side_effect=AssertionError("Unexpected network I/O")), patch("subprocess.Popen", side_effect=AssertionError("Unexpected process")):
            first = plan_context_budget(args)
            plan_context_budget({"used_tokens": 99})
            self.assertEqual(plan_context_budget(args), first)
        self.assertEqual(args, before)

    def test_extreme_numbers_stay_finite_and_schema_valid(self):
        result = plan_context_budget({"reference_limit_tokens": 1, "used_tokens": 10**12, "recent_turn_tokens": [0, 0, 10**12], "planned_increase_tokens": 10**12, "reserved_output_tokens": 10**12})
        validate_schema(result, OUTPUT_SCHEMA)
        encode(result)
        self.assertEqual(result["plan"]["after"]["used_tokens"], 3*10**12)

    def test_json_integral_numbers_match_input_schema(self):
        arguments = {"reference_limit_tokens": 100000.0, "used_tokens": 65000.0, "recent_turn_tokens": [45000.0, 55000.0, 65000.0]}
        validate_schema(arguments, INPUT_SCHEMA)
        result = plan_context_budget(arguments)
        validate_schema(result, OUTPUT_SCHEMA)
        self.assertIs(type(result["inputs"]["used_tokens"]), int)
        self.assertEqual(result["growth"]["tokens_per_turn"], 10000)
        for value in [float("inf"), float("nan"), float("-inf")]:
            with self.assertRaises(BudgetInputError):
                plan_context_budget({"used_tokens": value})


class ProtocolTests(unittest.TestCase):
    def test_discovery_annotations_negotiation_and_ping(self):
        request = copy.deepcopy(INIT)
        request["params"]["protocolVersion"] = "unsupported-client-version"
        self.assertEqual(handle(encode(request))["result"]["protocolVersion"], PROTOCOL)
        result = handle(encode({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))["result"]
        self.assertEqual([tool["name"] for tool in result["tools"]], ["plan_context_budget"])
        self.assertEqual(TOOL["annotations"], {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        self.assertEqual(handle(encode({"jsonrpc": "2.0", "id": 4, "method": "ping"}))["result"], {})

    def test_protocol_errors_and_notifications(self):
        for raw, code in [(b"{", -32700), (b'{"x":NaN}', -32700), (b'{"x":1,"x":2}', -32700), (b"[]", -32600), (b"x"*(MAX_BYTES+1), -32600), (encode({"jsonrpc": "2.0", "id": True, "method": "ping"}), -32600), (encode({"jsonrpc": "2.0", "id": 1, "method": "unknown"}), -32601), (encode({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}), -32602)]:
            with self.subTest(code=code):
                self.assertEqual(handle(raw)["error"]["code"], code)
        self.assertIsNone(handle(encode({"jsonrpc": "2.0", "method": "notifications/initialized"})))
        self.assertIsNone(handle(encode({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})))

    def test_real_stdio_subprocess_eight_cases_and_clean_shutdown(self):
        messages = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        messages.extend(call(case["arguments"], i+3) for i, case in enumerate(CASES))
        completed = subprocess.run([sys.executable, "-B", str(ROOT/"server.py")], input=b"\n".join(encode(m) for m in messages)+b"\n", capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 2+len(CASES))
        self.assertEqual(responses[0]["result"]["protocolVersion"], PROTOCOL)
        for case, response in zip(CASES, responses[2:]):
            CalculatorTests.check_case(self, case, response["result"])

    def test_stdio_oversize_recovery(self):
        completed = subprocess.run([sys.executable, "-B", str(ROOT/"server.py")], input=b"x"*(MAX_BYTES*3)+b"\n"+encode(INIT)+b"\n", capture_output=True, timeout=5)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["result"]["protocolVersion"], PROTOCOL)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = local_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        assert not cls.thread.is_alive()

    def request(self, message=None, method="POST", path="/mcp", headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        merged = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        for key, value in (headers or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        try:
            connection.request(method, path, body=raw if raw is not None else encode(message) if message is not None else None, headers=merged)
            reply = connection.getresponse()
            return reply.status, reply.read(), dict(reply.getheaders())
        finally:
            connection.close()

    def test_real_http_lifecycle_and_eight_cases(self):
        status, body, headers = self.request(INIT, headers={"MCP-Protocol-Version": None})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], PROTOCOL)
        self.assertNotIn("MCP-Session-Id", headers)
        status, body, _ = self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, b""))
        status, body, _ = self.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["tools"][0]["name"], TOOL["name"])
        for case in CASES:
            with self.subTest(case=case["name"]):
                status, body, headers = self.request(call(case["arguments"]))
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(headers["Cache-Control"], "no-store")
                CalculatorTests.check_case(self, case, json.loads(body)["result"])

    def test_loopback_host_origin_and_no_sse(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for headers in [{"Origin": "https://example.invalid"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:1"}, {"Host": "evil.invalid"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(INIT, headers=headers)[0], 403)
        for method in ["GET", "DELETE", "OPTIONS"]:
            self.assertEqual(self.request(method=method)[0], 405)
            self.assertEqual(self.request(method=method, headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request(INIT, headers={"Origin": "http://127.0.0.1:"+str(self.server.server_port)})[0], 200)

    def test_http_media_version_bounds_and_parse_errors(self):
        self.assertEqual(self.request(INIT, path="/other")[0], 404)
        self.assertEqual(self.request(INIT, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(INIT, headers={"Accept": "application/json"})[0], 406)
        self.assertEqual(self.request(INIT, headers={"MCP-Protocol-Version": "unsupported"})[0], 400)
        self.assertEqual(self.request(call({}), headers={"MCP-Protocol-Version": None})[0], 400)
        self.assertEqual(self.request(raw=b"x"*(MAX_BYTES+1))[0], 413)
        self.assertEqual(self.request(INIT, headers={"Transfer-Encoding": "chunked"})[0], 400)
        status, body, _ = self.request(raw=b"{")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
