"""Explicit mission lifecycle; version-specific parameter behavior requires review.

The transport is injected for deterministic tests. There is no disarm command.
"""
import math
import signal
import time
from .checks import all_pass, evaluate
from .telemetry import LinkLost, Rejected
from .mission_policy import policy_checks


class Takeover(RuntimeError):
    pass


SEQUENCE = ("CONNECT", "FULL_PREFLIGHT", "WAIT_FOR_ENTER", "FAST_RECHECK",
            "SET_GUIDED", "ARM", "TAKEOFF_SLOWLY", "SETTLE_AT_2M",
            "HOLD_10_SECONDS", "LAND", "WAIT_DISARM", "COMPLETE")


class Machine:
    def __init__(self, report):
        self.state = "CONNECT"
        self.report = report
        self.report.event("state", state=self.state)

    def transition(self, state):
        current = self.state
        normal = current in SEQUENCE[:-1] and state == SEQUENCE[SEQUENCE.index(current)+1]
        exceptional = (
            state == "ABORT_DISARMED" and current not in ("COMPLETE", "STOP_SENDING") or
            state in ("STOP_SENDING", "RELEASE_CONTROL_AND_MONITOR") and current != "COMPLETE" or
            state == "LAND" and current in ("ARM", "TAKEOFF_SLOWLY", "SETTLE_AT_2M", "HOLD_10_SECONDS", "HOLD_5_SECONDS") or
            state == "WAIT_DISARM" and current == "RELEASE_CONTROL_AND_MONITOR" or
            state == "FAILED" and current not in ("COMPLETE",))
        exceptional |= (state == "HOLD_5_SECONDS" and current == "SETTLE_AT_2M" or
                        state == "ROUTE" and current in ("HOLD_10_SECONDS", "HOLD_5_SECONDS") or
                        state == "LAND" and current == "ROUTE")
        exceptional |= ((current, state) in {
            ('FAST_RECHECK', 'UPLOAD_MISSION'), ('UPLOAD_MISSION', 'SET_GUIDED'),
            ('ARM', 'START_AUTO'), ('START_AUTO', 'AUTO_RUNNING'),
            ('AUTO_RUNNING', 'COMPLETE')})
        if not (normal or exceptional):
            raise RuntimeError(f"Illegal transition {current} -> {state}")
        self.state = state
        self.report.event("state", previous=current, state=state)


def target_ned(baseline, height):
    return baseline["x"], baseline["y"], baseline["z"]-height


