from drone_oled.font import wrap_text, width
from drone_oled.fsm import FSM
from drone_oled.input import KeyEvent
from drone_oled.model import TelemetryStore
from drone_oled.emergency import EmergencyEvent
from drone_oled.render import render


def test_wrap_preserves_complete_message_and_pixel_bounds():
    message = "PREARM: SYNTHETIC TEST MESSAGE WITH A_VERY_LONG_SENSOR_IDENTIFIER AND MORE DETAILS"
    lines = wrap_text(message)
    assert "".join(lines).replace(" ", "") == message.replace(" ", "")
    assert all(width(line) <= 124 for line in lines)
    assert all(len(line) <= 20 for line in lines)


def test_alert_pages_stay_until_operator_moves(config):
    fsm = FSM(config, {}, [])
    message = " ".join(["SYNTHETIC PREARM TEST MESSAGE"] * 5)
    fsm.alert(message, 100, title="CANNOT ARM")
    snap = TelemetryStore().snapshot(101)
    first = render(fsm, snap, 101).tobytes()
    assert render(fsm, snap, 150).tobytes() == first
    assert not fsm.key(KeyEvent("down", generation=fsm.state.generation), snap, 101)
    assert fsm.state.page == 1
    assert render(fsm, snap, 101).tobytes() != first
    fsm.key(KeyEvent("back", generation=fsm.state.generation), snap, 101)
    assert fsm.state.id == "home"


def test_fc_text_emergency_shows_full_wrapped_message(config):
    fsm = FSM(config, {}, [])
    message = "PREARM: SYNTHETIC " + ("LONG MESSAGE " * 12)
    fsm.emergency(EmergencyEvent("FC TEXT " + message[:32], message, 1), 100)
    snap = TelemetryStore().snapshot(101)
    first = render(fsm, snap, 101).tobytes()
    assert not fsm.key(KeyEvent("down", generation=fsm.state.generation), snap, 101)
    assert fsm.state.page == 1
    assert render(fsm, snap, 101).tobytes() != first
    assert fsm.state.id == "emergency"
