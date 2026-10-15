"""FC-resident AUTO plans. No guided position streaming or implicit reconnect/start.

Deployment requires separate AUTO commissioning; this module never changes FC
parameters. Upload is disarmed-only, followed by complete mission readback.
"""
from dataclasses import dataclass
import math
import time

from .telemetry import LinkLost, Rejected
from .routes import compile_plan
from .mission import Machine
from .mission_policy import policy_checks
from .checks import all_pass


@dataclass(frozen=True)
class Item:
    command: int
    x: int
    y: int
    z: float
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0
    param4: float = 0.0
    frame: int = 6  # GLOBAL_RELATIVE_ALT_INT; metres above home, not terrain


def build_items(config, route, latitude, longitude, yaw, home_altitude_m):
    plan = compile_plan(config, route)
    values = (latitude, longitude, yaw, home_altitude_m)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise Rejected('Finite global launch position, heading and home altitude required')
    if not -85 < latitude < 85 or not -180 <= longitude <= 180:
        raise Rejected('Launch coordinates outside supported region')
    lat, lon = round(latitude * 1e7), round(longitude * 1e7)
    # ArduPilot reserves item zero for home (absolute global altitude).
    items = [Item(16, lat, lon, home_altitude_m, frame=0),
             Item(22, lat, lon, plan.altitude_m),
             Item(19, lat, lon, plan.altitude_m, param1=plan.hold_s)]
    labels = ['Home', 'Takeoff', 'Initial hold']
    if plan.waypoints:
        items.append(Item(178, 0, 0, 0, param1=1,
                          param2=config['gcs']['speed_m_s'], param3=-1, frame=2))
        labels.append('Set onboard horizontal speed')
    for waypoint in plan.waypoints:
        north = waypoint.forward * math.cos(yaw) - waypoint.right * math.sin(yaw)
        east = waypoint.forward * math.sin(yaw) + waypoint.right * math.cos(yaw)
        target_lat = round((latitude + math.degrees(north / 6378137.0)) * 1e7)
        target_lon = round((longitude + math.degrees(east / (6378137.0 * math.cos(math.radians(latitude))))) * 1e7)
        items.append(Item(16, target_lat, target_lon, plan.altitude_m,
                          param1=waypoint.hold_s))
        labels.append(waypoint.action + ' / hold')
    items.append(Item(21, lat, lon, 0))
    labels.append('Land at launch')
    return tuple(items), tuple(labels)


