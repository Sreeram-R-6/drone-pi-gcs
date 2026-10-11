import base64
from http.client import HTTPConnection
import io
import json
import re
from threading import Thread
import time

from PIL import Image
import pytest

from drone_oled.webbridge import Bridge, call
from drone_oled.web import server
from drone_oled.fsm import FSM
from drone_oled.input import KeyEvent
from drone_oled.model import TelemetryStore
from drone_oled.render import render


@pytest.fixture
def bridge(tmp_path):
    bridge = Bridge(str(tmp_path / "ui.sock"))
    bridge.publish(Image.new("1", (128, 64)), 3, "home", True)
    yield bridge
    bridge.close()


def key(ident="first", **kwargs):
    return dict(op="key", key="ok", generation=3, id=ident, **kwargs)


def test_frame_is_pixel_exact(bridge):
    image = Image.new("1", (128, 64))
    image.putpixel((17, 21), 1)
    bridge.publish(image, 3, "home", True)
    frame = call(str(bridge.path), {"op": "frame"})
    decoded = Image.open(io.BytesIO(base64.b64decode(frame["frame"])))
    assert decoded.mode == "1" and decoded.size == (128, 64)
    assert decoded.tobytes() == image.tobytes()
    assert frame["display_ok"] is True and frame["age"] < 1


def test_unique_clicks_and_stale_generation(bridge):
    assert bridge.request(key()) == {"accepted": True}
    assert bridge.request(key())["error"] == "DUPLICATE PRESS"
    wrong = key("second")
    wrong["generation"] = 2
    assert bridge.request(wrong)["error"] == "SCREEN CHANGED"
    bridge.latest["stamp"] = time.monotonic() - 2
    assert bridge.request(key("third"))["error"] == "DISPLAY STALE"
    assert bridge.keys.qsize() == 1


def test_click_goes_through_same_fsm(bridge, config):
    fsm = FSM(config, {}, [])
    fsm.state.id = "home"
    fsm.state.changed_at = 0
    snapshot = TelemetryStore().snapshot(time.monotonic())
    bridge.publish(render(fsm, snapshot, time.monotonic()), fsm.state.generation, "home", True)
    req = key()
    req["generation"] = fsm.state.generation
    assert bridge.request(req)["accepted"]
    virtual, generation, stamp = bridge.keys.get_nowait()
    fsm.key(KeyEvent(virtual, generation=generation), snapshot, time.monotonic())
    assert fsm.state.id == "status"


def test_input_bound_and_rate_limit(bridge):
    assert bridge.request(key())["accepted"]
    assert bridge.request(key("second"))["error"] == "PRESS TOO FAST"
    bridge.last_press = 0
    req = key("third")
    req["key"] = "arm"
    assert bridge.request(req)["error"] == "INVALID KEY"
    assert bridge.keys.qsize() == 1


def test_http_localhost_token_and_frames(bridge):
    http = server(str(bridge.path), 0)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    port = http.server_address[1]
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode()
        assert response.status == 200
        assert page.count("data-key=") == 4
        token = re.search("const token='([^']+)'", page).group(1)
        connection.request("GET", "/api/frame")
        response = connection.getresponse()
        frame = json.loads(response.read())
        assert response.status == 200 and frame["generation"] == 3
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "X-OLED-Token": token,
        }
        request = key()
        request.pop("op")
        connection.request("POST", "/api/key", json.dumps(request), headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["accepted"]
        headers["Origin"] = "http://attacker.invalid"
        connection.request("POST", "/api/key", json.dumps(request), headers)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/", headers={"Host": "attacker.invalid"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert bridge.keys.qsize() == 1
    finally:
        connection.close()
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_four_button_navigation_reaches_all_motor_cells(config):
    fsm = FSM(config, {}, [])
    fsm.state.id = "motors"
    fsm.state.changed_at = 0
    snap = TelemetryStore().snapshot(time.monotonic())
    visited = set()
    for _ in range(4):
        visited.add(fsm.state.motor)
        assert not fsm.key(
            KeyEvent("next", generation=fsm.state.generation), snap, time.monotonic()
        )
    assert visited == {0, 1, 2, 3}
    assert fsm.state.motor == 0


def test_explicit_tailnet_host_works(bridge):
    http = server(str(bridge.path), 0, "127.0.0.1", ("100.64.0.2",))
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    port = http.server_address[1]
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/", headers={"Host": f"100.64.0.2:{port}"})
        response = connection.getresponse()
        page = response.read().decode()
        assert response.status == 200
        assert "crypto.randomUUID()" not in page
        token = re.search("const token='([^']+)'", page).group(1)
        request = key()
        request.pop("op")
        connection.request(
            "POST",
            "/api/key",
            json.dumps(request),
            {
                "Host": f"100.64.0.2:{port}",
                "Origin": f"http://100.64.0.2:{port}",
                "X-OLED-Token": token,
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["accepted"]
    finally:
        connection.close()
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
