#!/usr/bin/env python3
"""Supervised 2 m takeoff, forward-position test, and RTL for ArduCopter.

Architecture
------------
MAVProxy is the only serial owner and should forward the verified Pi UART link
to loopback UDP port 14550.  This program connects to that UDP endpoint; it
does not open ``/dev/ttyAMA2`` itself.

Default flight profile
----------------------
* require a disarmed ArduPilot quadrotor, GPS/home, EKF, MTF-01 flow/range,
  battery telemetry, and successful ArduPilot pre-arm checks;
* wait for a fresh, explicit Enter press;
* enter GUIDED, arm normally, and take off to 2.0 m;
* move 2.0 m forward relative to yaw recorded at takeoff;
* restore temporary speed limits, enter RTL, and monitor through disarm.

Any mission error while this program still owns GUIDED requests LAND.  A pilot
or FC mode change releases control immediately.  The program never force-arms
or force-disarms.  Test with props removed first, then only in the commissioned
outdoor area with the RC recovery switch ready.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import math
import statistics
import sys
import time
from typing import Callable, Optional

from pymavlink import mavutil


POSITION_ONLY_MASK = 0x0DF8
REQUIRED_EKF_FLAGS = (
    1  # attitude
    | 2  # horizontal velocity
    | 4  # vertical velocity
    | 8  # relative horizontal position
    | 32  # absolute vertical position
)
EKF_ABSOLUTE_HORIZONTAL_POSITION = 16
EKF_CONST_POS_MODE = 128


class FlightError(RuntimeError):
    """A mission or preflight condition failed."""


class PilotOverride(FlightError):
    """The pilot or FC selected a mode outside this mission's control."""


@dataclass
class SavedParameter:
    name: str
    value: float
    param_type: int


