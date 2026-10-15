#!/usr/bin/env python3
"""Browser PTY connected to the Pi's SSH server; SSH handles Pi authentication."""
import argparse
import base64
import fcntl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import pty
import pwd
import secrets
import select
import shlex
import signal
import struct
import termios
import threading
import time
from urllib.parse import parse_qs, urlsplit

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'scripts/web'
STATE=ROOT/'video/.terminal'


def ssh_command(mode, state_dir=STATE):
    if mode not in {'shell','mavproxy','codex'}:raise ValueError('Choose Pi terminal, MAVProxy or Codex')
    state_dir.mkdir(parents=True,exist_ok=True)
    keys=[]
    for path in Path('/etc/ssh').glob('ssh_host_*_key.pub'):
        fields=path.read_text().split()
        if len(fields)>=2:keys.append('127.0.0.1 '+fields[0]+' '+fields[1])
    if not keys:raise RuntimeError('Pi SSH host keys unavailable')
    known=state_dir/'known_hosts'
    known.write_text('\n'.join(keys)+'\n');known.chmod(0o600)
    python=ROOT/'drone_project/pi_flight/.venv/bin/python'
    remote='cd '+shlex.quote(str(ROOT))+' && '
    if mode=='mavproxy':
        remote+=shlex.join([str(python),str(ASSETS/'mavproxy_session.py')])+'; '
    elif mode=='codex':
        codex=Path(pwd.getpwuid(os.getuid()).pw_dir)/'.local/bin/codex'
        if not codex.is_file():raise RuntimeError('Codex CLI is not installed for this Pi account')
        remote+=shlex.join([str(codex),'--cd',str(ROOT),'--no-alt-screen'])+'; '
    remote+='exec /bin/bash -l'
    return ['/usr/bin/ssh','-tt','-o','StrictHostKeyChecking=yes',
            '-o',f'UserKnownHostsFile={known}','-o','PubkeyAuthentication=no',
            '-o','PreferredAuthentications=keyboard-interactive,password',
            '-o','NumberOfPasswordPrompts=3','-o','ServerAliveInterval=15',
            '-o','ServerAliveCountMax=3',pwd.getpwuid(os.getuid()).pw_name+'@127.0.0.1',remote]


class TerminalSession:
    def __init__(self, command=ssh_command):
        self.token=secrets.token_urlsafe(32)
        self.command=command
        self.lock=threading.RLock()
        self.pid=None;self.fd=None;self.output=bytearray();self.base=0
        self.mode=None;self.connected=False;self.last_seen=time.monotonic()
        self.reader=None;self.generation=secrets.token_hex(8)

    def start(self,mode):
        if mode not in {'shell','mavproxy','codex'}:raise ValueError('Choose Pi terminal, MAVProxy or Codex')
        with self.lock:
            if self.connected:raise ValueError('Disconnect the current terminal first')
            if self.reader and self.reader.is_alive():raise ValueError('Previous terminal is still closing; try again')
            command=self.command(mode)
            environment=dict(os.environ,TERM='xterm-256color',COLORTERM='truecolor')
            pid,fd=pty.fork()
            if pid==0:
                try:os.execve(command[0],command,environment)
                except BaseException:os._exit(127)
            self.pid,self.fd,self.mode,self.connected=pid,fd,mode,True
            self.output=bytearray();self.base=0;self.generation=secrets.token_hex(8)
            self.resize(90,28)
            self.reader=threading.Thread(target=self._read,args=(pid,fd),daemon=True)
            self.reader.start()

    def _read(self,pid,fd):
        try:
            while True:
                ready,_,_=select.select([fd],[],[],.5)
                if not ready:continue
                block=os.read(fd,65536)
                if not block:break
                with self.lock:
                    self.output.extend(block)
                    if len(self.output)>2*1024*1024:
                        count=len(self.output)-2*1024*1024
                        del self.output[:count];self.base+=count
        except OSError:pass
        finally:
            try:os.close(fd)
            except OSError:pass
            try:os.waitpid(pid,0)
            except ChildProcessError:pass
            with self.lock:
                if self.pid==pid:self.connected=False;self.fd=None

    def read(self,cursor=0):
        with self.lock:
            self.last_seen=time.monotonic()
            start=max(self.base,min(cursor,self.base+len(self.output)))
            chunk=bytes(self.output[start-self.base:])
            return {'connected':self.connected,'mode':self.mode,'cursor':self.base+len(self.output),
                    'generation':self.generation,'base':self.base,'output':base64.b64encode(chunk).decode()}

    def send(self,data):
        if not isinstance(data,str) or len(data.encode())>8192:raise ValueError('Input too large')
        with self.lock:
            if not self.connected or self.fd is None:raise ValueError('Terminal is disconnected')
            os.write(self.fd,data.encode());self.last_seen=time.monotonic()

    def resize(self,columns,rows):
        if type(columns) is not int or type(rows) is not int or not (20<=columns<=300 and 5<=rows<=100):raise ValueError('Invalid terminal size')
        with self.lock:
            if self.fd is not None:fcntl.ioctl(self.fd,termios.TIOCSWINSZ,struct.pack('HHHH',rows,columns,0,0))

    def close(self):
        with self.lock:
            pid=self.pid if self.connected else None
        if pid:
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
        if self.reader:self.reader.join(timeout=1)
        for closing_signal in (signal.SIGHUP,signal.SIGKILL):
            if not self.reader or not self.reader.is_alive():break
            try:os.kill(pid,closing_signal)
            except (ProcessLookupError,TypeError):pass
            self.reader.join(timeout=2)
        if self.reader and self.reader.is_alive():raise ValueError('Terminal is still closing; try again shortly')


