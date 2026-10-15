"""MAVLink over a sole-owner UART or an explicitly configured loopback router."""
from collections import defaultdict, deque
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


class LinkLost(RuntimeError):
    pass


class Rejected(RuntimeError):
    pass


@contextlib.contextmanager
def serial_owner(device):
    """Use the resolved device for locks so aliases cannot create two owners."""
    try:
        path = Path(device).resolve(strict=True)
    except FileNotFoundError as exc:
        raise Rejected(f'UART missing: {device}. Run GCS on the Pi and verify its UART device exists.') from exc
    if not path.is_char_device():
        raise Rejected(f"UART is not a character device: {device}")
    if not os.access(path,os.R_OK | os.W_OK):
        raise Rejected(f"UART access denied: {device}; check dialout permissions")
    if not shutil.which('fuser'):
        raise Rejected('fuser is required to check UART ownership')
    with process_lock('serial-'+path.name+'.lock'):
        holders = subprocess.run(['fuser',str(path)],capture_output=True,text=True,timeout=5)
        if holders.returncode == 0:
            raise Rejected(f"UART busy: {device} (PID {holders.stdout.strip()}). Close MAVProxy/other UART programs, then run ./gcs.")
        if holders.returncode != 1 or holders.stderr.strip():
            raise Rejected(f"Cannot verify UART ownership: {holders.stderr.strip()}")
        yield


@contextlib.contextmanager
def process_lock(name):
    # Per-user global locks also cover separate checkouts of this repository.
    root = Path(f"/tmp/pi-flight-{os.getuid()}")
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe lock directory")
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Duplicate owner: {name}") from exc
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self):
        self.samples = defaultdict(lambda: deque(maxlen=20000))
        self.faults = []
        self.origin_reset = False
        self.boot_reset = False
        self.origin = None
        self.boot_times = {}

    def put(self, kind, data, now):
        self.samples[kind].append((now, data))
        if kind == "STATUSTEXT":
            text = str(data.get("text", ""))
            if data.get("severity", 7) <= 3 or re.search(
                    r"pre.?arm|failsafe|unhealthy|internal error|crash|watchdog|glitch|inconsistent", text, re.I):
                if text not in self.faults:
                    self.faults.append(text)
        if kind == "GPS_GLOBAL_ORIGIN":
            origin = tuple(data.get(k) for k in ("latitude", "longitude", "altitude"))
            self.origin_reset |= self.origin is not None and origin != self.origin
            self.origin = origin
        if "time_boot_ms" in data:
            value = data["time_boot_ms"]
            old = self.boot_times.get(kind)
            self.boot_reset |= old is not None and value < old
            self.boot_times[kind] = value

    def latest(self, kind, now, max_age):
        if not self.samples[kind] or now-self.samples[kind][-1][0] > max_age:
            raise LinkLost(f"Stale/missing {kind}")
        return self.samples[kind][-1][1]


