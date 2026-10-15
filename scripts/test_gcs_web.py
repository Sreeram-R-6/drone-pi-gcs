"""Browser adapter and HTTP control tests; no aircraft access."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
import gcs_web as web


class Tests(unittest.TestCase):
    def test_script_handover_only_from_disarmed_idle_monitor(self):
        session=web.Session(Path('/unused'))
        session.thread=MagicMock()
        session.thread.is_alive.return_value=False
        session.ui.receive_only=True
        session.ui.transport=MagicMock()
        session.ui.transport.armed.return_value=True
        with self.assertRaisesRegex(ValueError,'already armed'):
            session.script_pause('a'*32)
        session.ui.transport.armed.return_value=False
        session.script_pause('a'*32)
        self.assertEqual(session.external_run,'a'*32)
        self.assertTrue(session.ui.quit_requested)
        with self.assertRaisesRegex(ValueError,'active script'):
            session.key('a')
        with patch.object(web.threading,'Thread') as thread:
            session.script_resume('a'*32)
            thread.return_value.start.assert_called_once()
        self.assertIsNone(session.external_run)

    def test_browser_renders_real_terminal(self):
        ui = web.BrowserTerminal(web.Screen())
        ui.receive_only = True
        ui.draw(force=True)
        text = '\n'.join(ui.screen.frame)
        self.assertIn('HOME MAP', text)
        self.assertIn('MISSION', text)
        self.assertIn('POWER', text)
        with ui.screen.lock:
            ui.screen.pending.extend([ord('m'), 10])
        ui.keys()
        self.assertTrue(ui.menu_requested)
        ui.flush_input()
        self.assertEqual(ui.screen.getch(), -1)

    def test_worker_crash_triggers_service_recovery(self):
        session = web.Session(Path('/unused'))
        server = object.__new__(web.Server)
        server.session = session
        server.service_actions()
        session.error = 'test controller failure'
        with self.assertRaisesRegex(RuntimeError, 'test controller failure'):
            server.service_actions()
        session.error = None
        session.ui.quit_requested = True
        server.service_actions()  # Deliberate operator exit stays stopped.

    def test_control_requires_token_and_same_origin(self):
        session = web.Session(Path('/unused'))
        with patch.object(session.thread, 'is_alive', return_value=True):
            server = web.Server(('127.0.0.1', 0), session)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(headers):
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                    conn.request('POST', '/api/key', json.dumps({'key':'m'}), headers)
                    response = conn.getresponse()
                    status = response.status
                    response.read()
                    conn.close()
                    return status
                self.assertEqual(request({}), 403)
                self.assertEqual(request({'X-GCS-Token':session.token, 'Origin':'https://evil.example'}), 403)
                self.assertEqual(request({'X-GCS-Token':session.token}), 200)
                self.assertEqual(session.screen.getch(), ord('m'))
                with self.assertRaises(ValueError): session.key('arm now')
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == '__main__': unittest.main()
