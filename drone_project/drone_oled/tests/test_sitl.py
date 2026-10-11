"""Opt-in integration with an OWNED simulator process, never an existing FC endpoint.

Set DRONE_OLED_SITL_BINARY to a native ArduCopter SITL executable. Each test uses
an isolated working directory and a unique private loopback UDP port. No serial
device, existing router, physical profile, or live aircraft parameter is touched.
"""

import os
from pathlib import Path
from queue import Queue, Empty
import socket
import subprocess
import time

import pytest

from drone_oled.config import load
from drone_oled.mav import MavLinkWorker
from drone_oled.model import TelemetryStore, readiness
from drone_oled.missions import MissionDefinition, MissionItem


def wait_until(predicate, timeout, detail):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(detail() if callable(detail) else detail)


@pytest.fixture
def simulator(tmp_path):
    binary = os.environ.get("DRONE_OLED_SITL_BINARY")
    if not binary:
        pytest.skip("Native SITL binary absent; set DRONE_OLED_SITL_BINARY to opt in")
    executable = Path(binary).resolve(strict=True)
    from pymavlink import mavutil

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = load()
    config["mavlink"]["endpoint"] = f"udpin:127.0.0.1:{port}"
    config["mavlink"]["ack_timeout_s"] = 5
    config["mavlink"]["state_timeout_s"] = 10
    for key in ("commands_enabled", "arm_enabled", "mission_enabled", "motor_test_enabled"):
        config["safety"][key] = True
    os.environ["MAVLINK20"] = "1"
    connection = mavutil.mavlink_connection(
        config["mavlink"]["endpoint"],
        source_system=253,
        source_component=191,
        dialect="ardupilotmega",
    )
    defaults = tmp_path / "sim.parm"
    # Simulator-only configuration. Never reuse this as a physical aircraft profile.
    defaults.write_text("FRAME_CLASS 1\nFRAME_TYPE 1\nBATT_MONITOR 4\nBATT_CAPACITY 5000\n")
    log = (tmp_path / "sitl.log").open("w")
    process = subprocess.Popen(
        [
            str(executable),
            "--model",
            "quad",
            "--home=-35.363261,149.165230,584,353",
            "--speedup=2",
            f"--serial0=udpclient:127.0.0.1:{port}",
            "--defaults",
            str(defaults),
            "--wipe",
        ],
        cwd=tmp_path,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    store = TelemetryStore()
    profile = {
        "approved": True,
        "flight_sw_version": 0,
        "board_version": 0,
        "uid2": "",
        "min_voltage_v": 10,
        "max_ekf_variance": 1,
        "motor_positions": ["RR", "FR", "RL", "FL"],
        "arming_mode": "GUIDED",
        "recovery_reviewed": True,
    }
    events = Queue()
    worker = MavLinkWorker(
        config, profile, store, lambda *event: events.put(event), connect=lambda: connection
    )
    worker.start()
    try:
        wait_until(
            lambda: store.snapshot(time.monotonic()).cached("version") is not None,
            30,
            lambda: "No simulator identity; inspect " + str(tmp_path / "sitl.log"),
        )
        snap = store.snapshot(time.monotonic())
        profile.update(
            flight_sw_version=snap.cached("version"),
            board_version=snap.cached("board"),
            uid2=snap.cached("uid2"),
        )
        wait_until(
            lambda: not readiness(store.snapshot(time.monotonic()), config, profile),
            90,
            lambda: (
                "SITL readiness: "
                + str(readiness(store.snapshot(time.monotonic()), config, profile))
            ),
        )
        yield worker, store, events, config
    finally:
        worker.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


def request(worker, events, kind, data=None, timeout=60):
    assert worker.submit(kind, data)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event, payload = events.get(timeout=0.1)
        except Empty:
            continue
        if event == "operation" and payload[0] == kind and payload[1] != "progress":
            assert payload[1] == "success", payload
            return payload
    pytest.fail("SITL operation timeout: " + kind)


def test_sitl_arm_disarm_and_motor_timeout(simulator):
    worker, store, events, config = simulator
    request(worker, events, "arm")
    assert store.snapshot(time.monotonic()).get("armed") is True
    request(worker, events, "disarm")
    assert store.snapshot(time.monotonic()).get("armed") is False
    request(worker, events, "motor_start", 1)
    request(worker, events, "motor_stop", 1)
    assert store.snapshot(time.monotonic()).get("armed") is False


def test_sitl_mission_upload_start_and_abort(simulator):
    worker, store, events, config = simulator
    snap = store.snapshot(time.monotonic())
    lat, lon = round(snap.get("lat") * 1e7), round(snap.get("lon") * 1e7)
    items = tuple(
        MissionItem(i, cmd, 6, params, lat, lon, alt)
        for i, (cmd, params, alt) in enumerate(
            [
                (16, (0.0, 0.0, 0.0, 0.0), 0.0),
                (22, (0.0, 0.0, 0.0, 0.0), 3.0),
                (19, (30.0, 0.0, 0.0, 0.0), 3.0),
                (21, (0.0, 0.0, 0.0, 0.0), 0.0),
            ]
        )
    )
    mission = MissionDefinition("SITL HOVER", items, tuple(i.label() for i in items[1:]))
    request(worker, events, "mission", mission)
    assert store.snapshot(time.monotonic()).get("mode") == "AUTO"
    wait_until(
        lambda: (store.snapshot(time.monotonic()).get("alt") or 0) > 1.5, 30, "SITL did not climb"
    )
    request(worker, events, "abort")
    assert store.snapshot(time.monotonic()).get("mode") in ("RTL", "LAND")
