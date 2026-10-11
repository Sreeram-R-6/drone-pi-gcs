"""Immutable, timestamped telemetry and fail-closed readiness."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any, Generic, TypeVar
import math

T = TypeVar("T")
MODES = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    5: "LOITER",
    6: "RTL",
    9: "LAND",
    16: "POSHOLD",
    17: "BRAKE",
}
MODE_IDS = {v: k for k, v in MODES.items()}


@dataclass(frozen=True)
class Sample(Generic[T]):
    value: T
    received_at: float
    source: str

    def age(self, now: float) -> float:
        return max(0.0, now - self.received_at)


@dataclass(frozen=True)
class Snapshot:
    samples: dict[str, Sample[Any]]
    now: float
    stale_s: float = 2.0

    def get(self, key: str, default: Any = None, max_age: float | None = None) -> Any:
        sample = self.samples.get(key)
        if sample is None or sample.age(self.now) > (self.stale_s if max_age is None else max_age):
            return default
        return sample.value

    def age(self, key: str) -> float:
        sample = self.samples.get(key)
        return sample.age(self.now) if sample else math.inf

    def cached(self, key: str, default: Any = None) -> Any:
        """Only session-scoped immutable identity/home data may use this accessor."""
        sample = self.samples.get(key)
        return sample.value if sample else default

    def healthy(self, mask: int) -> bool | None:
        present = self.get("sensors_present")
        enabled = self.get("sensors_enabled")
        health = self.get("sensors_health")
        if None in (present, enabled, health):
            return None
        if present & mask != mask or enabled & mask != mask:
            return None
        return health & mask == mask


class TelemetryStore:
    def __init__(self, stale_s: float = 2.0):
        self._lock = Lock()
        self._samples: dict[str, Sample[Any]] = {}
        self.stale_s = stale_s
        self.boot_times: dict[str, int] = {}
        self.session = 0

    def put(self, key: str, value: Any, now: float, source: str = "PI") -> None:
        with self._lock:
            self._samples[key] = Sample(value, now, source)

    def snapshot(self, now: float) -> Snapshot:
        with self._lock:
            return Snapshot(dict(self._samples), now, self.stale_s)

    def invalidate(self) -> None:
        pi_keys = {"pi_state", "wifi", "ethernet", "ip", "cpu", "pi_temp"}
        with self._lock:
            self._samples = {
                k: v for k, v in self._samples.items() if k in pi_keys and v.source == "PI"
            }
        self.boot_times.clear()
        self.session += 1

    def update(self, data: dict[str, Any], now: float, battery_id: int = 0) -> bool:
        kind = data.get("mavpackettype", "")
        reboot = False
        boot = data.get("time_boot_ms")
        if boot is not None:
            previous = self.boot_times.get(kind)
            if previous is not None and boot + 1000 < previous:
                self.invalidate()
                reboot = True
            self.boot_times[kind] = boot
        fields: dict[str, Any] = {}

        def value(field: str, divisor: float = 1, invalid: tuple = ()) -> Any:
            v = data.get(field)
            if v is None or v in invalid or not isinstance(v, (int, float)) or not math.isfinite(v):
                return None
            return v / divisor if divisor != 1 else v

        if kind == "HEARTBEAT":
            mode = data["custom_mode"]
            fields = {
                "heartbeat": True,
                "armed": bool(data["base_mode"] & 128),
                "mode": MODES.get(mode, f"MODE {mode}"),
                "mode_id": mode,
                "autopilot": data["autopilot"],
                "vehicle_type": data["type"],
                "system_status": data["system_status"],
            }
        elif kind == "SYS_STATUS":
            fields = {
                "sensors_" + suffix: data.get("onboard_control_sensors_" + suffix)
                for suffix in ("present", "enabled", "health")
            }
            if battery_id == 0:
                fields.update(
                    pack_v=value("voltage_battery", 1000, (65535,)),
                    current_a=value("current_battery", 100, (-1,)),
                    remaining=value("battery_remaining", invalid=(-1,)),
                )
        elif kind == "GPS_RAW_INT":
            fix = value("fix_type")
            fields = {
                "fix": fix if fix in range(9) else None,
                "sats": value("satellites_visible", invalid=(255,)),
                "hdop": value("eph", 100, (65535,)),
                "vdop": value("epv", 100, (65535,)),
                "gps_alt": value("alt", 1000),
                "gps_speed": value("vel", 100, (65535,)),
            }
        elif kind == "GLOBAL_POSITION_INT":
            fields = {
                "lat": value("lat", 1e7),
                "lon": value("lon", 1e7),
                "alt": value("relative_alt", 1000),
                "heading": value("hdg", 100, (65535,)),
                "vn": value("vx", 100),
                "ve": value("vy", 100),
                "vd": value("vz", 100),
            }
        elif kind == "VFR_HUD":
            fields = {"speed": value("groundspeed"), "climb": value("climb")}
        elif kind == "ATTITUDE":
            fields = {key: value(key, math.pi / 180) for key in ("roll", "pitch", "yaw")}
        elif kind == "BATTERY_STATUS" and data.get("id") == battery_id:
            raw = data.get("voltages", [])
            cells = [v / 1000 for v in raw if v != 65535]
            ext = [v / 1000 for v in data.get("voltages_ext", []) if v not in (0, 65535)]
            # One valid slot normally contains pack voltage, NOT one measured cell.
            measured_cells = cells + ext if len(cells) > 1 and 65534 not in raw else []
            fields = {
                "cells": measured_cells,
                "battery_temp": value("temperature", 100, (32767,)),
                "consumed": value("current_consumed", invalid=(-1,)),
                "remaining": value("battery_remaining", invalid=(-1,)),
                "current_a": value("current_battery", 100, (-1,)),
                "battery_time": value("time_remaining", invalid=(0,)),
                "charge_state": data.get("charge_state"),
                "battery_fault": data.get("fault_bitmask", 0),
            }
            if cells:
                fields["pack_v"] = sum(cells + ext)
        elif kind == "EKF_STATUS_REPORT":
            fields = {"ekf_flags": data.get("flags")}
            fields.update(
                {
                    "ekf_" + k: value(k)
                    for k in (
                        "velocity_variance",
                        "pos_horiz_variance",
                        "pos_vert_variance",
                        "compass_variance",
                    )
                }
            )
        elif kind == "RC_CHANNELS":
            fields = {"rc_received": True, "rc_rssi": value("rssi", invalid=(255,))}
        elif kind == "MISSION_CURRENT":
            fields = {
                "mission_seq": data.get("seq"),
                "mission_total": value("total", invalid=(0, 65535)),
                "mission_state": value("mission_state", invalid=(0,)),
                "mission_mode": value("mission_mode", invalid=(0,)),
            }
        elif kind == "MISSION_ITEM_REACHED":
            fields = {"mission_reached": data.get("seq")}
        elif kind == "EXTENDED_SYS_STATE":
            fields = {"landed": value("landed_state", invalid=(0,))}
        elif kind == "HOME_POSITION":
            fields = {
                "home": (value("latitude", 1e7), value("longitude", 1e7), value("altitude", 1000))
            }
        elif kind == "FENCE_STATUS":
            fields = {
                "fence_breach": data.get("breach_status"),
                "fence_type": data.get("breach_type"),
            }
        elif kind == "AUTOPILOT_VERSION":
            fields = {
                "version": data.get("flight_sw_version"),
                "board": data.get("board_version"),
                "uid2": bytes(data.get("uid2", [])).hex(),
            }
        with self._lock:
            for key, v in fields.items():
                self._samples[key] = Sample(v, now, kind)
        return reboot


def fc_arm_available(s: Snapshot, c: dict, kind: str) -> bool:
    """Delegate arm checks to Copter without weakening other command policies."""
    return bool(
        c["safety"]["arm_policy"] == "fc_managed"
        and kind in ("arm", "disarm", "land")
        and s.get("heartbeat") is True
        and s.get("autopilot") == 3
        and s.get("vehicle_type") == 2
    )


def identity_ok(s: Snapshot, profile: dict[str, Any]) -> bool:
    return bool(
        profile
        and s.get("autopilot") == 3
        and s.get("vehicle_type") == 2
        and s.cached("version") == profile.get("flight_sw_version")
        and s.cached("board") == profile.get("board_version")
        and s.cached("uid2") == profile.get("uid2")
    )


def readiness(s: Snapshot, c: dict, profile: dict, faults: list[str] | None = None) -> list[str]:
    reasons = []
    if s.get("heartbeat") is not True:
        return ["NO FC LINK"]
    if not identity_ok(s, profile):
        reasons.append("IDENTITY UNREVIEWED")
    for mask, name in [
        (3, "IMU"),
        (8, "BARO"),
        (4, "MAG"),
        (32, "GPS"),
        (65536, "RC"),
        (33554432, "BATT"),
        (268435456, "PREARM"),
    ]:
        if s.healthy(mask) is not True:
            reasons.append(name + " NOT READY")
    fix, sats, hdop = s.get("fix"), s.get("sats"), s.get("hdop")
    if fix not in (3, 4, 5, 6, 8):
        reasons.append("GPS FIX LOW")
    if sats is None or sats < c["readiness"]["min_satellites"]:
        reasons.append(
            f"GPS LOW {sats if sats is not None else '--'}/{c['readiness']['min_satellites']}"
        )
    if hdop is None or hdop > c["readiness"]["max_hdop"]:
        reasons.append("GPS HDOP HIGH")
    flags = s.get("ekf_flags")
    required = 1 | 2 | 4 | 16
    if (
        flags is None
        or flags & required != required
        or not flags & (32 | 64)
        or flags & (128 | 1024 | 32768)
    ):
        reasons.append("EKF NOT READY")
    limit = profile.get("max_ekf_variance")
    for key in ("velocity_variance", "pos_horiz_variance", "pos_vert_variance", "compass_variance"):
        v = s.get("ekf_" + key)
        if limit is None or v is None or v > limit:
            reasons.append("EKF LIMIT UNKNOWN" if limit is None else "EKF VARIANCE")
            break
    voltage, remaining = s.get("pack_v"), s.get("remaining")
    if voltage is None or not profile.get("min_voltage_v") or voltage < profile["min_voltage_v"]:
        reasons.append("BATT VOLT UNREADY")
    if remaining is None or remaining < c["readiness"]["min_remaining_pct"]:
        reasons.append("BATT CHARGE LOW")
    if s.get("battery_fault", 0):
        reasons.append("BATT FAULT")
    if s.get("fence_breach"):
        reasons.append("FENCE BREACH")
    return list(dict.fromkeys(reasons + (faults or [])))


def home_geometry(s: Snapshot) -> tuple[float | None, float | None]:
    home, lat, lon = s.cached("home"), s.get("lat"), s.get("lon")
    if not home or None in (*home[:2], lat, lon):
        return None, None
    a, b = math.radians(lat), math.radians(home[0])
    dl = math.radians(home[1] - lon)
    h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dl / 2) ** 2
    distance = 6371000 * 2 * math.asin(min(1, math.sqrt(h)))
    heading = (
        math.degrees(
            math.atan2(
                math.sin(dl) * math.cos(b),
                math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(dl),
            )
        )
        % 360
    )
    return distance, heading