class MissionTransfer:
    """Bounded integer mission protocol, with addressing and stale-frame filtering."""
    def __init__(self, transport, clock=time.monotonic):
        self.t, self.clock = transport, clock

    def guard(self):
        self.t.allow_send()
        self.t.require_live()
        if self.t.c.get('_bench_only') or not self.t.flight_authorized:
            raise Rejected('Mission transfer is locked')
        if self.t.armed():
            raise Rejected('Cannot replace mission while armed')

    def wait(self, kinds, after, send):
        for _ in range(3):
            self.guard()
            send()
            deadline = self.clock() + self.t.c['mission']['command_timeout_s']
            while self.clock() < deadline:
                self.guard()
                self.t.pump()
                self.t.heartbeat()
                events = []
                for kind in kinds:
                    for stamp, data in self.t.store.samples[kind]:
                        if stamp <= after or data.get('mission_type', 0) != 0:
                            continue
                        if data.get('target_system', 0) not in (0, self.t.c['link']['source_system']):
                            continue
                        if data.get('target_component', 0) not in (0, self.t.c['link']['source_component']):
                            continue
                        events.append((stamp, kind, data))
                if events:
                    return min(events, key=lambda row: row[0])
        raise Rejected('Mission protocol timeout; no arming permitted')

    def send_item(self, seq, item):
        self.guard()
        self.t.connection.mav.mission_item_int_send(
            *self.t.target, seq, item.frame, item.command, 0, 1,
            item.param1, item.param2, item.param3, item.param4,
            item.x, item.y, item.z, 0)

    def upload_verified(self, items):
        if not items or len(items) > 32:
            raise Rejected('Invalid onboard mission count')
        self.guard()
        mav = self.t.connection.mav
        cursor = self.clock()
        send = lambda: mav.mission_count_send(*self.t.target, len(items), 0)
        requested = set()
        deadline = self.clock() + 3 * (len(items) + 2) * self.t.c['mission']['command_timeout_s']
        while self.clock() < deadline:
            cursor, kind, data = self.wait(
                ('MISSION_REQUEST_INT', 'MISSION_REQUEST', 'MISSION_ACK'), cursor, send)
            if kind == 'MISSION_ACK':
                if data.get('type') != 0 or requested != set(range(len(items))):
                    raise Rejected('Mission upload rejected or acknowledged prematurely')
                break
            if kind != 'MISSION_REQUEST_INT':
                raise Rejected('FC requested legacy float mission protocol; integer upload required')
            seq = data.get('seq')
            if not isinstance(seq, int) or not 0 <= seq < len(items):
                raise Rejected('FC requested invalid mission sequence')
            requested.add(seq)
            send = lambda seq=seq: self.send_item(seq, items[seq])
        else:
            raise Rejected('Mission upload exceeded bounded transfer deadline')
        # Download the stored plan, not just the ACK. Never clear the old mission
        # first: a failed transaction must not deliberately erase its predecessor.
        cursor, _, count = self.wait(('MISSION_COUNT',), self.clock(),
            lambda: mav.mission_request_list_send(*self.t.target, 0))
        if count.get('count') != len(items):
            raise Rejected('Mission readback count mismatch')
        for seq, wanted in enumerate(items):
            cursor, _, actual = self.wait(('MISSION_ITEM_INT',), self.clock(),
                lambda seq=seq: mav.mission_request_int_send(*self.t.target, seq, 0))
            expected = vars(wanted)
            if actual.get('seq') != seq or actual.get('autocontinue') != 1:
                raise Rejected('Mission readback sequence/continuation mismatch')
            for field, value in expected.items():
                read = actual.get(field)
                # MAV_FRAME_GLOBAL and GLOBAL_INT are equivalent on download.
                if field == 'frame' and read in ({0, 5} if value == 0 else {3, 6} if value == 6 else {value}):
                    continue
                if not isinstance(read, (int, float)) or not math.isfinite(read) or not math.isclose(read, value, rel_tol=0, abs_tol=1e-5 if field in ('z', 'param1', 'param2', 'param3', 'param4') else 0):
                    raise Rejected('Mission readback mismatch: item '+str(seq)+' '+field)
        self.guard()
        mav.mission_ack_send(*self.t.target, 0, 0)
        self.t.report.event('onboard_upload_verified', items=[vars(item) for item in items])
        return True