class Transport:
    def __init__(self, config, report, receive_only=False):
        # Constructor is called only in explicitly connected modes.
        os.environ["MAVLINK20"] = "1"
        from pymavlink import mavutil
        self.mavutil = mavutil
        self.c = config
        self.report = report
        self.store = Store()
        self._resources = contextlib.ExitStack()
        endpoint = config['link']['endpoint']
        self.direct_uart = endpoint.startswith('/dev/')
        try:
            if self.direct_uart:
                self._resources.enter_context(serial_owner(endpoint))
            self.connection = mavutil.mavlink_connection(
                endpoint, baud=config['link']['baud'], autoreconnect=False,
                source_system=config["link"]["source_system"],
                source_component=config["link"]["source_component"], dialect="ardupilotmega")
        except BaseException:
            self._resources.close()
            raise
        self.target = (config["link"]["system_id"], config["link"]["component_id"])
        self.version = None
        self.params = {}
        self.last_sent_heartbeat = 0.0
        self.sending = True
        self.flight_authorized = False
        self.observer = None
        self.receive_only = receive_only
        self.last_monitor_request = None
        self.foreign_heartbeat = None

    def allow_send(self):
        if getattr(self, "receive_only", False):
            raise Rejected("Receive-only telemetry monitor: sending is disabled")

    def close(self):
        try:
            self.connection.close()
        finally:
            self._resources.close()

    def pump(self, duration=0.05):
        deadline = time.monotonic()+duration
        while time.monotonic() < deadline:
            message = self.connection.recv_match(blocking=True,
                timeout=min(.05, max(0, deadline-time.monotonic())))
            if message is None:
                continue
            if message.get_type() == "BAD_DATA":
                continue
            if (message.get_srcSystem(), message.get_srcComponent()) != self.target:
                if message.get_type() == 'HEARTBEAT':
                    self.foreign_heartbeat = (message.get_srcSystem(), message.get_srcComponent())
                self.report.event("foreign_message", type=message.get_type(),
                    system=message.get_srcSystem(), component=message.get_srcComponent())
                continue
            data = message.to_dict()
            # JSON standard cannot encode NaN; mark as missing, checks fail closed.
            def clean(value):
                if isinstance(value, float) and not math.isfinite(value):
                    return None
                if isinstance(value, dict):
                    return {k: clean(v) for k,v in value.items()}
                if isinstance(value, (tuple, list)):
                    return [clean(v) for v in value]
                if isinstance(value, bytes):
                    return value.hex()
                return value
            data = clean(data)
            now = time.monotonic()
            self.store.put(message.get_type(), data, now)
            self.report.event("rx", monotonic=now, message=data)
            if message.get_type() == "HEARTBEAT" and message.get_msgbuf()[0] != 0xFD:
                if "FC heartbeat is not MAVLink 2" not in self.store.faults:
                    self.store.faults.append("FC heartbeat is not MAVLink 2")
            if message.get_type() == "AUTOPILOT_VERSION":
                self.version = data
            if message.get_type() == "PARAM_VALUE":
                name = str(data["param_id"]).rstrip("\x00")
                self.params[name] = (data["param_value"], data["param_type"], now)
        if self.observer:
            self.observer(self)

    def connect(self, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.pump(min(.1, deadline-time.monotonic()))
            if self.store.samples["HEARTBEAT"]:
                hb = self.store.latest("HEARTBEAT", time.monotonic(), self.c["link"]["max_age_s"])
                if hb["autopilot"] != 3 or hb["type"] != 2:
                    raise Rejected("Unexpected autopilot/vehicle identity")
                return
        raise LinkLost("NOT CONNECTED")

    def heartbeat(self):
        if getattr(self, "receive_only", False):
            return
        self.require_live()
        now = time.monotonic()
        if self.sending and now-self.last_sent_heartbeat >= 1:
            self.connection.mav.heartbeat_send(6, 8, 0, 0, 4)
            self.last_sent_heartbeat = now

    def request_monitor_telemetry(self):
        """Restricted monitor handshake: only GCS heartbeat and stream requests.

        Does not unlock command(), parameter writes, or position targets.
        """
        self.require_live()
        now = time.monotonic()
        # Keep heartbeat cadence independent from the five-second stream retry.
        # A healthy UI must not appear lost to an enabled five-second failsafe.
        if now-self.last_sent_heartbeat >= 1:
            self.connection.mav.heartbeat_send(6, 8, 0, 0, 4)
            self.last_sent_heartbeat = now
        if self.last_monitor_request is not None and now-self.last_monitor_request < 5:
            return
        self.connection.mav.request_data_stream_send(*self.target, 0, 5, 1)
        for name in ('RC_OPTIONS','RSSI_TYPE'):
            self.connection.mav.param_request_read_send(*self.target,name.encode('ascii'),-1)
        # Request specific packets not consistently covered by legacy streams.
        for message_id in (32, 100, 106, 132, 147, 193, 241, 242, 49, 245):
            self.connection.mav.command_long_send(*self.target, 511, 0,
                                                 message_id, 200000, 0, 0, 0, 0, 0)
        self.last_monitor_request = now
        self.report.event('monitor_telemetry_requested', target=self.target)

    def require_live(self):
        if not self.sending:
            raise LinkLost("Sending disabled")
        return self.store.latest("HEARTBEAT", time.monotonic(), self.c["link"]["max_age_s"])

    def mode(self):
        hb = self.require_live()
        # mode_string_v10 expects a MAVLink-message-shaped object, including
        # get_type(), while the telemetry store deliberately retains JSON-safe dicts.
        heartbeat = type("Heartbeat", (), {**hb, "get_type": lambda self: "HEARTBEAT"})()
        return self.mavutil.mode_string_v10(heartbeat)

    def armed(self):
        return bool(self.require_live()["base_mode"] & 128)

    def command(self, command, params=(), flight=False, guard=None):
        self.allow_send()
        if self.c.get('_bench_only',False) and command not in (401,511,512):
            raise Rejected('Bench-only profile: actuating commands disabled')
        self.require_live()
        if flight and not self.flight_authorized:
            raise Rejected("Flight commands locked")
        # Drain queued state before choosing whether to transmit.
        self.pump(.02)
        if guard:
            guard()
        started = time.monotonic()
        self.connection.mav.command_long_send(*self.target, command, 0,
                                             *(list(params)+[0]*(7-len(params))))
        self.report.event("command", command=command, params=params)
        deadline = started+self.c["mission"]["command_timeout_s"]
        while time.monotonic() < deadline:
            self.pump()
            self.heartbeat()
            if guard:
                guard()
            for timestamp, ack in self.store.samples["COMMAND_ACK"]:
                if timestamp >= started and ack["command"] == command:
                    if ack.get("target_system", 0) not in (0, self.c["link"]["source_system"]):
                        continue
                    if ack.get("target_component", 0) not in (0, self.c["link"]["source_component"]):
                        continue
                    if ack["result"] == 0:
                        return ack
                    if ack["result"] != 5:
                        raise Rejected(f"Command {command}: {ack}; faults={self.store.faults}")
        raise Rejected(f"Command {command}: ACK timeout")

    def read_param(self, name, retries=3):
        self.allow_send()
        for attempt in range(retries):
            if getattr(self,'ui',None) is not None:
                self.ui.preflight_pending = f'Parameter {name}: readback {attempt+1}/{retries}'
            self.require_live()
            start = time.monotonic()
            self.connection.mav.param_request_read_send(*self.target, name.encode("ascii"), -1)
            while time.monotonic()-start < self.c["mission"]["command_timeout_s"]:
                self.pump()
                self.heartbeat()
                result = self.params.get(name)
                if result and result[2] >= start and result[0] is not None:
                    return result[:2]
        raise Rejected(f"Missing live parameter {name}")

    def set_climb_parameter(self, name, value, param_type):
        self.allow_send()
        if self.c.get('_bench_only',False):
            raise Rejected('Bench-only profile: parameter writes disabled')
        f = self.c["firmware"]
        if (not self.flight_authorized or self.armed() or name != f["climb_parameter"] or
                param_type != f["climb_parameter_type"]):
            raise Rejected("Parameter write not authorized or vehicle armed")
        if not math.isfinite(value) or not f["climb_parameter_min"] <= value <= f["climb_parameter_max"]:
            raise Rejected("Parameter outside reviewed range")
        self.connection.mav.param_set_send(*self.target, name.encode("ascii"), value, param_type)
        actual, actual_type = self.read_param(name)
        if actual_type != param_type or not math.isclose(actual, value, rel_tol=1e-6, abs_tol=1e-6):
            raise Rejected(f"Parameter readback mismatch: {actual}, type {actual_type}")

    def initialize_streams(self):
        # Only non-actuating requests. Unsupported requests remain visible and block relevant gates.
        self.command(512, [148]) # AUTOPILOT_VERSION
        ids = (0, 1, 24, 30, 32, 33, 65, 100, 106, 132, 147, 193, 241, 242, 49, 245)
        if self.c.get('gcs', {}).get('execution_backend') == 'onboard_auto':
            ids += (42,) # MISSION_CURRENT is monitoring, never a setpoint.
        for msg_id in ids:
            if getattr(self,'ui',None) is not None:
                self.ui.workflow_status = f'FC connected. Requesting telemetry message {msg_id}; unsupported requests may time out.'
                self.ui.notice = self.ui.workflow_status
            try:
                self.command(511, [msg_id, 200000 if msg_id != 0 else 1000000])
            except Rejected as exc:
                self.report.event("stream_request_rejected", message_id=msg_id, error=str(exc))

    def export_parameters(self, timeout=60):
        self.allow_send()
        """Complete snapshot with count/index validation; bounded, read-only."""
        self.require_live()
        start = time.monotonic()
        self.connection.mav.param_request_list_send(*self.target)
        entries = {}
        count = None
        while time.monotonic()-start < timeout:
            self.pump()
            self.heartbeat()
            for timestamp, data in self.store.samples["PARAM_VALUE"]:
                if timestamp < start:
                    continue
                # ArduPilot may asynchronously publish a changed parameter with
                # PARAM_INDEX=UINT16_MAX while a list download is in progress.
                # It is a valid update, but it is not one entry in the indexed
                # list and must not be mistaken for a count/index discontinuity.
                if data["param_index"] == 0xFFFF:
                    continue
                if count is None:
                    count = data["param_count"]
                if count != data["param_count"] or not 0 <= data["param_index"] < count:
                    raise Rejected("Parameter export count/index changed")
                entries[data["param_index"]] = data
            if getattr(self,'ui',None) is not None:
                self.ui.workflow_status = f'FC parameter download: {len(entries)}/{count or "?"}. Start remains locked.'
                self.ui.notice = self.ui.workflow_status
            if count and len(entries) == count:
                self.report.save("complete-parameters.json", list(entries.values()))
                return
        self.report.save("incomplete-parameters.json", list(entries.values()))
        raise Rejected(f"Incomplete parameter export: {len(entries)}/{count}")

    def sample(self, seconds):
        start = time.monotonic()
        while time.monotonic()-start < seconds:
            if getattr(self,'ui',None) is not None:
                remaining = max(0.,seconds-(time.monotonic()-start))
                self.ui.preflight_pending = f'Stationary sensor sample: {remaining:.0f}s remaining; keep aircraft still'
            self.pump()
            self.heartbeat()
        if getattr(self,'ui',None) is not None:
            self.ui.preflight_pending = ''
        return start, time.monotonic()

    def position_target(self, north, east, down, guard):
        self.allow_send()
        if not self.flight_authorized:
            raise Rejected("Setpoints locked")
        self.pump(.001)
        guard()
        self.connection.mav.set_position_target_local_ned_send(
            int(time.monotonic()*1000) & 0xffffffff, *self.target, 1, 3576,
            north, east, down, 0, 0, 0, 0, 0, 0, 0, 0)


def parameter_audit(transport, config):
    """Read-only audit with mandatory category coverage and exact type/value checks."""
    from .checks import Check
    path = config["parameter_audit"]["profile_path"]
    if not path:
        return [Check("live parameter audit", "UNKNOWN", None, "firmware-matched reviewed profile")]
    profile = json.loads((Path(config["_base"])/path).read_text())
    if profile.get('reviewed') is False:
        return [Check('live parameter audit', 'UNKNOWN', None,
                      'reviewed firmware-matched parameter values and MAVLink types',
                      'Draft profile only; do not approve until every value and recovery behavior is reviewed')]
    required = {"arming", "serial_pi", "serial_flow", "ekf", "flow", "range", "battery",
                "rc_failsafe", "gcs_failsafe", "ekf_failsafe", "fence", "rtl", "landing", "climb"}
    if set(profile.get("categories", {})) != required:
        raise Rejected("Parameter profile must cover every required safety category")
    if profile.get("flight_sw_version") != config["aircraft"]["flight_sw_version"]:
        raise Rejected("Parameter profile firmware mismatch")
    pi_group = config["aircraft"]["pi_serial_group"]
    flow_group = config["aircraft"]["flow_serial_group"]
    if (not re.fullmatch(r"SERIAL[1-9][0-9]*", pi_group) or
            not re.fullmatch(r"SERIAL[1-9][0-9]*", flow_group) or pi_group == flow_group):
        raise Rejected("Missing/ambiguous distinct SERIAL mappings")
    for group, category, protocol in ((pi_group, "serial_pi", 2), (flow_group, "serial_flow", 1)):
        values = profile["categories"][category]
        if (values.get(group+"_PROTOCOL", {}).get("value") != protocol or
                values.get(group+"_BAUD", {}).get("value") != 115):
            raise Rejected(f"Reviewed {category} profile lacks expected protocol/115200 audit")
    checks, saved = [], {}
    for category, parameters in profile["categories"].items():
        if not parameters:
            raise Rejected(f"Empty profile category {category}")
        for name, expected in parameters.items():
            try:
                value, kind = transport.read_param(name)
                saved[name] = {"value": value, "type": kind}
                passed = kind == expected["type"] and value == expected["value"]
                checks.append(Check(name, "PASS" if passed else "FAIL", saved[name], str(expected)))
            except Rejected as exc:
                checks.append(Check(name, "UNKNOWN", None, str(expected), str(exc)))
    # ArduPilot 4.7 replaced the positive ARMING_CHECK mask with the
    # inverse ARMING_SKIPCHK mask. Never request an obsolete parameter or
    # interpret a nonzero skip mask as "all checks enabled".
    version = config['aircraft']['flight_sw_version']
    modern_arming = (version >> 24, (version >> 16) & 255) >= (4,7)
    arming_name, arming_expected = ('ARMING_SKIPCHK',0) if modern_arming else ('ARMING_CHECK',1)
    arming = transport.read_param(arming_name)
    saved[arming_name] = {'value':arming[0],'type':arming[1]}
    checks.append(Check("all arming checks", "PASS" if arming[0] == arming_expected else "FAIL",
                        arming, f'{arming_name}={arming_expected}',
                        'No FC arming checks may be skipped' if modern_arming else ''))
    transport.report.save("live-parameters.json", saved)
    return checks
