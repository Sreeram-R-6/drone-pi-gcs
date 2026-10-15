"""Strict configuration boundaries; unknown settings block flight."""
import math
from pathlib import Path


def load(path):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open(path, "rb") as stream:
        config = tomllib.load(stream)
    backend = config.get('gcs', {}).get('execution_backend', 'guided')
    if backend not in ('guided', 'onboard_auto'):
        raise ValueError('Unknown mission execution backend')
    onboard_policy = config.get('firmware', {}).get('mission_policy') == 'onboard_auto_v1'
    if (backend == 'onboard_auto') != onboard_policy:
        raise ValueError('Onboard AUTO backend and onboard_auto_v1 policy must be selected together')
    endpoint = config["link"]["endpoint"]
    direct_uart = endpoint == config["link"]["device"] and endpoint.startswith("/dev/")
    if not direct_uart and endpoint not in {"udpin:127.0.0.1:14550", "tcp:127.0.0.1:5760"}:
        raise ValueError("Endpoint must be the configured UART device or loopback TCP 5760 / UDP 14550")
    if config["link"]["baud"] != 115200:
        raise ValueError("This deployment requires 115200 baud")
    if config["mission"]["altitude_m"] != 2 or config["mission"]["hold_s"] != 10:
        raise ValueError("This deployment is restricted to 2 m / 10 s")
    def finite(value):
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Nonfinite configuration value")
    finite(config)
    for section, keys in {
        "link": ["connect_timeout_s", "max_age_s"],
        "sampling": ["full_s", "fast_s", "heartbeat_hz", "position_hz", "sensor_hz"],
        "mission": list(config["mission"]),
    }.items():
        if any(config[section][key] <= 0 for key in keys):
            raise ValueError(f"Nonpositive timing/limit in {section}")
    config["_base"] = str(Path(path).resolve().parent)
    return config


def unresolved(config):
    reasons = []
    for section in ("gps", "flow", "range", "battery", "attitude"):
        for key, value in config[section].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if key == "sensor_id":
                    if value < 0:
                        reasons.append(f"{section}.{key}")
                elif value <= 0:
                    reasons.append(f"{section}.{key}")
    for key, value in config["commissioning"].items():
        if key in ('flow_axes_range_calibration_reviewed','manual_hover_log_reviewed'):
            continue # Removed checklist gates; do not reintroduce via legacy helper.
        if isinstance(value, bool) and not value:
            reasons.append(f"commissioning.{key}")
    c = config["commissioning"]
    if c["clear_radius_m"] < max(15, c["required_clear_radius_m"]):
        reasons.append("at least 15 m clear controlled radius")
    if not config["link"]["device_confirmed"]:
        reasons.append("UART device confirmation")
    for key in ("firmware_target", "flight_sw_version", "board_version", "uid",
                "pi_serial_group", "flow_serial_group"):
        if key == 'uid' and config['aircraft'].get('uid2'):
            continue
        if not config["aircraft"][key]:
            reasons.append(f"aircraft.{key}")
    for key in ("review_source", "climb_parameter", "climb_parameter_type",
                "climb_parameter_units_per_m_s", "climb_change_approved", "takeoff_altitude_reference"):
        if not config["firmware"][key]:
            reasons.append(f"firmware.{key}")
    for section, key in (("commissioning", "report_path"),
                         ("parameter_audit", "profile_path")):
        value = config[section][key]
        if not value or not (Path(config["_base"]) / value).is_file():
            reasons.append(f"{section}.{key} missing evidence file")
    return reasons


def bench_profile(config):
    """Overlay diagnostic limits only; the launcher blocks motor/mission actions."""
    import copy
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    path = Path(__file__).parents[1]/'bench_limits.toml'
    with path.open('rb') as stream:
        limits = tomllib.load(stream)
    result = copy.deepcopy(config)
    for section,values in limits.items():
        if section not in ('gps','flow','range','battery','attitude'):
            raise ValueError('Invalid bench limits section')
        for key,value in values.items():
            if key not in result[section] or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid bench limit: '+section+'.'+key)
        result[section].update(values)
    result['_bench_only'] = True
    return result
