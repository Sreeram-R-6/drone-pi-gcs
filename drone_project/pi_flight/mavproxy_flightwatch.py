"""MAVProxy plugin: bounded first heartbeat, stop on stale live link.

No arming/parameter/mode commands. Readiness file is private to this run.
"""
import json
import os
from pathlib import Path
import time
from MAVProxy.modules.lib import mp_module


class FlightWatch(mp_module.MPModule):
    def __init__(self, mpstate):
        super().__init__(mpstate, "flightwatch", "bounded FC readiness")
        self.started = time.monotonic()
        self.last = None
        self.path = Path(os.environ["PI_FLIGHT_READY"])
        self.timeout = float(os.environ["PI_FLIGHT_TIMEOUT"])
        self.target = (int(os.environ["PI_FLIGHT_SYSID"]), int(os.environ["PI_FLIGHT_COMPID"]))
        # Only the mission's GCS heartbeat should sustain the reviewed companion
        # failsafe. MAVProxy must not mask a crashed mission with its own heartbeat.
        self.mpstate.settings.heartbeat = 0
        for master in self.mpstate.mav_master:
            master.autoreconnect = False
        # A UDP output receives forwarded commands too. Bind its local socket to
        # loopback explicitly, rather than relying only on its destination address.
        for output in self.mpstate.mav_outputs:
            address, port = output.port.getsockname()
            if port == 0:
                output.port.bind(("127.0.0.1", 0))
            elif address != "127.0.0.1":
                raise RuntimeError("MAVProxy output socket is not bound to loopback")

    def mavlink_packet(self, msg):
        if msg.get_type() != "HEARTBEAT" or (msg.get_srcSystem(), msg.get_srcComponent()) != self.target:
            return
        if msg.autopilot != 3 or msg.type != 2 or msg.get_msgbuf()[0] != 0xFD:
            print("FAIL: unexpected identity or MAVLink version", flush=True)
            self.mpstate.status.exit = True
            return
        self.last = time.monotonic()
        self.path.write_text(json.dumps({"heartbeat_monotonic": self.last, "pid": os.getpid()}))

    def idle_task(self):
        now = time.monotonic()
        if self.last is None and now-self.started > self.timeout:
            print("NOT CONNECTED", flush=True)
            self.mpstate.status.exit = True
        elif self.last is not None and now-self.last > float(os.environ["PI_FLIGHT_MAX_AGE"]):
            print("LINK LOST: MAVProxy stopping; rely on verified FC failsafes", flush=True)
            self.mpstate.status.exit = True


def init(mpstate):
    return FlightWatch(mpstate)
