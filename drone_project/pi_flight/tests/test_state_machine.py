import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from flightcheck.config import load
from flightcheck.mission import Machine, Mission, SEQUENCE, Takeover, target_ned
from flightcheck.telemetry import LinkLost, Rejected, Store


class FakeReport:
    def __init__(self):
        self.events = []
    def event(self, kind, **data):
        self.events.append((kind, data))
    def save(self, name, data):
        self.events.append((name, data))


class FakeTransport:
    def __init__(self):
        self.now = 100.
        self.current_mode = "ALT_HOLD"
        self.is_armed = False
        self.sending = True
        self.flight_authorized = True
        self.calls = []
        self.store = Store()
        self.takeoff_time = None
        self.land_time = None
        self.parameter = (250., 9)
        self.reject = None
        self.hook = None
        self.refresh()
    def refresh(self):
        height = 0 if self.takeoff_time is None else min(2, max(0, (self.now-self.takeoff_time)*.3))
        self.store.put("LOCAL_POSITION_NED", dict(x=12.,y=15.,z=-3.-height,vx=0.,vy=0.,vz=0.), self.now)
        self.store.put("GLOBAL_POSITION_INT", {"relative_alt": 1000+height*1000}, self.now)
        self.store.put("DISTANCE_SENSOR", {"current_distance":20+height*100}, self.now)
    def pump(self, duration=.05):
        self.now += max(.05, duration)
        if self.land_time is not None and self.now-self.land_time > .2:
            self.is_armed = False
        self.refresh()
        if self.hook:
            self.hook()
    def require_live(self):
        if not self.sending:
            raise LinkLost("synthetic link loss")
    def heartbeat(self):
        self.require_live()
    def mode(self):
        self.require_live()
        return self.current_mode
    def armed(self):
        self.require_live()
        return self.is_armed
    def command(self, command, params=(), flight=False, guard=None):
        if guard:
            guard()
        self.calls.append((command, params))
        if command == self.reject:
            raise Rejected("synthetic rejection")
        if command == 176:
            self.current_mode = "GUIDED" if params[1] == 4 else "LAND"
            if self.current_mode == "LAND":
                self.land_time = self.now
        if command == 400:
            self.is_armed = True
        if command == 22:
            self.takeoff_time = self.now
    def read_param(self, name):
        return self.parameter
    def set_climb_parameter(self, name, value, kind):
        if self.is_armed:
            raise AssertionError("parameter set while armed")
        self.calls.append(("parameter", (name,value,kind)))
        self.parameter = (value,kind)
    def position_target(self, *coords, guard=None):
        guard()
        self.calls.append(("target", coords))


def scenario():
    c = load(Path(__file__).parents[1]/"config.example.toml")
    c["firmware"].update(climb_parameter="WPNAV_SPEED_UP", climb_parameter_type=9,
                         climb_parameter_units_per_m_s=100, climb_parameter_min=10,
                         climb_parameter_max=1000, takeoff_altitude_reference="relative_home")
    report = FakeReport()
    t = FakeTransport()
    machine = Machine(report)
    for state in ("FULL_PREFLIGHT", "WAIT_FOR_ENTER", "FAST_RECHECK"):
        machine.transition(state)
    mission = Mission(t,c,report,machine,clock=lambda:t.now)
    mission.monitor_limits = lambda: (0. if t.takeoff_time is None else min(2., (t.now-t.takeoff_time)*.3), 0.)
    mission.prearm_monitor = lambda: None # Pure sensor gates are tested separately.
    return mission,t,report


