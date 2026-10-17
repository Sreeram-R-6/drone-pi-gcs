#!/usr/bin/env python3
"""Mobile drone dashboard and explicit allowlisted flight-script launcher."""
import argparse
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import secrets
import sys
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name("web")
sys.path.insert(0,str(ASSETS))
from script_runs import ScriptRuns, RunConflict
from geofence_service import GeofenceService


def load_config(path):
    data = json.loads(Path(path).read_text())
    for item in data["sites"] + data.get("support_services", []):
        if not re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", item["unit"]):
            raise ValueError("Invalid service unit")
    for item in data["sites"]:
        if type(item["port"]) is not int or not 1 <= item["port"] <= 65535:
            raise ValueError("Invalid local website port")
    return data


def unit_status(unit):
    try:
        result = subprocess.run(["systemctl", "show", unit, "--property=ActiveState,UnitFileState"],
                                capture_output=True, text=True, timeout=2)
        if result.returncode:
            return {"state": "unknown", "startup": "unknown"}
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return {"state": values.get("ActiveState", "unknown"),
                "startup": values.get("UnitFileState") or "not-installed"}
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "unknown", "startup": "unknown"}


def site_status(item):
    row = {**item, **unit_status(item["unit"])}
    row["url"] = f"http://localhost:{item['port']}/"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{item['port']}/", timeout=1) as response:
            row["online"] = 200 <= response.status < 400
    except (OSError, URLError):
        row["online"] = False
    return row


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config, runs=None, fences=None):
        self.fences = fences or GeofenceService()
        self.config = config
        self.runs = runs or ScriptRuns()
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)

    def server_close(self):
        self.fences.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def valid_host(self):
        try:
            host=urlsplit('//'+self.headers.get('Host','')).hostname or ''
            allowed=host in {'localhost','127.0.0.1','::1'} or host.endswith('.ts.net')
            if not allowed:
                address=ipaddress.ip_address(host)
                allowed=(address.version==4 and address in ipaddress.ip_network('100.64.0.0/10')) or (address.version==6 and address in ipaddress.ip_network('fd7a:115c:a1e0::/48'))
            if allowed:return True
        except ValueError:pass
        self.reply({'error':'Use localhost or this device’s Tailscale address.'},status=403)
        return False

    def log_message(self, format, *args):
        if not self.path.startswith('/api/'):
            super().log_message(format,*args)

    def reply(self, data, kind='application/json; charset=utf-8', status=200):
        if not isinstance(data,bytes):
            data=json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        host=urlsplit('//'+self.headers.get('Host','localhost')).hostname or 'localhost'
        if ':' in host:host='['+host+']'
        map_policy="; img-src 'self' data: https://tile.openstreetmap.org; style-src 'self' 'unsafe-inline'" if self.path=='/geofence' else "; style-src 'self'"
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'"+map_policy+"; frame-src http://"+host+":8071; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def do_POST(self):
        if not self.valid_host():return
        if (self.headers.get('Origin') != 'http://'+self.headers.get('Host','') or
            not secrets.compare_digest(self.headers.get('X-Dashboard-Token',''),self.server.token)):
            self.reply({'error':'Reload this page before using flight controls.'},status=403)
            return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=(65536 if self.path.startswith('/api/geofence/') else 2048):raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Invalid request')
            if self.path.startswith('/api/geofence/'):
                result=self.server.fences.launch(self.path.rsplit('/',1)[1],data)
            elif self.path=='/api/scripts/prepare':
                result=self.server.runs.prepare(data.get('script'),data.get('record_video'))
            elif self.path=='/api/scripts/run':
                if not isinstance(data.get('ticket'),str):raise ValueError('Review the flight first')
                result=self.server.runs.launch(data['ticket'],data.get('confirmed') is True)
            elif self.path=='/api/scripts/stop':
                result=self.server.runs.stop()
            else:
                self.reply({'error':'Not found'},status=404);return
            self.reply(result,status=202)
        except RunConflict as error:self.reply({'error':str(error)},status=409)
        except (ValueError,TypeError) as error:self.reply({'error':str(error)},status=400)
        except (OSError,RuntimeError) as error:self.reply({'error':str(error)},status=503)

    def do_GET(self):
        if not self.valid_host():return
        assets={'/':('index.html','text/html; charset=utf-8'),
                '/scripts':('scripts.html','text/html; charset=utf-8'),
                '/dashboard.css':('dashboard.css','text/css; charset=utf-8'),
                '/dashboard.js':('dashboard.js','text/javascript; charset=utf-8'),
                '/scripts.js':('scripts.js','text/javascript; charset=utf-8')}
        if self.path=='/api/geofence':
            self.reply({'token':self.server.token,**self.server.fences.status()});return
        if self.path.startswith('/map-assets/'):
            name=self.path[len('/map-assets/'):];relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts or not re.fullmatch(r'[a-zA-Z0-9_./-]+',name):self.send_error(404);return
            path=ASSETS/'vendor/map'/relative
            if not path.is_file():self.send_error(404);return
            kind={'.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.png':'image/png','.svg':'image/svg+xml'}.get(path.suffix)
            if not kind:self.send_error(404);return
            self.reply(path.read_bytes(),kind);return
        assets.update({'/geofence':('geofence.html','text/html; charset=utf-8'),'/geofence.js':('geofence.js','text/javascript; charset=utf-8'),'/geofence.css':('geofence.css','text/css; charset=utf-8')})
        if self.path in assets:
            name,kind=assets[self.path]
            self.reply((ASSETS/name).read_bytes(),kind);return
        elif self.path=='/api/scripts':
            try:self.reply({'token':self.server.token,'run':self.server.runs.status()})
            except (OSError,RuntimeError,ValueError) as error:self.reply({'error':str(error)},status=503)
            return
        elif self.path == "/api/sites":
            with ThreadPoolExecutor(max_workers=5) as pool:
                sites = list(pool.map(site_status, self.server.config["sites"]))
                support = list(pool.map(lambda item: {**item, **unit_status(item["unit"])},
                                        self.server.config.get("support_services", [])))
            data = json.dumps({"sites": sites, "support_services": support}).encode()
            content_type = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.reply(data,content_type)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8070)
    parser.add_argument("--config", type=Path, default=ASSETS / "sites.json")
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("--port must be 1..65535")
    with DashboardServer(("127.0.0.1", options.port), load_config(options.config)) as server:
        print(f"Drone dashboard: http://localhost:{options.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
