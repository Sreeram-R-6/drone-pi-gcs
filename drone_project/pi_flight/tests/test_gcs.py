import contextlib
import curses
import io
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import preflight_hover
from flightcheck.config import load
from flightcheck.routes import RouteMission, ROUTES, Waypoint, coordinates, validate_route
from flightcheck.telemetry import LinkLost, Rejected, Store
from flightcheck.terminal import Terminal, relative_home
from test_state_machine import scenario
from reviewed_fixture import reviewed_config


def route_scenario(name="forward"):
    old,t,r = scenario()
    old.c['gcs']['routes_reviewed'] = True
    old.c['mission']['hold_s'] = 5.
    m = RouteMission(t,old.c,r,old.machine,route=name,clock=lambda:t.now)
    m.yaw = 0.
    m.monitor_limits = old.monitor_limits
    m.prearm_monitor = lambda: None
    original_refresh = t.refresh
    target = None
    def refresh():
        original_refresh()
        if target:
            p = dict(t.store.samples['LOCAL_POSITION_NED'][-1][1])
            p.update(x=target[0],y=target[1])
            t.store.put('LOCAL_POSITION_NED', p, t.now)
    def position_target(*coords, guard=None):
        nonlocal target
        guard()
        target = coords
        t.calls.append(('target', coords))
    t.refresh = refresh
    t.position_target = position_target
    return m,t,r


