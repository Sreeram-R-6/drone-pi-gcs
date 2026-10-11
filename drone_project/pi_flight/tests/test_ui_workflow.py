import contextlib
import curses
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import preflight_hover
import terminal_gcs
from flightcheck.checks import Check
from flightcheck.config import load
from flightcheck.monitor import MISSION_SELECTED
from flightcheck.terminal import Terminal
from test_state_machine import scenario


def passed():
    return [Check('fixture sensor','PASS',1,'test evidence')]


class WorkflowTests(unittest.TestCase):
    def test_reconnect_from_connection_error_only_retries_monitor(self):
        ui = SimpleNamespace(quit_requested=False,mission=None,view_state='CONNECTION ERROR',
                             write=lambda text:len(text),flush=lambda:None,draw=Mock())
        ui.keys = lambda:setattr(ui,'reconnect_requested',True)
        calls = []
        def run(args,ui):
            calls.append(list(args))
            if len(calls) == 1:
                return 3
            ui.quit_requested = True
            return 0
        with patch('terminal_gcs.preflight_hover.main',side_effect=run),patch('curses.flushinp'):
            self.assertEqual(terminal_gcs.dashboard(ui,['--monitor']),0)
        self.assertEqual(calls,[['--monitor'],['--monitor']])
        self.assertIsNone(ui.manual_action)
        self.assertIsNone(ui.service_request)
    def test_selection_returns_to_dashboard_with_start_locked(self):
        screen = Mock()
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
            ui = Terminal(screen)
        ui.receive_only = True
        with patch.object(ui,'_choose',return_value=True),patch.object(ui,'draw') as draw:
            self.assertTrue(ui.choose())
        draw.assert_called_once_with(force=True)
        self.assertTrue(ui.mission_selected)
        self.assertFalse(ui.receive_only)
        self.assertEqual(ui.view_state,'MISSION SELECTED / CHECKING')
        self.assertIsNone(ui.mission)
        self.assertIn('start locked',ui.workflow_status)
    def test_monitor_menu_key_does_not_send_controls_or_consume_next_enter(self):
        values = iter([ord('m'),10,-1])
        screen = SimpleNamespace(keypad=lambda _:None,nodelay=lambda _:None,getch=lambda:next(values))
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
            ui = Terminal(screen)
        ui.receive_only = True
        ui.keys()
        self.assertTrue(ui.menu_requested)
        self.assertIsNone(ui.request)
        self.assertEqual(screen.getch(),10) # Flush at menu entry discards this.

    def test_live_menu_keeps_pumping_and_restores_observer_on_cancel(self):
        screen = Mock()
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
            ui = Terminal(screen)
        ui.route = 'forward'
        observer = Mock()
        t = SimpleNamespace(observer=observer,pump=Mock())
        def menu(transport):
            self.assertIsNone(transport.observer)
            transport.pump(.01)
            ui.route = 'box'
            return False
        with patch.object(ui,'_choose',side_effect=menu):
            self.assertFalse(ui.choose(t))
        self.assertEqual(ui.route,'forward')
        self.assertIs(t.observer,observer)
        t.pump.assert_called_once()

    def test_selected_mission_returns_to_monitor_after_blocked_preflight(self):
        ui = SimpleNamespace(quit_requested=False,route='hover',mission=None,
                             view_state='LIVE / RECEIVE ONLY',workflow_status='',write=lambda text:len(text),flush=lambda:None)
        seen = []
        def run(args,ui):
            seen.append(list(args))
            if len(seen) == 1:
                return MISSION_SELECTED
            if len(seen) == 2:
                ui.view_state = 'PREFLIGHT BLOCKED'
                ui.workflow_status = 'Unreviewed sensors'
                return 3
            ui.quit_requested = True
            return 0
        with patch('terminal_gcs.preflight_hover.main',side_effect=run),patch('curses.flushinp'):
            self.assertEqual(terminal_gcs.dashboard(ui,['--config','example.toml','--monitor']),0)
        self.assertEqual(seen[1],['--config','example.toml','--check-once','--flight'])
        self.assertEqual(seen[2],seen[0])
        self.assertIsNone(ui.mission)
        self.assertEqual(ui.workflow_status,'Unreviewed sensors')

    def test_unreviewed_route_blocks_without_aggregate_configuration_check(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        ui = SimpleNamespace(route='box')
        with tempfile.TemporaryDirectory() as root:
            c['_base'] = root
            with patch('preflight_hover.load',return_value=c),patch('preflight_hover.Transport') as transport,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight_hover.main(['--check-once','--flight'],ui=ui),3)
            transport.assert_not_called()
            self.assertEqual(ui.view_state,'PREFLIGHT BLOCKED')
            self.assertTrue(ui.checks)
            self.assertNotIn('configuration/evidence',[check.name for check in ui.checks])
            self.assertTrue(ui.show_checks)

    def gated_run(self,final_answer='',fast_state='PASS',borrowed=False):
        old,t,_ = scenario()
        c = old.c
        t.c = c
        c['link']['device_confirmed'] = True
        c['commissioning']['wiring_props_off_verified'] = True
        c['commissioning']['thresholds_approved'] = True # Explicit approval in simulated flight fixture only.
        c['firmware']['prearm_command_supported'] = True
        t.initialize_streams = Mock()
        t.export_parameters = Mock()
        t.connect = Mock()
        t.close = Mock()
        t.version = {}
        t.sample = Mock(side_effect=[(100,130),(130,133)])
        t.store.put('ATTITUDE',{'yaw':0.},100)
        t.flight_authorized = False
        t.receive_only = True
        t.observer = Mock()
        t.report = Mock()
        t.require_live = Mock()
        t.pump = Mock()
        old_report, old_observer, old_store = t.report, t.observer, t.store
        ui = SimpleNamespace(route='forward',observe=Mock(),checks=passed())
        previous_checks = ui.checks
        if borrowed:
            t.pump.side_effect = lambda:self.assertIs(ui.checks,previous_checks)
        with tempfile.TemporaryDirectory() as root:
            c['_base'] = root
            with patch('preflight_hover.load',return_value=c), \
                 patch('flightcheck.routes.validate_route'),patch('preflight_hover.Transport',return_value=t), \
                 patch('preflight_hover.identity_checks',side_effect=lambda *_:passed()), \
                 patch('preflight_hover.parameter_audit',side_effect=lambda *_:passed()), \
                 patch('preflight_hover.evaluate',side_effect=[passed(),[Check('fresh sensor',fast_state,1,'fresh pass required')]]), \
                 patch('preflight_hover.HUMAN_ITEMS',('Pilot and area ready',)), \
                 patch('preflight_hover.input_live',side_effect=['YES',final_answer]), \
                 patch('preflight_hover.sys.stdin.isatty',return_value=True), \
                 patch('preflight_hover.time.monotonic',return_value=100), \
                 patch('flightcheck.routes.RouteMission') as mission,contextlib.redirect_stdout(io.StringIO()):
                mission.return_value.run.return_value = True
                result = preflight_hover.main(['--check-once','--flight'],ui=ui,
                                             existing_transport=t if borrowed else None)
                called = mission.return_value.run.call_count
                t.export_parameters.assert_not_called()
                if borrowed:
                    t.initialize_streams.assert_not_called()
                    t.connect.assert_not_called()
                    t.close.assert_not_called()
                    self.assertIs(t.store,old_store)
                    self.assertIs(t.c,c)
                    self.assertIs(t.report,old_report)
                    self.assertIs(t.observer,old_observer)
                    self.assertTrue(t.receive_only)
                    self.assertFalse(t.flight_authorized)
        return result,called,t,ui

    def test_selected_mission_borrows_live_link_and_preserves_checks(self):
        result,calls,t,ui = self.gated_run(borrowed=True)
        self.assertEqual((result,calls),(0,1))
        self.assertEqual(t.sample.call_count,2)

    def test_borrowed_link_restored_after_failed_final_recheck(self):
        result,calls,t,ui = self.gated_run(borrowed=True,fast_state='FAIL')
        self.assertEqual((result,calls),(3,0))

    def test_final_enter_starts_only_after_both_rechecks_and_confirmations(self):
        result,calls,t,ui = self.gated_run()
        self.assertEqual(result,0)
        self.assertEqual(calls,1)
        self.assertTrue(t.flight_authorized)
        self.assertEqual(t.sample.call_count,2)

    def test_cancelled_final_enter_never_starts_mission(self):
        result,calls,t,ui = self.gated_run(final_answer='cancel')
        self.assertEqual(result,3)
        self.assertEqual(calls,0)
        self.assertFalse(t.flight_authorized)

    def test_failed_warn_unknown_final_recheck_never_starts(self):
        for state in ('FAIL','WARN','UNKNOWN'):
            with self.subTest(state=state):
                result,calls,t,ui = self.gated_run(fast_state=state)
                self.assertEqual(result,3)
                self.assertEqual(calls,0)
                self.assertFalse(t.flight_authorized)
                self.assertEqual(ui.view_state,'PREFLIGHT BLOCKED')


if __name__ == '__main__':
    unittest.main()
