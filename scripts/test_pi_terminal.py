"""Real PTY and browser-session tests; no SSH login or aircraft commands."""
import base64
import http.client
import json
import os
from pathlib import Path
import threading
import time
import tempfile
import shlex
import unittest
from unittest.mock import patch, Mock

import pi_terminal as web


class Tests(unittest.TestCase):
    def test_real_pty_input_output_and_resize(self):
        session=web.TerminalSession(command=lambda mode:['/bin/bash','--noprofile','--norc'])
        try:
            session.start('shell');session.resize(80,24)
            self.assertEqual(tuple(os.get_terminal_size(session.fd)),(80,24))
            session.send("printf 'PTY_WORKS\\n'; exit\n")
            for _ in range(50):
                if not session.read()['connected']:break
                time.sleep(.05)
            output=base64.b64decode(session.read()['output'])
            self.assertIn(b'PTY_WORKS',output)
            self.assertFalse(session.connected)
        finally:session.close()

    def test_invalid_mode_never_spawns_process(self):
        session=web.TerminalSession()
        with patch.object(web.pty,'fork') as fork:
            with self.assertRaises(ValueError):session.start('arbitrary-command')
        fork.assert_not_called()

    def test_separate_browser_sessions_and_token_required(self):
        server=web.TerminalServer(('127.0.0.1',0))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,method='GET',data=None,headers=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request(method,path,None if data is None else json.dumps(data),headers or {})
            response=client.getresponse();result=response.status,dict(response.getheaders()),response.read();client.close();return result
        try:
            first=request('/api/session');second=request('/api/session')
            a=json.loads(first[2]);b=json.loads(second[2])
            self.assertNotEqual(a['token'],b['token'])
            self.assertNotEqual(first[1]['Set-Cookie'],second[1]['Set-Cookie'])
            self.assertTrue(all(session.pid is None for session in server.sessions.values()))
            headers={'Cookie':first[1]['Set-Cookie'].split(';')[0],
                     'Origin':f'http://127.0.0.1:{server.server_port}','X-Terminal-Token':b['token']}
            self.assertEqual(request('/api/connect','POST',{'mode':'shell'},headers)[0],403)
            headers['X-Terminal-Token']=a['token'];headers['Origin']='http://attacker.example'
            self.assertEqual(request('/api/connect','POST',{'mode':'shell'},headers)[0],403)
            self.assertEqual(request('/api/output')[0],404)
            for path in ('/','/terminal.js','/xterm.js','/xterm.css','/addon-fit.js'):
                self.assertEqual(request(path)[0],200)
            self.assertEqual(request('/api/session',headers={'Host':'attacker.example'})[0],403)
        finally:server.shutdown();thread.join();server.server_close()

    def test_tabs_restart_reconnect_and_browser_isolation(self):
        server=web.TerminalServer(('127.0.0.1',0),session_factory=lambda:web.TerminalSession(
            command=lambda mode:['/bin/bash','--noprofile','--norc']))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,data=None,auth=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            headers={} if auth is None else auth
            client.request('GET' if data is None else 'POST',path,
                           None if data is None else json.dumps(data),headers)
            response=client.getresponse();result=response.status,dict(response.getheaders()),json.loads(response.read())
            client.close();return result
        try:
            _,headers,info=request('/api/session')
            auth={'Cookie':headers['Set-Cookie'].split(';')[0],
                  'Origin':f'http://127.0.0.1:{server.server_port}','X-Terminal-Token':info['token']}
            first=info['terminals'][0]['id']
            second=request('/api/new',{},auth)[2]['id']
            for key in (first,second):
                self.assertEqual(request('/api/connect',{'id':key,'mode':'shell'},auth)[0],200)
            workspace=next(iter(server.sessions.values()))
            a,b=workspace.get(first),workspace.get(second)
            old_pid=a.pid;other_pid=b.pid
            old_generation=a.generation
            self.assertEqual(request('/api/restart',{'id':first},auth)[0],200)
            self.assertNotEqual(a.pid,old_pid);self.assertNotEqual(a.generation,old_generation)
            self.assertEqual(b.pid,other_pid);self.assertTrue(b.connected)
            _,other_headers,other_info=request('/api/session')
            other_auth={'Cookie':other_headers['Set-Cookie'].split(';')[0],
                        'Origin':auth['Origin'],'X-Terminal-Token':other_info['token']}
            self.assertEqual(request('/api/input',{'id':first,'data':'hello'},other_auth)[0],400)
            self.assertEqual(request('/api/output?id='+first,auth=other_auth)[0],400)
            self.assertEqual(request('/api/disconnect',{'id':first},auth)[0],200)
            self.assertFalse(a.connected);self.assertTrue(b.connected)
            self.assertEqual(request('/api/connect',{'id':first,'mode':'shell'},auth)[0],200)
            self.assertEqual(request('/api/remove',{'id':second},auth)[0],200)
            self.assertNotIn(second,workspace.terminals);self.assertTrue(a.connected)
            for _ in range(7):self.assertEqual(request('/api/new',{},auth)[0],200)
            self.assertEqual(request('/api/new',{},auth)[0],400)
        finally:server.shutdown();thread.join();server.server_close()

    def test_codex_launch_uses_project_folder_and_normal_cli(self):
        public_key=Mock();public_key.read_text.return_value='ssh-ed25519 test-public-key'
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web.Path,'glob',return_value=[public_key]), \
             patch.object(web.Path,'is_file',return_value=True):
            command=web.ssh_command('codex',web.Path(directory))
        remote=shlex.split(command[-1])
        self.assertIn(str(web.ROOT),remote)
        self.assertIn('--cd',remote)
        self.assertEqual(remote[remote.index('--cd')+1],str(web.ROOT))
        self.assertIn('--no-alt-screen;',remote)
        self.assertFalse(any('bypass' in arg or arg=='--execute' for arg in remote))

    def test_codex_embed_allows_only_matching_dashboard_origin(self):
        server=web.TerminalServer(('127.0.0.1',0))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request('GET','/codex',headers={'Host':'100.112.205.54:8071'})
            response=client.getresponse();page=response.read()
            self.assertEqual(response.status,200)
            self.assertIn(b'class="codex-embed"',page)
            self.assertIn("frame-ancestors 'self' http://100.112.205.54:8070;",response.getheader('Content-Security-Policy'))
            client.close()
            self.assertFalse(any(workspace.connected for workspace in server.sessions.values()))
        finally:server.shutdown();thread.join();server.server_close()

    def test_force_arm_encoding_with_mock_mavlink_transport(self):
        try:
            from MAVProxy.modules.mavproxy_cmdlong import CmdlongModule
        except ImportError:
            self.skipTest('Run with flight venv to validate installed MAVProxy encoding')
        fake=Mock()
        fake.settings.target_system=1;fake.settings.target_component=1
        CmdlongModule.cmd_long(fake,['COMPONENT_ARM_DISARM','1','21196','0','0','0','0','0'])
        fake.master.mav.command_long_send.assert_called_once_with(1,1,400,0,1.0,21196.0,0.0,0.0,0.0,0.0,0.0)

    def test_mavproxy_uses_udp_router_not_serial_or_auto_arm(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('mavproxy_session',Path(__file__).parent/'web/mavproxy_session.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        command=module.command()
        self.assertIn('--master=tcp:127.0.0.1:5760',command)
        self.assertIn('--heartbeat-rate=0',command)
        self.assertIn('--source-system=207',module.command(207))
        with tempfile.TemporaryDirectory() as directory:
            original=module.process_lock
            def isolated(name):return original(Path(directory).name+'-'+name)
            with patch.object(module,'process_lock',side_effect=isolated):
                with module.console_identity() as first, module.console_identity() as second:
                    self.assertNotEqual(first,second)
                    with original(Path(directory).name+'-mission.lock'):pass
        self.assertFalse(any('/dev/' in arg or '--cmd=' in arg for arg in command))


if __name__=='__main__':unittest.main()
