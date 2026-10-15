#!/usr/bin/env python3
"""Dependency-free local QR receiver, compatible with POST /qr {data: ...}."""
import argparse
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock
from urllib.parse import urlsplit


class QRServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.latest = {"data": "No QR data received yet", "time": ""}
        self.lock = Lock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, data, status=200, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with self.server.lock:
            value = self.server.latest.copy()
        if self.path == "/api/latest":
            self.reply(json.dumps(value).encode())
        elif self.path == "/":
            page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="3"><title>QR receiver</title><style>body{background:#0d121b;color:#e8edf5;font:16px system-ui;max-width:850px;margin:70px auto;padding:24px}a{color:#79d9bd}h1{font-size:34px}.value{background:#182333;padding:32px;border-radius:14px;font-size:30px;overflow-wrap:anywhere}p{color:#9caac0}</style><a href="http://localhost:8070/">← Dashboard</a><h1>QR receiver</h1><p>Latest value from the vision pipeline</p><div class="value">VALUE</div><p>TIME</p><p>Refreshes every 3 seconds</p></html>'''
            before, after = page.split("VALUE")
            page = before + escape(value["data"]) + after.replace("TIME", escape(value["time"]))
            try:
                host = urlsplit("//" + self.headers.get("Host", "localhost")).hostname or "localhost"
            except ValueError:
                host = "localhost"
            if ":" in host:
                host = "[" + host + "]"
            page = page.replace('href="http://localhost:8070/"', 'href="' + escape(f"http://{host}:8070/", quote=True) + '"')
            self.reply(page.encode(), content_type="text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/qr":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Invalid request length")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict) or "data" not in value:
                raise ValueError("Missing QR data")
            with self.server.lock:
                self.server.latest = {"data": str(value["data"]),
                                      "time": datetime.now(timezone.utc).isoformat()}
            self.reply(json.dumps({"status": "received", "data": str(value["data"])}).encode())
        except (ValueError, UnicodeDecodeError):
            self.reply(b'{"status":"error","message":"Expected JSON with data field"}', 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5000)
    options = parser.parse_args()
    with QRServer(("127.0.0.1", options.port)) as server:
        print(f"QR receiver: http://localhost:{options.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
