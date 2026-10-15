#!/usr/bin/env python3
"""
ArduCopter GUIDED flight.

Sequence:
  Preflight
  GUIDED
  Arm
  Takeoff to 2m with COMMAND_ACK verification
  Forward 2m while holding initial yaw
  RTL
  Monitor landing

No mission upload.
No fence upload.
No home-distance preflight requirement.
Retains flight-controller arming and navigation checks.
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

HEIGHT = 2.0
DISTANCE = 2.0
RADIUS = 3.0

TAKEOFF_TIMEOUT = 35
ACK_TIMEOUT = 5
MOVEMENT_TIMEOUT = 50
RTL_TIMEOUT = 180


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

    if altitude > HEIGHT + 0.6:
        raise flight.FlightError(
            "Rangefinder altitude limit exceeded"
        )


def send_target_hold_yaw(
    link,
    north,
    east,
    down,
    yaw
):
    """
    Position target with yaw target.

    Does not command a yaw-rate rotation.
    """
    link.master.mav.set_position_target_local_ned_send(
        0,
        link.target_system,
        link.target_component,
        mav.MAV_FRAME_LOCAL_NED,
        0x09F8,
        north,
        east,
        down,
        0, 0, 0,
        0, 0, 0,
        yaw,
        0
    )


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


def verified_takeoff(link, args, start):

    print(
        "Sending GUIDED TAKEOFF command: 2m",
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
        HEIGHT
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

    while time.monotonic() < deadline:

        link.pump(0.1)

        guard(link, args, start)

        local = get_local(link)

        altitude = rangefinder_height(link, start)

        drift = math.hypot(
            local.x - start.x,
            local.y - start.y
        )

        if drift > 0.5:
            raise flight.FlightError(
                f"Excessive takeoff drift: {drift:.2f}m > 0.50m; "
                f"height={altitude:.2f}m"
            )

        if time.monotonic() >= next_report:
            print(f"Takeoff: range_height={altitude:.2f}m drift={drift:.2f}m "
                  f"horizontal_speed={math.hypot(local.vx, local.vy):.2f}m/s",
                  flush=True)
            next_report = time.monotonic() + 1

        if abs(altitude - HEIGHT) <= 0.2:

            if settled is None:
                settled = time.monotonic()

            if time.monotonic() - settled >= 2:
                print(
                    "Takeoff complete: rangefinder height reached and held for 2s",
                    flush=True
                )
                return float(local.z)

        else:
            settled = None

    raise flight.FlightError(
        "Takeoff rangefinder height timeout"
    )


def move_forward(link, args, start, yaw, hold_down=None):

    north = (
        start.x + DISTANCE * math.cos(yaw)
    )

    east = (
        start.y + DISTANCE * math.sin(yaw)
    )

    print(
        "Moving forward 2m; holding initial heading",
        flush=True
    )

    deadline = time.monotonic() + MOVEMENT_TIMEOUT
    settled = None

    while time.monotonic() < deadline:

        link.pump(0.1)

        guard(link, args, start)

        send_target_hold_yaw(
            link,
            north,
            east,
            start.z - HEIGHT if hold_down is None else hold_down,
            yaw
        )

        position = get_local(link)

        altitude = rangefinder_height(link, start)

        if abs(altitude - HEIGHT) > 0.6:
            raise flight.FlightError(
                "Altitude deviated during movement"
            )

        error = math.hypot(
            position.x - north,
            position.y - east
        )

        speed = math.hypot(
            position.vx,
            position.vy
        )

        if error <= 0.2 and speed <= 0.2:

            if settled is None:
                settled = time.monotonic()

            if time.monotonic() - settled >= 2:
                print(
                    "Forward point reached",
                    flush=True
                )
                return

        else:
            settled = None

    raise flight.FlightError(
        "Forward movement timeout"
    )


def do_rtl(link):

    print(
        "Forward movement complete. Requesting RTL.",
        flush=True
    )

    link.set_mode("RTL")

    print(
        "RTL mode requested",
        flush=True
    )

    deadline = time.monotonic() + RTL_TIMEOUT

    while time.monotonic() < deadline:

        link.pump(0.2)

        if not link.is_armed():
            print(
                "RTL complete: landed and disarmed",
                flush=True
            )
            return

    raise flight.FlightError(
        "RTL landing not confirmed within timeout"
    )


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

    options = parser.parse_args()

    print(
        "Plan: preflight -> GUIDED -> arm -> "
        "takeoff 2m -> forward 2m -> RTL"
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
        climb_rate=0.3,
        forward_speed=0.3
    )

    link = Link(options.connection)

    saved = []
    owned = False
    recorder = None

    try:

        link.connect_vehicle(15)

        flight.configure_streams(link)

        flight.collect_preflight(link, args)

        # RTL requires a valid home position.
        # No distance-to-home requirement is imposed.
        home = link.cached("HOME_POSITION", 3)

        if home is None:
            raise flight.FlightError(
                "RTL requires a valid recorded home"
            )

        flight.run_prearm_checks(link)

        (
            climb,
            climb_value,
            horizontal,
            horizontal_value
        ) = flight.speed_parameter_values(
            link, args
        )

        for parameter, value in (
            (climb, climb_value),
            (horizontal, horizontal_value)
        ):
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

        hold_down = verified_takeoff(
            link,
            args,
            start
        )

        move_forward(
            link,
            args,
            start,
            yaw,
            hold_down
        )

        # RTL is now responsible for navigation.
        link.set_mode("RTL")
        owned = False

        print(
            "RTL active/requested; monitoring disarm",
            flush=True
        )

        deadline = time.monotonic() + RTL_TIMEOUT

        while time.monotonic() < deadline:

            link.pump(0.2)

            if not link.is_armed():
                print(
                    "Mission complete: landed and disarmed",
                    flush=True
                )
                return 0 if recorder is None or recorder.check() else 1

        raise flight.FlightError(
            "RTL timeout: landing not confirmed"
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
