"""Small heading-relative commissioning routes; no FC parameter bypasses."""
from dataclasses import dataclass
import math
from .mission import Mission, Takeover
from .telemetry import LinkLost, Rejected


@dataclass(frozen=True)
class Waypoint:
    action: str
    forward: float
    right: float
    hold_s: float = 5.0


ROUTES = {
    "hover": (),
    "forward": (Waypoint("Forward 1 m", 1, 0), Waypoint("Back to launch", 0, 0)),
    "box": (Waypoint("Forward 1 m", 1, 0), Waypoint("Right 1 m", 1, 1),
            Waypoint("Backward 1 m", 0, 1), Waypoint("Left to launch", 0, 0)),
}


def coordinates(baseline, yaw, waypoint, altitude):
    f, r = waypoint.forward, waypoint.right
    return (baseline["x"] + f*math.cos(yaw) - r*math.sin(yaw),
            baseline["y"] + f*math.sin(yaw) + r*math.cos(yaw),
            baseline["z"] - altitude)


def validate_route(config, name):
    g = config.get("gcs", {})
    if name not in ROUTES:
        raise ValueError("Unknown mission")
    if not g.get("routes_reviewed", False):
        raise Rejected("GCS 5-second hold/routes require commissioning review")
    for key in ("speed_m_s", "position_tolerance_m", "arrival_speed_m_s", "leg_timeout_s"):
        v = g.get(key, 0)
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise Rejected(f"Unreviewed GCS limit: {key}")
    if g["speed_m_s"] >= config["mission"]["max_horizontal_speed_m_s"]:
        raise Rejected("Route speed must be below abort speed")
    if g['arrival_speed_m_s'] >= config['mission']['max_horizontal_speed_m_s']:
        raise Rejected('Arrival speed must be below abort speed')
    radius = max((math.hypot(w.forward, w.right) for w in ROUTES[name]), default=0)
    if radius + g["position_tolerance_m"] >= config["mission"]["max_radius_m"]:
        raise Rejected("Route plus tolerance exceeds reviewed flight radius")
    previous = (0.,0.)
    for waypoint in ROUTES[name]:
        if not all(math.isfinite(v) for v in (waypoint.forward,waypoint.right,waypoint.hold_s)) or waypoint.hold_s <= 0:
            raise Rejected('Invalid waypoint or hold duration')
        target = (waypoint.forward,waypoint.right)
        minimum_time = math.dist(previous,target)/g['speed_m_s']+config['mission']['settle_s']+waypoint.hold_s
        if g['leg_timeout_s'] <= minimum_time:
            raise Rejected('Leg timeout must exceed travel, settle and hold duration')
        previous = target


