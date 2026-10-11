"""Configuration and explicit commissioning boundaries."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib


class ConfigError(ValueError):
    pass


DEFAULTS: dict[str, Any] = {
    "display": {
        "driver": "ssd1306",
        "bus": 1,
        "address": 0x3C,
        "rotation": 0,
        "i2c_hz": 400000,
        "max_fps": 10,
        "contrast": 128,
        "always_on": True,
        "dim_after_s": 0,
        "blank_after_s": 0,
        "boot_escape_s": 45,
        "splash": "",
    },
    "buttons": {
        "up": 26,
        "down": 21,
        "left": 20,
        "right": 16,
        "back": 19,
        "ok": 13,
        "pull_up": True,
        "debounce_ms": 25,
        "released_before_enable_ms": 100,
        "long_press_ms": 800,
        "repeat_delay_ms": 400,
        "repeat_hz": 8,
        "transition_guard_ms": 150,
    },
    "mavlink": {
        "endpoint": "udpin:127.0.0.1:14553",
        "source_system": 253,
        "source_component": 191,
        "target_system": 1,
        "target_component": 1,
        "stale_s": 2.0,
        "heartbeat_lost_s": 3.0,
        "ack_timeout_s": 3.0,
        "state_timeout_s": 5.0,
        "read_retries": 2,
    },
    "readiness": {"min_satellites": 6, "max_hdop": 2.0, "min_remaining_pct": 20},
    "safety": {
        "commands_enabled": False,
        "arm_enabled": False,
        "arm_policy": "commissioned",
        "mission_enabled": False,
        "motor_test_enabled": False,
        "calibration_enabled": False,
        "parameter_write_enabled": False,
        "firmware_profile": "",
        "abort_mode": "RTL",
        "abort_fallback": "LAND",
    },
    "motors": {"duration_s": 3.0, "throttle_percent": 5.0, "max_throttle_percent": 10.0},
    "missions": {
        "directory": "missions",
        "countdown_s": 10,
        "max_display_steps": 99,
        "transfer_timeout_s": 2.0,
        "transfer_attempts": 3,
        "transfer_total_s": 60.0,
    },
    "battery": {"id": 0, "verified_cell_count": 0},
    "pi": {
        "wifi_interface": "wlan0",
        "ethernet_interface": "eth0",
        "sample_hz": 1,
        "stale_s": 3.0,
        "command_timeout_s": 1.0,
    },
    "logging": {"directory": "", "max_bytes": 5242880, "backup_count": 3},
    "web": {"enabled": False, "socket": "/run/drone-oled/ui.sock"},
}


def load(path: str | Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULTS)
    base = Path.cwd()
    if path:
        base = Path(path).resolve().parent
        with open(path, "rb") as stream:
            incoming = tomllib.load(stream)
        for section, values in incoming.items():
            if section not in config or not isinstance(values, dict):
                raise ConfigError(f"Unknown section: {section}")
            for key, value in values.items():
                if key not in config[section]:
                    raise ConfigError(f"Unknown setting: {section}.{key}")
                expected = config[section][key]
                if isinstance(expected, bool):
                    valid = isinstance(value, bool)
                elif isinstance(expected, (int, float)):
                    valid = isinstance(value, (int, float)) and not isinstance(value, bool)
                    valid = valid and math.isfinite(value)
                    if isinstance(expected, int):
                        valid = valid and isinstance(value, int)
                else:
                    valid = isinstance(value, str)
                if not valid:
                    raise ConfigError(f"Wrong type: {section}.{key}")
                config[section][key] = value
    for section, key in [
        ("missions", "directory"),
        ("display", "splash"),
        ("safety", "firmware_profile"),
        ("logging", "directory"),
    ]:
        value = config[section][key]
        if value:
            config[section][key] = str((base / value).resolve())
    validate(config)
    return config


def validate(c: dict[str, Any]) -> None:
    b, d, m = c["buttons"], c["display"], c["mavlink"]
    pins = [b[k] for k in ("up", "down", "left", "right", "back", "ok")]
    if len(set(pins)) != 6 or any(p not in range(4, 28) or p in (14, 15) for p in pins):
        raise ConfigError("Button GPIOs must be unique BCM4..27 excluding UART14/15")
    if b["pull_up"] is not True or not 20 <= b["debounce_ms"] <= 30:
        raise ConfigError("Internal pull-ups and 20..30ms debounce required")
    for key in (
        "released_before_enable_ms",
        "long_press_ms",
        "repeat_delay_ms",
        "repeat_hz",
        "transition_guard_ms",
    ):
        if b[key] <= 0:
            raise ConfigError(f"Nonpositive button timing: {key}")
    if d["driver"] not in ("ssd1306", "sh1106") or d["rotation"] not in range(4):
        raise ConfigError("Unsupported OLED driver/rotation")
    if not 0x08 <= d["address"] <= 0x77 or not 0 <= d["contrast"] <= 255:
        raise ConfigError("Invalid I2C address/contrast")
    if d["i2c_hz"] not in (100000, 400000):
        raise ConfigError("Commission I2C at 100kHz or 400kHz")
    if not 1 <= d["max_fps"] <= (10 if d["i2c_hz"] == 400000 else 5):
        raise ConfigError("FPS exceeds I2C budget")
    if not m["endpoint"].startswith("udpin:127.0.0.1:"):
        raise ConfigError("OLED may only connect to loopback UDP, never serial")
    try:
        port = int(m["endpoint"].rsplit(":", 1)[1])
    except ValueError as exc:
        raise ConfigError("Invalid UDP port") from exc
    if not 1024 <= port <= 65535:
        raise ConfigError("Invalid UDP port")
    for key in ("source_system", "source_component", "target_system", "target_component"):
        if not 1 <= m[key] <= 255:
            raise ConfigError(f"Invalid MAVLink identity {key}")
    if m["source_system"] == m["target_system"]:
        raise ConfigError("Source and target system must differ")
    for key in ("stale_s", "heartbeat_lost_s", "ack_timeout_s", "state_timeout_s"):
        if m[key] <= 0:
            raise ConfigError(f"Nonpositive timeout {key}")
    if m["heartbeat_lost_s"] < m["stale_s"]:
        raise ConfigError("Heartbeat-loss timeout must not precede staleness")
    motors = c["motors"]
    if not 0 < motors["duration_s"] <= 3:
        raise ConfigError("Motor tests must be bounded to 3 seconds")
    if not 0 < motors["throttle_percent"] <= motors["max_throttle_percent"] <= 10:
        raise ConfigError("Motor test percentage must be bounded to 10%")
    if c["missions"]["countdown_s"] < 10 or not 1 <= c["missions"]["max_display_steps"] <= 99:
        raise ConfigError("Mission countdown >=10s; displayed steps <=99")
    for section, keys in {
        "missions": ("transfer_timeout_s", "transfer_attempts", "transfer_total_s"),
        "pi": ("sample_hz", "stale_s", "command_timeout_s"),
        "display": ("boot_escape_s",),
        "logging": ("max_bytes", "backup_count"),
    }.items():
        if any(c[section][key] <= 0 for key in keys):
            raise ConfigError(f"Nonpositive limit in {section}")
    if c["safety"]["abort_mode"] not in ("RTL", "LOITER", "LAND"):
        raise ConfigError("Unsupported abort mode")
    if c["safety"]["abort_fallback"] != "LAND":
        raise ConfigError("Only reviewed LAND fallback is supported")
    if not 0 <= c["battery"]["verified_cell_count"] <= 14:
        raise ConfigError("Invalid cell count")
    if not 1 <= c["readiness"]["min_satellites"] <= 254:
        raise ConfigError("Invalid minimum satellites")
    if not 0 < c["readiness"]["max_hdop"] or not 0 <= c["readiness"]["min_remaining_pct"] <= 100:
        raise ConfigError("Invalid readiness limits")
    safety = c["safety"]
    if safety["arm_policy"] not in ("commissioned", "fc_managed"):
        raise ConfigError("arm_policy must be commissioned or fc_managed")
    needs_profile = safety["arm_policy"] == "commissioned" or any(
        safety[key]
        for key in (
            "mission_enabled",
            "motor_test_enabled",
            "calibration_enabled",
            "parameter_write_enabled",
        )
    )
    if safety["commands_enabled"] and needs_profile and not safety["firmware_profile"]:
        raise ConfigError("Commands require an explicit commissioned firmware profile")


def load_profile(c: dict[str, Any]) -> dict[str, Any]:
    path = c["safety"]["firmware_profile"]
    if not path:
        return {}
    with open(path) as stream:
        profile = json.load(stream)
    if not isinstance(profile, dict):
        raise ConfigError("Firmware profile must be a JSON object")
    required = {
        "flight_sw_version",
        "board_version",
        "uid2",
        "approved",
        "min_voltage_v",
        "max_ekf_variance",
        "motor_positions",
        "arming_mode",
        "recovery_reviewed",
    }
    if not required <= profile.keys() or profile["approved"] is not True:
        raise ConfigError("Incomplete or unapproved firmware profile")
    for key in ("flight_sw_version", "board_version"):
        if type(profile[key]) is not int or not 0 < profile[key] < 2**32:
            raise ConfigError(f"Invalid identity field: {key}")
    if type(profile["recovery_reviewed"]) is not bool:
        raise ConfigError("recovery_reviewed must be a boolean")
    if not isinstance(profile["uid2"], str) or not profile["uid2"] or len(profile["uid2"]) != 36:
        raise ConfigError("Nonzero 18-byte UID2 required")
    try:
        uid = bytes.fromhex(profile["uid2"])
    except ValueError as exc:
        raise ConfigError("Invalid UID2") from exc
    if not any(uid) or profile["arming_mode"] not in ("GUIDED", "LOITER", "STABILIZE"):
        raise ConfigError("Invalid identity/arming mode")
    for key in ("min_voltage_v", "max_ekf_variance"):
        if (
            type(profile[key]) not in (int, float)
            or not math.isfinite(profile[key])
            or profile[key] <= 0
        ):
            raise ConfigError(f"Unreviewed profile limit: {key}")
    if (
        not isinstance(profile["motor_positions"], list)
        or not all(isinstance(x, str) for x in profile["motor_positions"])
        or (
            profile["motor_positions"]
            and sorted(profile["motor_positions"]) != ["FL", "FR", "RL", "RR"]
        )
    ):
        raise ConfigError("Motor positions must map test sequences 1..4")
    calibrations = profile.get("calibrations", [])
    if not isinstance(calibrations, list) or any(
        x not in ("GYRO", "BARO ZERO", "COMPASS") for x in calibrations
    ):
        raise ConfigError("Unsupported calibration profile")
    if "COMPASS" in calibrations and (
        type(profile.get("compass_mask")) is not int or not 1 <= profile["compass_mask"] <= 255
    ):
        raise ConfigError("Compass calibration requires explicit compass_mask 1..255")
    parameters = profile.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ConfigError("parameters must be a JSON object")
    for name, spec in parameters.items():
        if name not in (
            "FS_THR_ENABLE",
            "FENCE_ENABLE",
            "FENCE_RADIUS",
            "FENCE_ALT_MAX",
            "BATT_LOW_VOLT",
        ):
            raise ConfigError("Unreviewed editable parameter " + name)
        if not isinstance(spec, dict) or not {"min", "max", "step", "type"} <= spec.keys():
            raise ConfigError("Incomplete parameter limits")
        if any(
            type(spec[k]) not in (int, float) or not math.isfinite(spec[k])
            for k in ("min", "max", "step")
        ):
            raise ConfigError("Nonfinite parameter limits")
        if spec["step"] <= 0 or spec["max"] < spec["min"] or spec["type"] not in range(1, 10):
            raise ConfigError("Invalid parameter limits/type")
        if "values" in spec and (
            not isinstance(spec["values"], list)
            or not spec["values"]
            or any(
                type(v) not in (int, float)
                or not math.isfinite(v)
                or not spec["min"] <= v <= spec["max"]
                for v in spec["values"]
            )
            or spec["values"] != sorted(set(spec["values"]))
        ):
            raise ConfigError("Invalid parameter enum values")
    texts = profile.get("failsafe_texts", {})
    if not isinstance(texts, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in texts.items()
    ):
        raise ConfigError("failsafe_texts must map exact captured FC strings to labels")
    return profile
