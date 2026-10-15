"""Synthetic FC mission protocol: never opens UART or arms physical hardware."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from flightcheck.onboard import Item, MissionTransfer, OnboardMission, build_items
from flightcheck.telemetry import Store, Rejected, LinkLost
from flightcheck.mission import Machine
from flightcheck.mission_policy import policy_checks
from flightcheck.checks import Check
from reviewed_fixture import reviewed_config
from test_state_machine import FakeReport


def config():
    c = reviewed_config()
    c['mission']['hold_s'] = 5
    c['gcs'].update(routes_reviewed=True, speed_m_s=.2, position_tolerance_m=.2,
                    arrival_speed_m_s=.1, leg_timeout_s=30, execution_backend='onboard_auto')
    c['firmware'].update(mission_policy='onboard_auto_v1', takeoff_altitude_reference='relative_home')
    return c


class Wire:
    def __init__(self):
        self.c, self.store, self.report = config(), Store(), FakeReport()
        self.target = (1, 1)
        self.now, self.flight_authorized, self.is_armed = 10., True, False
        self.connection = SimpleNamespace(mav=self)
        self.saved = {}
        self.pending = []
        self.bad = None
        self.quiet = False
        self.calls = []

    def allow_send(self):
        pass

    def require_live(self):
        pass

    def armed(self):
        return self.is_armed

    def heartbeat(self):
        pass

    def emit(self, kind, **data):
        if not self.quiet:
            self.pending.append((kind, {'target_system':254, 'target_component':191,
                                        'mission_type':0, **data}))

    def pump(self):
        self.now += .1
        for kind, data in self.pending:
            self.store.put(kind, data, self.now)
        self.pending.clear()

    def mission_count_send(self, system, component, count, mission_type):
        self.calls.append('count')
        self.count = count
        self.emit('MISSION_REQUEST_INT', seq=0)

    def mission_item_int_send(self, system, component, seq, frame, command, current,
                              autocontinue, p1, p2, p3, p4, x, y, z, mission_type):
        self.calls.append('item')
        self.saved[seq] = dict(seq=seq, frame=frame, command=command, autocontinue=autocontinue,
                               param1=p1, param2=p2, param3=p3, param4=p4, x=x, y=y, z=z)
        if seq+1 < self.count:
            self.emit('MISSION_REQUEST_INT', seq=seq+1)
        else:
            self.emit('MISSION_ACK', type=0)

    def mission_request_list_send(self, *args):
        self.emit('MISSION_COUNT', count=self.count)

    def mission_request_int_send(self, system, component, seq, mission_type):
        data = dict(self.saved[seq])
        if self.bad:
            data[self.bad] = 999
        self.emit('MISSION_ITEM_INT', **data)

    def mission_ack_send(self, *args):
        self.calls.append('readback_ack')


class OnboardTests(unittest.TestCase):
    def test_hover_is_takeoff_hold_land_with_explicit_home(self):
        items, labels = build_items(config(), 'hover', 10, 76, 0, 100)
        self.assertEqual([i.command for i in items], [16,22,19,21])
        self.assertEqual(items[0].frame, 0)
        self.assertEqual(items[1].z, 2)
        self.assertEqual(items[2].param1, 5)
        self.assertEqual(items[-1].x, 100000000)
        self.assertEqual(len(items), len(labels))

    def test_forward_and_box_return_to_launch_before_land(self):
        for route, count in [('forward',7), ('box',9)]:
            items, _ = build_items(config(), route, 10, 76, 0, 100)
            self.assertEqual(len(items), count)
            self.assertEqual(items[4].command, 16)
            self.assertGreater(items[4].x, items[0].x)
            self.assertEqual((items[-2].x, items[-2].y), (items[0].x, items[0].y))
            self.assertEqual(items[-1].command, 21)

    def test_heading_rotates_route_east(self):
        import math
        items, _ = build_items(config(), 'forward', 10, 76, math.pi/2, 100)
        self.assertEqual(items[4].x, items[0].x)
        self.assertGreater(items[4].y, items[0].y)

    def test_invalid_coordinates_heading_and_route_rejected(self):
        for lat, yaw in [(90,0), (float('nan'),0), (10,float('inf'))]:
            with self.assertRaises(Rejected):
                build_items(config(), 'hover', lat, 76, yaw, 100)
        with self.assertRaises(ValueError):
            build_items(config(), 'unknown', 10, 76, 0, 100)

    def test_upload_requires_complete_readback(self):
        t = Wire()
        items, _ = build_items(t.c, 'box', 10, 76, 0, 100)
        self.assertTrue(MissionTransfer(t, lambda:t.now).upload_verified(items))
        self.assertIn('readback_ack', t.calls)
        self.assertEqual(len(t.saved), len(items))

    def test_corrupted_readback_fails_closed(self):
        for field in ('command', 'x', 'y', 'z', 'frame', 'param1', 'seq', 'autocontinue'):
            t = Wire()
            t.bad = field
            with self.assertRaises(Rejected):
                MissionTransfer(t, lambda:t.now).upload_verified((Item(22,1,2,2),))
            self.assertNotIn('readback_ack', t.calls)

    def test_armed_unauthorized_and_bench_transfer_send_nothing(self):
        for kind in ('armed', 'locked', 'bench'):
            t = Wire()
            if kind == 'armed': t.is_armed = True
            if kind == 'locked': t.flight_authorized = False
            if kind == 'bench': t.c['_bench_only'] = True
            with self.assertRaises(Rejected):
                MissionTransfer(t, lambda:t.now).upload_verified((Item(22,1,2,2),))
            self.assertEqual(t.calls, [])

    def test_timeout_and_stale_ack_cannot_pass(self):
        t = Wire()
        t.quiet = True
        t.store.put('MISSION_ACK', {'type':0}, t.now-1)
        with self.assertRaises(Rejected):
            MissionTransfer(t, lambda:t.now).upload_verified((Item(22,1,2,2),))
        self.assertEqual(t.calls, ['count']*3)

    def test_onboard_policy_requires_review_and_fc_safety_readbacks(self):
        c = config()
        values = dict(WP_RFND_USE=0, FS_GCS_ENABLE=0, FS_GCS_TIMEOUT=5,
                      MAV_GCS_SYSID=254, MAV_GCS_SYSID_HI=0, FS_OPTIONS=16,
                      ARMING_SKIPCHK=0, FS_THR_ENABLE=1, FS_EKF_ACTION=1,
                      WP_SPD_UP=.3000000119, MIS_RESTART=1)
        t = SimpleNamespace(read_param=lambda name:(values[name],9))
        self.assertEqual(policy_checks(t,c)[0].state, 'UNKNOWN')
        c['commissioning']['onboard_auto_reviewed'] = True
        self.assertTrue(all(ch.state == 'PASS' for ch in policy_checks(t,c)))
        values['FS_EKF_ACTION'] = 0
        self.assertTrue(any(ch.state == 'FAIL' for ch in policy_checks(t,c)))

    def test_illegal_reentry_never_transmits(self):
        t = Wire()
        m = OnboardMission(t,t.c,t.report,clock=lambda:t.now)
        self.assertFalse(m.run())
        self.assertFalse(t.flight_authorized)
        self.assertEqual(t.calls, [])

    def launch_fixture(self):
        t = Wire()
        t.current_mode = 'STABILIZE'
        t.active = False
        t.report.checklist = Mock()
        t.refresh = lambda: [t.store.put(kind, data, t.now) for kind, data in (
            ('EXTENDED_SYS_STATE', {'landed_state':1}),
            ('ATTITUDE', {'yaw':0}),
            ('LOCAL_POSITION_NED', {'x':0,'y':0,'z':0}),
            ('GLOBAL_POSITION_INT', {'lat':100000000,'lon':760000000,'relative_alt':0}),
            ('HOME_POSITION', {'latitude':100000000,'longitude':760000000,'altitude':100000}))]
        t.refresh()
        pump = t.pump
        def launch_pump():
            pump()
            t.refresh()
            if t.active:
                t.seq = min(t.seq+1, t.count-1)
                t.store.put('MISSION_CURRENT', {'seq':t.seq}, t.now)
                if t.seq == t.count-1:
                    t.is_armed = False
        t.pump = launch_pump
        t.mode = lambda:t.current_mode
        def command(number, params=(), flight=False, guard=None):
            if guard: guard()
            t.calls.append((number,params))
            if number == 176: t.current_mode = 'GUIDED'
            if number == 400: t.is_armed = bool(params[0])
            if number == 300:
                t.current_mode, t.active, t.seq = 'AUTO', True, 1
            t.pump()
            if guard: guard()
        t.command = command
        def select(system, component, seq):
            t.emit('MISSION_CURRENT',seq=seq)
        t.mission_set_current_send = select
        t.sample = lambda duration:(t.now-3,t.now)
        machine = Machine(t.report)
        machine.state = 'FAST_RECHECK'
        m = OnboardMission(t,t.c,t.report,machine,clock=lambda:t.now)
        return m,t

    def test_verified_upload_precedes_arm_and_only_one_start_no_setpoints(self):
        m,t = self.launch_fixture()
        passed = [Check('synthetic ready','PASS',True,'ready')]
        with patch('flightcheck.onboard.policy_checks', return_value=passed), \
             patch('flightcheck.checks.evaluate', return_value=passed):
            self.assertTrue(m.run())
        commands = [x[0] for x in t.calls if isinstance(x,tuple)]
        self.assertEqual(commands, [176,400,512,300])
        self.assertLess(t.calls.index('readback_ack'), next(i for i,x in enumerate(t.calls) if isinstance(x,tuple) and x[0]==400))
        self.assertEqual(m.machine.state,'COMPLETE')
        before = list(t.calls)
        self.assertFalse(m.run())
        self.assertEqual(t.calls,before)

    def test_link_loss_after_start_sends_no_land_disarm_or_restart(self):
        m,t = self.launch_fixture()
        passed = [Check('synthetic ready','PASS',True,'ready')]
        state = m.state
        def disconnect(name):
            state(name)
            if name == 'AUTO_RUNNING':
                t.require_live = Mock(side_effect=LinkLost('synthetic disconnect'))
        m.state = disconnect
        with patch('flightcheck.onboard.policy_checks', return_value=passed), \
             patch('flightcheck.checks.evaluate', return_value=passed):
            self.assertFalse(m.run())
        self.assertEqual([x[0] for x in t.calls if isinstance(x,tuple)], [176,400,512,300])
        self.assertEqual(m.machine.state,'STOP_SENDING')
        self.assertTrue(m.started)
        self.assertFalse(t.flight_authorized)

    def test_changed_home_aborts_before_start_and_normally_disarms_on_ground(self):
        m,t = self.launch_fixture()
        refresh = t.refresh
        def shifted_home():
            refresh()
            if t.is_armed:
                t.store.put('HOME_POSITION', {'latitude':100000000,
                    'longitude':760000000,'altitude':101000}, t.now)
        t.refresh = shifted_home
        passed = [Check('synthetic ready','PASS',True,'ready')]
        with patch('flightcheck.onboard.policy_checks', return_value=passed), \
             patch('flightcheck.checks.evaluate', return_value=passed):
            self.assertFalse(m.run())
        commands = [x for x in t.calls if isinstance(x,tuple)]
        self.assertNotIn(300, [x[0] for x in commands])
        self.assertEqual(commands[-1], (400,[0,0]))
        self.assertFalse(t.is_armed)
        self.assertFalse(m.started)

    def test_lost_start_ack_is_unknown_and_never_retried_or_auto_cancelled(self):
        m,t = self.launch_fixture()
        command = t.command
        def lost_ack(number, *args, **kwargs):
            command(number, *args, **kwargs)
            if number == 300:
                raise Rejected('synthetic start ACK timeout after FC accepted start')
        t.command = lost_ack
        passed = [Check('synthetic ready','PASS',True,'ready')]
        with patch('flightcheck.onboard.policy_checks', return_value=passed), \
             patch('flightcheck.checks.evaluate', return_value=passed):
            self.assertFalse(m.run())
        self.assertEqual([x[0] for x in t.calls if isinstance(x,tuple)], [176,400,512,300])
        self.assertTrue(m.started)
        self.assertEqual(m.machine.state,'STOP_SENDING')

    def test_unreviewed_policy_blocks_before_upload_and_arm(self):
        t = Wire()
        machine = Machine(t.report)
        machine.state = 'FAST_RECHECK'
        m = OnboardMission(t,t.c,t.report,machine,clock=lambda:t.now)
        with patch.object(m, 'ground_guard'):
            self.assertFalse(m.run())
        self.assertEqual(t.calls, [])
        self.assertFalse(t.flight_authorized)