class RouteMission(Mission):
    def __init__(self, *args, route="hover", ui=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.route = route
        self.ui = ui
        self.yaw = None

    def tick(self):
        super().tick()
        self.poll_controls()

    def monitor_limits(self):
        result = super().monitor_limits()
        if self.machine.state in ("HOLD_10_SECONDS", "HOLD_5_SECONDS"):
            p = self.t.store.latest("LOCAL_POSITION_NED", self.clock(), self.c["link"]["max_age_s"])
            if math.hypot(p["x"]-self.baseline["x"], p["y"]-self.baseline["y"]) > self.c["gcs"]["position_tolerance_m"]:
                raise Rejected("Left initial position hold band")
            if math.hypot(p["vx"], p["vy"]) > self.c["gcs"]["arrival_speed_m_s"]:
                raise Rejected("Initial hold horizontal speed exceeded")
        return result

    def position_settled(self):
        p = self.t.store.latest("LOCAL_POSITION_NED", self.clock(), self.c["link"]["max_age_s"])
        return (math.hypot(p["x"]-self.baseline["x"], p["y"]-self.baseline["y"]) <= self.c["gcs"]["position_tolerance_m"] and
                math.hypot(p["vx"], p["vy"]) <= self.c["gcs"]["arrival_speed_m_s"])

    def poll_controls(self):
        if self.ui:
            self.ui.mission = self
            request, self.ui.request = self.ui.request, None
            if request == "RTL":
                if not self.owned or self.t.mode() != "GUIDED":
                    self.ui.notice = "RTL refused: pilot/FC already owns recovery"
                    return
                self.request_rtl()
            elif request == "DISARM":
                # Normal disarm only with explicit, fresh FC landed evidence.
                try:
                    landed = self.t.store.latest("EXTENDED_SYS_STATE", self.clock(), self.c["link"]["max_age_s"])
                except LinkLost:
                    self.ui.notice = "DISARM refused: fresh landed evidence unavailable"
                    return
                if landed.get("landed_state") != 1:
                    self.ui.notice = "DISARM refused: aircraft is not confirmed on ground"
                else:
                    if self.machine.state == "RELEASE_CONTROL_AND_MONITOR":
                        self.ui.notice = "DISARM refused: pilot/FC owns aircraft"
                        return
                    ground_mode = self.t.mode()
                    def ground_guard():
                        self.t.require_live()
                        if self.t.mode() != ground_mode:
                            self.owned = False
                            raise Takeover("Pilot/FC mode changed before ground disarm")
                        landed_now = self.t.store.latest("EXTENDED_SYS_STATE", self.clock(), self.c["link"]["max_age_s"])
                        if landed_now.get("landed_state") != 1:
                            raise Rejected("Landed evidence changed before disarm")
                    self.t.command(400, [0, 0], flight=True, guard=ground_guard)
                    self.ui.notice = "Normal ground disarm accepted; waiting for disarmed heartbeat"
                    if self.machine.state != "WAIT_DISARM":
                        raise RuntimeError("Operator ground disarm")

    def request_rtl(self):
        if not self.c.get("gcs", {}).get("rtl_reviewed", False):
            self.ui.notice = "RTL blocked: firmware/home/altitude behavior not reviewed"
            return
        now = self.clock()
        gps = self.t.store.latest("GPS_RAW_INT", now, self.c["link"]["max_age_s"])
        ekf = self.t.store.latest("EKF_STATUS_REPORT", now, self.c["link"]["max_age_s"])
        home = self.t.store.samples["HOME_POSITION"]
        if gps.get("fix_type", 0) < 3 or not home or not ekf.get("flags", 0) & 16:
            self.ui.notice = "RTL refused: GPS, absolute EKF position or home unavailable"
            return
        def rtl_guard():
            if self.t.mode() not in ("GUIDED", "RTL", "LAND"):
                self.owned = False
                raise Takeover("Pilot changed mode during RTL")
        self.t.command(176, [1, 6], flight=True, guard=rtl_guard)
        self.await_state(lambda: self.t.mode() in ("RTL", "LAND"),
                         self.c["mission"]["command_timeout_s"], rtl_guard)
        self.owned = False
        self.ui.notice = "RTL observed; FC owns return/landing. Monitoring until disarm."
        raise Takeover("Operator RTL")

    def after_hold(self):
        if not ROUTES[self.route]:
            return
        self.machine.transition("ROUTE")
        age = self.c["link"]["max_age_s"]
        # Launch yaw is captured before arm by the GCS, never redefined per leg.
        yaw = self.yaw
        if yaw is None or not math.isfinite(yaw):
            raise Rejected("Launch heading unavailable")
        g = self.c["gcs"]
        previous = (self.baseline["x"], self.baseline["y"], self.baseline["z"]-2)
        for index, waypoint in enumerate(ROUTES[self.route], 1):
            target = coordinates(self.baseline, yaw, waypoint, 2)
            started = self.clock()
            settled = held = None
            length = math.dist(previous, target)
            while True:
                self.tick()
                now = self.clock()
                if now-started > g["leg_timeout_s"]:
                    raise Rejected(f"Waypoint {index} arrival/hold timeout")
                fraction = min(1., (now-started)*g["speed_m_s"]/length) if length else 1.
                commanded = tuple(a + fraction*(b-a) for a,b in zip(previous, target))
                if now-self.last_target >= 1/self.c["mission"]["setpoint_hz"]:
                    self.t.position_target(*commanded, guard=self.guard)
                    self.last_target = now
                p = self.t.store.latest("LOCAL_POSITION_NED", now, age)
                error = math.hypot(p["x"]-target[0], p["y"]-target[1])
                valid = (fraction == 1 and error <= g["position_tolerance_m"] and
                         math.hypot(p["vx"], p["vy"]) <= g["arrival_speed_m_s"] and
                         abs(p["z"]-target[2]) <= self.c["mission"]["altitude_tolerance_m"] and
                         abs(p["vz"]) <= self.c["mission"]["settle_vertical_speed_m_s"])
                settled = (now if settled is None else settled) if valid else None
                if held is not None and not valid:
                    raise Rejected("Left waypoint hold band")
                if settled is not None and now-settled >= self.c["mission"]["settle_s"] and held is None:
                    held = now
                if self.ui:
                    self.ui.action = waypoint.action + (" / hold" if held is not None else " / move + settle")
                    self.ui.waypoint = f"{index}/{len(ROUTES[self.route])} error {error:.2f} m"
                    self.ui.progress = ((index-1) + (min(1., (now-held)/waypoint.hold_s) if held else fraction*.5))/len(ROUTES[self.route])
                if held is not None and now-held >= waypoint.hold_s:
                    break
            self.report.event("waypoint_complete", index=index, action=waypoint.action, target=target)
            previous = target
        if self.ui:
            self.ui.progress = 1.
