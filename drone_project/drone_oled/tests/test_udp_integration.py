"""Real MAVLink packets over an owned loopback socket; no physical FC access."""

import os
from queue import Queue, Empty
import socket
from threading import Event, Thread
import time

from drone_oled.mav import MavLinkWorker
from drone_oled.model import TelemetryStore


def test_udp_router_contract_and_disabled_motor_never_sends_stop(config):
    os.environ["MAVLINK20"] = "1"
    from pymavlink import mavutil

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config["mavlink"]["endpoint"] = f"udpin:127.0.0.1:{port}"
    config["mavlink"]["ack_timeout_s"] = 0.2
    fc = mavutil.mavlink_connection(
        f"udpout:127.0.0.1:{port}", source_system=1, source_component=1, dialect="ardupilotmega"
    )
    stop = Event()
    received = []

    def fake_fc():
        next_hb = 0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_hb:
                fc.mav.heartbeat_send(2, 3, 0, 5, 3)
                next_hb = now + 0.1
            message = fc.recv_match(blocking=True, timeout=0.01)
            if message is not None and message.get_type() == "COMMAND_LONG":
                received.append(message.command)
                fc.mav.command_ack_send(message.command, 0, 0, 0, 253, 191)
                if message.command == 512:
                    fc.mav.autopilot_version_send(
                        0, 67568127, 0, 0, 76218368, [0] * 8, [0] * 8, [0] * 8, 0, 0, 0, [1] * 18
                    )

    peer = Thread(target=fake_fc, daemon=True)
    store = TelemetryStore()
    events = Queue()
    worker = MavLinkWorker(config, {}, store, lambda *e: events.put(e))
    worker.start()
    peer.start()
    try:
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and store.snapshot(time.monotonic()).cached("version") is None
        ):
            time.sleep(0.02)
        assert store.snapshot(time.monotonic()).cached("version") == 67568127
        assert worker.submit("motor_start", 1)
        outcome = None
        while time.monotonic() < deadline:
            try:
                kind, data = events.get(timeout=0.1)
            except Empty:
                continue
            if kind == "operation" and data[0] == "motor_start":
                outcome = data
                break
        assert outcome and outcome[1] == "failure" and "DISABLED" in outcome[2]
        assert 511 in received and 512 in received
        assert not {209, 400, 176, 224, 300} & set(received)
    finally:
        worker.close()
        stop.set()
        peer.join(timeout=1)
        fc.close()
