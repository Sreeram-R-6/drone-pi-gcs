#!/usr/bin/env python3
"""
Rangefinder-feedback GUIDED flight under a 2m measured-clearance ceiling.
Capture launch XY/heading, bootstrap lift-off, climb to 1.5m with live range
feedback, settle, fly to the destination, return to launch XY and LAND.
Default destination is 2m forward; optional GPS coordinates must be within 2m.
No flight mission or fence upload. FC arming/navigation checks remain active.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .recorder import VideoRecorder, add_video_arguments

os.environ["MAVLINK20"] = "1"

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[2]
        / "drone_project"
        / "picode"
    )
)

import testforward as flight
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "drone_project/pi_flight"))
from flightcheck.telemetry import process_lock

mav = flight.mavutil.mavlink

HEIGHT = 1.5
MAX_CLEARANCE = 2.0
ALTITUDE_ABORT = 1.85
BOOTSTRAP_HEIGHT = 0.8
VERTICAL_SPEED_LIMIT = 0.15
HORIZONTAL_SPEED_LIMIT = 0.2
DISTANCE = 2.0
RADIUS = 3.0

TAKEOFF_TIMEOUT = 35
ACK_TIMEOUT = 5
MOVEMENT_TIMEOUT = 50
LAND_TIMEOUT = 180
HOVER_TIMEOUT = 15
TAKEOFF_DRIFT_LIMIT = 0.5
HOVER_POSITION_TOLERANCE = 0.15
HOVER_SPEED_TOLERANCE = 0.15
HOLD_INTERVAL = 0.2
CORRECTION_CLEARANCE = 0.3
CORRECTION_CLEARANCE_SECONDS = 0.3


class Link(flight.VehicleLink):

    def __init__(self, connection):
        super().__init__(connection, 254)
        self.last_heartbeat = -math.inf
        self.last_takeoff_ack = None
        self.recorder = None

    def pump(self, timeout=0.2):

        if self.recorder is not None:
            self.recorder.check()

        if time.monotonic() - self.last_heartbeat >= 1:
            self.master.mav.heartbeat_send(
                mav.MAV_TYPE_GCS,
                mav.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mav.MAV_STATE_ACTIVE
            )
            self.last_heartbeat = time.monotonic()

        msg = self.master.recv_match(
            blocking=True,
            timeout=timeout
        )

        if msg is None or msg.get_type() == "BAD_DATA":
            return None

        if self.target_system and (
            msg.get_srcSystem() != self.target_system
            or msg.get_srcComponent() != self.target_component
        ):
            return None

        kind = msg.get_type()

        self.latest[kind] = (
            msg,
            time.monotonic()
        )

        if kind == "STATUSTEXT":
            text = str(msg.text).rstrip("\x00")
            self.status_text.append(
                (time.monotonic(), text)
            )
            print("AP:", text, flush=True)

        if kind == "COMMAND_ACK":
            if msg.command == mav.MAV_CMD_NAV_TAKEOFF:
                self.last_takeoff_ack = (
                    msg,
                    time.monotonic()
                )
                print(
                    "TAKEOFF ACK:",
                    mav.enums["MAV_RESULT"][msg.result].name,
                    flush=True
                )

        return msg


def get_local(link):

    local = link.cached("LOCAL_POSITION_NED", 1)

    if local is None:
        raise flight.FlightError(
            "Local position telemetry stale"
        )

    values = (
        local.x,
        local.y,
        local.z,
        local.vx,
        local.vy,
        local.vz
    )

    if not all(math.isfinite(v) for v in values):
        raise flight.FlightError(
            "Invalid local position"
        )

    return local


@dataclass(frozen=True)
class TakeoffReference:
    x: float
    y: float
    z: float
    ground_range_m: float


def downward_range(link):
    """Fresh downward slant range projected vertically, in metres."""
    sensor = link.cached("DISTANCE_SENSOR", 1)
    attitude = link.cached("ATTITUDE", 1)
    if sensor is None or int(sensor.orientation) != 25:
        raise flight.FlightError("Fresh downward rangefinder reading unavailable")
    raw = float(sensor.current_distance)
    minimum = max(1, float(getattr(sensor, "min_distance", 1)))
    maximum = min(800, float(getattr(sensor, "max_distance", 800)))
    if not all(math.isfinite(value) for value in (raw, minimum, maximum)) or not minimum <= raw <= maximum:
        raise flight.FlightError("Downward rangefinder reading invalid or out of range")
    if attitude is None or not all(math.isfinite(value) for value in (attitude.roll, attitude.pitch)):
        raise flight.FlightError("Fresh attitude required for rangefinder height")
    if math.hypot(attitude.roll, attitude.pitch) > math.radians(25):
        raise flight.FlightError("Excessive tilt for rangefinder height")
    return raw / 100 * math.cos(attitude.roll) * math.cos(attitude.pitch)


def rangefinder_height(link, start):
    return downward_range(link) - start.ground_range_m


def guard(link, args, start):

    flight.ensure_guided_health(link, args)

    if not link.is_armed():
        raise flight.FlightError(
            "Unexpected disarm"
        )

    local = get_local(link)

    distance = math.hypot(
        local.x - start.x,
        local.y - start.y
    )

    if distance >= RADIUS:
        raise flight.FlightError(
            "3m local-position limit reached"
        )

    altitude = rangefinder_height(link, start)

    clearance = altitude + start.ground_range_m
    if clearance >= ALTITUDE_ABORT:
        raise flight.FlightError(
            f"Rangefinder ceiling protection: clearance={clearance:.2f}m "
            f">= {ALTITUDE_ABORT:.2f}m; 2m ceiling"
        )


class RangeTracking:
    """Bounded velocity requests, using range height rather than a fixed EKF Z."""
    def __init__(self):
        self.previous = None
        self.vertical_speed = 0.0  # Positive upward.

    def command(self, link, start, north, east, yaw):
        local = get_local(link)
        height = rangefinder_height(link, start)
        now = time.monotonic()
        cached = getattr(link, "latest", {})
        sample_at = cached.get("DISTANCE_SENSOR", (None, now))[1] if isinstance(cached, dict) else now
        if self.previous is None:
            self.previous = (sample_at, height)
        elif sample_at > self.previous[0]:
            dt = sample_at - self.previous[0]
            if 0.02 <= dt <= 1:
                measured = (height - self.previous[1]) / dt
                if not math.isfinite(measured) or abs(measured) > 3:
                    raise flight.FlightError("Implausible rangefinder height change")
                self.vertical_speed = .7*self.vertical_speed + .3*measured
            self.previous = (sample_at, height)
        clearance = height + start.ground_range_m
        # Stop before the ceiling if measured climb momentum consumes the margin.
        upward = max(0, self.vertical_speed, -local.vz)
        predicted = clearance + .4*upward + upward*upward/(2*.3)
        if clearance >= ALTITUDE_ABORT or predicted >= 1.95:
            raise flight.FlightError(f"Predictive ceiling protection: clearance={clearance:.2f}m predicted={predicted:.2f}m")
        vx = .6*(north-local.x) - .4*local.vx
        vy = .6*(east-local.y) - .4*local.vy
        speed = math.hypot(vx, vy)
        if speed > HORIZONTAL_SPEED_LIMIT:
            vx *= HORIZONTAL_SPEED_LIMIT/speed
            vy *= HORIZONTAL_SPEED_LIMIT/speed
        up = max(-VERTICAL_SPEED_LIMIT, min(VERTICAL_SPEED_LIMIT,
                 .6*(HEIGHT-height)-.4*self.vertical_speed))
        # MAVLink velocity-only control must include all three axes. NED Z is down.
        link.master.mav.set_position_target_local_ned_send(
            int(now*1000)&0xffffffff, link.target_system, link.target_component,
            mav.MAV_FRAME_LOCAL_NED, 0x09C7, 0, 0, 0,
            vx, vy, -up, 0, 0, 0, yaw, 0)
        return height, self.vertical_speed, up


def destination(link, start, yaw, latitude=None, longitude=None):
    if latitude is None:
        return start.x + DISTANCE*math.cos(yaw), start.y + DISTANCE*math.sin(yaw)
    position = link.cached("GLOBAL_POSITION_INT", 1)
    gps = link.cached("GPS_RAW_INT", 2)
    if position is None or gps is None or gps.fix_type < 3:
        raise flight.FlightError("Fresh 3D GPS required to convert destination coordinates")
    lat = position.lat/1e7;lon = position.lon/1e7
    north = math.radians(latitude-lat)*6371000
    east = math.radians((longitude-lon+180)%360-180)*6371000*math.cos(math.radians(lat))
    if math.hypot(north,east) > DISTANCE:
        raise flight.FlightError("Destination must be within 2 m of the captured launch coordinates")
    return start.x+north,start.y+east


def takeoff_reference(link, timeout=3):
    """Capture fresh position/heading after arm, immediately before takeoff.

    A reference collected before preflight or camera startup can drift while
    still on the ground and falsely trigger the takeoff movement guard.
    """
    armed_at = time.monotonic()
    deadline = armed_at + timeout
    for kind in ("LOCAL_POSITION_NED", "ATTITUDE", "DISTANCE_SENSOR"):
        link.wait_for(kind, max(0.01, deadline - time.monotonic()), newer_than=armed_at,
                      predicate=(lambda message: int(message.orientation) == 25)
                      if kind == "DISTANCE_SENSOR" else None)
    flight.ensure_guided(link)
    if not link.is_armed():
        raise flight.FlightError("Disarmed before capturing takeoff reference")
    local = get_local(link)
    start = TakeoffReference(float(local.x), float(local.y), float(local.z), downward_range(link))
    attitude = link.cached("ATTITUDE", 1)
    if attitude is None or not math.isfinite(attitude.yaw):
        raise flight.FlightError("Fresh initial heading unavailable")
    yaw = float(attitude.yaw)
    print(f"Takeoff reference: N={start.x:.2f} E={start.y:.2f} "
          f"D={start.z:.2f} ground_range={start.ground_range_m:.2f}m "
          f"yaw={math.degrees(yaw):.1f}deg", flush=True)
    return start, yaw


def verified_takeoff(link, args, start, yaw=None):

    print(
        "Sending GUIDED bootstrap TAKEOFF command: 0.8m; range-feedback target 1.5m",
        flush=True
    )

    if not link.is_armed():
        raise flight.FlightError(
            "Cannot take off: vehicle disarmed"
        )

    if link.cached("HEARTBEAT", 2) is None:
        raise flight.FlightError(
            "Cannot take off: heartbeat stale"
        )

    if yaw is None:
        attitude = link.cached("ATTITUDE", 1)
        yaw = float(attitude.yaw) if attitude is not None else math.nan
    if not math.isfinite(yaw):
        raise flight.FlightError("Fresh initial heading required for takeoff correction")

    # Prevent reading an older takeoff acknowledgment.
    link.last_takeoff_ack = None

    sent_at = time.monotonic()

    link.master.mav.command_long_send(
        link.target_system,
        link.target_component,
        mav.MAV_CMD_NAV_TAKEOFF,
        0,
        0,
        0,
        0,
        math.nan,
        0,
        0,
        BOOTSTRAP_HEIGHT
    )

    print(
        "Waiting for TAKEOFF COMMAND_ACK...",
        flush=True
    )

    acknowledged = False
    ack_deadline = time.monotonic() + ACK_TIMEOUT

    while time.monotonic() < ack_deadline:

        link.pump(0.1)

        if not link.is_armed():
            raise flight.FlightError(
                "Disarmed while awaiting takeoff ACK"
            )

        if link.last_takeoff_ack is None:
            continue

        ack, received_at = link.last_takeoff_ack

        if received_at < sent_at:
            continue

        if ack.result in (
            mav.MAV_RESULT_ACCEPTED,
            mav.MAV_RESULT_IN_PROGRESS
        ):
            acknowledged = True
            break

        raise flight.FlightError(
            "Takeoff rejected: " +
            mav.enums["MAV_RESULT"][ack.result].name
        )

    if not acknowledged:
        raise flight.FlightError(
            "No TAKEOFF ACK received; "
            "landing as a precaution"
        )

    print(
        "Takeoff command accepted. Monitoring rangefinder height.",
        flush=True
    )

    deadline = time.monotonic() + TAKEOFF_TIMEOUT
    settled = None
    next_report = 0
    clearance_since = None
    correcting = False
    tracker = RangeTracking()
    next_correction = 0

    while time.monotonic() < deadline:

        link.pump(0.1)

        guard(link, args, start)

        local = get_local(link)

        altitude = rangefinder_height(link, start)

        drift = math.hypot(
            local.x - start.x,
            local.y - start.y
        )

        if drift > TAKEOFF_DRIFT_LIMIT:
            raise flight.FlightError(
                f"Excessive takeoff drift: {drift:.2f}m > 0.50m; "
                f"height={altitude:.2f}m"
            )

        now = time.monotonic()
        if not correcting:
            if altitude >= CORRECTION_CLEARANCE:
                if clearance_since is None:
                    clearance_since = now
                if now - clearance_since >= CORRECTION_CLEARANCE_SECONDS:
                    correcting = True
                    print("Ground clearance confirmed: launch XY and continuous range-height control", flush=True)
            else:
                clearance_since = None
        if correcting and now >= next_correction:
            tracker.command(link, start, start.x, start.y, yaw)
            next_correction = now + HOLD_INTERVAL

        if time.monotonic() >= next_report:
            print(f"Takeoff: range_height={altitude:.2f}m drift={drift:.2f}m "
                  f"horizontal_speed={math.hypot(local.vx, local.vy):.2f}m/s "
                  f"local_z={local.z:.2f}m local_vz={local.vz:.2f}m/s "
                  f"range_climb={tracker.vertical_speed:.2f}m/s target_height={HEIGHT:.2f}m",
                  flush=True)
            next_report = time.monotonic() + 1

        if (correcting and abs(altitude - HEIGHT) <= 0.1
                and drift <= HOVER_POSITION_TOLERANCE
                and math.hypot(local.vx, local.vy) <= HOVER_SPEED_TOLERANCE
                and abs(local.vz) <= HOVER_SPEED_TOLERANCE):

            if settled is None:
                settled = time.monotonic()

            if time.monotonic() - settled >= 2:
                print(
                    "Takeoff complete: 1.5m range height and launch XY stable for 2s",
                    flush=True
                )
                return float(local.z)

        else:
            settled = None

    raise flight.FlightError(
        "Takeoff rangefinder height timeout"
    )


def travel_to(link, args, start, yaw, north, east, label, timeout, launch_limit=False):
    print(f"{label}: target N={north:.3f} E={east:.3f}; range height={HEIGHT:.2f}m", flush=True)
    deadline = time.monotonic()+timeout
    settled = None
    next_send = 0
    next_report = 0
    tracker = RangeTracking()
    while time.monotonic() < deadline:
        link.pump(.1)
        guard(link,args,start)
        local = get_local(link)
        height = rangefinder_height(link,start)
        drift = math.hypot(local.x-start.x,local.y-start.y)
        if launch_limit and drift > TAKEOFF_DRIFT_LIMIT:
            raise flight.FlightError(f"Excessive launch hover drift: {drift:.2f}m > 0.50m")
        if height < HEIGHT-.4:
            raise flight.FlightError("Altitude deviated during coordinate travel")
        now = time.monotonic()
        if now >= next_send:
            tracker.command(link,start,north,east,yaw);next_send=now+HOLD_INTERVAL
        error = math.hypot(local.x-north,local.y-east)
        speed = math.hypot(local.vx,local.vy)
        if now >= next_report:
            print(f"{label}: range_height={height:.2f}m target_error={error:.2f}m "
                  f"horizontal_speed={speed:.2f}m/s local_z={local.z:.2f}m "
                  f"local_vz={local.vz:.2f}m/s range_climb={tracker.vertical_speed:.2f}m/s", flush=True)
            next_report=now+1
        if abs(height-HEIGHT)<=.1 and error<=HOVER_POSITION_TOLERANCE and speed<=HOVER_SPEED_TOLERANCE and abs(local.vz)<=HOVER_SPEED_TOLERANCE:
            if settled is None:settled=now
            if now-settled>=2:
                print(label+" stable for 2s",flush=True);return
        else:settled=None
    raise flight.FlightError(label+" did not stabilize; coordinate travel cancelled")


def stabilize_launch(link, args, start, yaw, hold_down=None):
    # hold_down is accepted for compatibility; range feedback controls height.
    travel_to(link,args,start,yaw,start.x,start.y,"Launch hover",HOVER_TIMEOUT,True)


def move_forward(link, args, start, yaw, hold_down=None, target=None):
    # hold_down is accepted for compatibility; range feedback controls height.
    north,east=target if target is not None else destination(link,start,yaw)
    travel_to(link,args,start,yaw,north,east,"Destination",MOVEMENT_TIMEOUT)


def return_launch(link, args, start, yaw):
    travel_to(link,args,start,yaw,start.x,start.y,"Return to launch",MOVEMENT_TIMEOUT)


def _main():

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--connection",
        default="tcp:127.0.0.1:5760"
    )

    parser.add_argument(
        "--execute",
        action="store_true"
    )
    add_video_arguments(parser)
    parser.add_argument("--no-video", action="store_true", help="explicitly disable camera recording")

    parser.add_argument("--target-latitude", type=float)
    parser.add_argument("--target-longitude", type=float)
    options = parser.parse_args()
    if (options.target_latitude is None) != (options.target_longitude is None):
        parser.error("Provide both --target-latitude and --target-longitude")
    if options.target_latitude is not None and not (
        math.isfinite(options.target_latitude) and abs(options.target_latitude)<=85
        and math.isfinite(options.target_longitude) and abs(options.target_longitude)<=180):
        parser.error("Invalid destination coordinates")

    print(
        "Plan: preflight -> GUIDED -> arm -> "
        "range-controlled takeoff 1.5m (2m ceiling) -> settle -> destination -> launch coordinates -> LAND"
    )

    if not options.execute:
        if not options.no_video:
            print(f"Video: {options.camera} -> {options.video_dir.resolve()} (starts only with --execute)")
        print(
            "Plan only. Use --execute for flight."
        )
        return 0

    args = argparse.Namespace(
        sample_seconds=3,
        min_satellites=10,
        max_eph_cm=150,
        max_gps_p95=0.6,
        # Keep fresh/finite flow telemetry, but do not gate on opt_qua.
        min_flow_quality=None,
        max_preflight_tilt=8,
        min_battery_voltage=14.4,
        min_battery_remaining=30,
        climb_rate=VERTICAL_SPEED_LIMIT,
        forward_speed=HORIZONTAL_SPEED_LIMIT
    )

    link = Link(options.connection)

    saved = []
    owned = False
    recorder = None

    try:

        link.connect_vehicle(15)

        flight.configure_streams(link)

        flight.collect_preflight(link, args)

        # Keep a valid home for FC navigation/failsafe recovery.
        # No distance-to-home requirement is imposed.
        home = link.cached("HOME_POSITION", 3)

        if home is None:
            raise flight.FlightError(
                "Valid recorded home required for FC navigation/failsafes"
            )

        flight.run_prearm_checks(link)
        if options.target_latitude is not None:
            # Reject distant/invalid GPS destinations before arming or changing settings.
            destination(link,get_local(link),0,options.target_latitude,options.target_longitude)

        (
            climb,
            climb_value,
            horizontal,
            horizontal_value
        ) = flight.speed_parameter_values(
            link, args
        )

        # Keep native takeoff XY navigation active after lift-off. A delayed
        # navigation threshold above 2 m would permit drift throughout ascent.
        # Early velocity targets switch out of native TAKEOFF. Its deprecated
        # takeoff-only auto-enable modes would otherwise be silently skipped.
        auto_fence = link.get_parameter(("FENCE_AUTOENABLE",))
        if auto_fence.value not in (0, 3):
            raise flight.FlightError("FENCE_AUTOENABLE must be 0 or 3 for corrected takeoff; review fence activation before flight")
        navigation = link.get_parameter(("WP_NAVALT_MIN",))
        if not math.isfinite(navigation.value) or navigation.value < 0:
            raise flight.FlightError("Invalid WP_NAVALT_MIN")
        temporary = [(climb, climb_value), (horizontal, horizontal_value)]
        if navigation.value != 0:
            temporary.append((navigation, 0.0))
        for parameter, value in temporary:
            saved.append(parameter)
            link.set_parameter(parameter, value)

        args.sample_seconds = 3

        flight.collect_preflight(link, args)

        if not options.no_video:
            recorder = VideoRecorder.from_options(options)
            recorder.start()
            link.recorder = recorder

        link.set_mode("GUIDED")

        owned = True

        flight.arm(link)

        start, yaw = takeoff_reference(link)
        target = destination(link,start,yaw,options.target_latitude,options.target_longitude)
        print(f"Launch coordinates: N={start.x:.3f} E={start.y:.3f}; destination N={target[0]:.3f} E={target[1]:.3f}",flush=True)
        gps_position = link.cached("GLOBAL_POSITION_INT", 1)
        if gps_position is not None and hasattr(gps_position,"lat"):
            print(f"Launch GPS: {gps_position.lat/1e7:.7f}, {gps_position.lon/1e7:.7f}",flush=True)

        hold_down = verified_takeoff(
            link,
            args,
            start,
            yaw
        )

        stabilize_launch(link, args, start, yaw, hold_down)

        move_forward(
            link,
            args,
            start,
            yaw,
            hold_down,
            target
        )

        return_launch(link,args,start,yaw)
        # LAND avoids RTL's configured climb; only the FC handles final descent.
        link.set_mode("LAND")
        owned = False

        print(
            "LAND requested at launch coordinates; monitoring disarm",
            flush=True
        )

        deadline = time.monotonic() + LAND_TIMEOUT

        while time.monotonic() < deadline:

            link.pump(0.2)

            if not link.is_armed():
                print(
                    "Mission complete: landed and disarmed",
                    flush=True
                )
                return 0 if recorder is None or recorder.check() else 1

        raise flight.FlightError(
            "LAND timeout: landing not confirmed"
        )

    except flight.PilotOverride as error:

        owned = False

        print(
            f"Pilot/FC override: {error}",
            file=sys.stderr
        )

        return 2

    except (Exception, KeyboardInterrupt) as error:

        print(
            f"Mission stopped: {error}",
            file=sys.stderr
        )

        if owned:
            flight.land_if_owned(link)

        return 1

    finally:
        try:
            heartbeat = link.cached("HEARTBEAT", 2)
            if saved and heartbeat is not None and not link.is_armed():
                remaining = flight.restore_parameters(link, saved)
                if remaining:
                    print("Restore manually: " + ", ".join(
                        f"{p.name}={p.value}" for p in remaining), file=sys.stderr)
            elif saved:
                print("Restoration deferred; after landing restore: " + ", ".join(
                    f"{p.name}={p.value}" for p in saved), file=sys.stderr)
        finally:
            try:
                link.master.close()
            finally:
                if recorder is not None:
                    recorder.stop()


def main():
    if "--execute" in sys.argv:
        try:
            with process_lock("mission.lock"):
                return _main()
        except RuntimeError as error:
            print(f"Controller unavailable: {error}", file=sys.stderr)
            return 1
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
