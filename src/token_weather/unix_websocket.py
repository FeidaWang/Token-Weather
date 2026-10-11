"""Bounded JSON-RPC over a user-owned private Unix WebSocket endpoint.

Only an explicit canonical socket is accepted. No endpoint discovery, TCP
connection, listener, authentication token or optional dependency is involved.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import secrets
import selectors
import socket
import struct
import time

from .desktop_adapter import JsonRpc
from .rpc_correlation import ResponseCorrelator
from .storage import WeatherError

MAX_MESSAGE = 2_000_000
MAX_HEADERS = 16_384
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class UnixWebSocketRpc(JsonRpc):
    def __init__(self, socket_path: Path):
        from .owner_session import validate_owner_socket
        validate_owner_socket(socket_path)
        self.closed = False
        self.process = None
        self.counter = 0
        self.notifications = []
        self.correlation = ResponseCorrelator()
        self.buffer = b""
        self.fragments = None
        self.selector = selectors.DefaultSelector()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        try:
            self.socket.connect(str(socket_path))
            self._handshake()
            self.selector.register(self.socket, selectors.EVENT_READ)
        except (OSError, WeatherError):
            self.close()
            raise WeatherError("Private Unix WebSocket connection or handshake failed") from None

    def _handshake(self):
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        deadline = time.monotonic() + 5
        self.socket.sendall(request.encode("ascii"))
        received = b""
        while b"\r\n\r\n" not in received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WeatherError("Unix WebSocket handshake timed out")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(4096)
            if not chunk:
                raise WeatherError("Unix WebSocket disconnected during handshake")
            received += chunk
            boundary = received.find(b"\r\n\r\n")
            if (boundary < 0 and len(received) > MAX_HEADERS) or boundary > MAX_HEADERS:
                raise WeatherError("Oversized Unix WebSocket handshake")
        header, self.buffer = received.split(b"\r\n\r\n", 1)
        try:
            rows = header.decode("ascii").split("\r\n")
            if rows[0].split(" ", 2)[:2] != ["HTTP/1.1", "101"]:
                raise ValueError("status")
            headers = {}
            for row in rows[1:]:
                name, value = row.split(":", 1)
                name = name.lower()
                if not name or name in headers or name.strip() != name:
                    raise ValueError("header")
                headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
            if (headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in [token.strip().lower() for token in headers.get("connection", "").split(",")]
                    or headers.get("sec-websocket-accept") != expected
                    or "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
                raise ValueError("upgrade")
        except (UnicodeError, ValueError):
            raise WeatherError("Invalid Unix WebSocket upgrade response") from None
        self.socket.settimeout(5)

    def _fail(self, message):
        self.close()
        raise WeatherError(message)

    def _send_frame(self, opcode: int, payload: bytes):
        if self.closed:
            raise WeatherError("Unix WebSocket transport is closed")
        if len(payload) > MAX_MESSAGE:
            raise WeatherError("Oversized Unix WebSocket request")
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length <= 65535:
            header += b"\xfe" + struct.pack("!H", length)
        else:
            header += b"\xff" + struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        try:
            self.socket.sendall(header + mask + masked)
        except OSError:
            self._fail("Unix WebSocket write failed; reconcile before retrying")

    def write(self, message: dict) -> None:
        self._send_frame(1, json.dumps(message).encode("utf-8"))

    def _decode(self, payload: bytes) -> dict:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            self._fail("Invalid Unix WebSocket JSON message")
        if not isinstance(message, dict):
            self._fail("Unix WebSocket message must be an object")
        return message

    def _take_message(self) -> dict | None:
        for _ in range(128):
            if len(self.buffer) < 2:
                return None
            first, second = self.buffer[:2]
            fin, opcode = bool(first & 0x80), first & 0x0f
            if first & 0x70 or second & 0x80 or opcode not in (0, 1, 8, 9, 10):
                self._fail("Unsupported Unix WebSocket frame")
            length, offset = second & 0x7f, 2
            if length in (126, 127):
                extra = 2 if length == 126 else 8
                if len(self.buffer) < offset + extra:
                    return None
                length = int.from_bytes(self.buffer[offset:offset + extra], "big")
                if length < (126 if extra == 2 else 65536):
                    self._fail("Invalid Unix WebSocket frame length")
                offset += extra
            if length > MAX_MESSAGE or opcode >= 8 and (not fin or length > 125):
                self._fail("Oversized or fragmented Unix WebSocket frame")
            if len(self.buffer) < offset + length:
                return None
            payload, self.buffer = self.buffer[offset:offset + length], self.buffer[offset + length:]
            if opcode == 8:
                self._fail("Unix WebSocket disconnected; evidence is incomplete")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                if self.fragments is not None:
                    self._fail("Overlapping Unix WebSocket messages")
                if fin:
                    return self._decode(payload)
                self.fragments = payload
            else:
                if self.fragments is None:
                    self._fail("Unexpected Unix WebSocket continuation")
                self.fragments += payload
                if len(self.fragments) > MAX_MESSAGE:
                    self._fail("Oversized Unix WebSocket message")
                if fin:
                    payload, self.fragments = self.fragments, None
                    return self._decode(payload)
        self._fail("Unix WebSocket frame limit reached; evidence is incomplete")

    def _receive(self, timeout: float) -> bool:
        if self.closed:
            raise WeatherError("Unix WebSocket transport is closed")
        if not self.selector.select(timeout):
            return False
        try:
            chunk = self.socket.recv(65536)
        except OSError:
            self._fail("Unix WebSocket receive failed; evidence is incomplete")
        if not chunk:
            self._fail("Unix WebSocket disconnected; evidence is incomplete")
        self.buffer += chunk
        if len(self.buffer) + len(self.fragments or b"") > MAX_MESSAGE + 65550:
            self._fail("Oversized Unix WebSocket receive buffer")
        return True

    def _read_message(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if self.closed:
                raise WeatherError("Unix WebSocket transport is closed")
            message = self._take_message()
            if message is not None:
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._receive(remaining):
                raise WeatherError("App-server event timed out; evidence is incomplete")

    def poll_notification(self) -> dict | None:
        for _ in range(100):
            if self.notifications:
                message = self.notifications.pop(0)
            else:
                message = self._take_message()
                if message is None:
                    if not self._receive(0):
                        return None
                    message = self._take_message()
                    if message is None:
                        return None
            self.correlation.capture(message)
            if "method" in message and "id" in message:
                self._deny_request(message)
            elif "method" in message:
                return message
        raise WeatherError("App-server message limit reached; evidence is incomplete")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.correlation.clear()
        self.selector.close()
        self.socket.close()
        self.buffer = b""
        self.fragments = None
        self.notifications.clear()
