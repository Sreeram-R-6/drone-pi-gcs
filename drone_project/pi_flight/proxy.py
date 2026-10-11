#!/usr/bin/env python3
"""Single serial owner and bounded MAVProxy startup. Offline is the default."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from flightcheck.config import load
from flightcheck.reporting import Report
from flightcheck.telemetry import serial_owner
from uart import inspect

BASE = Path(__file__).resolve().parent


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true")
    modes.add_argument("--check-once", action="store_true")
    modes.add_argument("--wait-for-fcu", action="store_true")
    p.add_argument("--timeout", type=float)
    a = p.parse_args(argv)
    if a.wait_for_fcu and a.timeout is None:
        p.error("--wait-for-fcu requires --timeout SECONDS")
    if a.timeout is not None and not 0 < a.timeout <= 300:
        p.error("timeout must be in (0, 300]")
    if not (a.check_once or a.wait_for_fcu):
        print("OFFLINE: MAVProxy not started; no serial or UDP opened")
        return 0
    c = load(BASE/"config.toml")
    if any(path.exists() for path in (Path.home()/".mavinit.scr", Path.home()/".mavproxy/mavinit.scr")):
        print("FAIL: a MAVProxy user startup script exists; review automatic commands first")
        return 3
    if not c["link"]["device_confirmed"] or not c["commissioning"]["wiring_props_off_verified"]:
        print("FAIL: UART device and props-off wiring unconfirmed")
        return 3
    evidence = inspect()
    device = c["link"]["device"]
    if (evidence["selected_device"] != device or evidence["pinmux"] == "FAIL" or
            not evidence.get("permissions") or evidence.get("getty") == "active" or
            evidence.get("holders") != "none visible"):
        print("FAIL: UART inspection", evidence)
        return 3
    if evidence["pinmux"] == "UNKNOWN":
        print("WARN: pinctrl/raspi-gpio unavailable; continuing props-off link test using active device-tree mapping", flush=True)
    if not shutil.which("fuser"):
        raise RuntimeError("fuser required for port ownership check")
    report = Report(BASE/"logs/mavproxy")
    ready = report.path/"ready.json"
    env = dict(os.environ, MAVLINK20="1", PYTHONPATH=str(BASE),
               PI_FLIGHT_READY=str(ready), PI_FLIGHT_TIMEOUT=str(a.timeout or c["link"]["connect_timeout_s"]),
               PI_FLIGHT_SYSID=str(c["link"]["system_id"]), PI_FLIGHT_COMPID=str(c["link"]["component_id"]),
               PI_FLIGHT_MAX_AGE=str(c["link"]["max_age_s"]))
    timeout = float(env["PI_FLIGHT_TIMEOUT"])
    argv = [str(BASE/".venv/bin/mavproxy.py"), "--master="+device, "--baudrate=115200",
            "--mavversion=2.0", "--out=udp:127.0.0.1:14550", "--nowait", "--non-interactive",
            "--retries=1", "--heartbeat-rate=0",
            "--target-system="+str(c["link"]["system_id"]),
            "--target-component="+str(c["link"]["component_id"]),
            "--default-modules=link", "--load-module=mavproxy_flightwatch",
            "--aircraft="+str(report.path), "--cmd=set heartbeat 0"]
    # Empty run directory prevents project-local mavinit scripts from being executed.
    # HOME redirect is deliberately not used: --aircraft determines init file scope.
    with serial_owner(device):
        status = subprocess.run(["fuser", device], capture_output=True)
        if status.returncode == 0:
            print("FAIL: serial port already held")
            return 3
        # Isolate MAVProxy in its own process group so every shutdown path can
        # terminate its serial owner, including tmux's SIGHUP.
        process = subprocess.Popen(argv, cwd=report.path, env=env, start_new_session=True)
        def stop_proxy():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        old_handlers = {s: signal.signal(s, interrupted)
                        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        started = time.monotonic()
        connected = False
        try:
            while process.poll() is None:
                if ready.exists():
                    try:
                        data = json.loads(ready.read_text())
                        connected = data["pid"] == process.pid and time.monotonic()-data["heartbeat_monotonic"] <= c["link"]["max_age_s"]
                    except (ValueError, KeyError):
                        pass
                if not connected and time.monotonic()-started >= timeout+2:
                    print("NOT CONNECTED", flush=True)
                    stop_proxy()
                    break
                time.sleep(.1)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stop_proxy()
        except KeyboardInterrupt:
            stop_proxy()
        finally:
            stop_proxy()
            for s, handler in old_handlers.items():
                signal.signal(s, handler)
        return 0 if connected else 4


if __name__ == "__main__":
    raise SystemExit(main())