class OnboardMission:
    """Start once, then observe. Losing this process does not cancel AUTO."""
    def __init__(self, transport, config, report, machine=None, route='hover', ui=None,
                 clock=time.monotonic):
        self.t, self.c, self.report = transport, config, report
        self.machine = machine or Machine(report)
        self.route, self.ui, self.clock = route, ui, clock
        self.execution_started = False
        self.started = False
        self.yaw = None
        self.baseline = None
        self.cancel_requested = False

    def state(self, name):
        self.machine.transition(name)
        if self.ui:
            self.ui.action = name.replace('_', ' ')

    def ground_guard(self, mode=None, allow_cancelled=False):
        if self.cancel_requested and not allow_cancelled:
            raise Rejected('Operator cancelled launch/monitoring')
        self.t.require_live()
        if mode is not None and self.t.mode() != mode:
            raise Rejected('Pilot/FC changed mode; launch cancelled')
        landed = self.t.store.latest('EXTENDED_SYS_STATE', self.clock(), self.c['link']['max_age_s'])
        if landed.get('landed_state') != 1:
            raise Rejected('Fresh on-ground evidence required')

    def await_state(self, predicate, guard):
        deadline = self.clock() + self.c['mission']['command_timeout_s']
        while self.clock() < deadline:
            self.t.pump()
            self.t.heartbeat()
            guard()
            if predicate():
                return
        raise Rejected('Onboard launch state confirmation timeout')

    def controls(self):
        if not self.ui:
            return
        request, self.ui.request = self.ui.request, None
        if request == 'DISARM':
            try:
                self.ground_guard()
            except (LinkLost, Rejected):
                self.ui.notice = 'DISARM refused: fresh on-ground evidence required'
                return
            mode = self.t.mode()
            self.t.command(400, [0, 0], flight=True, guard=lambda: self.ground_guard(mode))
        elif request == 'RTL':
            if not self.c.get('gcs', {}).get('rtl_reviewed'):
                self.ui.notice = 'RTL blocked: recovery altitude/home behavior not reviewed'
                return
            mode = self.t.mode()
            if mode != 'AUTO':
                self.ui.notice = 'RTL refused: pilot/FC already owns recovery'
                return
            now = self.clock()
            gps = self.t.store.latest('GPS_RAW_INT', now, self.c['link']['max_age_s'])
            ekf = self.t.store.latest('EKF_STATUS_REPORT', now, self.c['link']['max_age_s'])
            self.t.store.latest('HOME_POSITION', now, self.c['link']['max_age_s'])
            if gps.get('fix_type', 0) < 3 or not ekf.get('flags', 0) & 16:
                self.ui.notice = 'RTL refused: GPS/absolute position unavailable'
                return
            def unchanged():
                self.t.require_live()
                if self.t.mode() not in ('AUTO', 'RTL', 'LAND'):
                    raise Rejected('Pilot changed mode during RTL request')
            self.t.command(176, [1, 6], flight=True, guard=unchanged)

    def run(self):
        if self.execution_started or self.machine.state != 'FAST_RECHECK':
            self.t.flight_authorized = False
            return False
        self.execution_started = True
        try:
            self.ground_guard()
            if self.t.armed() or not self.t.flight_authorized:
                raise Rejected('Onboard launch requires disarmed vehicle and final authorization')
            if self.c['firmware'].get('mission_policy') != 'onboard_auto_v1':
                raise Rejected('Onboard backend requires matching onboard policy')
            checks = policy_checks(self.t, self.c)
            if not checks or not all_pass(checks):
                raise Rejected('Onboard policy/readbacks not approved')
            now, age = self.clock(), self.c['link']['max_age_s']
            pos = self.t.store.latest('GLOBAL_POSITION_INT', now, age)
            home = self.t.store.latest('HOME_POSITION', now, age)
            yaw = self.t.store.latest('ATTITUDE', now, age)['yaw']
            self.yaw = yaw
            self.baseline = dict(self.t.store.latest('LOCAL_POSITION_NED', now, age))
            if not math.isfinite(pos['relative_alt']) or abs(pos['relative_alt']) > 500:
                raise Rejected('Launch must be within 0.5 m of home altitude')
            items, labels = build_items(self.c, self.route, pos['lat']/1e7,
                                       pos['lon']/1e7, yaw, home['altitude']/1000)
            self.state('UPLOAD_MISSION')
            transfer = MissionTransfer(self.t, self.clock)
            transfer.upload_verified(items)
            cursor, _, current = transfer.wait(('MISSION_CURRENT',), self.clock(),
                lambda: self.t.connection.mav.mission_set_current_send(*self.t.target, 1))
            if current.get('seq') != 1:
                raise Rejected('FC did not select the first executable item')
            # Upload/readback can take time: require another complete fast sample.
            from .checks import evaluate
            start, end = self.t.sample(self.c['sampling']['fast_s'])
            checks = evaluate(self.t.store, self.c, start, end) + policy_checks(self.t, self.c)
            self.report.checklist(checks, 'onboard-post-upload')
            if not all_pass(checks):
                raise Rejected('Post-upload launch recheck failed')
            original_mode = self.t.mode()
            self.state('SET_GUIDED')
            self.t.command(176, [1, 4], flight=True,
                guard=lambda: self.ground_guard(original_mode) if self.t.mode() != 'GUIDED' else self.ground_guard())
            self.await_state(lambda: self.t.mode() == 'GUIDED', lambda: self.ground_guard())
            self.state('ARM')
            arm_started = self.clock()
            self.t.command(400, [1, 0], flight=True, guard=lambda: self.ground_guard('GUIDED'))
            self.await_state(self.t.armed, lambda: self.ground_guard('GUIDED'))
            self.t.command(512, [242], guard=lambda: self.ground_guard('GUIDED'))
            self.await_state(lambda: bool(self.t.store.samples['HOME_POSITION']) and
                self.t.store.samples['HOME_POSITION'][-1][0] > arm_started,
                lambda: self.ground_guard('GUIDED'))
            # Arming can reset home. Refuse a stale altitude/landing reference.
            armed_home = self.t.store.latest('HOME_POSITION', self.clock(), age)
            offset_n = math.radians((armed_home['latitude'] - items[0].x)/1e7)*6378137
            offset_e = math.radians((armed_home['longitude'] - items[0].y)/1e7)*6378137*math.cos(math.radians(pos['lat']/1e7))
            if math.hypot(offset_n, offset_e) > .5 or abs(armed_home['altitude']/1000 - home['altitude']/1000) > .25:
                raise Rejected('Arming changed home reference; rebuild mission before takeoff')
            self.state('START_AUTO')
            # Report intent before transmission: an ACK timeout cannot prove the
            # start was rejected. Never automatically retry mission start.
            self.started = True
            def start_guard():
                mode = self.t.mode()
                if not self.t.armed() or mode not in ('GUIDED', 'AUTO'):
                    raise Rejected('Pilot/FC changed state during mission start')
                if mode == 'GUIDED':
                    self.ground_guard('GUIDED')
            self.t.command(300, [0, 0], flight=True, guard=start_guard)
            self.await_state(lambda: self.t.mode() == 'AUTO', lambda: self.t.require_live())
            self.state('AUTO_RUNNING')
            observed = False
            while True:
                if self.cancel_requested:
                    raise Rejected('Operator stopped monitoring; onboard execution is not cancelled')
                self.t.pump()
                self.t.heartbeat()
                self.t.require_live()
                mode, armed = self.t.mode(), self.t.armed()
                current = self.t.store.latest('MISSION_CURRENT', self.clock(), age)
                seq = current.get('seq', -1)
                if not isinstance(seq, int) or not 1 <= seq < len(items):
                    raise Rejected('Invalid or unavailable onboard mission progress')
                observed |= seq == len(items)-1
                if self.ui:
                    self.ui.waypoint = str(seq)+' / '+str(len(items)-1)
                    self.ui.action = labels[seq]
                    self.ui.progress = max(self.ui.progress, (seq-1)/(len(items)-1))
                if not armed:
                    self.ground_guard()
                    if not observed:
                        raise Rejected('Disarmed before final landing item was observed')
                    self.state('COMPLETE')
                    if self.ui:
                        self.ui.progress = 1.0
                    return True
                if mode not in ('AUTO', 'LAND'):
                    self.state('RELEASE_CONTROL_AND_MONITOR')
                    return False  # No mode re-acquisition or automatic mission restart.
                self.controls()
        except (Exception, KeyboardInterrupt) as exc:
            self.report.event('onboard_monitor_stopped', error=str(exc),
                              start_may_have_been_sent=self.started,
                              completion_unknown=self.started)
            if not self.started:
                # If launch failed after arming, only ordinary ground disarm is
                # allowed, with fresh landed evidence and unchanged GUIDED mode.
                try:
                    if self.t.armed():
                        self.ground_guard('GUIDED', allow_cancelled=True)
                        self.t.command(400, [0, 0], flight=True,
                                       guard=lambda: self.ground_guard('GUIDED', allow_cancelled=True))
                except (Exception, KeyboardInterrupt) as recovery:
                    self.report.event('onboard_ground_recovery_unconfirmed', error=str(recovery))
            # Never label the aircraft disarmed unless a fresh heartbeat says so.
            try:
                disarmed = not self.t.armed()
            except Exception:
                disarmed = False
            self.state('STOP_SENDING' if self.started or not disarmed else 'ABORT_DISARMED')
            if self.ui:
                self.ui.notice = ('Monitoring stopped; onboard mission may still be active. Use RC; do not restart.'
                                 if self.started else 'Launch aborted: '+str(exc))
            return False
        finally:
            self.t.flight_authorized = False
