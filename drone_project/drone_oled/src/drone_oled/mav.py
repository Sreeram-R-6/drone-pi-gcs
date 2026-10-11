"""Single-owner MAVLink worker and bounded, observed-state transactions."""

from __future__ import annotations

from collections import deque
import logging
import math
import os
from queue import Empty, Queue
from threading import Event, Lock, Thread
import time
from typing import Any, Callable

from .model import TelemetryStore, identity_ok, readiness, MODE_IDS, fc_arm_available
from .missions import MissionDefinition

LOG = logging.getLogger(__name__)
RATES = {1: 2, 24: 2, 33: 5, 74: 2, 30: 10, 147: 2, 193: 2, 65: 2, 42: 2, 245: 2, 242: 1, 162: 1}


class CommandRejected(RuntimeError):
    pass


class OutcomeUnknown(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class CommandClient:
    """Only the MAVLink worker calls this class; pump still ingests telemetry while waiting."""

    def __init__(
        self,
        connection,
        config: dict,
        store: TelemetryStore,
        pump: Callable,
        cancel: Event,
        stop: Event,
    ):
        self.connection, self.c, self.store = connection, config, store
        self.pump, self.cancel, self.stop = pump, cancel, stop
        self.inbox: deque[tuple[float, dict]] = deque(maxlen=1024)
        self.busy: set[int] = set()
        self.recent_text: tuple[float, str] = (0, "")

    def observe(self, data: dict, now: float):
        self.inbox.append((now, data))
        if data.get("mavpackettype") == "STATUSTEXT":
            self.recent_text = now, data.get("text", "")

    def check(self, cancellable: bool = True):
        if self.stop.is_set() or (cancellable and self.cancel.is_set()):
            raise Cancelled("FOLLOW-UP CANCELLED")

    def send(self, command: int, params: tuple, confirmation: int = 0):
        m = self.c["mavlink"]
        p = tuple(params) + (0,) * (7 - len(params))
        LOG.info("TX COMMAND id=%s params=%s confirmation=%s", command, p, confirmation)
        self.connection.mav.command_long_send(
            m["target_system"], m["target_component"], command, confirmation, *p
        )

    def command(
        self,
        command: int,
        params: tuple = (),
        observed: Callable | None = None,
        timeout: float | None = None,
        cancellable: bool = True,
    ) -> dict:
        self.check(cancellable)
        if command in self.busy:
            raise CommandRejected("COMMAND ALREADY PENDING")
        self.busy.add(command)
        started = time.monotonic()
        self.inbox.clear()
        self.send(command, params)
        deadline = started + (timeout or self.c["mavlink"]["ack_timeout_s"])
        hard_deadline = started + max(10, timeout or 0)
        accepted = None
        try:
            while time.monotonic() < deadline:
                self.check(cancellable)
                self.pump(0.02)
                for stamp, data in list(self.inbox):
                    if (
                        stamp < started
                        or data.get("mavpackettype") != "COMMAND_ACK"
                        or data.get("command") != command
                    ):
                        continue
                    # MAVLink2 ACK target fields may be absent/zero on legacy FCs.
                    m = self.c["mavlink"]
                    if data.get("target_system", 0) not in (0, m["source_system"]) or data.get(
                        "target_component", 0
                    ) not in (0, m["source_component"]):
                        continue
                    self.inbox.remove((stamp, data))
                    result = data["result"]
                    LOG.info(
                        "ACK id=%s result=%s progress=%s", command, result, data.get("progress")
                    )
                    if result == 5:
                        deadline = min(
                            hard_deadline, time.monotonic() + self.c["mavlink"]["ack_timeout_s"]
                        )
                    elif result == 0:
                        accepted = data
                        break
                    else:
                        recent = (
                            self.recent_text[1]
                            if time.monotonic() - self.recent_text[0] <= 5
                            else "REASON UNAVAILABLE"
                        )
                        raise CommandRejected(f"FC RESULT {result}: {recent}")
                if accepted:
                    break
            if accepted is None:
                raise OutcomeUnknown(f"ACK TIMEOUT CMD {command}; OUTCOME UNKNOWN")
            if observed:
                until = time.monotonic() + self.c["mavlink"]["state_timeout_s"]
                while time.monotonic() < until:
                    self.check(cancellable)
                    if observed(self.store.snapshot(time.monotonic())):
                        return accepted
                    self.pump(0.02)
                raise OutcomeUnknown(f"STATE TIMEOUT CMD {command}; OUTCOME UNKNOWN")
            return accepted
        finally:
            self.busy.discard(command)

    def wait_message(
        self,
        kind: str,
        predicate: Callable = lambda d: True,
        timeout: float = 2,
        cancellable: bool = True,
    ) -> dict | None:
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.check(cancellable)
            for stamp, data in list(self.inbox):
                if data.get("mavpackettype") == kind and predicate(data):
                    self.inbox.remove((stamp, data))
                    return data
            self.pump(0.02)
        return None


class MissionTransfer:
    def __init__(self, client: CommandClient):
        self.client = client

    def upload(self, mission: MissionDefinition):
        if not mission.executable:
            raise CommandRejected("MISSION NOT EXECUTABLE")
        cl, c = self.client, self.client.c
        ts, tc = c["mavlink"]["target_system"], c["mavlink"]["target_component"]
        timeout = c["missions"]["transfer_timeout_s"]
        attempts = c["missions"]["transfer_attempts"]
        deadline = time.monotonic() + c["missions"]["transfer_total_s"]
        cl.inbox.clear()
        cl.connection.mav.mission_count_send(ts, tc, len(mission.items), 0)
        last_sent = time.monotonic()
        sent: set[int] = set()
        last_item = None
        failures = 0
        while time.monotonic() < deadline:
            # Consume ACKs without waiting an entire request timeout at the end.
            cl.check()
            cl.pump(0.02)
            request = None
            ack = None
            for stamp, data in list(cl.inbox):
                kind = data.get("mavpackettype")
                if kind not in ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"):
                    continue
                cl.inbox.remove((stamp, data))
                if data.get("mission_type", 0) != 0 or data.get("target_system", 0) not in (
                    0,
                    c["mavlink"]["source_system"],
                ):
                    continue
                if kind == "MISSION_ACK":
                    ack = data
                elif kind == "MISSION_REQUEST":
                    raise CommandRejected("FC REQUESTED LEGACY MISSION; INT REQUIRED")
                else:
                    request = data
            if ack:
                if ack["type"] != 0 or len(sent) != len(mission.items):
                    raise CommandRejected(f"MISSION ACK {ack['type']} / INCOMPLETE")
                self.verify(mission, deadline)
                return
            if request:
                seq = request["seq"]
                if not isinstance(seq, int) or not 0 <= seq < len(mission.items):
                    raise CommandRejected("MISSION REQUEST OUT OF RANGE")
                last_item = mission.items[seq]
                self._send(last_item)
                sent.add(seq)
                failures = 0
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= timeout:
                failures += 1
                if failures >= attempts:
                    raise OutcomeUnknown("MISSION TRANSFER TIMEOUT; FC MISSION MAY BE MODIFIED")
                if last_item:
                    self._send(last_item)
                else:
                    cl.connection.mav.mission_count_send(ts, tc, len(mission.items), 0)
                last_sent = time.monotonic()
        raise OutcomeUnknown("MISSION TRANSFER DEADLINE")

    def _send(self, item):
        cl, m = self.client, self.client.c["mavlink"]
        LOG.info("TX MISSION_ITEM seq=%s command=%s", item.seq, item.command)
        cl.connection.mav.mission_item_int_send(
            m["target_system"],
            m["target_component"],
            item.seq,
            item.frame,
            item.command,
            0,
            1,
            *item.params,
            item.x,
            item.y,
            item.z,
            0,
        )

    def verify(self, mission: MissionDefinition, deadline: float | None = None):
        cl, m = self.client, self.client.c["mavlink"]
        ts, tc = m["target_system"], m["target_component"]
        deadline = deadline or time.monotonic() + cl.c["missions"]["transfer_total_s"]
        cl.inbox.clear()
        cl.connection.mav.mission_request_list_send(ts, tc, 0)
        count = cl.wait_message("MISSION_COUNT", lambda d: d.get("mission_type", 0) == 0)
        if not count or count["count"] != len(mission.items):
            raise CommandRejected("MISSION READBACK COUNT MISMATCH")
        for item in mission.items:
            if time.monotonic() >= deadline:
                raise OutcomeUnknown("MISSION READBACK TOTAL DEADLINE")
            readback = None
            for _ in range(cl.c["missions"]["transfer_attempts"]):
                cl.connection.mav.mission_request_int_send(ts, tc, item.seq, 0)
                readback = cl.wait_message(
                    "MISSION_ITEM_INT",
                    lambda d, seq=item.seq: d["seq"] == seq and d.get("mission_type", 0) == 0,
                    timeout=min(2, max(0, deadline - time.monotonic())),
                )
                if readback:
                    break
            if not readback:
                raise OutcomeUnknown("MISSION READBACK TIMEOUT")
            if readback["command"] != item.command:
                raise CommandRejected("MISSION READBACK COMMAND MISMATCH")
            # FC owns home item zero; ArduPilot may replace its coordinates.
            if item.seq == 0:
                continue
            frame = {0: 5, 3: 6}.get(readback["frame"], readback["frame"])
            floats = [readback[f"param{i}"] for i in range(1, 5)] + [readback["z"]]
            expected = list(item.params) + [item.z]
            if (
                frame != item.frame
                or readback["x"] != item.x
                or readback["y"] != item.y
                or any(
                    not (
                        (math.isnan(a) and math.isnan(b))
                        or math.isclose(a, b, abs_tol=1e-4, rel_tol=1e-5)
                    )
                    for a, b in zip(floats, expected, strict=True)
                )
            ):
                raise CommandRejected(f"MISSION READBACK MISMATCH {item.seq}")
        cl.connection.mav.mission_ack_send(ts, tc, 0, 0)


class MavLinkWorker(Thread):
    def __init__(
        self,
        config: dict,
        profile: dict,
        store: TelemetryStore,
        emit: Callable,
        connect: Callable | None = None,
    ):
        super().__init__(name="mavlink", daemon=True)
        self.c, self.profile, self.store, self.emit = config, profile, store, emit
        self.connect = connect
        self.tasks: Queue = Queue(maxsize=8)
        self.stop_event, self.cancel_event = Event(), Event()
        self.connection: Any = None
        self.client: CommandClient | None = None
        self.expected_modes: set[str] = set()
        self.expected_until = 0.0
        self.operation = ""
        self.motor_active_sequence: int | None = None
        self.uncertain = False
        self.received_identity: tuple | None = None
        self._submission_lock = Lock()
        self._occupied = False
        self._chunks: dict[int, tuple[float, int, str, int]] = {}
        self.params: dict[str, tuple[float, int, float]] = {}

    def submit(self, kind: str, data: Any = None) -> bool:
        with self._submission_lock:
            if self._occupied:
                return False
            self._occupied = True
            self.cancel_event.clear()
            self.tasks.put_nowait((kind, data))
            return True

    def cancel(self):
        self.cancel_event.set()

    def snapshot(self):
        return self.store.snapshot(time.monotonic())

    def require_client(self) -> CommandClient:
        if self.client is None:
            raise CommandRejected("NO FC LINK")
        return self.client

    def _open(self):
        if self.connect:
            return self.connect()
        os.environ["MAVLINK20"] = "1"
        from pymavlink import mavutil

        m = self.c["mavlink"]
        return mavutil.mavlink_connection(
            m["endpoint"],
            dialect="ardupilotmega",
            source_system=m["source_system"],
            source_component=m["source_component"],
        )

    def pump(self, timeout: float = 0.02):
        message = self.connection.recv_match(blocking=True, timeout=timeout)
        if message is None or message.get_type() == "BAD_DATA":
            return
        m = self.c["mavlink"]
        if (message.get_srcSystem(), message.get_srcComponent()) != (
            m["target_system"],
            m["target_component"],
        ):
            return
        now = time.monotonic()
        data = message.to_dict()
        if data.get("target_system", 0) not in (0, m["source_system"]) or data.get(
            "target_component", 0
        ) not in (0, m["source_component"]):
            return
        if data["mavpackettype"] == "STATUSTEXT":
            text = data.get("text", "")
            if isinstance(text, bytes):
                text = text.decode("ascii", "replace")
            text = text.split("\0", 1)[0]
            ident, seq = data.get("id", 0), data.get("chunk_seq", 0)
            if ident:
                self._chunks = {k: v for k, v in self._chunks.items() if now - v[0] <= 2}
                if seq == 0:
                    self._chunks[ident] = (now, 1, text, data["severity"])
                else:
                    old = self._chunks.get(ident)
                    if not old or old[1] != seq:
                        self._chunks.pop(ident, None)
                        return
                    self._chunks[ident] = (now, seq + 1, old[2] + text, old[3])
                if len(text) == 50:
                    return
                _, _, text, severity = self._chunks.pop(ident)
                data["severity"] = severity
            data["text"] = text
            LOG.info("FC TEXT severity=%s text=%s", data["severity"], text)
            self.emit("text", data)
        reboot = self.store.update(data, now, self.c["battery"]["id"])
        if data["mavpackettype"] == "PARAM_VALUE":
            if not math.isfinite(data["param_value"]):
                LOG.warning("Invalid nonfinite parameter response")
                return
            name = str(data["param_id"]).split("\0", 1)[0]
            entry = (data["param_value"], data["param_type"], now)
            self.params[name] = entry
            self.store.put("param_" + name, entry[:2], now, "PARAM_VALUE")
        if data["mavpackettype"] == "MAG_CAL_PROGRESS" and self.operation == "calibrate":
            self.emit("operation", ("calibrate", "progress", f"COMPASS {data['completion_pct']}%"))
        if data["mavpackettype"] == "AUTOPILOT_VERSION":
            identity = (
                data["flight_sw_version"],
                data["board_version"],
                bytes(data.get("uid2", [])).hex(),
            )
            if self.received_identity and self.received_identity != identity:
                reboot = True
                self.store.invalidate()
                self.store.update(data, now, self.c["battery"]["id"])
            self.received_identity = identity
        if reboot:
            self.uncertain = True
            self.cancel_event.set()
            self.emit("reboot", None)
        if self.client:
            self.client.observe(data, now)

    def _guard(self, kind: str):
        self.require_client().check()
        if not self.c["safety"]["commands_enabled"]:
            raise CommandRejected("CONTROLS DISABLED")
        s = self.snapshot()
        delegated = fc_arm_available(s, self.c, kind)
        if not delegated and not identity_ok(s, self.profile):
            raise CommandRejected("FC IDENTITY UNREADY")
        if self.uncertain and kind not in ("abort", "land", "motor_stop", "disarm"):
            raise CommandRejected("UNKNOWN PRIOR OUTCOME; RESTART AFTER RECONCILIATION")
        flag = {
            "arm": "arm_enabled",
            "disarm": "arm_enabled",
            "mission": "mission_enabled",
            "motor_start": "motor_test_enabled",
            "calibrate": "calibration_enabled",
            "mag_accept": "calibration_enabled",
            "write_param": "parameter_write_enabled",
        }.get(kind)
        if flag and not self.c["safety"][flag]:
            raise CommandRejected("FEATURE DISABLED")
        if kind in ("arm", "mission") and not (delegated and kind == "arm"):
            reasons = readiness(s, self.c, self.profile)
            if reasons:
                raise CommandRejected("; ".join(reasons))
        if kind in ("arm", "mission", "motor_start", "calibrate", "mag_accept", "write_param"):
            if s.get("armed") is not False or s.get("landed") != 1:
                raise CommandRejected("MUST BE DISARMED AND LANDED")

    def mode(self, name: str, cancellable: bool = True):
        self.expected_modes.add(name)
        self.expected_until = time.monotonic() + self.c["mavlink"]["state_timeout_s"] + 3
        self.require_client().command(
            176, (1, MODE_IDS[name], 0), lambda s: s.get("mode") == name, cancellable=cancellable
        )

    def _arm(self, arm: bool, cancellable: bool = True):
        if not arm and self.snapshot().get("landed") != 1:
            raise CommandRejected("DISARM BLOCKED: NOT CONFIRMED LANDED")
        self.require_client().command(
            400, (int(arm), 0), lambda s: s.get("armed") is arm, cancellable=cancellable
        )

    def recovery(self):
        s = self.snapshot()
        if s.get("armed") is False:
            return "VEHICLE DISARMED; NO RECOVERY SENT"
        if s.get("armed") is not True:
            raise OutcomeUnknown("ARM STATE UNKNOWN; RECOVERY NOT CONFIRMED")
        if not self.profile.get("recovery_reviewed"):
            raise CommandRejected("RECOVERY PROFILE UNREVIEWED")
        if s.get("landed") == 1:
            self._arm(False, cancellable=False)
            return "DISARMED ON GROUND"
        mode = self.c["safety"]["abort_mode"]
        flags = s.get("ekf_flags")
        if mode in ("RTL", "LOITER") and (
            not s.cached("home") or flags is None or not flags & 16 or flags & (1024 | 32768)
        ):
            mode = "LAND"
        try:
            self.mode(mode, cancellable=False)
        except CommandRejected:
            if mode == "LAND":
                raise
            self.mode("LAND", cancellable=False)
            mode = "LAND"
        return mode + " REQUESTED"

    def motor_stop(self, sequence: int):
        # A zero FC timeout terminates the test in the firmware's next motor-test tick.
        self.require_client().command(209, (sequence, 0, 0, 0, 1, 0, 0), cancellable=False)
        deadline = self.snapshot().cached("motor_deadline", time.monotonic() + 3)
        while time.monotonic() < deadline + 0.2:
            self.require_client().check(cancellable=False)
            self.pump(0.02)
        self.motor_active_sequence = None
        return "TEST TIMEOUT/STOP SENT; CHECK MOTOR"

    def read_parameter(self, name: str):
        cl, m = self.require_client(), self.c["mavlink"]
        for _ in range(self.c["mavlink"]["read_retries"] + 1):
            cl.inbox.clear()
            cl.connection.mav.param_request_read_send(
                m["target_system"], m["target_component"], name.encode(), -1
            )
            response = cl.wait_message("PARAM_VALUE", lambda d: d["param_id"] == name)
            if response:
                return response
        raise OutcomeUnknown("PARAM READ TIMEOUT " + name)

    def execute(self, kind: str, data):
        cl = self.require_client()
        if kind == "motor_stop":
            # A stop for a locally issued test remains available if ordinary controls disable.
            if self.snapshot().cached("motor_deadline") is None:
                raise CommandRejected("NO LOCALLY ISSUED MOTOR TEST")
            return self.motor_stop(data)
        if kind == "read_params":
            for name in data:
                try:
                    self.read_parameter(name)
                except OutcomeUnknown:
                    LOG.warning("Parameter unavailable: %s", name)
            return "PARAMETERS READ"
        self._guard(kind)
        if kind == "arm":
            self._arm(True)
            return "ARMED"
        if kind == "disarm":
            self._arm(False)
            return "DISARMED"
        if kind == "land":
            if self.snapshot().get("armed") is not True:
                raise CommandRejected("NOT ARMED")
            self.mode("LAND")
            return "LAND REQUESTED"
        if kind == "abort":
            # Preserve pilot/FC mode takeover instead of overriding it.
            if self.snapshot().get("mode") != "AUTO":
                return "AUTO ALREADY EXITED; PILOT/FC HAS CONTROL"
            return self.recovery()
        if kind == "motor_start":
            if data not in (1, 2, 3, 4) or len(self.profile.get("motor_positions", [])) != 4:
                raise CommandRejected("MOTOR MAP UNREVIEWED")
            for name, expected in self.profile.get("motor_configuration", {}).items():
                actual = self.read_parameter(name)
                if actual["param_value"] != expected:
                    raise CommandRejected("MOTOR CONFIG CHANGED: " + name)
            self._guard(kind)
            deadline = time.monotonic() + self.c["motors"]["duration_s"]
            cl.check()
            self.motor_active_sequence = data
            self.store.put("motor_deadline", deadline, time.monotonic(), "LOCAL COMMAND")
            cl.command(
                209,
                (
                    data,
                    0,
                    self.c["motors"]["throttle_percent"],
                    self.c["motors"]["duration_s"],
                    1,
                    0,
                    0,
                ),
            )
            return "TEST REQUEST ACCEPTED"
        if kind == "mission":
            if not data.executable or not self.profile.get("recovery_reviewed"):
                raise CommandRejected("MISSION/RECOVERY UNREVIEWED")
            self.emit("operation", (kind, "progress", "UPLOADING"))
            MissionTransfer(cl).upload(data)
            self._guard(kind)
            cl.command(224, (1,), lambda s: s.get("mission_seq") == 1)
            self.mode(self.profile["arming_mode"])
            self._guard(kind)
            self.emit("operation", (kind, "progress", "ARMING"))
            self._arm(True)
            cl.check()
            reasons = readiness(self.snapshot(), self.c, self.profile)
            if reasons:
                raise CommandRejected("; ".join(reasons))
            self.expected_modes.add("AUTO")
            self.expected_until = time.monotonic() + self.c["mavlink"]["state_timeout_s"] + 3
            self.emit("operation", (kind, "progress", "STARTING"))
            cl.command(
                300,
                (0, 0, 0, 0, 0, 0, 0),
                lambda s: s.get("mode") == "AUTO" and s.get("mission_seq") is not None,
            )
            return "MISSION STARTED"
        if kind == "write_param":
            name, value = data
            spec = self.profile.get("parameters", {}).get(name)
            if not spec or not math.isfinite(value) or not spec["min"] <= value <= spec["max"]:
                raise CommandRejected("PARAMETER NOT REVIEWED")
            if spec.get("values") and value not in spec["values"]:
                raise CommandRejected("PARAMETER ENUM INVALID")
            old = self.read_parameter(name)
            if old["param_type"] != spec["type"]:
                raise CommandRejected("PARAMETER TYPE MISMATCH")
            self._guard(kind)
            m = self.c["mavlink"]
            LOG.info("TX PARAM_SET name=%s old=%s new=%s", name, old["param_value"], value)
            cl.connection.mav.param_set_send(
                m["target_system"], m["target_component"], name.encode(), value, spec["type"]
            )
            result = self.read_parameter(name)
            if result["param_type"] != spec["type"] or not math.isclose(
                result["param_value"], value, rel_tol=1e-5, abs_tol=1e-5
            ):
                raise OutcomeUnknown("PARAMETER READBACK MISMATCH")
            return "WRITE VERIFIED; PERSISTENCE NOT TESTED"
        if kind == "mag_accept":
            mask = self.snapshot().cached("mag_cal_ready")
            if not mask:
                raise CommandRejected("NO COMPLETED LOCAL COMPASS CAL")
            cl.command(42425, (mask, 0, 0, 0, 0, 0, 0))
            self.store.put("mag_cal_ready", None, time.monotonic(), "LOCAL CALIBRATION")
            return "COMPASS SAVE ACK; REBOOT/VERIFY PER FC"
        if kind == "calibrate":
            if data not in self.profile.get("calibrations", []):
                raise CommandRejected("CALIBRATION NOT COMMISSIONED")
            if data == "COMPASS":
                mask = self.profile["compass_mask"]
                self.store.put("mag_cal_ready", None, time.monotonic(), "LOCAL CALIBRATION")
                cl.command(42424, (mask, 0, 0, 0, 0, 0, 0))
                deadline = time.monotonic() + 180
                completed = 0
                while completed & mask != mask and time.monotonic() < deadline:
                    result = cl.wait_message(
                        "MAG_CAL_REPORT",
                        lambda d: bool(mask & (1 << d["compass_id"])),
                        timeout=max(0, deadline - time.monotonic()),
                    )
                    if not result or result["cal_status"] != 4 or result.get("autosaved"):
                        raise CommandRejected("COMPASS CAL FAILED/UNKNOWN OR UNEXPECTED AUTOSAVE")
                    completed |= 1 << result["compass_id"]
                if completed & mask != mask:
                    raise OutcomeUnknown("COMPASS CAL TIMEOUT")
                self.store.put("mag_cal_ready", mask, time.monotonic(), "LOCAL CALIBRATION")
                return "COMPASS DONE; SELECT COMPASS TO SAVE"
            params = {"GYRO": (1, 0, 0, 0, 0, 0, 0), "BARO ZERO": (0, 0, 1, 0, 0, 0, 0)}
            if data not in params:
                raise CommandRejected("USE REVIEWED GCS CALIBRATION")
            cl.command(241, params[data], timeout=180)
            return "CAL ACK ACCEPTED; VERIFY IN GCS"
        raise CommandRejected("UNKNOWN OPERATION")

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.connection = self._open()
                self.client = CommandClient(
                    self.connection,
                    self.c,
                    self.store,
                    self.pump,
                    self.cancel_event,
                    self.stop_event,
                )
                configured = False
                version_requested = 0.0
                while not self.stop_event.is_set():
                    self.pump(0.02)
                    if (
                        self.snapshot().get("heartbeat")
                        and time.monotonic() - version_requested > 5
                        and self.snapshot().cached("version") is None
                    ):
                        self.cancel_event.clear()
                        try:
                            self.client.command(512, (148,))
                        except (CommandRejected, OutcomeUnknown, Cancelled):
                            LOG.warning("AUTOPILOT_VERSION request unavailable")
                        version_requested = time.monotonic()
                    if not configured and self.snapshot().get("autopilot") == 3:
                        # Read-only telemetry configuration; never arm on reconnect.
                        for msg, hz in RATES.items():
                            if self.stop_event.is_set() or self.cancel_event.is_set():
                                break
                            try:
                                self.client.command(511, (msg, round(1e6 / hz)))
                            except (CommandRejected, OutcomeUnknown, Cancelled):
                                LOG.warning("Interval request unavailable: %s", msg)
                        configured = True
                    try:
                        kind, data = self.tasks.get_nowait()
                    except Empty:
                        continue
                    self.operation = kind
                    self.expected_modes.clear()
                    try:
                        detail = self.execute(kind, data)
                        self.emit("operation", (kind, "success", detail))
                    except (CommandRejected, OutcomeUnknown, Cancelled) as exc:
                        if isinstance(exc, OutcomeUnknown):
                            self.uncertain = True
                        detail = str(exc)
                        try:
                            if kind == "motor_start" and self.motor_active_sequence == data:
                                detail += "; " + self.motor_stop(data)
                            elif kind == "calibrate" and data == "COMPASS":
                                self.client.command(
                                    42426, (self.profile["compass_mask"],), cancellable=False
                                )
                            elif (
                                kind in ("mission", "arm") and self.snapshot().get("armed") is True
                            ):
                                detail += "; " + self.recovery()
                        except (CommandRejected, OutcomeUnknown, Cancelled) as recovery_exc:
                            if isinstance(recovery_exc, OutcomeUnknown):
                                self.uncertain = True
                            detail += "; RECOVERY: " + str(recovery_exc)
                        self.emit("operation", (kind, "failure", detail))
                    finally:
                        self.operation = ""
                        with self._submission_lock:
                            self._occupied = False
                        self.cancel_event.clear()
            except Exception:
                LOG.exception("MAVLink connection/worker failure")
                self.uncertain = True
                self.emit("worker_error", "MAVLINK WORKER RECONNECTING")
                if self.operation:
                    self.emit(
                        "operation",
                        (self.operation, "failure", "CONNECTION FAILED; OUTCOME UNKNOWN"),
                    )
                    self.operation = ""
                with self._submission_lock:
                    self._occupied = False
            finally:
                if self.connection:
                    self.connection.close()
                    self.connection = None
            self.stop_event.wait(1)

    def close(self):
        self.stop_event.set()
        self.cancel_event.set()
        self.join(timeout=2)
