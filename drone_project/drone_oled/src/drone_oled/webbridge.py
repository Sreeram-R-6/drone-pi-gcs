"""Local Unix socket bridge; only the OLED main loop consumes virtual buttons."""

from __future__ import annotations

import base64
from collections import deque
import io
import json
from pathlib import Path
from queue import Full, Queue
import socket
import socketserver
from threading import Lock, Thread
import time


class Bridge:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = Lock()
        self.keys: Queue = Queue(maxsize=16)
        self.latest: dict = {}
        self.seen: deque[str] = deque(maxlen=128)
        self.last_press = 0.0
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(1)
                try:
                    request = json.loads(self.rfile.readline(2049))
                    result = bridge.request(request)
                except (ValueError, OSError, TypeError, AttributeError):
                    result = {"error": "INVALID REQUEST"}
                self.wfile.write(json.dumps(result).encode() + b"\n")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Do not remove another live renderer's socket.
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink(missing_ok=True)
            else:
                raise RuntimeError("OLED bridge already running")
            finally:
                probe.close()
        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        self.path.chmod(0o600)
        self.thread = Thread(target=self.server.serve_forever, daemon=True, name="oled-bridge")
        self.thread.start()

    def publish(self, frame, generation: int, screen: str, display_ok: bool):
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        with self.lock:
            self.latest = dict(
                frame=base64.b64encode(buf.getvalue()).decode(),
                generation=generation,
                screen=screen,
                display_ok=display_ok,
                stamp=time.monotonic(),
            )

    def request(self, request: dict) -> dict:
        with self.lock:
            now = time.monotonic()
            if request.get("op") == "frame":
                if not self.latest:
                    return {"error": "OLED STARTING"}
                result = dict(self.latest)
                result["age"] = now - result.pop("stamp")
                return result
            if request.get("op") != "key" or request.get("key") not in (
                "prev",
                "next",
                "ok",
                "back",
            ):
                return {"error": "INVALID KEY"}
            ident = request.get("id")
            if not isinstance(ident, str) or not 1 <= len(ident) <= 64:
                return {"error": "INVALID ID"}
            if ident in self.seen:
                return {"error": "DUPLICATE PRESS"}
            if not self.latest or now - self.latest["stamp"] > 1:
                return {"error": "DISPLAY STALE"}
            if (
                type(request.get("generation")) is not int
                or request["generation"] != self.latest["generation"]
            ):
                return {"error": "SCREEN CHANGED"}
            if now - self.last_press < 0.2:
                return {"error": "PRESS TOO FAST"}
            try:
                self.keys.put_nowait((request["key"], request["generation"], now))
            except Full:
                return {"error": "INPUT BUSY"}
            self.last_press = now
            self.seen.append(ident)
            return {"accepted": True}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def call(path: str, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        connection.connect(path)
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            data = stream.readline(32769)
        if len(data) > 32768:
            raise ValueError("Bridge response too large")
        return json.loads(data)