class RoutesTests(unittest.TestCase):
    def test_deployment_routes_are_reviewed_and_valid(self):
        c = reviewed_config()
        for name in ROUTES:
            validate_route(c,name)

    def test_impossible_leg_timeout_rejected(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        c['gcs']['routes_reviewed'] = True
        c['gcs']['leg_timeout_s'] = 10
        with self.assertRaises(Rejected):
            validate_route(c,'forward')
    def test_launch_yaw_and_origin(self):
        b = {'x':12.,'y':15.,'z':-3.}
        n,e,d = coordinates(b, math.pi/2, Waypoint('test',1,1), 2)
        self.assertAlmostEqual(n,11)
        self.assertAlmostEqual(e,16)
        self.assertEqual(d,-5)

    def test_review_radius_speed_gates(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        with self.assertRaises(Rejected):
            validate_route(c,'hover')
        c['gcs']['routes_reviewed'] = True
        validate_route(c,'box')
        c['mission']['max_radius_m'] = 1.5
        with self.assertRaises(Rejected):
            validate_route(c,'box')
        c['gcs']['speed_m_s'] = float('nan')
        with self.assertRaises(Rejected):
            validate_route(c,'forward')

    def test_full_routes_hold_and_land(self):
        for name in ROUTES:
            with self.subTest(name=name), contextlib.redirect_stdout(io.StringIO()):
                m,t,r = route_scenario(name)
                self.assertTrue(m.run())
                self.assertFalse(t.is_armed)
                self.assertEqual(t.parameter,(250,9))
                events = [data for kind,data in r.events if kind == 'waypoint_complete']
                self.assertEqual(len(events),len(ROUTES[name]))
                self.assertGreaterEqual(t.land_time-t.takeoff_time, 1.8/.3+3+5+len(events)*8)
                self.assertFalse(any(cmd == 400 and params[0] == 0 for cmd,params in t.calls))

    def test_route_timeout_lands_and_restores(self):
        with contextlib.redirect_stdout(io.StringIO()):
            m,t,r = route_scenario()
            def no_motion(*coords,guard=None):
                guard()
            t.position_target = no_motion
            self.assertFalse(m.run())
            self.assertFalse(t.is_armed)
            self.assertEqual(t.parameter,(250,9))
            self.assertEqual(sum(cmd == 176 and p[1] == 9 for cmd,p in t.calls),1)

    def test_route_takeover_stops_targets(self):
        with contextlib.redirect_stdout(io.StringIO()):
            m,t,r = route_scenario()
            def hook():
                if m.machine.state == 'ROUTE':
                    t.current_mode = 'ALT_HOLD'
                    t.land_time = t.now
                    t.hook = None
            t.hook = hook
            self.assertFalse(m.run())
            self.assertFalse(any(cmd == 176 and p[1] == 9 for cmd,p in t.calls))

    def test_disarm_refuses_airborne_unknown_and_stale(self):
        for landed in (None,0,2,3,4):
            m,t,r = route_scenario()
            m.ui = SimpleNamespace(request='DISARM',notice='')
            if landed is not None:
                t.store.put('EXTENDED_SYS_STATE',{'landed_state':landed},t.now)
            m.poll_controls()
            self.assertIn('refused',m.ui.notice)
            self.assertEqual(t.calls,[])
        m,t,r = route_scenario()
        t.store.put('EXTENDED_SYS_STATE',{'landed_state':1},t.now-10)
        m.ui = SimpleNamespace(request='DISARM',notice='')
        m.poll_controls()
        self.assertEqual(t.calls,[])

    def test_ground_disarm_is_normal_and_guarded(self):
        m,t,r = route_scenario()
        m.machine.state = 'WAIT_DISARM'
        m.ui = SimpleNamespace(request='DISARM',notice='')
        t.store.put('EXTENDED_SYS_STATE',{'landed_state':1},t.now)
        m.poll_controls()
        self.assertEqual(t.calls,[(400,[0,0])])

    def test_rtl_unreviewed_and_takeover_blocked(self):
        m,t,r = route_scenario()
        m.ui = SimpleNamespace(request='RTL',notice='')
        m.poll_controls()
        self.assertEqual(t.calls,[])
        self.assertIn('owns',m.ui.notice)
        m.owned = True
        t.current_mode = 'GUIDED'
        m.ui.request = 'RTL'
        m.poll_controls()
        self.assertEqual(t.calls,[])
        self.assertIn('blocked',m.ui.notice)

    def test_rtl_ack_and_observed_mode_then_release(self):
        m,t,r = route_scenario()
        m.c['gcs']['rtl_reviewed'] = True
        m.ui = SimpleNamespace(request='RTL',notice='')
        m.owned = True
        t.current_mode = 'GUIDED'
        t.store.put('GPS_RAW_INT',{'fix_type':3},t.now)
        t.store.put('EKF_STATUS_REPORT',{'flags':16},t.now)
        t.store.put('HOME_POSITION',{'latitude':0,'longitude':0},t.now)
        def command(cmd,params,**kwargs):
            kwargs['guard']()
            t.calls.append((cmd,params))
            t.current_mode = 'RTL'
        t.command = command
        from flightcheck.mission import Takeover
        with self.assertRaises(Takeover):
            m.poll_controls()
        self.assertEqual(t.calls,[(176,[1,6])])
        self.assertFalse(m.owned)

    def test_ui_flight_review_blocks_before_transport(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        with patch('preflight_hover.load',return_value=c), patch('preflight_hover.Transport') as transport, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(preflight_hover.main(['--check-once','--flight'],ui=SimpleNamespace(route='forward')),3)
            transport.assert_not_called()

    def test_ui_offline_never_opens_socket(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        with tempfile.TemporaryDirectory() as base:
            c['_base'] = base
            ui = SimpleNamespace(route='box')
            with patch('preflight_hover.load',return_value=c), patch('preflight_hover.Transport') as transport, patch('socket.socket',side_effect=AssertionError('socket opened')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight_hover.main(['--offline'],ui=ui),0)
                self.assertEqual(ui.blockers,[])
                self.assertNotIn('configuration/evidence',[check.name for check in ui.checks])
                self.assertEqual(ui.config['mission']['hold_s'],5)
                transport.assert_not_called()

    def test_home_map_uses_global_home_not_local_origin(self):
        s = Store()
        s.put('HOME_POSITION',{'latitude':0,'longitude':0},1)
        s.put('GLOBAL_POSITION_INT',{'lat':0,'lon':1000},100)
        s.put('LOCAL_POSITION_NED',{'x':999,'y':999},100)
        n,e = relative_home(s,100,2)
        self.assertAlmostEqual(n,0)
        self.assertAlmostEqual(e,11.131949,places=5)
        with self.assertRaises(LinkLost):
            relative_home(s,103,2)

    def test_keyboard_selects_buttons_and_buffered_enter_cannot_launch(self):
        keys = iter([curses.KEY_LEFT,10,-1])
        screen = SimpleNamespace(keypad=lambda x:None,nodelay=lambda x:None,getch=lambda:next(keys))
        with patch('curses.curs_set'):
            ui = Terminal(screen)
        ui.keys()
        self.assertIsNone(ui.request) # No mission: Enter has no flight effect.
        ui.mission = SimpleNamespace(cancel_requested=False)
        keys = iter([curses.KEY_RIGHT,10,-1])
        ui.keys()
        self.assertEqual(ui.request,'RTL')


if __name__ == '__main__':
    unittest.main()
