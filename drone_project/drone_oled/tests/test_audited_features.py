import json
import pytest
from drone_oled.config import load_profile, ConfigError
from drone_oled.fsm import FSM
from drone_oled.input import KeyEvent


def test_four_button_failsafe_edit_and_enum_skip(config, profile, ready_store):
    config["safety"].update(commands_enabled=True, parameter_write_enabled=True)
    profile["parameters"] = {
        "FS_THR_ENABLE": dict(min=1, max=7, step=1, type=2, values=[1, 3, 4, 5, 6, 7])
    }
    ready_store.put("param_FS_THR_ENABLE", (1, 2), 100)
    fsm = FSM(config, profile, [])
    fsm.state.id = "failsafe"
    fsm.state.changed_at = 0

    def press(key):
        return fsm.key(
            KeyEvent(key, generation=fsm.state.generation), ready_store.snapshot(100), 100
        )

    assert not press("ok")
    assert fsm.state.draft == ("FS_THR_ENABLE", 1)
    assert not press("down")
    assert fsm.state.draft == ("FS_THR_ENABLE", 3)
    assert fsm.state.focus == 0
    assert not press("back")
    assert fsm.state.draft is None and fsm.state.id == "failsafe"
    press("ok")
    press("down")
    press("ok")
    assert fsm.state.id == "confirm" and fsm.state.focus == 0
    assert fsm.state.confirm_kind == "write"


@pytest.mark.parametrize("values", [[1, 2, 1], [], [8], [float("inf")], ["bad"]])
def test_profile_rejects_invalid_parameter_enums(config, profile, tmp_path, values):
    profile["parameters"] = {"FS_THR_ENABLE": dict(min=1, max=7, step=1, type=2, values=values)}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    config["safety"]["firmware_profile"] = str(path)
    with pytest.raises(ConfigError, match="enum"):
        load_profile(config)
