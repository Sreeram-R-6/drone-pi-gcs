import pytest
from drone_oled.config import load
from drone_oled.model import TelemetryStore


@pytest.fixture
def config():
    return load()


@pytest.fixture
def profile():
    return {
        "approved": True,
        "flight_sw_version": 67568127,
        "board_version": 76218368,
        "uid2": "01" * 18,
        "min_voltage_v": 14.0,
        "max_ekf_variance": 0.8,
        "motor_positions": ["RR", "FR", "RL", "FL"],
        "arming_mode": "GUIDED",
        "recovery_reviewed": True,
    }


@pytest.fixture
def ready_store(profile):
    store = TelemetryStore()
    now = 100.0
    values = {
        "heartbeat": True,
        "armed": False,
        "landed": 1,
        "mode": "LOITER",
        "mode_id": 5,
        "autopilot": 3,
        "vehicle_type": 2,
        "version": profile["flight_sw_version"],
        "board": profile["board_version"],
        "uid2": profile["uid2"],
        "sensors_present": 3 | 4 | 8 | 32 | 65536 | 33554432 | 268435456,
        "sensors_enabled": 3 | 4 | 8 | 32 | 65536 | 33554432 | 268435456,
        "sensors_health": 3 | 4 | 8 | 32 | 65536 | 33554432 | 268435456,
        "fix": 3,
        "sats": 11,
        "hdop": 1.46,
        "vdop": 1.72,
        "ekf_flags": 1 | 2 | 4 | 16 | 32,
        "pack_v": 16.1,
        "remaining": 99,
        "battery_fault": 0,
        "fence_breach": 0,
        "home": (10.2, 76.3, 10.0),
        "lat": 10.2,
        "lon": 76.3,
        "alt": 0,
        "speed": 0,
        "roll": 0,
        "pitch": 0,
        "current_a": 0.31,
        "consumed": 1,
        "pi_state": "running",
        "wifi": -52,
        "ethernet": True,
        "ip": "192.168.1.123",
        "cpu": 3.5,
        "pi_temp": 45.0,
    }
    values.update(
        {
            "ekf_" + key: 0.1
            for key in (
                "velocity_variance",
                "pos_horiz_variance",
                "pos_vert_variance",
                "compass_variance",
            )
        }
    )
    for key, value in values.items():
        store.put(key, value, now)
    return store
