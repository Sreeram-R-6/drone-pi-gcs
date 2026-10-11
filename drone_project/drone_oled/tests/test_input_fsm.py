import pytest
from drone_oled.input import InputInterpreter, KeyEvent
from drone_oled.fsm import FSM
from drone_oled.emergency import EmergencyEvent, EmergencyDetector
from drone_oled.missions import MissionDefinition, MissionItem


def enabled_input(config):
    interpreter = InputInterpreter(config["buttons"])
    interpreter.tick(0, 0)
    interpreter.tick(0.11, 0)
    return interpreter


def test_boot_held_ok_never_activates(config):
    i = InputInterpreter(config["buttons"])
    i.edge("ok", True, 0)
    assert not i.tick(0.03, 0)
    assert not i.tick(1, 0)
    i.edge("ok", False, 1)
    assert not i.tick(1.03, 0)
    i.tick(1.2, 0)
    assert i.enabled


def test_bounce_no_command_and_ok_no_repeat(config):
    i = enabled_input(config)
    for stamp, pressed in [(0.2, True), (0.21, False), (0.22, True)]:
        i.edge("ok", pressed, stamp)
        assert not i.tick(stamp, 0)
    assert not i.tick(0.25, 0)
    assert not i.tick(2, 0)
    i.edge("ok", False, 2.1)
    events = i.tick(2.13, 0)
    assert len(events) == 1 and events[0].key == "ok"


def test_back_long_is_not_also_short(config):
    i = enabled_input(config)
    i.edge("back", True, 0.2)
    i.tick(0.23, 2)
    events = i.tick(1.04, 2)
    assert [e.kind for e in events] == ["long"]
    i.edge("back", False, 1.1)
    assert not i.tick(1.13, 2)


def test_urgent_back_does_not_wait_release(config):
    i = enabled_input(config)
    i.edge("back", True, 0.2)
    events = i.tick(0.23, 4, True)
    assert [e.key for e in events] == ["back"]
    assert not i.tick(1.2, 4, True)


def test_old_generation_release_preserved(config):
    i = enabled_input(config)
    i.edge("ok", True, 0.2)
    i.tick(0.23, 1)
    i.edge("ok", False, 0.3)
    assert i.tick(0.33, 2)[0].generation == 1


def test_chord_suppresses_activation(config):
    i = enabled_input(config)
    for key in ("ok", "right"):
        i.edge(key, True, 0.2)
    assert not i.tick(0.23, 0)
    for key in ("ok", "right"):
        i.edge(key, False, 0.3)
    assert not i.tick(0.33, 0)


def test_repeat_direction_only(config):
    i = enabled_input(config)
    i.edge("down", True, 0.2)
    i.tick(0.23, 0)
    assert i.tick(0.64, 0)[0].kind == "repeat"


def key(fsm, name, snap, now=101, kind="short", generation=None):
    return fsm.key(
        KeyEvent(name, kind, fsm.state.generation if generation is None else generation), snap, now
    )


def test_safe_confirm_and_wrap(config, profile, ready_store):
    config["safety"]["commands_enabled"] = config["safety"]["arm_enabled"] = True
    f = FSM(config, profile, [])
    f.state.id = "menu"
    snap = ready_store.snapshot(100)
    assert not key(f, "ok", snap)
    assert f.state.id == "confirm" and f.state.focus == 0
    assert not key(f, "ok", snap, 102)
    assert f.state.id == "menu"
    key(f, "up", snap, 103)
    assert f.state.focus == 5


def test_arm_exactly_once_and_repeat_ignored(config, profile, ready_store):
    config["safety"]["commands_enabled"] = config["safety"]["arm_enabled"] = True
    f = FSM(config, profile, [])
    f.state.id = "menu"
    snap = ready_store.snapshot(100)
    key(f, "ok", snap)
    key(f, "right", snap, 102)
    effects = key(f, "ok", snap, 103)
    assert [e.kind for e in effects] == ["arm"]
    assert not key(f, "ok", snap, 104)
    assert not key(f, "ok", snap, 105, "repeat")


