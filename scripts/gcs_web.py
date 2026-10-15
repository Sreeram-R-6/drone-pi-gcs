#!/usr/bin/env python3
"""Browser GCS using the terminal controller and its existing flight gates."""
import argparse
from collections import deque
import curses
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'drone_project/pi_flight'))
from flightcheck.terminal import Terminal
import terminal_gcs

KEYS = {'up': curses.KEY_UP, 'down': curses.KEY_DOWN, 'left': curses.KEY_LEFT,
        'right': curses.KEY_RIGHT, 'pageup': curses.KEY_PPAGE,
        'pagedown': curses.KEY_NPAGE, 'home': curses.KEY_HOME,
        'enter': 10, 'escape': 27, 'backspace': 127}


class Screen:
    """A fixed-size browser canvas implementing Terminal's curses screen API."""
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = deque(maxlen=128)
        self.frame = []
        self.erase()

    def keypad(self, value): pass
    def nodelay(self, value): pass
    def getmaxyx(self): return 44, 140
    def erase(self): self.cells = [[' '] * 140 for _ in range(44)]
    def addnstr(self, y, x, value, limit, attr=0):
        for offset, char in enumerate(value[:limit]):
            self.cells[y][x + offset] = char
    def refresh(self):
        with self.lock:
            self.frame = [''.join(row).rstrip() for row in self.cells]
    def getch(self):
        with self.lock:
            return self.pending.popleft() if self.pending else -1
    def clear_input(self):
        with self.lock:
            self.pending.clear()


class BrowserTerminal(Terminal):
    def flush_input(self): self.screen.clear_input()


class Session:
    def __init__(self, config, offline=False):
        self.config, self.offline = config, offline
        self.external_run = None
        self.handover = threading.Lock()
        self.screen = Screen()
        self.ui = BrowserTerminal(self.screen)
        self.token = secrets.token_urlsafe(32)
        self.error = None
        self.thread = threading.Thread(target=self.run, args=(config, offline), daemon=True)

    def run(self, config, offline):
        try:
            terminal_gcs.dashboard(self.ui, ['--config', str(config), '--monitor'] + (['--offline'] if offline else []))
        except Exception as exc:
            self.error = str(exc)

    def snapshot(self):
        with self.screen.lock:
            return {'lines': self.screen.frame, 'state': self.ui.view_state,
                    'notice': self.ui.notice, 'prompt': self.ui.prompt,
                    'running': self.thread.is_alive(), 'error': self.error,
                    'token': self.token, 'script_run': self.external_run,
                    'receive_only': self.ui.receive_only}

    def script_pause(self, run_id):
        with self.handover:
            if self.external_run and self.external_run != run_id:
                raise ValueError('Another script owns the handover')
            if self.external_run == run_id and not self.thread.is_alive():
                return
            with self.screen.lock:
                ui = self.ui
                if (not ui.receive_only or ui.mission or getattr(ui,'arm_control_active',False)
                        or getattr(ui,'manual_action',None) or getattr(ui,'service_request',None)
                        or getattr(ui,'menu_requested',False)):
                    raise ValueError('GCS operation in progress; return to the idle monitor first')
                transport = getattr(ui,'transport',None)
                if transport is not None and transport.armed():
                    raise ValueError('Vehicle already armed; script handover refused')
                self.external_run = run_id
                self.screen.pending.clear()
                ui.quit_requested = True
            self.thread.join(timeout=6)
            if self.thread.is_alive():
                raise ValueError('GCS has not released control; launch refused')
            self.ui.notice = 'Telemetry/control released to script '+run_id[:8]

    def script_resume(self, run_id):
        with self.handover:
            if self.external_run != run_id:
                raise ValueError('Script handover does not match')
            if self.thread.is_alive():
                raise ValueError('GCS is still closing')
            self.ui = BrowserTerminal(self.screen)
            self.error = None
            self.external_run = None
            self.thread = threading.Thread(target=self.run, args=(self.config,self.offline), daemon=True)
            self.thread.start()

    def key(self, value):
        if self.external_run:
            raise ValueError('GCS released to an active script; use the Scripts page')
        if not self.thread.is_alive():
            raise ValueError('Session ended; restart the service deliberately to open a new monitor.')
        if value in KEYS:
            code = KEYS[value]
        elif isinstance(value, str) and len(value) == 1 and 32 <= ord(value) < 127:
            code = ord(value)
        else:
            raise ValueError('Send one key at a time')
        with self.screen.lock:
            if len(self.screen.pending) >= 128:
                raise ValueError('Input queue full')
            self.screen.pending.append(code)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, session):
        self.session = session
        super().__init__(address, Handler)


    def service_actions(self):
        # A crashed controller thread must not leave a falsely healthy HTTP server.
        # systemd restarts the process into receive-only monitoring, never a mission.
        if self.session.error is not None:
            raise RuntimeError('GCS controller failed: ' + self.session.error)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Poll requests must not fill the shared controller event log.

    def reply(self, status, data, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def do_GET(self):
        assets = {'/': ('gcs.html', 'text/html; charset=utf-8'),
                  '/gcs.js': ('gcs.js', 'text/javascript; charset=utf-8'),
                  '/gcs.css': ('gcs.css', 'text/css; charset=utf-8')}
        if self.path == '/api/state':
            self.reply(200, json.dumps(self.server.session.snapshot()).encode())
        elif self.path in assets:
            name, kind = assets[self.path]
            self.reply(200, (ROOT / 'scripts/web' / name).read_bytes(), kind)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in ('/api/key','/api/script/pause','/api/script/resume'):
            self.send_error(404)
            return
        # A per-process token plus strict same-origin checks block cross-site control.
        origin = self.headers.get('Origin')
        if (origin and urlsplit(origin).netloc != self.headers.get('Host')) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send_error(403)
            return
        if not secrets.compare_digest(self.headers.get('X-GCS-Token', ''), self.server.session.token):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 256:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/key':
                self.server.session.key(data['key'])
            else:
                run_id = data['run_id']
                if not isinstance(run_id,str) or len(run_id)!=32 or any(c not in '0123456789abcdef' for c in run_id):
                    raise ValueError('Invalid run identifier')
                if self.path.endswith('/pause'):
                    self.server.session.script_pause(run_id)
                else:
                    self.server.session.script_resume(run_id)
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(400, json.dumps({'error': str(exc)}).encode())
            return
        self.reply(200, b'{"ok":true}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8071)
    parser.add_argument('--config', type=Path, default=ROOT / 'drone_project/pi_flight/config.toml')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    session = Session(args.config, args.offline)
    with Server(('127.0.0.1', args.port), session) as server:
        session.thread.start()
        try: server.serve_forever()
        except KeyboardInterrupt: pass


if __name__ == '__main__': main()