class VehicleLink:
    """Small synchronous MAVLink transport with freshness-aware caching."""

    def __init__(self, connection: str, source_system: int) -> None:
        self.master = mavutil.mavlink_connection(
            connection,
            source_system=source_system,
            autoreconnect=False,
        )
        self.latest: dict[str, tuple[object, float]] = {}
        self.status_text: deque[tuple[float, str]] = deque(maxlen=100)
        self.target_system = 0
        self.target_component = 0

    def pump(self, timeout: float = 0.2) -> Optional[object]:
        message = self.master.recv_match(blocking=True, timeout=timeout)
        if message is None:
            return None
        message_type = message.get_type()
        if message_type == "BAD_DATA":
            return message
        now = time.monotonic()
        self.latest[message_type] = (message, now)
        if message_type == "STATUSTEXT":
            text = str(getattr(message, "text", "")).rstrip("\x00")
            self.status_text.append((now, text))
            print(f"AP: {text}", flush=True)
        return message

    def wait_for(
        self,
        message_type: str,
        timeout: float,
        predicate: Optional[Callable[[object], bool]] = None,
        newer_than: Optional[float] = None,
    ) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            message = self.pump(min(0.5, remaining))
            if message is None or message.get_type() != message_type:
                continue
            stamp = self.latest[message_type][1]
            if newer_than is not None and stamp < newer_than:
                continue
            if predicate is None or predicate(message):
                return message
        raise FlightError(f"Timed out waiting for {message_type}")

    def cached(
        self, message_type: str, max_age: float = 2.0
    ) -> Optional[object]:
        item = self.latest.get(message_type)
        if item is None or time.monotonic() - item[1] > max_age:
            return None
        return item[0]

    def connect_vehicle(self, timeout: float) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.pump(min(0.5, deadline - time.monotonic()))
            if message is None or message.get_type() != "HEARTBEAT":
                continue
            if (
                message.autopilot
                != mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
            ):
                continue
            if message.type != mavutil.mavlink.MAV_TYPE_QUADROTOR:
                raise FlightError(
                    f"Expected a quadrotor, MAV_TYPE={message.type}"
                )
            self.target_system = message.get_srcSystem()
            self.target_component = message.get_srcComponent()
            self.master.target_system = self.target_system
            self.master.target_component = self.target_component
            print(
                f"Connected: system={self.target_system} "
                f"component={self.target_component}",
                flush=True,
            )
            return message
        raise FlightError(
            f"No ArduPilot quadrotor heartbeat within {timeout:.1f}s"
        )

    def request_message_interval(self, message_id: int, hz: float) -> None:
        interval_us = int(1_000_000 / hz)
        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            message_id,
            interval_us,
            0,
            0,
            0,
            0,
            0,
        )

    def command_long(
        self,
        command: int,
        params: tuple[float, float, float, float, float, float, float],
        timeout: float = 8.0,
    ) -> object:
        sent_at = time.monotonic()
        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            command,
            0,
            *params,
        )
        ack = self.wait_for(
            "COMMAND_ACK",
            timeout,
            predicate=lambda msg: int(msg.command) == int(command),
            newer_than=sent_at,
        )
        accepted = {
            mavutil.mavlink.MAV_RESULT_ACCEPTED,
            mavutil.mavlink.MAV_RESULT_IN_PROGRESS,
        }
        if ack.result not in accepted:
            raise FlightError(
                f"Command {command} rejected: MAV_RESULT={ack.result}"
            )
        return ack

    def mode_name(self, heartbeat: Optional[object] = None) -> str:
        heartbeat = heartbeat or self.cached("HEARTBEAT")
        if heartbeat is None:
            return "UNKNOWN"
        mapping = self.master.mode_mapping() or {}
        reverse = {value: key for key, value in mapping.items()}
        return reverse.get(
            int(heartbeat.custom_mode), str(heartbeat.custom_mode)
        )

    def set_mode(self, mode: str, timeout: float = 12.0) -> None:
        mapping = self.master.mode_mapping()
        if not mapping or mode not in mapping:
            raise FlightError(f"Mode {mode} is not available")
        custom_mode = mapping[mode]
        self.master.mav.set_mode_send(
            self.target_system,
            mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            custom_mode,
        )
        self.wait_for(
            "HEARTBEAT",
            timeout,
            predicate=lambda msg: int(msg.custom_mode) == int(custom_mode),
        )
        print(f"Mode: {mode}", flush=True)

    def is_armed(self) -> bool:
        heartbeat = self.cached("HEARTBEAT", max_age=3.0)
        if heartbeat is not None:
            return bool(
                heartbeat.base_mode
                & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
        return bool(self.master.motors_armed())

    def wait_armed(self, armed: bool, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.pump(0.5)
            if message is None or message.get_type() != "HEARTBEAT":
                continue
            state = bool(
                message.base_mode
                & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
            if state == armed:
                return
        state_name = "armed" if armed else "disarmed"
        raise FlightError(f"Vehicle did not become {state_name}")

    def get_parameter(
        self, names: tuple[str, ...], timeout_each: float = 3.0
    ) -> SavedParameter:
        for name in names:
            sent_at = time.monotonic()
            self.master.mav.param_request_read_send(
                self.target_system,
                self.target_component,
                name.encode("ascii"),
                -1,
            )
            try:
                message = self.wait_for(
                    "PARAM_VALUE",
                    timeout_each,
                    predicate=lambda msg, expected=name: (
                        _param_name(msg) == expected
                    ),
                    newer_than=sent_at,
                )
            except FlightError:
                continue
            return SavedParameter(
                name, float(message.param_value), message.param_type
            )
        raise FlightError(
            f"None of these parameters exists: {', '.join(names)}"
        )

    def set_parameter(self, parameter: SavedParameter, value: float) -> None:
        sent_at = time.monotonic()
        self.master.mav.param_set_send(
            self.target_system,
            self.target_component,
            parameter.name.encode("ascii"),
            float(value),
            parameter.param_type,
        )
        message = self.wait_for(
            "PARAM_VALUE",
            5.0,
            predicate=lambda msg: _param_name(msg) == parameter.name,
            newer_than=sent_at,
        )
        tolerance = max(0.001, abs(value) * 0.01)
        if not math.isclose(
            float(message.param_value), value, abs_tol=tolerance
        ):
            raise FlightError(
                f"{parameter.name} readback {message.param_value} != {value}"
            )


def _param_name(message: object) -> str:
    value = message.param_id
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore").rstrip("\x00")
    return str(value).rstrip("\x00")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    raw_index = math.ceil(fraction * len(ordered)) - 1
    index = max(0, min(len(ordered) - 1, raw_index))
    return ordered[index]


def _local_offsets(
    samples: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    latitude0 = statistics.median(point[0] for point in samples)
    longitude0 = statistics.median(point[1] for point in samples)
    radius = 6_378_137.0
    scale = math.cos(math.radians(latitude0))
    return [
        (
            math.radians(latitude - latitude0) * radius,
            math.radians(longitude - longitude0) * radius * scale,
        )
        for latitude, longitude in samples
    ]


def configure_streams(link: VehicleLink) -> None:
    message_rates = {
        mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS: 2,
        mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT: 5,
        mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE: 10,
        mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED: 10,
        mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT: 5,
        mavutil.mavlink.MAVLINK_MSG_ID_OPTICAL_FLOW_RAD: 10,
        mavutil.mavlink.MAVLINK_MSG_ID_DISTANCE_SENSOR: 10,
        mavutil.mavlink.MAVLINK_MSG_ID_EKF_STATUS_REPORT: 2,
        mavutil.mavlink.MAVLINK_MSG_ID_HOME_POSITION: 1,
    }
    for message_id, rate in message_rates.items():
        link.request_message_interval(message_id, rate)


def collect_preflight(link: VehicleLink, args: argparse.Namespace) -> None:
    print(
        f"Collecting {args.sample_seconds:.0f}s preflight sample...",
        flush=True,
    )
    deadline = time.monotonic() + args.sample_seconds
    gps_samples: list[tuple[float, float]] = []
    flow_quality: list[int] = []
    ranges: list[float] = []
    latest_gps = None
    while time.monotonic() < deadline:
        message = link.pump(0.25)
        if message is None:
            continue
        message_type = message.get_type()
        if message_type == "GPS_RAW_INT":
            latest_gps = message
            if message.fix_type >= 3 and message.lat and message.lon:
                gps_samples.append((message.lat / 1e7, message.lon / 1e7))
        elif message_type == "OPTICAL_FLOW_RAD":
            flow_quality.append(int(message.quality))
        elif (
            message_type == "DISTANCE_SENSOR"
            and int(message.orientation) == 25
        ):
            ranges.append(float(message.current_distance) / 100.0)

    failures: list[str] = []
    heartbeat = link.cached("HEARTBEAT", 2.5)
    if heartbeat is None:
        failures.append("heartbeat is stale")
    elif heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
        failures.append("vehicle is already armed")

    if latest_gps is None:
        failures.append("no GPS_RAW_INT")
    else:
        if latest_gps.fix_type < 3:
            failures.append(f"GPS fix_type={latest_gps.fix_type}, need >=3")
        if latest_gps.satellites_visible < args.min_satellites:
            failures.append(
                f"GPS satellites={latest_gps.satellites_visible}, "
                f"need >={args.min_satellites}"
            )
        if (
            latest_gps.eph in (0, 65535)
            or latest_gps.eph > args.max_eph_cm
        ):
            failures.append(
                f"GPS eph={latest_gps.eph}cm, need known and "
                f"<={args.max_eph_cm}cm"
            )

    if len(gps_samples) < max(10, int(args.sample_seconds * 2)):
        failures.append(f"only {len(gps_samples)} valid GPS samples")
    else:
        offsets = _local_offsets(gps_samples)
        radii = [math.hypot(north, east) for north, east in offsets]
        p95 = _percentile(radii, 0.95)
        maximum = max(radii)
        print(
            f"GPS stability: p95={p95:.2f}m max={maximum:.2f}m",
            flush=True,
        )
        if p95 > args.max_gps_p95:
            failures.append(
                f"GPS p95 radius={p95:.2f}m exceeds {args.max_gps_p95:.2f}m"
            )

    if not flow_quality:
        failures.append("no OPTICAL_FLOW_RAD from the FC")
    else:
        median_quality = statistics.median(flow_quality)
        print(f"Optical flow median quality={median_quality:.0f}", flush=True)
        if median_quality < args.min_flow_quality:
            failures.append(
                f"flow quality={median_quality:.0f}, "
                f"need >={args.min_flow_quality}"
            )

    if not ranges:
        failures.append("no downward DISTANCE_SENSOR data")
    else:
        median_range = statistics.median(ranges)
        print(f"Downward range median={median_range:.2f}m", flush=True)
        if not 0.01 <= median_range <= 8.0:
            failures.append(
                f"MTF-01 range={median_range:.2f}m is outside 0.01..8m"
            )

    local_position = link.cached("LOCAL_POSITION_NED", 2.0)
    global_position = link.cached("GLOBAL_POSITION_INT", 2.0)
    attitude = link.cached("ATTITUDE", 2.0)
    ekf = link.cached("EKF_STATUS_REPORT", 3.0)
    home = link.cached("HOME_POSITION", 10.0)
    system_status = link.cached("SYS_STATUS", 3.0)
    for label, message in (
        ("local position", local_position),
        ("global position", global_position),
        ("attitude", attitude),
        ("EKF status", ekf),
        ("home position", home),
        ("system status", system_status),
    ):
        if message is None:
            failures.append(f"no fresh {label}")

    if ekf is not None:
        flags = int(ekf.flags)
        if flags & REQUIRED_EKF_FLAGS != REQUIRED_EKF_FLAGS:
            failures.append(f"EKF flags 0x{flags:x} lack required state flags")
        if not flags & EKF_ABSOLUTE_HORIZONTAL_POSITION:
            failures.append("EKF has no absolute horizontal position for RTL")
        if flags & EKF_CONST_POS_MODE:
            failures.append("EKF is in constant-position mode")

    if home is not None and (
        int(home.latitude) == 0 or int(home.longitude) == 0
    ):
        failures.append("home position is invalid")

    if attitude is not None:
        tilt = math.degrees(math.hypot(attitude.roll, attitude.pitch))
        print(f"Stationary tilt={tilt:.1f}deg", flush=True)
        if tilt > args.max_preflight_tilt:
            failures.append(
                f"stationary tilt={tilt:.1f}deg exceeds "
                f"{args.max_preflight_tilt:.1f}deg"
            )

    if system_status is not None:
        voltage = float(system_status.voltage_battery) / 1000.0
        remaining = int(system_status.battery_remaining)
        print(
            f"Battery={voltage:.2f}V remaining={remaining}%", flush=True
        )
        if voltage <= 0 or voltage < args.min_battery_voltage:
            failures.append(
                f"battery={voltage:.2f}V, "
                f"need >={args.min_battery_voltage:.2f}V"
            )
        if remaining < 0 or remaining < args.min_battery_remaining:
            failures.append(
                f"battery remaining={remaining}%, "
                f"need >={args.min_battery_remaining}%"
            )

    bad_status = (
        "prearm",
        "failsafe",
        "unhealthy",
        "gps glitch",
        "ekf variance",
        "internal error",
        "crash",
    )
    recent_text = [text for _, text in link.status_text]
    status_failures = [
        text
        for text in recent_text
        if any(fragment in text.lower() for fragment in bad_status)
    ]
    if status_failures:
        failures.append(
            "ArduPilot status: " + " | ".join(status_failures[-3:])
        )

    if failures:
        raise FlightError("Preflight failed:\n- " + "\n- ".join(failures))
    run_prearm_checks(link)
    print("AUTOMATED PREFLIGHT: PASS", flush=True)


def run_prearm_checks(link: VehicleLink) -> None:
    command = getattr(mavutil.mavlink, "MAV_CMD_RUN_PREARM_CHECKS", None)
    if command is None:
        raise FlightError("pymavlink lacks MAV_CMD_RUN_PREARM_CHECKS")
    link.command_long(command, (0, 0, 0, 0, 0, 0, 0), timeout=12.0)
    print("ArduPilot pre-arm command: PASS", flush=True)


def confirm_launch(args: argparse.Namespace) -> None:
    print("\nALL AUTOMATED CHECKS PASSED.")
    print(
        f"Target: {args.altitude:.1f}m, forward: {args.distance:.1f}m, "
        f"climb limit: {args.climb_rate:.2f}m/s, then RTL."
    )
    print(
        "Confirm 15m clear area, RC recovery switch, pilot, and observer "
        "are ready."
    )
    response = input("Press Enter to arm and fly; type anything to cancel: ")
    if response:
        raise FlightError("Cancelled by operator")


def arm(link: VehicleLink) -> None:
    link.command_long(
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        (1, 0, 0, 0, 0, 0, 0),
        timeout=12.0,
    )
    link.wait_armed(True, 12.0)
    print("Armed", flush=True)


def takeoff(
    link: VehicleLink,
    start: object,
    altitude: float,
    timeout: float,
    args: argparse.Namespace,
) -> None:
    link.command_long(
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
        (0, 0, 0, math.nan, 0, 0, altitude),
        timeout=10.0,
    )
    deadline = time.monotonic() + timeout
    settled_since: Optional[float] = None
    while time.monotonic() < deadline:
        link.pump(0.2)
        ensure_guided_health(link, args)
        position = link.cached("GLOBAL_POSITION_INT", 1.0)
        local_position = link.cached("LOCAL_POSITION_NED", 1.0)
        attitude = link.cached("ATTITUDE", 1.0)
        if position is None:
            continue
        height = float(position.relative_alt) / 1000.0
        if local_position is not None:
            horizontal_drift = math.hypot(
                local_position.x - start.x,
                local_position.y - start.y,
            )
            if horizontal_drift > 1.0:
                raise FlightError(
                    f"Takeoff horizontal drift={horizontal_drift:.2f}m"
                )
        tilt = (
            math.degrees(math.hypot(attitude.roll, attitude.pitch))
            if attitude is not None
            else 0.0
        )
        if tilt > 25:
            raise FlightError("Excessive tilt during takeoff")
        if height > altitude + 0.6:
            raise FlightError(f"Takeoff overshoot: {height:.2f}m")
        print(f"Takeoff altitude={height:.2f}m", flush=True)
        if abs(height - altitude) <= 0.20:
            settled_since = settled_since or time.monotonic()
            if time.monotonic() - settled_since >= 2.0:
                return
        else:
            settled_since = None
    raise FlightError("Takeoff altitude was not reached and settled")


def ensure_guided(link: VehicleLink) -> None:
    heartbeat = link.cached("HEARTBEAT", 2.5)
    if heartbeat is None:
        raise FlightError("Heartbeat lost")
    mode = link.mode_name(heartbeat)
    if mode != "GUIDED":
        raise PilotOverride(f"Control released because mode changed to {mode}")


def ensure_guided_health(
    link: VehicleLink, args: argparse.Namespace
) -> None:
    ensure_guided(link)
    ekf = link.cached("EKF_STATUS_REPORT", 2.5)
    flow = link.cached("OPTICAL_FLOW_RAD", 1.5)
    distance = link.cached("DISTANCE_SENSOR", 1.5)
    system_status = link.cached("SYS_STATUS", 3.0)
    if ekf is None:
        raise FlightError("EKF status became stale")
    flags = int(ekf.flags)
    if flags & REQUIRED_EKF_FLAGS != REQUIRED_EKF_FLAGS:
        raise FlightError(f"EKF health flags degraded: 0x{flags:x}")
    if flags & EKF_CONST_POS_MODE:
        raise FlightError("EKF entered constant-position mode")
    if flow is None or int(flow.quality) < args.min_flow_quality:
        quality = "stale" if flow is None else str(int(flow.quality))
        raise FlightError(f"Optical-flow quality degraded: {quality}")
    if distance is None or int(distance.orientation) != 25:
        raise FlightError("Downward rangefinder became stale")
    range_metres = float(distance.current_distance) / 100.0
    if not 0.01 <= range_metres <= 8.0:
        raise FlightError(
            f"Downward range became invalid: {range_metres:.2f}m"
        )
    if system_status is None:
        raise FlightError("Battery/system status became stale")
    voltage = float(system_status.voltage_battery) / 1000.0
    remaining = int(system_status.battery_remaining)
    if voltage < args.min_battery_voltage:
        raise FlightError(f"Battery voltage dropped to {voltage:.2f}V")
    if remaining < 0 or remaining < args.min_battery_remaining:
        raise FlightError(f"Battery remaining dropped to {remaining}%")


def send_position_target(
    link: VehicleLink, north: float, east: float, down: float
) -> None:
    link.master.mav.set_position_target_local_ned_send(
        int(time.monotonic() * 1000) & 0xFFFFFFFF,
        link.target_system,
        link.target_component,
        mavutil.mavlink.MAV_FRAME_LOCAL_NED,
        POSITION_ONLY_MASK,
        north,
        east,
        down,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def fly_forward(
    link: VehicleLink,
    start: object,
    yaw: float,
    altitude: float,
    distance: float,
    timeout: float,
    args: argparse.Namespace,
) -> None:
    target_north = float(start.x) + distance * math.cos(yaw)
    target_east = float(start.y) + distance * math.sin(yaw)
    target_down = float(start.z) - altitude
    print(
        f"Forward target N={target_north:.2f} E={target_east:.2f} "
        f"Alt={altitude:.2f}",
        flush=True,
    )
    deadline = time.monotonic() + timeout
    settled_since: Optional[float] = None
    next_report = 0.0
    while time.monotonic() < deadline:
        send_position_target(link, target_north, target_east, target_down)
        interval_deadline = time.monotonic() + 0.2
        while time.monotonic() < interval_deadline:
            link.pump(0.05)
        ensure_guided_health(link, args)
        position = link.cached("LOCAL_POSITION_NED", 1.0)
        attitude = link.cached("ATTITUDE", 1.0)
        if position is None:
            continue
        target_error = math.hypot(
            position.x - target_north, position.y - target_east
        )
        from_start = math.hypot(position.x - start.x, position.y - start.y)
        altitude_now = float(start.z) - float(position.z)
        if from_start > distance + 1.0:
            raise FlightError(
                f"Horizontal boundary exceeded: {from_start:.2f}m"
            )
        if altitude_now > altitude + 0.6 or altitude_now < altitude - 0.7:
            raise FlightError(
                f"Altitude boundary exceeded: {altitude_now:.2f}m"
            )
        tilt = (
            math.degrees(math.hypot(attitude.roll, attitude.pitch))
            if attitude is not None
            else 0.0
        )
        if tilt > 25:
            raise FlightError("Excessive tilt while moving forward")
        if time.monotonic() >= next_report:
            print(
                f"Forward error={target_error:.2f}m "
                f"travel={from_start:.2f}m altitude={altitude_now:.2f}m",
                flush=True,
            )
            next_report = time.monotonic() + 1.0
        if (
            target_error <= 0.30
            and math.hypot(position.vx, position.vy) <= 0.35
        ):
            settled_since = settled_since or time.monotonic()
            if time.monotonic() - settled_since >= 1.5:
                print("Forward target reached", flush=True)
                return
        else:
            settled_since = None
    raise FlightError("Forward target was not reached")


def monitor_rtl(link: VehicleLink, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    allowed = {"RTL", "LAND"}
    while time.monotonic() < deadline:
        message = link.pump(0.5)
        if message is None:
            continue
        if message.get_type() == "HEARTBEAT":
            armed = (
                message.base_mode
                & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
            if not armed:
                print("RTL complete: landed and disarmed", flush=True)
                return
            mode = link.mode_name(message)
            if mode not in allowed:
                raise PilotOverride(
                    f"Control released because mode changed to {mode}"
                )
    raise FlightError("RTL timed out before disarm")


def land_if_owned(link: VehicleLink) -> None:
    if not link.is_armed():
        return
    mode = link.mode_name()
    if mode != "GUIDED":
        print(
            f"Vehicle is armed in {mode}; not overriding pilot/FC mode",
            file=sys.stderr,
        )
        return
    print("Mission fault: requesting LAND", file=sys.stderr, flush=True)
    try:
        link.set_mode("LAND", timeout=8.0)
    except Exception as error:
        print(f"LAND request failed: {error}", file=sys.stderr, flush=True)
        return
    try:
        link.wait_armed(False, 90.0)
        print("Landed and disarmed", file=sys.stderr, flush=True)
    except Exception as error:
        print(f"Landing monitor failed: {error}", file=sys.stderr, flush=True)


def speed_parameter_values(
    link: VehicleLink, args: argparse.Namespace
) -> tuple[SavedParameter, float, SavedParameter, float]:
    climb = link.get_parameter(("WP_SPD_UP", "WPNAV_SPEED_UP"))
    horizontal = link.get_parameter(("WP_SPD", "WPNAV_SPEED"))
    climb_value = (
        args.climb_rate
        if climb.name == "WP_SPD_UP"
        else args.climb_rate * 100.0
    )
    horizontal_value = (
        args.forward_speed
        if horizontal.name == "WP_SPD"
        else args.forward_speed * 100.0
    )
    return climb, climb_value, horizontal, horizontal_value


def restore_parameters(
    link: VehicleLink, parameters: list[SavedParameter]
) -> list[SavedParameter]:
    """Restore parameters without issuing a mode or motion command."""
    not_restored: list[SavedParameter] = []
    for parameter in reversed(parameters):
        try:
            link.set_parameter(parameter, parameter.value)
            print(f"Restored {parameter.name}={parameter.value}", flush=True)
        except Exception as error:
            not_restored.append(parameter)
            print(
                f"WARNING: failed to restore {parameter.name}: {error}",
                file=sys.stderr,
                flush=True,
            )
    return list(reversed(not_restored))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", default="udpin:127.0.0.1:14550")
    parser.add_argument("--source-system", type=int, default=250)
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument("--sample-seconds", type=float, default=10.0)
    parser.add_argument("--altitude", type=float, default=2.0)
    parser.add_argument("--distance", type=float, default=2.0)
    parser.add_argument("--climb-rate", type=float, default=0.30)
    parser.add_argument("--forward-speed", type=float, default=0.50)
    parser.add_argument("--takeoff-timeout", type=float, default=35.0)
    parser.add_argument("--forward-timeout", type=float, default=30.0)
    parser.add_argument("--rtl-timeout", type=float, default=180.0)
    parser.add_argument("--min-satellites", type=int, default=10)
    parser.add_argument("--max-eph-cm", type=int, default=150)
    parser.add_argument("--max-gps-p95", type=float, default=1.5)
    parser.add_argument("--min-flow-quality", type=int, default=50)
    parser.add_argument("--max-preflight-tilt", type=float, default=8.0)
    parser.add_argument("--min-battery-voltage", type=float, default=14.4)
    parser.add_argument("--min-battery-remaining", type=int, default=50)
    args = parser.parse_args()
    positive = (
        "connect_timeout",
        "sample_seconds",
        "altitude",
        "distance",
        "climb_rate",
        "forward_speed",
        "takeoff_timeout",
        "forward_timeout",
        "rtl_timeout",
    )
    for name in positive:
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if not 1 <= args.source_system <= 255:
        parser.error("--source-system must be 1..255")
    return args


def main() -> int:
    args = parse_args()
    link = VehicleLink(args.connection, args.source_system)
    saved_parameters: list[SavedParameter] = []
    try:
        link.connect_vehicle(args.connect_timeout)
        configure_streams(link)
        collect_preflight(link, args)
        confirm_launch(args)

        # Recheck all flight-critical telemetry after the human confirmation.
        full_sample_seconds = args.sample_seconds
        args.sample_seconds = 3.0
        try:
            collect_preflight(link, args)
        finally:
            args.sample_seconds = full_sample_seconds

        (
            climb,
            climb_value,
            horizontal,
            horizontal_value,
        ) = speed_parameter_values(link, args)
        saved_parameters.extend((climb, horizontal))
        link.set_parameter(climb, climb_value)
        link.set_parameter(horizontal, horizontal_value)
        print(
            f"Temporary limits: {climb.name}={climb_value}, "
            f"{horizontal.name}={horizontal_value}",
            flush=True,
        )

        start_position = link.wait_for("LOCAL_POSITION_NED", 3.0)
        attitude = link.wait_for("ATTITUDE", 3.0)
        yaw = float(attitude.yaw)

        link.set_mode("GUIDED")
        arm(link)
        takeoff(
            link,
            start_position,
            args.altitude,
            args.takeoff_timeout,
            args,
        )
        fly_forward(
            link,
            start_position,
            yaw,
            args.altitude,
            args.distance,
            args.forward_timeout,
            args,
        )

        # Restore normal navigation limits before RTL uses them.
        saved_parameters = restore_parameters(link, saved_parameters)
        if saved_parameters:
            raise FlightError(
                "Could not restore temporary speed limits before RTL"
            )
        link.set_mode("RTL")
        monitor_rtl(link, args.rtl_timeout)
        print("TEST VERDICT: PASS", flush=True)
        return 0
    except PilotOverride as error:
        print(f"PILOT/FC OVERRIDE: {error}", file=sys.stderr, flush=True)
        saved_parameters = restore_parameters(link, saved_parameters)
        return 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr, flush=True)
        land_if_owned(link)
        return 130
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        land_if_owned(link)
        return 1
    finally:
        if saved_parameters and not link.is_armed():
            restore_parameters(link, saved_parameters)


if __name__ == "__main__":
    raise SystemExit(main())
