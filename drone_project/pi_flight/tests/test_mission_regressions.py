"""Flight lifecycle fault injection with synthetic clocks and controllers only."""
import contextlib
import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from flightcheck.routes import ROUTES, Waypoint, compile_plan
from flightcheck.telemetry import Rejected
from test_gcs import route_scenario
from test_state_machine import scenario


class MissionRegressionTests(unittest.TestCase):
    def quiet_run(self, mission):
        with contextlib.redirect_stdout(io.StringIO()):
            return mission.run()

    def assert_no_mutation(self, transport):
        self.assertFalse(any(command in ('parameter', 176, 400, 22, 'target')
                             for command, _ in transport.calls))

    def test_unknown_route_rejected_before_parameter_write_or_arm(self):
        m, t, _ = route_scenario()
        m.route = 'invalid'
        self.assertFalse(self.quiet_run(m))
        self.assert_no_mutation(t)
        self.assertFalse(t.flight_authorized)

    def test_missing_heading_rejected_before_parameter_write_or_arm(self):
        for yaw in (None, float('nan'), float('inf')):
            m, t, _ = route_scenario()
            m.yaw = yaw
            self.assertFalse(self.quiet_run(m))
            self.assert_no_mutation(t)

    def test_unreviewed_altitude_or_hold_rejected_before_arm(self):
        for key, value in (('altitude_m', 100), ('hold_s', 2), ('setpoint_hz', 0)):
            m, t, _ = route_scenario()
            m.c['mission'][key] = value
            self.assertFalse(self.quiet_run(m))
            self.assert_no_mutation(t)

    def test_route_definition_cannot_change_in_flight(self):
        m, t, r = route_scenario('forward')
        with patch.dict(ROUTES):
            def change_route():
                if t.takeoff_time is not None:
                    ROUTES['forward'] = (Waypoint('Unsafe replacement', 100, 0),)
                    t.hook = None
            t.hook = change_route
            self.assertTrue(self.quiet_run(m))
        events = [data for kind, data in r.events if kind == 'waypoint_complete']
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['action'], 'Forward 1 m')

    def test_completed_mission_is_not_restarted_and_permission_is_revoked(self):
        m, t, _ = scenario()
        self.assertTrue(self.quiet_run(m))
        self.assertFalse(t.flight_authorized)
        calls = list(t.calls)
        self.assertFalse(self.quiet_run(m))
        self.assertEqual(t.calls, calls)

    def test_entry_without_final_recheck_sends_nothing(self):
        m, t, _ = scenario()
        m.machine.state = 'CONNECT'
        self.assertFalse(self.quiet_run(m))
        self.assert_no_mutation(t)

    def test_invalid_baseline_blocks_before_mutation(self):
        m, t, _ = scenario()
        t.store.samples['LOCAL_POSITION_NED'][-1][1]['z'] = float('nan')
        self.assertFalse(self.quiet_run(m))
        self.assert_no_mutation(t)

    def test_restore_failure_is_contained_and_pending_restore_reported(self):
        m, t, r = scenario()
        original = t.set_climb_parameter
        def fail_restore(name, value, kind):
            if t.takeoff_time is not None:
                raise Rejected('synthetic restoration readback failure')
            original(name, value, kind)
        t.set_climb_parameter = fail_restore
        self.assertFalse(self.quiet_run(m))
        self.assertEqual(m.machine.state, 'STOP_SENDING')
        self.assertFalse(t.sending)
        self.assertFalse(t.flight_authorized)
        self.assertTrue(m.parameter_dirty)
        self.assertTrue(any(kind == 'recovery_error' for kind, _ in r.events))

    def test_compiled_plan_is_immutable(self):
        m, _, _ = route_scenario('box')
        plan = compile_plan(m.c, 'box')
        self.assertIsInstance(plan.waypoints, tuple)
        with self.assertRaises(AttributeError):
            plan.altitude_m = 20

    def test_gcs_route_progress_never_moves_backwards(self):
        m, t, _ = route_scenario('box')
        m.ui = SimpleNamespace(request=None, progress=0.0)
        samples = []
        t.hook = lambda: samples.append(m.ui.progress)
        self.assertTrue(self.quiet_run(m))
        self.assertTrue(all(a <= b for a, b in zip(samples, samples[1:])))
        self.assertEqual(m.ui.progress, 1.0)
