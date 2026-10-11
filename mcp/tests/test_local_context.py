from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from budget import BudgetInputError
from local_context import local_context
from local_server import local_handle
from server import encode, handle


class LocalTransportTests(unittest.TestCase):
    def test_local_tool_never_appears_in_remote_server(self):
        request = encode({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in handle(request)["result"]["tools"]], ["plan_context_budget"])
        self.assertEqual([tool["name"] for tool in local_handle(request)["result"]["tools"]], ["plan_context_budget", "inspect_local_context"])

    def test_malformed_local_request_never_reads(self):
        raw = b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/call","params":{"name":"inspect_local_context"}}'
        self.assertEqual(local_handle(raw)["error"]["code"], -32700)


@unittest.skipIf(sys.version_info < (3, 11), "Local Codex parser requires Python 3.11+")
class LocalObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"; self.workspace = self.base / "work"
        self.home.mkdir(); self.workspace.mkdir(); (self.home / "sessions").mkdir()
        self.path = self.home / "sessions/rollout-synthetic-root.jsonl"
        self.at = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc).timestamp()
        stamp = datetime.fromtimestamp(self.at, timezone.utc).isoformat()
        rows = [
            {"type": "session_meta", "payload": {"id": "synthetic-root", "cwd": str(self.workspace), "cli_version": "0.160.0", "source": "cli"}},
            {"type": "turn_context", "timestamp": stamp, "payload": {"model": "synthetic-model", "effort": "medium", "turn_id": "synthetic-turn"}},
            {"type": "response_item", "payload": {"type": "message", "text": "PRIVATE-SYNTHETIC-MESSAGE"}},
            {"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 59000, "output_tokens": 6000, "cached_input_tokens": 30000, "reasoning_output_tokens": 1000, "total_tokens": 65000},
                "total_token_usage": {"total_tokens": 999999}, "model_context_window": 100000,
            }}},
        ]
        self.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.args = {"thread_id": "synthetic-root", "workspace": str(self.workspace), "planned_increase_tokens": 20000, "reserved_output_tokens": 5000}

    def tearDown(self):
        self.temp.cleanup()

    def test_reads_available_metrics_without_returning_text_ids_paths(self):
        before = self.path.read_bytes()
        output = local_context(self.args, now=self.at+10, home=self.home)
        self.assertEqual(output["observations"]["tokens"], 65000)
        self.assertEqual(output["budget"]["plan"]["after"]["remaining_tokens"], 10000)
        self.assertEqual(output["adaptation"]["suggested_max_task_increase_tokens"], 30000)
        serialised = json.dumps(output)
        for marker in ["PRIVATE-SYNTHETIC-MESSAGE", "synthetic-root", "synthetic-turn", str(self.workspace), str(self.path)]:
            self.assertNotIn(marker, serialised)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(output["automatic_control"]["model_changes_enabled"])
        self.assertFalse(output["automatic_control"]["effort_changes_enabled"])
        self.assertFalse(output["automatic_control"]["chat_rotation_enabled"])

    def test_stale_usage_never_drives_current_advice(self):
        output = local_context(self.args, now=self.at+1000, home=self.home)
        self.assertTrue(output["observations"]["stale"])
        self.assertIsNone(output["budget"]["current"]["used_tokens"])
        self.assertIsNone(output["adaptation"]["suggested_max_task_increase_tokens"])
        self.assertEqual(output["adaptation"]["handover_advice"], "refresh_observation")

    def test_heuristic_reserve_is_labelled_and_not_a_platform_limit(self):
        args = dict(self.args); args.pop("reserved_output_tokens")
        output = local_context(args, now=self.at+10, home=self.home)
        self.assertEqual(output["adaptation"]["suggested_output_reserve_tokens"], 9000)
        self.assertIn("heuristic", output["adaptation"]["reserve_basis"])
        self.assertIsNone(output["budget"]["plan"]["after"]["used_tokens"])

    def test_workspace_mismatch_and_extra_fields_refused(self):
        args = dict(self.args); args["workspace"] = str(self.home)
        with self.assertRaises(Exception):
            local_context(args, now=self.at, home=self.home)
        args = dict(self.args); args["messages"] = []
        with self.assertRaises(BudgetInputError):
            local_context(args, now=self.at, home=self.home)
