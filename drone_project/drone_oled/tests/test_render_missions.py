import json
from pathlib import Path
import pytest
from PIL import Image
from drone_oled.app import SCREENS
from drone_oled.font import FONT, fit, width
from drone_oled.fsm import FSM
from drone_oled.gfx import Canvas
from drone_oled.hardware import Display
from drone_oled.missions import MissionStore, MissionInvalid
from drone_oled.model import TelemetryStore
from drone_oled.render import render, splash

ROOT = Path(__file__).resolve().parents[1]


def test_font_authoritative():
    assert len(FONT) == 59
    assert FONT["A"] == bytes.fromhex("7E1111117E")
    assert width("ARMED", 3) == 87 and width("10", 5) == 55
    assert fit("X" * 21, 128) and not fit("X" * 22, 128)
    assert not fit("é", 128)


def test_corners():
    c = Canvas()
    c.block(2, 3, 20, 10, True)
    assert not c.image.getpixel((2, 3))
    assert c.image.getpixel((3, 3))


@pytest.mark.parametrize("screen", SCREENS)
@pytest.mark.parametrize("stale", [False, True])
def test_every_screen_with_missing_and_live_data(config, profile, ready_store, screen, stale):
    missions = MissionStore(ROOT / "missions").load_all()
    f = FSM(config, profile, missions)
    f.state.id = screen
    f.state.confirm_kind = "arm"
    f.state.deadline = 110
    for page in range(5) if screen == "status" else (0,):
        f.state.page = page
        image = render(f, ready_store.snapshot(110 if stale else 100), 100)
        assert image.mode == "1" and image.size == (128, 64)


def test_splash_real_asset_and_cold_boot(config, profile):
    f = FSM(config, profile, [])
    image = render(f, TelemetryStore().snapshot(0), 0)
    expected = splash(str(ROOT / "src/drone_oled/assets/splash.png"))
    assert image.tobytes() == expected.tobytes()
    assert expected.getbbox() == (0, 0, 128, 64)


def test_sample_missions_are_never_executable():
    store = MissionStore(ROOT / "missions")
    assert len(store.load_all()) == 5 and not store.errors
    assert all(not m.executable and m.errors == ("DISPLAY ONLY",) for m in store.missions)


def canonical():
    return {
        "name": "HOVER TEST",
        "items": [
            {"command": 16, "frame": 0, "params": [0, 0, 0, 0, 10.2, 76.3, 0]},
            {"command": 22, "frame": 3, "params": [0, 0, 0, 0, 10.2, 76.3, 3]},
            {"command": 19, "frame": 3, "params": [30, 0, 0, 0, 10.2, 76.3, 3]},
            {"command": 21, "frame": 3, "params": [0, 0, 0, 0, 10.2, 76.3, 0]},
        ],
    }


def test_explicit_mission_coordinates(tmp_path):
    path = tmp_path / "test.json"
    path.write_text(json.dumps(canonical()))
    mission = MissionStore(tmp_path).load(path)
    assert mission.executable
    assert mission.items[1].frame == 6
    assert mission.items[1].x == 102000000
    assert mission.steps[0] == ("TAKEOFF", "ALT 3M")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(name="lowercase"),
        lambda d: d["items"][1].update(params=[0, 0, 0, 0, 100, 0, 3]),
        lambda d: d["items"][1].update(frame=10),
        lambda d: d["items"][1].update(params=[0, 0, 0, 0, 0, 0, float("nan")]),
    ],
)
def test_malformed_mission_rejected(tmp_path, mutate):
    data = canonical()
    mutate(data)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(MissionInvalid):
        MissionStore(tmp_path).load(path)


def test_unsupported_command_visible_but_not_executable(tmp_path):
    data = canonical()
    data["items"][2]["command"] = 999
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    mission = MissionStore(tmp_path).load(path)
    assert mission.steps[1][0] == "CMD 999"
    assert not mission.executable


def test_complex_qgc_rejected(tmp_path):
    data = {
        "fileType": "Plan",
        "version": 1,
        "name": "GRID",
        "mission": {
            "version": 2,
            "firmwareType": 3,
            "vehicleType": 2,
            "plannedHomePosition": [10, 76, 0],
            "items": [{"type": "ComplexItem"}, {"type": "ComplexItem"}],
        },
    }
    path = tmp_path / "grid.plan"
    path.write_text(json.dumps(data))
    with pytest.raises(MissionInvalid, match="complex"):
        MissionStore(tmp_path).load(path)


def test_qgc_simple_import_null_yaw_and_mission_frame(tmp_path):
    import math

    data = {
        "fileType": "Plan",
        "version": 1,
        "name": "QGC HOVER",
        "mission": {
            "version": 2,
            "firmwareType": 3,
            "vehicleType": 2,
            "plannedHomePosition": [10.2, 76.3, 10],
            "items": [
                {
                    "type": "SimpleItem",
                    "command": 22,
                    "frame": 3,
                    "autoContinue": True,
                    "params": [0, 0, 0, None, 10.2, 76.3, 3],
                },
                {
                    "type": "SimpleItem",
                    "command": 178,
                    "frame": 2,
                    "autoContinue": True,
                    "params": [1, 2, 0, 0, 0, 0, 0],
                },
                {
                    "type": "SimpleItem",
                    "command": 21,
                    "frame": 3,
                    "autoContinue": True,
                    "params": [0, 0, 0, None, 10.2, 76.3, 0],
                },
            ],
        },
    }
    path = tmp_path / "hover.plan"
    path.write_text(json.dumps(data))
    mission = MissionStore(tmp_path).load(path)
    assert mission.executable and math.isnan(mission.items[1].params[3])
    assert mission.items[2].frame == 2 and mission.items[2].x == 0
    data["geoFence"] = {"polygons": [{"polygon": [[1, 2], [2, 3], [3, 4]]}]}
    path.write_text(json.dumps(data))
    with pytest.raises(MissionInvalid, match="safety data"):
        MissionStore(tmp_path).load(path)


def test_offline_replay_refuses_devices_and_urls():
    from drone_oled.app import replay

    for path in (Path("/dev/null"), Path("udpin:127.0.0.1:14551")):
        with pytest.raises(ValueError, match="regular .tlog"):
            replay(path, TelemetryStore())


def test_display_dirty_and_recovery(config):
    calls = []

    class Device:
        def contrast(self, x):
            pass

        def display(self, image):
            calls.append(image)

        def cleanup(self):
            pass

    display = Display(config["display"], Device)
    frame = Image.new("1", (128, 64))
    assert display.show(frame, 1)
    assert not display.show(frame, 2)
    frame.putpixel((2, 2), 1)
    assert display.show(frame, 3)
    display.close()
    assert len(calls) == 3


def test_i2c_backoff_does_not_stop_app(config):
    tries = []

    def fail():
        tries.append(True)
        raise OSError("NACK")

    display = Display(config["display"], fail)
    frame = Image.new("1", (128, 64))
    assert not display.show(frame, 1)
    assert not display.show(frame, 1.1)
    assert not display.show(frame, 1.3)
    assert len(tries) == 2