class TerminalWorkspace:
    """Cookie-scoped collection; terminal IDs never grant access across cookies."""
    def __init__(self, factory):
        self.token=secrets.token_urlsafe(32)
        self.factory=factory;self.lock=threading.RLock()
        self.terminals={};self.last_seen=time.monotonic()
        self.default=self.add()

    def add(self):
        with self.lock:
            if len(self.terminals)>=8:raise ValueError('Maximum eight terminals; close a tab to add another')
            key=secrets.token_urlsafe(16)
            self.terminals[key]=self.factory()
            return key

    @property
    def connected(self):
        with self.lock:return any(s.connected for s in self.terminals.values())
    @property
    def pid(self):return self.terminals[self.default].pid if self.default in self.terminals else None
    def describe(self):
        with self.lock:
            self.last_seen=time.monotonic()
            return {'token':self.token,'terminals':[{'id':key,'mode':s.mode,'connected':s.connected} for key,s in self.terminals.items()]}
    def get(self, key=None):
        self.last_seen=time.monotonic()
        session=self.terminals.get(key or self.default)
        if session is None:raise ValueError('Terminal not found in this browser')
        return session
    def close(self):
        for session in list(self.terminals.values()):session.close()


class TerminalServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,session_factory=TerminalSession):
        self.sessions={};self.lock=threading.Lock();self.factory=session_factory
        super().__init__(address,Handler)
    def server_close(self):
        super().server_close()
        for session in list(self.sessions.values()):session.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass  # Never log terminal input or password bytes.
    def reply(self,data,status=200,kind='application/json; charset=utf-8',cookie=None):
        if not isinstance(data,bytes):data=json.dumps(data).encode()
        self.send_response(status);self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        host=urlsplit('//'+self.headers.get('Host','localhost')).hostname or 'localhost'
        if ':' in host:host='['+host+']'
        ancestors="'self' http://"+host+':8070' if urlsplit(self.path).path=='/codex' else "'none'"
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors "+ancestors+"; base-uri 'none'")
        if cookie:self.send_header('Set-Cookie',f'pi_terminal={cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def allowed(self):
        try:
            host=urlsplit('//'+self.headers.get('Host','')).hostname or ''
            if host in {'localhost','127.0.0.1','::1'} or host.endswith('.ts.net'):return True
            address=ipaddress.ip_address(host)
            if address in ipaddress.ip_network('100.64.0.0/10') or address in ipaddress.ip_network('fd7a:115c:a1e0::/48'):return True
        except ValueError:pass
        self.reply({'error':'Use localhost or the Pi Tailscale address'},403);return False

    def session(self):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return None
        value=cookie.get('pi_terminal')
        return self.server.sessions.get(value.value) if value else None

    def do_GET(self):
        if not self.allowed():return
        url=urlsplit(self.path)
        assets={'/':('terminal.html','text/html; charset=utf-8'),'/terminal.js':('terminal.js','text/javascript; charset=utf-8'),
                '/dashboard.css':('dashboard.css','text/css; charset=utf-8'),
                '/xterm.js':('vendor/xterm.js','text/javascript; charset=utf-8'),
                '/xterm.css':('vendor/xterm.css','text/css; charset=utf-8'),
                '/addon-fit.js':('vendor/addon-fit.js','text/javascript; charset=utf-8')}
        if url.path=='/codex':
            page=(ASSETS/'terminal.html').read_text().replace('<body>','<body class="codex-embed">').replace('<title>Pi terminal · MAVProxy</title>','<title>Codex terminal</title>')
            self.reply(page.encode(),kind='text/html; charset=utf-8');return
        if url.path in assets:
            name,kind=assets[url.path];self.reply((ASSETS/name).read_bytes(),kind=kind);return
        session=self.session()
        if url.path=='/api/session':
            cookie=None
            with self.server.lock:
                if session is None:
                    expired=[key for key,value in self.server.sessions.items() if not value.connected and time.monotonic()-value.last_seen>1800]
                    for key in expired:del self.server.sessions[key]
                    if len(self.server.sessions)>=8:
                        idle=sorted((value.last_seen,key) for key,value in self.server.sessions.items() if not value.connected)
                        if not idle:self.reply({'error':'Too many active terminal sessions'},429);return
                        del self.server.sessions[idle[0][1]]
                    cookie=secrets.token_urlsafe(32);session=TerminalWorkspace(self.server.factory);self.server.sessions[cookie]=session
            self.reply(session.describe(),cookie=cookie)
        elif url.path=='/api/output' and session:
            try:
                query=parse_qs(url.query)
                terminal=session.get(query.get('id',[None])[0])
                cursor=int(query.get('cursor',['0'])[0])
                with terminal.lock:
                    if query.get('generation',[terminal.generation])[0]!=terminal.generation:cursor=0
                    output=terminal.read(cursor)
                self.reply(output)
            except ValueError:self.reply({'error':'Invalid cursor'},400)
        else:self.reply({'error':'Session not found'},404)

    def do_POST(self):
        if not self.allowed():return
        session=self.session()
        if (not session or self.headers.get('Origin')!='http://'+self.headers.get('Host','') or
            not secrets.compare_digest(self.headers.get('X-Terminal-Token',''),session.token)):
            self.reply({'error':'Reload the terminal page'},403);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384:raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Invalid request')
            with session.lock:
                if self.path=='/api/new':
                    key=session.add();self.reply({'ok':True,'id':key});return
                terminal=session.get(data.get('id'))
                if self.path=='/api/connect':terminal.start(data.get('mode'))
                elif self.path=='/api/restart':
                    mode=terminal.mode or data.get('mode','shell')
                    terminal.close();terminal.start(mode)
                elif self.path=='/api/input':terminal.send(data.get('data'))
                elif self.path=='/api/resize':terminal.resize(data.get('columns'),data.get('rows'))
                elif self.path=='/api/disconnect':terminal.close()
                elif self.path=='/api/remove':
                    terminal.close()
                    key=data.get('id') or session.default
                    del session.terminals[key]
                    if not session.terminals:session.default=session.add()
                    elif key==session.default:session.default=next(iter(session.terminals))
                else:self.reply({'error':'Not found'},404);return
            self.reply({'ok':True})
        except (ValueError,TypeError) as error:self.reply({'error':str(error)},400)
        except (OSError,RuntimeError) as error:self.reply({'error':str(error)},503)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8071)
    args=parser.parse_args()
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    with TerminalServer(('127.0.0.1',args.port)) as server:
        print(f'Pi terminal: http://localhost:{args.port}',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass


if __name__=='__main__':main()