def test_stale_generation_does_not_activate(config, profile, ready_store):
    f = FSM(config, profile, [])
    f.state.id = "home"
    key(f, "ok", ready_store.snapshot(100), generation=99)
    assert f.state.id == "home"


def test_airborne_disarm_blocked(config, profile, ready_store):
    config["safety"]["commands_enabled"] = config["safety"]["arm_enabled"] = True
    ready_store.put("armed", True, 100)
    ready_store.put("landed", 2, 100)
    f = FSM(config, profile, [])
    assert f.guard("disarm", ready_store.snapshot(100)) == ["DISARM BLOCKED"]


def test_countdown_no_effects_then_abort(config, profile, ready_store):
    f = FSM(config, profile, [])
    f.state.id = "count"
    f.state.deadline = 110
    assert not f.tick(ready_store.snapshot(100), 105)
    assert not key(f, "back", ready_store.snapshot(100), 106)
    assert f.state.id == "mdetail"


def test_final_precheck_blocks_stale_countdown(config, profile, ready_store):
    config["safety"]["commands_enabled"] = config["safety"]["mission_enabled"] = True
    f = FSM(config, profile, [])
    f.state.id = "count"
    f.state.deadline = 110
    assert not f.tick(ready_store.snapshot(110), 110)
    assert f.state.id == "alert"


@pytest.mark.parametrize("screen", ["confirm", "count", "run", "mtest", "home"])
def test_emergency_cancels_and_dismiss_does_not_resume_actions(
    config, profile, ready_store, screen
):
    f = FSM(config, profile, [])
    f.state.id = screen
    f.state.focus = 1
    f.state.pending = screen == "confirm"
    effects = f.emergency(EmergencyEvent("RC LOSS", "ACTUAL FC TEXT", 1), 100)
    assert f.state.id == "emergency"
    assert bool(effects) == (screen in ("confirm", "run", "mtest"))
    effects = key(f, "back", ready_store.snapshot(100), 100.01)
    assert [e.kind for e in effects] == ["acknowledge"]
    assert f.state.id not in ("count", "confirm", "mtest")


def test_lost_heartbeat_debounce_and_recovery(config, ready_store):
    d = EmergencyDetector(config, {})
    assert not d.update(ready_store.snapshot(100))
    assert not d.update(ready_store.snapshot(102.5))
    assert d.update(ready_store.snapshot(103.01))[0].kind == "NO FC LINK"
    for now in (104, 105, 106.1):
        ready_store.put("heartbeat", True, now)
        d.update(ready_store.snapshot(now))
    assert "NO FC LINK" not in d.active


def test_text_quote_not_invented(config):
    d = EmergencyDetector(config, {})
    event = d.text(2, "PreArm: Logging failed")
    assert event.text == "PreArm: Logging failed"
    assert d.text(6, "arbitrary info") is None


def test_missing_rc_telemetry_not_rc_loss(config):
    from drone_oled.model import TelemetryStore

    d = EmergencyDetector(config, {})
    assert not d.update(TelemetryStore().snapshot(100))


def test_completion_needs_disarm_and_not_last_seq_alone(config, profile, ready_store):
    items = tuple(
        MissionItem(i, cmd, 6, (0, 0, 0, 0), 0, 0, 3) for i, cmd in enumerate((16, 22, 21))
    )
    mission = MissionDefinition("TEST", items, tuple(i.label() for i in items[1:]))
    f = FSM(config, profile, [mission])
    f.state.id, f.state.phase = "run", "RUNNING"
    ready_store.put("mission_seq", 2, 100)
    ready_store.put("mode", "AUTO", 100)
    f.tick(ready_store.snapshot(100), 100)
    assert f.state.id == "run"
    ready_store.put("mission_reached", 2, 100)
    ready_store.put("mission_state", 5, 100)
    f.tick(ready_store.snapshot(100), 100)
    assert f.state.id == "alert" and f.state.message == "MISSION DONE"
