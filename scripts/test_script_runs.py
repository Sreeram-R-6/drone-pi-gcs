"""Flight launch tests with mocked jobs and controller; never arm an aircraft."""
import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import web_dashboard as web
from script_runs import ScriptRuns, RunConflict, save


class RunsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.spawn=MagicMock(return_value=MagicMock(pid=12345))
        self.runs=ScriptRuns(self.folder,self.spawn)

    def ticket(self):
        return self.runs.prepare('flight_2m',True)['ticket']

    def test_only_supported_script_and_explicit_confirmation(self):
        with self.assertRaises(ValueError):self.runs.prepare('/bin/bash',True)
        with self.assertRaises(ValueError):self.runs.launch(self.ticket(),False)
        self.spawn.assert_not_called()

    def test_ticket_expires_and_is_single_use(self):
        ticket=self.ticket();self.runs.tickets[ticket]['expires']=0
        with self.assertRaises(ValueError):self.runs.launch(ticket,True)
        ticket=self.ticket();self.runs.launch(ticket,True)
        with self.assertRaises(ValueError):self.runs.launch(ticket,True)
        self.assertEqual(self.spawn.call_count,1)

    def test_running_job_blocks_second_launch_and_survives_manager_restart(self):
        run=self.runs.launch(self.ticket(),True)
        with self.assertRaises(RunConflict):self.runs.launch(self.ticket(),True)
        reopened=ScriptRuns(self.folder,self.spawn)
        self.assertEqual(reopened.status()['run_id'],run['run_id'])
        self.assertEqual(self.spawn.call_count,1)

    def test_run_command_does_not_include_user_shell_arguments(self):
        self.runs.launch(self.ticket(),True)
        command=self.spawn.call_args.args[0]
        self.assertTrue(command[2].endswith('/scripts/web/flight_job.py'))
        self.assertNotIn('shell',self.spawn.call_args.kwargs)
        self.assertNotIn('--execute',command) # Only the fixed job enters flight.

    def test_stop_signals_only_matching_job_once(self):
        run=self.runs.launch(self.ticket(),True)
        path=self.folder/(run['run_id']+'.json')
        state=json.loads(path.read_text());state.update(pid=12345,state='running');save(path,state)
        with patch.object(self.runs,'matches',return_value=True),patch('script_runs.os.kill') as kill:
            self.runs.stop();self.runs.stop()
        kill.assert_called_once_with(12345,signal.SIGINT)

    def test_job_wrapper_executes_only_mocked_script(self):
        run=self.runs.launch(self.ticket(),True)
        spec=importlib.util.spec_from_file_location('flight_job',Path(__file__).parent/'web/flight_job.py')
        job=importlib.util.module_from_spec(spec);spec.loader.exec_module(job)
        sys.path.insert(0,str(Path(__file__).parent))
        from flight_2m import mission
        previous=signal.getsignal(signal.SIGTERM)
        try:
            with patch('sys.argv',['flight_job.py','--folder',str(self.folder),'--run-id',run['run_id']]), \
                 patch.object(mission,'main',return_value=0) as flight, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(job.main(),0)
            flight.assert_called_once()
            self.assertEqual(self.runs.status()['state'],'completed')
        finally:signal.signal(signal.SIGTERM,previous)


class HTTPTests(unittest.TestCase):
    def test_assets_and_authorization_without_flight(self):
        with tempfile.TemporaryDirectory() as folder:
            spawn=MagicMock();runs=ScriptRuns(folder,spawn)
            server=web.DashboardServer(('127.0.0.1',0),{'sites':[]},runs)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(path,method='GET',data=None,auth=True):
                headers={'Content-Type':'application/json'}
                if auth:headers.update({'Origin':f'http://127.0.0.1:{server.server_port}','X-Dashboard-Token':server.token})
                client=http.client.HTTPConnection('127.0.0.1',server.server_port)
                client.request(method,path,None if data is None else json.dumps(data),headers)
                response=client.getresponse();result=response.status,response.read();client.close();return result
            try:
                for path in ('/','/scripts','/dashboard.css','/dashboard.js','/scripts.js'):
                    self.assertEqual(request(path)[0],200)
                self.assertEqual(json.loads(request('/api/scripts')[1])['run']['state'],'idle')
                self.assertEqual(request('/api/scripts/prepare','POST',{'script':'flight_2m','record_video':True},False)[0],403)
                self.assertEqual(request('/api/scripts/prepare','POST',{'script':'arbitrary','record_video':True})[0],400)
                self.assertEqual(request('/api/scripts/prepare','POST',{'script':'flight_2m','record_video':True})[0],202)
                spawn.assert_not_called()
                client=http.client.HTTPConnection('127.0.0.1',server.server_port)
                client.request('GET','/api/scripts',headers={'Host':'attacker.example'})
                response=client.getresponse();self.assertEqual(response.status,403);response.read();client.close()
            finally:server.shutdown();thread.join();server.server_close()


if __name__=='__main__':unittest.main()
