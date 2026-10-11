import http.client
import importlib.util
from http.server import HTTPServer
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_preview", ROOT / "demo/server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DemoAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), module.DemoHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_real_licensed_assets_and_csp_load_same_origin(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"Scenario planner", body)
        for name in ("clear", "fair", "cloudy", "showers", "storm"):
            status, headers, body = self.get("/assets/" + name + ".svg")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", body)

    def test_assets_are_not_an_arbitrary_file_endpoint(self):
        for path in ("/assets/../../plugin.json", "/assets/LICENSE-NOTO.txt", "/assets/nonexistent.svg"):
            self.assertEqual(self.get(path)[0], 404)
        self.assertEqual(self.get("/assets/clear.svg", {"Origin": "https://untrusted.invalid"})[0], 403)
