"""Temporary local demo of actual MCP calls; no cloud hosting or paid APIs."""
import argparse
from http.server import HTTPServer
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Handler


class DemoHandler(Handler):
    def do_GET(self):
        asset_paths = {"/assets/" + name + ".svg" for name in ("clear", "fair", "cloudy", "showers", "storm")}
        if self.path not in {"/", "/index.html", "/icon.png"} | asset_paths:
            return super().do_GET()
        original = self.path
        self.path = "/mcp"
        permitted = self.allowed()
        self.path = original
        if not permitted:
            return
        icon = original == "/icon.png"
        vector = original in asset_paths
        source = ROOT / "demo" / original.lstrip("/") if vector else ROOT.parent / "assets/icon.png" if icon else ROOT / "demo/index.html"
        body = source.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml" if vector else "image/png" if icon else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    with HTTPServer(("127.0.0.1", args.port), DemoHandler) as server:
        print("Demo: http://127.0.0.1:" + str(server.server_port) + "/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
