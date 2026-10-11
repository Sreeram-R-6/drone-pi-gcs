from collections import deque
from threading import Event
from types import SimpleNamespace
import time
import pytest
from drone_oled.mav import (
    CommandClient,
    OutcomeUnknown,
    CommandRejected,
    Cancelled,
    MissionTransfer,
    MavLinkWorker,
)
from drone_oled.model import TelemetryStore
from drone_oled.missions import MissionDefinition, MissionItem


def client(config, ack=0, observe=None):
    config["mavlink"]["ack_timeout_s"] = 0.03
    config["mavlink"]["state_timeout_s"] = 0.03
    store = TelemetryStore()
    pending = deque()
    sent = []

    def send(ts, tc, cmd, confirmation, *params):
        sent.append((cmd, params))
        if ack is not None:
            pending.append(
                {
                    "mavpackettype": "COMMAND_ACK",
                    "command": cmd,
                    "result": ack,
                    "target_system": config["mavlink"]["source_system"],
                }
            )

    cl = None

    def pump(timeout):
        if pending:
            cl.observe(pending.popleft(), time.monotonic())
        if observe:
            observe(store)

    connection = SimpleNamespace(mav=SimpleNamespace(command_long_send=send))
    cl = CommandClient(connection, config, store, pump, Event(), Event())
    return cl, sent


def test_arm_ack_and_heartbeat_both_required(config):
    cl, sent = client(config)
    with pytest.raises(OutcomeUnknown, match="STATE TIMEOUT"):
        cl.command(400, (1, 0), lambda s: s.get("armed") is True)
    assert sent == [(400, (1, 0, 0, 0, 0, 0, 0))]


def test_ack_plus_observed_success(config):
    cl, sent = client(config, observe=lambda store: store.put("armed", True, time.monotonic()))
    assert cl.command(400, (1, 0), lambda s: s.get("armed") is True)["result"] == 0
    assert len(sent) == 1


def test_no_ack_even_if_state_matches(config):
    cl, sent = client(
        config, ack=None, observe=lambda store: store.put("armed", True, time.monotonic())
    )
    with pytest.raises(OutcomeUnknown, match="ACK TIMEOUT"):
        cl.command(400, (1, 0), lambda s: s.get("armed") is True)
    assert len(sent) == 1


@pytest.mark.parametrize("result", [1, 2, 3, 4, 6, 255])
def test_rejections_not_resent(config, result):
    cl, sent = client(config, ack=result)
    with pytest.raises(CommandRejected, match="REASON UNAVAILABLE"):
        cl.command(400, (1, 0))
    assert len(sent) == 1


def test_cancellation_before_send(config):
    cl, sent = client(config)
    cl.cancel.set()
    with pytest.raises(Cancelled):
        cl.command(400, (1, 0))
    assert not sent


def test_wrong_target_ack_ignored(config):
    cl, sent = client(config, ack=None)
    once = []

    def pump(_):
        if not once:
            cl.observe(
                {"mavpackettype": "COMMAND_ACK", "command": 400, "result": 0, "target_system": 99},
                time.monotonic(),
            )
            once.append(True)

    cl.pump = pump
    with pytest.raises(OutcomeUnknown):
        cl.command(400, (1, 0))


def test_worker_gate_blocks_before_any_command(config, profile, ready_store):
    w = MavLinkWorker(config, profile, ready_store, lambda *args: None)
    cl, _ = client(config)
    w.client = cl
    with pytest.raises(CommandRejected, match="DISABLED"):
        w.execute("arm", None)


def test_stop_no_force_disarm_command(config):
    cl, sent = client(config)
    w = MavLinkWorker(config, {}, cl.store, lambda *args: None)
    w.client = cl
    cl.store.put("motor_deadline", time.monotonic() - 0.5, time.monotonic())
    w.motor_stop(2)
    assert sent == [(209, (2, 0, 0, 0, 1, 0, 0))]


def test_transfer_request_response_and_readback(config):
    config["missions"]["transfer_timeout_s"] = 0.01
    items = tuple(
        MissionItem(i, cmd, 6, (0, 0, 0, 0), 100, 200, 3.0 if i else 0)
        for i, cmd in enumerate((16, 22, 21))
    )
    mission = MissionDefinition("TEST", items, tuple(i.label() for i in items[1:]))
    pending = deque()
    sent = []
    cl = None

    def count(ts, tc, n, mission_type):
        pending.append({"mavpackettype": "MISSION_REQUEST_INT", "seq": 0})

    def item_send(
        ts, tc, seq, frame, cmd, current, autocontinue, p1, p2, p3, p4, x, y, z, mission_type
    ):
        sent.append(seq)
        pending.append(
            {"mavpackettype": "MISSION_REQUEST_INT", "seq": seq + 1}
            if seq + 1 < len(items)
            else {"mavpackettype": "MISSION_ACK", "type": 0}
        )

    def request_list(*_):
        pending.append({"mavpackettype": "MISSION_COUNT", "count": len(items)})

    def request_item(ts, tc, seq, mission_type):
        item = items[seq]
        pending.append(
            {
                "mavpackettype": "MISSION_ITEM_INT",
                "seq": seq,
                "command": item.command,
                "frame": item.frame,
                "x": item.x,
                "y": item.y,
                "z": item.z,
                **{f"param{i + 1}": v for i, v in enumerate(item.params)},
            }
        )

    def pump(_):
        if pending:
            cl.observe(pending.popleft(), time.monotonic())

    mav = SimpleNamespace(
        mission_count_send=count,
        mission_item_int_send=item_send,
        mission_request_list_send=request_list,
        mission_request_int_send=request_item,
        mission_ack_send=lambda *_: None,
    )
    cl = CommandClient(SimpleNamespace(mav=mav), config, TelemetryStore(), pump, Event(), Event())
    MissionTransfer(cl).upload(mission)
    assert sent == [0, 1, 2]


def test_transfer_invalid_request_blocks(config):
    config["missions"]["transfer_timeout_s"] = 0.01
    items = tuple(
        MissionItem(i, cmd, 6, (0, 0, 0, 0), 100, 200, 3.0) for i, cmd in enumerate((16, 22, 21))
    )
    mission = MissionDefinition("TEST", items, tuple(i.label() for i in items[1:]))
    cl = None
    pending = deque()

    def count(*_):
        pending.append({"mavpackettype": "MISSION_REQUEST_INT", "seq": 999})

    def pump(_):
        if pending:
            cl.observe(pending.popleft(), time.monotonic())

    cl = CommandClient(
        SimpleNamespace(mav=SimpleNamespace(mission_count_send=count)),
        config,
        TelemetryStore(),
        pump,
        Event(),
        Event(),
    )
    with pytest.raises(CommandRejected, match="OUT OF RANGE"):
        MissionTransfer(cl).upload(mission)
