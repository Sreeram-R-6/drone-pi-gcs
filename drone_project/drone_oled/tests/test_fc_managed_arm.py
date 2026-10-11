from types import SimpleNamespace
import time
import pytest
from drone_oled.config import validate, ConfigError
from drone_oled.fsm import FSM
from drone_oled.mav import MavLinkWorker, CommandRejected
from drone_oled.model import TelemetryStore


def arm_context(config):
    config["safety"].update(commands_enabled=True, arm_enabled=True, arm_policy="fc_managed")
    store = TelemetryStore()
    now = time.monotonic()
    for key, value in dict(
        heartbeat=True, autopilot=3, vehicle_type=2, armed=False, landed=1
    ).items():
        store.put(key, value, now)
    worker = MavLinkWorker(config, {}, store, lambda *args: None)
    worker.client = SimpleNamespace(check=lambda: None)
    return store, worker, FSM(config, {}, [])


def test_fc_managed_arm_without_profile_gps_or_battery(config):
    store, worker, fsm = arm_context(config)
    validate(config)
    assert not fsm.guard("arm", store.snapshot(time.monotonic()))
    worker._guard("arm")


@pytest.mark.parametrize(
    "key,value",
    [("autopilot", 12), ("vehicle_type", 1), ("heartbeat", False), ("armed", True), ("landed", 2)],
)
def test_arm_keeps_link_supported_vehicle_and_ground_guards(config, key, value):
    store, worker, fsm = arm_context(config)
    store.put(key, value, time.monotonic())
    assert fsm.guard("arm", store.snapshot(time.monotonic()))
    with pytest.raises(CommandRejected):
        worker._guard("arm")


def test_stale_link_cannot_arm(config):
    store, worker, fsm = arm_context(config)
    for key in ("heartbeat", "autopilot", "vehicle_type"):
        store.put(key, {"heartbeat": True, "autopilot": 3, "vehicle_type": 2}[key], 0)
    assert fsm.guard("arm", store.snapshot(time.monotonic()))
    with pytest.raises(CommandRejected):
        worker._guard("arm")


def test_airborne_disarm_still_blocked(config):
    store, worker, fsm = arm_context(config)
    store.put("armed", True, time.monotonic())
    store.put("landed", 2, time.monotonic())
    assert fsm.guard("disarm", store.snapshot(time.monotonic()))
    with pytest.raises(CommandRejected, match="NOT CONFIRMED LANDED"):
        worker._arm(False)


def test_mission_still_requires_profile(config):
    arm_context(config)
    config["safety"]["mission_enabled"] = True
    with pytest.raises(ConfigError, match="profile"):
        validate(config)


def test_other_commands_not_enabled(config):
    store, worker, fsm = arm_context(config)
    assert fsm.guard("spin", store.snapshot(time.monotonic()))
    with pytest.raises(CommandRejected):
        worker._guard("motor_start")
