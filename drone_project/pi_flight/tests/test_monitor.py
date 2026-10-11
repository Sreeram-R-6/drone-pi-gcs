import contextlib
import io
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import preflight_hover
from flightcheck import monitor
from flightcheck.config import load
from flightcheck.telemetry import LinkLost, Rejected, Store, Transport
from flightcheck.checks import Check


class MonitorTests(unittest.TestCase):
    def test_vehicle_identity_hidden_from_monitor_not_source_checks(self):
        checks = [Check('vehicle identity/state','FAIL',{},'expected state'),
                  Check('GPS quality/stability','FAIL',{},'GPS limits')]
        self.assertEqual([c.name for c in monitor.dashboard_checks(checks)],
                         ['GPS quality/stability'])
        self.assertEqual(len(checks),2)

    def test_mission_selection_keeps_same_uart_owner(self):
        config = load(Path(__file__).parents[1]/'config.example.toml')
        ui = SimpleNamespace(quit_requested=False,observe=Mock(),menu_requested=True,
                             choose=Mock(return_value=True),route='hover',checks=[Check('retained','PASS',1,'test')])
        t = Mock()
        t.store = Store()
        t.target = (1,1)
        t.require_live.return_value = {'autopilot':3,'type':2}
        t.armed.return_value = False
        original_checks = ui.checks
        def mission(args,ui,existing_transport):
            self.assertIs(existing_transport,t)
            self.assertIs(ui.checks,original_checks)
            t.close.assert_not_called()
            ui.view_state = 'PREFLIGHT BLOCKED'
            ui.quit_requested = True
            return 3
        with tempfile.TemporaryDirectory() as root:
            config['_base'] = root
            with patch('flightcheck.monitor.Transport',return_value=t) as factory, \
                 patch('flightcheck.monitor.process_lock',return_value=contextlib.nullcontext()), \
                 patch('preflight_hover.main',side_effect=mission),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.run(config,ui=ui),0)
            factory.assert_called_once()
            t.connect.assert_called_once()
            t.close.assert_called_once() # Only on final dashboard exit.
            self.assertIs(ui.transport,t)
            self.assertTrue(ui.receive_only)

    def test_flow_checks_removed_from_dashboard_not_input_list(self):
        checks = [Check('optical flow','FAIL',0,'test'),Check('OPTICAL_FLOW stream','FAIL',0,'test'),
                  Check('GPS quality/stability','FAIL',0,'test'),Check('flow calibration/fusion review','UNKNOWN',0,'test')]
        displayed = monitor.dashboard_checks(checks)
        self.assertEqual([c.name for c in displayed],['GPS quality/stability','flow calibration/fusion review'])
        self.assertEqual(len(checks),4)
    def test_uart_open_failure_is_logged_and_visible_without_stale_transport(self):
        config = load(Path(__file__).parents[1]/'config.example.toml')
        ui = SimpleNamespace(transport=Mock(),quit_requested=False)
        with tempfile.TemporaryDirectory() as base:
            config['_base'] = base
            with patch('flightcheck.monitor.Transport',side_effect=Rejected('UART busy: close MAVProxy')),patch('flightcheck.monitor.process_lock',return_value=contextlib.nullcontext()),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.run(config,ui=ui),3)
            self.assertIsNone(ui.transport)
            self.assertEqual(ui.view_state,'CONNECTION ERROR')
            self.assertTrue(ui.receive_only)
            self.assertIn('press R',ui.notice)
            self.assertIn('connection_error',(ui.report.path/'events.jsonl').read_text())
    def test_receive_only_blocks_every_send_api_even_if_flight_flag_is_set(self):
        t = object.__new__(Transport)
        t.receive_only = True
        t.flight_authorized = True
        t.connection = Mock()
        t.heartbeat()
        for action in (lambda:t.command(400,[1,0],flight=True),
                       lambda:t.command(176,[1,6],flight=True),
                       lambda:t.read_param('ARMING_CHECK'),
                       lambda:t.set_climb_parameter('X',1,9),
                       lambda:t.export_parameters(),
                       lambda:t.initialize_streams(),
                       lambda:t.position_target(0,0,-2,lambda:None)):
            with self.assertRaises(Rejected):
                action()
        self.assertEqual(t.connection.mock_calls,[])

    def test_monitor_accepts_uncommissioned_config_without_releasing_flight(self):
        config = load(Path(__file__).parents[1]/'config.example.toml')
        with patch('preflight_hover.load',return_value=config),patch('flightcheck.monitor.run',return_value=0) as run:
            self.assertEqual(preflight_hover.main(['--monitor']),0)
            run.assert_called_once()
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            preflight_hover.main(['--monitor','--flight'])

    def test_real_loopback_receives_heartbeat_without_transmitting(self):
        from pymavlink.dialects.v20 import ardupilotmega as mav
        config = load(Path(__file__).parents[1]/'config.example.toml')
        # Ephemeral isolated port; never send fixture data into the user's GCS port.
        config['link']['endpoint'] = 'udpin:127.0.0.1:0'
        t = Transport(config,SimpleNamespace(event=Mock()),receive_only=True)
        sender = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:
            sender.settimeout(.1)
            packet = mav.MAVLink_heartbeat_message(2,3,1,0,3,3).pack(mav.MAVLink(None,srcSystem=1,srcComponent=1))
            sender.sendto(packet,t.connection.port.getsockname())
            t.connect(.5)
            self.assertFalse(t.armed())
            self.assertEqual(t.store.samples['HEARTBEAT'][-1][1]['autopilot'],3)
            t.heartbeat()
            with self.assertRaises(socket.timeout):
                sender.recvfrom(4096)
        finally:
            sender.close()
            t.close()

    def test_ui_waits_for_forwarding_after_initial_timeout(self):
        config = load(Path(__file__).parents[1]/'config.example.toml')
        ui = SimpleNamespace(quit_requested=False,observe=Mock())
        t = Mock()
        t.store = Store()
        t.target = (1,1)
        t.connect.side_effect = LinkLost('No output yet')
        t.require_live.return_value = {'autopilot':3,'type':2,'base_mode':1}
        calls = 0
        def pump():
            nonlocal calls
            calls += 1
            if calls >= 2:
                ui.quit_requested = True
        t.pump.side_effect = pump
        with tempfile.TemporaryDirectory() as base:
            config['_base'] = base
            with patch('flightcheck.monitor.Transport',return_value=t) as factory,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.run(config,ui=ui,timeout=.1),0)
            factory.assert_called_once_with(config,ui.report,receive_only=True)
            self.assertEqual(ui.view_state,'FC CONNECTED / FLIGHT LOCKED')
            t.request_monitor_telemetry.assert_called()
            t.command.assert_not_called()
            t.heartbeat.assert_not_called()
            t.export_parameters.assert_not_called()
            t.close.assert_called_once()

    def test_headless_monitor_timeout_explains_forwarding(self):
        config = load(Path(__file__).parents[1]/'config.example.toml')
        config['link']['endpoint'] = 'udpin:127.0.0.1:14550'
        t = Mock()
        t.connect.side_effect = LinkLost('No forwarding')
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as base:
            config['_base'] = base
            with patch('flightcheck.monitor.Transport',return_value=t),contextlib.redirect_stdout(output):
                self.assertEqual(monitor.run(config,timeout=.1),4)
        self.assertIn('mavproxy.service',output.getvalue())
        self.assertIn('OLED uses 14553, GCS uses 14550',output.getvalue())
        t.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
