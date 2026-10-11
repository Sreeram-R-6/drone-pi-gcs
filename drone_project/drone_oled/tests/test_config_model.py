import copy
import math
import pytest
from drone_oled.config import ConfigError, load, validate
from drone_oled.model import TelemetryStore, readiness, identity_ok


def test_default_pins_and_fail_closed(config):
    assert [config["buttons"][k] for k in ("up", "down", "left", "right", "back", "ok")] == [
        26,
        21,
        20,
        16,
        19,
        13,
    ]
    assert config["buttons"]["pull_up"] is True
    assert config["display"]["always_on"] is True
    assert not config["safety"]["commands_enabled"]


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("buttons", "ok", 14),
        ("buttons", "pull_up", False),
        ("buttons", "ok", 26),
        ("motors", "duration_s", 3.1),
        ("motors", "max_throttle_percent", 11),
        ("mavlink", "endpoint", "/dev/serial0"),
        ("mavlink", "endpoint", "udpin:0.0.0.0:14551"),
        ("missions", "countdown_s", 9),
        ("display", "max_fps", 60),
        ("safety", "commands_enabled", True),
    ],
)
def test_unsafe_config_rejected(config, section, key, value):
    config[section][key] = value
    with pytest.raises(ConfigError):
        validate(config)


def test_unknown_and_wrong_types(tmp_path):
    path = tmp_path / "bad.toml"
    for text in ['[buttons]\npull_up="true"', "[display]\nmax_fps=nan", "[buttons]\nokk=13"]:
        path.write_text(text)
        with pytest.raises(ConfigError):
            load(path)


def test_snapshot_age_and_isolation():
    store = TelemetryStore()
    store.put("a", 2, 10)
    snap = store.snapshot(11)
    store.put("a", 3, 11)
    assert snap.get("a") == 2
    assert store.snapshot(13.01).get("a") is None
    assert math.isinf(snap.age("missing"))


def test_readiness_requires_absolute_ekf(config, profile, ready_store):
    assert readiness(ready_store.snapshot(100), config, profile) == []
    ready_store.put("ekf_flags", 367, 100)
    assert "EKF NOT READY" in readiness(ready_store.snapshot(100), config, profile)
    assert "NO FC LINK" in readiness(ready_store.snapshot(103), config, profile)


def test_identity_not_from_heartbeat_alone(profile, ready_store):
    assert identity_ok(ready_store.snapshot(100), profile)
    changed = copy.deepcopy(profile)
    changed["uid2"] = "02" * 18
    assert not identity_ok(ready_store.snapshot(100), changed)


def test_pack_only_not_cell_and_sentinels():
    store = TelemetryStore()
    store.update(
        {
            "mavpackettype": "BATTERY_STATUS",
            "id": 0,
            "voltages": [16099] + [65535] * 9,
            "voltages_ext": [0] * 4,
            "current_battery": -1,
            "current_consumed": -1,
            "temperature": 32767,
            "time_remaining": 0,
            "battery_remaining": -1,
        },
        100,
    )
    snap = store.snapshot(100)
    assert snap.get("pack_v") == 16.099
    assert snap.get("cells") == []
    assert snap.get("battery_temp") is None
    assert snap.get("current_a") is None
    assert snap.get("consumed") is None
    assert snap.get("battery_time") is None


def test_real_cells_extension():
    store = TelemetryStore()
    store.update(
        {
            "mavpackettype": "BATTERY_STATUS",
            "id": 0,
            "voltages": [4000] * 10,
            "voltages_ext": [4000, 4000, 0, 0],
        },
        1,
    )
    snap = store.snapshot(1)
    assert len(snap.get("cells")) == 12
    assert snap.get("pack_v") == 48


def test_gps_units_unknown():
    store = TelemetryStore()
    store.update(
        {
            "mavpackettype": "GPS_RAW_INT",
            "fix_type": 3,
            "satellites_visible": 255,
            "eph": 146,
            "epv": 65535,
            "alt": 13410,
            "vel": 4,
        },
        1,
    )
    s = store.snapshot(1)
    assert s.get("sats") is None and s.get("vdop") is None
    assert s.get("hdop") == 1.46 and s.get("gps_alt") == 13.41 and s.get("gps_speed") == 0.04


def test_reboot_invalidates_home_identity():
    store = TelemetryStore()
    store.put("home", (1, 2, 3), 1)
    store.put("version", 1, 1)
    store.update(
        {"mavpackettype": "ATTITUDE", "time_boot_ms": 10000, "roll": 0, "pitch": 0, "yaw": 0}, 1
    )
    assert store.update(
        {"mavpackettype": "ATTITUDE", "time_boot_ms": 10, "roll": 0, "pitch": 0, "yaw": 0}, 2
    )
    assert store.snapshot(2).cached("home") is None
    assert store.snapshot(2).cached("version") is None
