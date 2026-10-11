"""Offline HTML export and a loopback-only, read-only weather panel."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import hmac
import json
import threading
from urllib.parse import urlsplit

from .storage import WeatherError


def weather_html(report: dict, live: bool = False) -> str:
    data = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = files("token_weather").joinpath("assets/weather.html").read_text(encoding="utf-8")
    return template.replace("__WEATHER_LIVE__", "true" if live else "false").replace("__WEATHER_DATA__", data)


def weather_server(collect, port: int = 0, *, service=None, control_token: str | None = None, on_stop=None) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise WeatherError("Weather panel port must be 0–65535")

    class Handler(BaseHTTPRequestHandler):
        def allowed_host(self):
            # A Host check prevents another origin from rebinding DNS to this server.
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return self.headers.get("Host") in hosts

        def respond(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed_host():
                self.send_error(403)
                return
            route = urlsplit(self.path).path
            if route == "/api/service/status" and service is not None:
                self.respond(json.dumps(service()).encode(), "application/json; charset=utf-8")
                return
            if route not in ("/", "/api/weather"):
                self.send_error(404)
                return
            try:
                report = collect()
                body = (json.dumps(report, ensure_ascii=False) if route == "/api/weather" else weather_html(report, live=True)).encode()
            except (WeatherError, OSError, ValueError, TypeError, KeyError):
                self.send_error(503, "Weather reading unavailable")
                return
            self.respond(body, "application/json; charset=utf-8" if route == "/api/weather" else "text/html; charset=utf-8")

        def do_POST(self):
            if service is None or control_token is None or on_stop is None:
                self.send_error(501)
                return
            identity = service()
            if (not self.allowed_host() or self.headers.get("Origin") is not None
                    or urlsplit(self.path).path != "/api/service/stop"
                    or self.headers.get("X-Token-Weather-Instance") != identity["instance_id"]
                    or not hmac.compare_digest(self.headers.get("X-Token-Weather-Control", ""), control_token)):
                self.send_error(403)
                return
            if self.headers.get("Content-Length") != "2" or self.rfile.read(2) != b"{}":
                self.send_error(400)
                return
            self.respond(json.dumps(identity).encode(), "application/json; charset=utf-8")
            on_stop()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
