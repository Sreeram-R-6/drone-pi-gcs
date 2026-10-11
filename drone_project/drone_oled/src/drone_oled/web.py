"""Local/Tailscale OLED mirror with four deliberate virtual buttons."""

from __future__ import annotations

import argparse
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets

from .webbridge import call

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drone OLED</title><style>
*{box-sizing:border-box}body{margin:0;background:#101414;color:#e8f5ed;font:16px system-ui;min-height:100vh;display:grid;place-items:center}
main{width:min(560px,94vw);text-align:center}h1{font-size:22px;font-weight:500}#screen{display:block;width:100%;aspect-ratio:2;background:#000;border:12px solid #26332d;border-radius:12px;image-rendering:pixelated}
#status{min-height:24px;color:#a9cbb7;margin:18px 0}.buttons{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}button{min-height:58px;background:#263c31;color:#fff;border:1px solid #567c65;border-radius:9px;font:600 15px system-ui;cursor:pointer;touch-action:manipulation}button:active{background:#436c54}button:disabled{opacity:.4;cursor:default}small{display:block;margin:20px;color:#8da797}
</style><main><h1>DRONE OLED</h1><img id="screen" alt="Live 128 by 64 OLED display"><p id="status" role="status">CONNECTING…</p><div class="buttons">
<button data-key="prev" disabled>← PREV</button><button data-key="next" disabled>NEXT →</button><button data-key="ok" disabled>OK</button><button data-key="back" disabled>BACK</button></div><small>PREV / NEXT navigate. OK selects. BACK returns.<br>Vehicle confirmations also apply here.</small></main><script>
const token='__TOKEN__',img=document.querySelector('#screen'),status=document.querySelector('#status'),buttons=[...document.querySelectorAll('button')];let generation=null,online=false,busy=false;
function enable(){buttons.forEach(b=>b.disabled=!online||busy)}
async function poll(){try{const r=await fetch('/api/frame',{cache:'no-store',signal:AbortSignal.timeout(1500)}),d=await r.json();if(!r.ok||d.error||d.age>1)throw Error(d.error||'DISPLAY STALE');img.src='data:image/png;base64,'+d.frame;generation=d.generation;online=true;status.textContent=d.display_ok?'LIVE · '+d.screen.toUpperCase():'OLED WRITE FAILED · '+d.screen.toUpperCase()}catch(e){online=false;status.textContent='DISCONNECTED · '+e.message}enable();setTimeout(poll,200)}
buttons.forEach(b=>b.addEventListener('click',async()=>{if(!online||busy)return;busy=true;enable();try{const r=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json','X-OLED-Token':token},body:JSON.stringify({key:b.dataset.key,generation,id:Date.now().toString(36)+"-"+Math.random().toString(36).slice(2)}),signal:AbortSignal.timeout(1500)});const d=await r.json();if(!r.ok||d.error)status.textContent=d.error||'INPUT FAILED'}catch(e){status.textContent='INPUT FAILED · '+e.message}finally{busy=false;enable()}}));poll();
</script></html>"""


def server(
    path: str, port: int = 8090, bind: str = "127.0.0.1", allowed_hosts: tuple[str, ...] = ()
) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Frame polling must not flood journald; actual UI actions are logged by app.
            pass

        def reply(self, status: int, body: bytes, mime="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            peer = ipaddress.ip_address(self.client_address[0])
            if not peer.is_loopback and peer not in ipaddress.ip_network("100.64.0.0/10"):
                self.reply(403, b'{"error":"TAILSCALE OR LOCALHOST ONLY"}')
                return False
            if self.headers.get("Host") not in hosts:
                self.reply(403, b'{"error":"HOST NOT ALLOWED"}')
                return False
            return True

        def do_GET(self):
            if not self.valid_host():
                return
            if self.path == "/":
                self.reply(
                    200, PAGE.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8"
                )
            elif self.path == "/api/frame":
                self.backend({"op": "frame"})
            else:
                self.reply(404, b'{"error":"NOT FOUND"}')

        def do_POST(self):
            if not self.valid_host():
                return
            if self.path != "/api/key":
                self.reply(404, b'{"error":"NOT FOUND"}')
                return
            if self.headers.get("Origin") not in origins or not secrets.compare_digest(
                self.headers.get("X-OLED-Token", ""), token
            ):
                self.reply(403, b'{"error":"INVALID ORIGIN OR TOKEN"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError()
                self.connection.settimeout(2)
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError()
                request["op"] = "key"
            except (ValueError, OSError):
                self.reply(400, b'{"error":"INVALID REQUEST"}')
                return
            self.backend(request)

        def backend(self, request):
            try:
                result = call(path, request)
                self.reply(409 if "error" in result else 200, json.dumps(result).encode())
            except (OSError, ValueError):
                self.reply(503, b'{"error":"OLED SERVICE UNAVAILABLE"}')

    http = ThreadingHTTPServer((bind, port), Handler)
    actual_port = http.server_address[1]
    hosts.clear()
    hosts.update({f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"})
    hosts.update(f"{host}:{actual_port}" for host in allowed_hosts)
    origins.clear()
    origins.update(f"http://{host}" for host in hosts)
    http.daemon_threads = True
    return http


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/drone-oled/ui.sock")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--allow-host", action="append", default=[])
    args = parser.parse_args()
    http = server(args.socket, args.port, args.bind, tuple(args.allow_host))
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()


if __name__ == "__main__":
    main()