class StateTests(unittest.TestCase):
    def test_illegal_transition(self):
        with self.assertRaises(RuntimeError):
            Machine(FakeReport()).transition("ARM")

    def test_ned_target_preserves_original_xy(self):
        self.assertEqual(target_ned({"x":12,"y":15,"z":-3},2), (12,15,-5))

    def test_success_hold_after_settle_and_restore(self):
        m,t,r = scenario()
        self.assertTrue(m.run())
        self.assertEqual(t.parameter, (250,9))
        states = [d["state"] for kind,d in r.events if kind == "state"]
        self.assertEqual(states, list(SEQUENCE))
        self.assertGreaterEqual(t.land_time-t.takeoff_time, 1.8/.3+3+10)
        self.assertTrue(all(coords == (12,15,-5) for cmd,coords in t.calls if cmd == "target"))
        self.assertEqual([p for cmd,p in t.calls if cmd == 400], [[1,0]])

    def test_command_rejection_guided_arm_takeoff(self):
        for command in (176,400,22):
            with self.subTest(command=command):
                m,t,r = scenario()
                t.reject = command
                self.assertFalse(m.run())
                self.assertFalse(t.is_armed)
                self.assertEqual(t.parameter, (250,9))
                if command == 22:
                    self.assertEqual(sum(cmd == 176 and p[1] == 9 for cmd,p in t.calls),1)

    def test_pilot_and_failsafe_modes_stop_commands(self):
        for mode in ("STABILIZE","ALT_HOLD","LOITER","RTL","LAND"):
            m,t,r = scenario()
            m.owned = True
            t.is_armed = True
            t.current_mode = mode
            with self.assertRaises(Takeover):
                m.guard()
            self.assertFalse(m.owned)
            self.assertEqual(t.calls, [])

    def test_link_loss_does_not_claim_land(self):
        m,t,r = scenario()
        t.is_armed = True
        m.owned = True
        m.recover(LinkLost("lost"))
        self.assertEqual(m.machine.state, "STOP_SENDING")
        self.assertFalse(t.sending)
        self.assertEqual(t.calls, [])

    def test_signal_and_exception_in_each_active_state(self):
        for state in ("SET_GUIDED","ARM","TAKEOFF_SLOWLY","SETTLE_AT_2M","HOLD_10_SECONDS"):
            for error in (RuntimeError("fault"),KeyboardInterrupt()):
                with self.subTest(state=state, error=type(error)):
                    m,t,r = scenario()
                    raised = False
                    def hook():
                        nonlocal raised
                        if m.machine.state == state and not raised:
                            raised = True
                            raise error
                    t.hook = hook
                    self.assertFalse(m.run())
                    self.assertFalse(t.is_armed)
                    self.assertEqual(t.parameter, (250,9))
                    self.assertFalse(any(cmd == 400 and p[0] == 0 for cmd,p in t.calls))

    def test_repeated_signal_is_idempotent(self):
        m,t,r = scenario()
        m.signal(2,None)
        m.signal(2,None)
        self.assertTrue(m.cancel_requested)
        self.assertEqual(t.calls, [])

    def test_takeover_during_setpoint_no_further_output(self):
        m,t,r = scenario()
        def hook():
            if m.machine.state == "HOLD_10_SECONDS":
                t.current_mode = "ALT_HOLD"
                t.land_time = t.now # Fake pilot lands/disarms independently.
                t.hook = None
        t.hook = hook
        self.assertFalse(m.run())
        self.assertFalse(any(cmd == 176 and p[1] == 9 for cmd,p in t.calls))

    def test_pilot_takeover_during_land_is_failed_test(self):
        m,t,r = scenario()
        def hook():
            if m.machine.state == "WAIT_DISARM":
                t.current_mode = "ALT_HOLD"
                t.hook = None
        t.hook = hook
        self.assertFalse(m.run())
        self.assertEqual(sum(cmd == 176 and p[1] == 9 for cmd,p in t.calls),1)

    def test_exception_and_interrupt_during_landing(self):
        for state in ("LAND", "WAIT_DISARM"):
            for error in (RuntimeError("landing fault"), KeyboardInterrupt()):
                with self.subTest(state=state, error=type(error)):
                    m,t,r = scenario()
                    raised = False
                    def hook():
                        nonlocal raised
                        if m.machine.state == state and not raised:
                            raised = True
                            raise error
                    t.hook = hook
                    self.assertFalse(m.run())
                    self.assertFalse(t.is_armed)
                    self.assertEqual(sum(cmd == 176 and p[1] == 9 for cmd,p in t.calls),1)

    def test_takeoff_command_uses_reviewed_altitude_reference(self):
        m,t,r = scenario()
        self.assertTrue(m.run())
        self.assertEqual([p[-1] for cmd,p in t.calls if cmd == 22], [3.])

    def test_unreviewed_altitude_reference_never_arms(self):
        m,t,r = scenario()
        m.c["firmware"]["takeoff_altitude_reference"] = "unknown"
        self.assertFalse(m.run())
        self.assertFalse(any(cmd == 400 for cmd,p in t.calls))

    def test_sensor_fault_does_not_prevent_owned_land(self):
        m,t,r = scenario()
        def bad_sensor():
            raise RuntimeError("excessive tilt")
        m.monitor_limits = bad_sensor
        self.assertFalse(m.run())
        self.assertFalse(t.is_armed)
        self.assertEqual(sum(cmd == 176 and p[1] == 9 for cmd,p in t.calls),1)


if __name__ == "__main__":
    unittest.main()