class Mission:
    def __init__(self, transport, config, report, machine=None, clock=time.monotonic):
        self.t, self.c, self.report = transport, config, report
        self.machine = machine or Machine(report)
        self.clock = clock
        self.owned = False
        self.land_sent = False
        self.original_parameter = None
        self.parameter_dirty = False
        self.failed = False
        self.cancel_requested = False
        self.baseline = None
        self.relative_baseline = None
        self.last_target = -math.inf
        self.execution_started = False

    def prepare(self):
        """Reject invalid entry and numeric limits before any mutating command."""
        if self.c.get('firmware', {}).get('mission_policy') == 'onboard_auto_v1':
            raise Rejected('GUIDED execution cannot use a GCS-independent AUTO policy')
        self.t.require_live()
        if self.t.armed():
            raise Takeover('Vehicle already armed; mission cannot acquire control')
        if self.machine.state != 'FAST_RECHECK':
            raise Rejected('Mission entry requires completed final recheck')
        for name in ('altitude_m', 'climb_m_s', 'hold_s', 'settle_s',
                     'setpoint_hz', 'command_timeout_s', 'takeoff_timeout_s',
                     'disarm_timeout_s', 'max_altitude_m'):
            value = self.c['mission'].get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise Rejected('Invalid mission setting: '+name)
        if self.c['mission']['altitude_m'] > self.c['mission']['max_altitude_m']:
            raise Rejected('Takeoff height exceeds abort altitude')
        if self.c['mission']['hold_s'] not in (5, 10):
            raise Rejected('Unreviewed initial hold duration')

    def signal(self, signum, frame):
        # Signals request cleanup; repeated Ctrl+C cannot interrupt landing monitor.
        self.cancel_requested = True
        self.report.event("signal", number=signum)

    def guard(self, check_limits=True):
        self.t.require_live()
        if self.owned and self.t.mode() != "GUIDED":
            self.owned = False
            raise Takeover(f"Control released: mode {self.t.mode()}")
        if check_limits and self.machine.state in ("TAKEOFF_SLOWLY", "SETTLE_AT_2M", "HOLD_10_SECONDS", "HOLD_5_SECONDS", "ROUTE"):
            self.monitor_limits()

    def prearm_monitor(self):
        now = self.clock()
        checks = evaluate(self.t.store, self.c, now-self.c["sampling"]["fast_s"], now)
        if not all_pass(checks):
            raise Rejected("Pre-arm state changed: " + ", ".join(c.name for c in checks if c.state != "PASS"))

    def tick(self):
        self.t.pump()
        self.guard()
        self.t.heartbeat()
        if self.cancel_requested:
            raise RuntimeError("Operator interrupt")

    def await_state(self, predicate, timeout, guard=None):
        end = self.clock()+timeout
        while self.clock() < end:
            self.t.pump()
            self.t.require_live()
            self.t.heartbeat()
            if guard:
                guard()
            if predicate():
                return
            if self.cancel_requested:
                raise RuntimeError("Operator interrupt")
        raise Rejected("Observed state timeout")

    def monitor_limits(self):
        now = self.clock()
        age = self.c["link"]["max_age_s"]
        local = self.t.store.latest("LOCAL_POSITION_NED", now, age)
        global_pos = self.t.store.latest("GLOBAL_POSITION_INT", now, age)
        attitude = self.t.store.latest("ATTITUDE", now, age)
        height = self.baseline["z"]-local["z"]
        relative = global_pos["relative_alt"]/1000-self.relative_baseline
        radius = math.hypot(local["x"]-self.baseline["x"], local["y"]-self.baseline["y"])
        tilt = math.degrees(math.hypot(attitude["roll"], attitude["pitch"]))
        speed = math.hypot(local["vx"], local["vy"])
        metrics = {"height_m": height, "relative_m": relative, "radius_m": radius,
                   "tilt_deg": tilt, "speed_m_s": speed, "vertical_m_s": -local["vz"]}
        limits = self.c["mission"]
        valid = (all(math.isfinite(v) for v in metrics.values()) and
            -.3 <= height <= limits["max_altitude_m"] and radius <= limits["max_radius_m"] and
            tilt <= limits["max_tilt_deg"] and speed <= limits["max_horizontal_speed_m_s"] and
            abs(local["vz"]) <= limits["max_vertical_speed_m_s"] and
            abs(height-relative) <= limits["max_altitude_disagreement_m"])
        distance = self.t.store.latest("DISTANCE_SENSOR", now, age)
        # Project the downward slant range to vertical using current attitude.
        vertical_range = distance["current_distance"]/100 * math.cos(attitude["roll"])*math.cos(attitude["pitch"])
        valid &= abs(vertical_range-self.range_baseline-height) <= limits["max_altitude_disagreement_m"]
        checks = evaluate(self.t.store, self.c, now-self.c["sampling"]["fast_s"], now, airborne=True)
        # Full airborne window starts after arm; prior disarmed heartbeat is excluded
        # from this one gate. Current arm state is verified separately below.
        checks = [c for c in checks if c.name != "vehicle identity/state"]
        self.report.event("flight_sample", metrics=metrics, failed_checks=[c.name for c in checks if c.state != "PASS"])
        if not valid or not all_pass(checks) or not self.t.armed():
            raise RuntimeError(f"Mission limit/sensor failure: {metrics}")
        return height, abs(local["vz"])

    def restore(self):
        if self.parameter_dirty:
            if self.t.armed():
                raise Rejected("Restoration deferred until automatic disarm")
            name, value, kind = self.original_parameter
            self.t.set_climb_parameter(name, value, kind)
            self.parameter_dirty = False
            self.report.event("parameter_restored", name=name, value=value, type=kind)

    def land(self):
        self.guard(check_limits=False)
        if not self.owned or self.land_sent:
            return
        self.machine.transition("LAND")
        self.land_sent = True # At most one request, including on missing ACK.
        def landing_guard():
            mode = self.t.mode()
            if mode not in ("GUIDED", "LAND"):
                self.owned = False
                raise Takeover(f"Pilot/FC mode changed to {mode}")
        self.t.command(176, [1, 9], flight=True, guard=landing_guard)
        end = self.clock()+self.c["mission"]["command_timeout_s"]
        while self.clock() < end:
            self.t.pump()
            self.t.heartbeat()
            landing_guard()
            if self.t.mode() == "LAND":
                self.owned = False
                self.machine.transition("WAIT_DISARM")
                return
        raise Rejected("LAND was requested but not observed")

    def monitor_disarm(self):
        """Read-only after takeover; no parameter restoration until disarmed."""
        end = self.clock()+self.c["mission"]["disarm_timeout_s"]
        last_notice = -math.inf
        while True:
            self.t.pump()
            self.t.require_live()
            self.poll_controls()
            # Keep GCS heartbeat in normal landing, but send nothing after takeover.
            if self.machine.state == "WAIT_DISARM":
                if self.t.mode() != "LAND" and self.t.armed():
                    self.failed = True
                    self.machine.transition("RELEASE_CONTROL_AND_MONITOR")
                    print("Pilot/FC changed landing mode; monitoring only.", flush=True)
                else:
                    self.t.heartbeat()
            if not self.t.armed():
                return
            if self.clock() > end:
                self.failed = True
                if self.clock()-last_notice > 5:
                    print("FAIL: disarm deadline exceeded; pilot intervention required. Monitoring only.", flush=True)
                    self.report.event("disarm_timeout")
                    last_notice = self.clock()
            # Intentional continued monitoring of a live armed aircraft, never respawn/reconnect.

    def recover(self, exc):
        self.failed = True
        self.report.event("mission_fault", error=repr(exc))
        if isinstance(exc, LinkLost):
            self.t.sending = False
            self.machine.transition("STOP_SENDING")
            print("LINK LOST: commands stopped. LAND unconfirmed; pilot/FC failsafes own recovery.", flush=True)
            return
        try:
            self.t.pump(.02)
            self.t.require_live()
            if not self.t.armed():
                self.restore()
                self.machine.transition("ABORT_DISARMED")
                return
            if isinstance(exc, Takeover) or not self.owned or self.t.mode() != "GUIDED":
                self.owned = False
                self.machine.transition("RELEASE_CONTROL_AND_MONITOR")
                print("Pilot/FC owns aircraft. All mission commands stopped.", flush=True)
            else:
                try:
                    self.land()
                except Rejected as land_error:
                    self.report.event("land_unconfirmed", error=str(land_error))
                    self.owned = False
                    self.machine.transition("RELEASE_CONTROL_AND_MONITOR")
                    print("LAND not confirmed. Pilot intervention required; monitoring only.", flush=True)
                except Takeover:
                    self.owned = False
                    self.machine.transition("RELEASE_CONTROL_AND_MONITOR")
            self.monitor_disarm()
            self.restore()
            self.machine.transition("FAILED")
        except LinkLost:
            self.t.sending = False
            self.machine.transition("STOP_SENDING")
            print("LINK LOST: commands stopped. Rely on verified FC failsafes/pilot. LAND unconfirmed.", flush=True)
        except (Exception, KeyboardInterrupt) as recovery_error:
            # A rejected restoration or secondary transport error must not
            # escape into curses and leave a stale 'mission active' screen.
            # Do not retry arm, landing or parameter writes blindly.
            self.t.sending = False
            self.owned = False
            self.machine.transition('STOP_SENDING')
            self.report.event('recovery_error', error=repr(recovery_error),
                              restoration_pending=self.parameter_dirty)
            print('RECOVERY ERROR: commands stopped; pilot/FC owns recovery. '+str(recovery_error), flush=True)

    def run(self):
        """Entry only after fresh preflight and explicit Enter. Never called offline."""
        if self.execution_started or self.machine.state != 'FAST_RECHECK':
            self.t.flight_authorized = False
            self.report.event('mission_reentry_rejected', state=self.machine.state)
            return False
        self.execution_started = True
        old_handlers = {s: signal.signal(s, self.signal) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.prepare()
            hold_state = "HOLD_5_SECONDS" if self.c["mission"]["hold_s"] == 5 else "HOLD_10_SECONDS"
            age = self.c["link"]["max_age_s"]
            now = self.clock()
            self.baseline = dict(self.t.store.latest("LOCAL_POSITION_NED", now, age))
            self.relative_baseline = self.t.store.latest("GLOBAL_POSITION_INT", now, age)["relative_alt"]/1000
            self.range_baseline = self.t.store.latest("DISTANCE_SENSOR", now, age)["current_distance"]/100
            if (not all(math.isfinite(self.baseline[key]) for key in ('x','y','z')) or
                    not math.isfinite(self.relative_baseline) or
                    not math.isfinite(self.range_baseline) or self.range_baseline <= 0):
                raise Rejected('Invalid launch position/altitude/range baseline')
            self.report.event("baseline", local=self.baseline, relative_m=self.relative_baseline,
                              range_m=self.range_baseline, mode=self.t.mode())
            f = self.c["firmware"]
            if f["takeoff_altitude_reference"] not in ("relative_home", "above_launch"):
                raise Rejected("Takeoff reference frame has not been reviewed")
            policy = policy_checks(self.t, self.c)
            if policy and not all_pass(policy):
                raise Rejected('Takeoff/recovery policy not ready: '+', '.join(
                    check.name for check in policy if check.state != 'PASS'))
            value, kind = self.t.read_param(f["climb_parameter"])
            if kind != f["climb_parameter_type"]:
                raise Rejected("Climb parameter type mismatch")
            if not f["climb_parameter_min"] <= value <= f["climb_parameter_max"]:
                raise Rejected("Original climb value cannot be safely restored within reviewed range")
            self.original_parameter = (f["climb_parameter"], value, kind)
            self.report.save("restore-parameter.json", {"name": f["climb_parameter"], "value": value, "type": kind})
            self.parameter_dirty = True # Also restore if write succeeds but readback fails.
            self.t.set_climb_parameter(f["climb_parameter"],
                self.c["mission"]["climb_m_s"]*f["climb_parameter_units_per_m_s"], kind)
            self.prearm_monitor()
            self.machine.transition("SET_GUIDED")
            initial = self.t.mode()
            def mode_guard():
                if self.t.mode() not in (initial, "GUIDED"):
                    raise Takeover("Mode changed during GUIDED request")
            self.t.command(176, [1, 4], flight=True, guard=mode_guard)
            self.await_state(lambda: self.t.mode() == "GUIDED", self.c["mission"]["command_timeout_s"], mode_guard)
            self.owned = True
            self.tick()
            self.prearm_monitor()
            self.machine.transition("ARM")
            self.t.command(400, [1, 0], flight=True, guard=self.guard)
            self.await_state(self.t.armed, self.c["mission"]["command_timeout_s"], self.guard)
            self.machine.transition("TAKEOFF_SLOWLY")
            takeoff_height = self.c["mission"]["altitude_m"]
            if f["takeoff_altitude_reference"] == "relative_home":
                takeoff_height += self.relative_baseline
            self.t.command(22, [0, 0, 0, 0, 0, 0, takeoff_height],
                           flight=True, guard=self.guard)
            deadline = self.clock()+self.c["mission"]["takeoff_timeout_s"]
            settled_since = None
            hold_since = None
            last_print = -math.inf
            while True:
                self.tick()
                height, vertical_speed = self.monitor_limits()
                now = self.clock()
                within = (abs(height-self.c["mission"]["altitude_m"]) <=
                    self.c["mission"]["altitude_tolerance_m"] and
                    vertical_speed <= self.c["mission"]["settle_vertical_speed_m_s"])
                within = within and self.position_settled()
                if self.machine.state == "TAKEOFF_SLOWLY" and within:
                    self.machine.transition("SETTLE_AT_2M")
                if self.machine.state == "SETTLE_AT_2M":
                    settled_since = (settled_since if settled_since is not None else now) if within else None
                    if settled_since is not None and now-settled_since >= self.c["mission"]["settle_s"]:
                        self.machine.transition(hold_state)
                        hold_since = now
                if self.machine.state == hold_state:
                    if not within:
                        raise RuntimeError("Left valid hold band")
                    if now-hold_since >= self.c["mission"]["hold_s"]:
                        break
                elif now > deadline:
                    raise RuntimeError("Takeoff/settle timeout")
                if self.machine.state in ("SETTLE_AT_2M", hold_state) and now-self.last_target >= 1/self.c["mission"]["setpoint_hz"]:
                    self.t.position_target(*target_ned(self.baseline, self.c["mission"]["altitude_m"]), guard=self.guard)
                    self.last_target = now
                if now-last_print >= 1:
                    print(f"{self.machine.state}: height={height:.2f} m, vertical speed={vertical_speed:.2f} m/s", flush=True)
                    last_print = now
            self.after_hold()
            self.land()
            self.monitor_disarm()
            self.restore()
            self.machine.transition("FAILED" if self.failed else "COMPLETE")
        except (Exception, KeyboardInterrupt) as exc:
            self.recover(exc)
        finally:
            self.t.flight_authorized = False
            for s, handler in old_handlers.items():
                signal.signal(s, handler)
            self.report.event("result", state=self.machine.state,
                              restoration_pending=self.parameter_dirty)
        return self.machine.state == "COMPLETE" and not self.parameter_dirty

    def after_hold(self):
        """Optional supervised route; the original hover entry point stays unchanged."""

    def poll_controls(self):
        """Optional controls while the FC lands and auto-disarms."""

    def position_settled(self):
        return True
