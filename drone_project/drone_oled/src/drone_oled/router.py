"""Audited foreground MAVProxy launcher: exclusive checked UART owner, loopback outputs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from .config import tomllib


def inspect(device: str) -> dict:
    def read(path: str) -> str:
        try:
            return Path(path).read_bytes().rstrip(b"\0").decode().strip()
        except OSError:
            return "UNKNOWN"

    model = read("/proc/device-tree/model")
    selected = Path(device)
    resolved = selected.resolve()
    pinctrl = shutil.which("pinctrl") or shutil.which("raspi-gpio")
    pinmux = "UNKNOWN"
    if pinctrl:
        cmd = (
            [pinctrl, "get", "14,15"]
            if Path(pinctrl).name == "pinctrl"
            else [pinctrl, "get", "14-15"]
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
        pinmux = result.stdout.strip()
    holder = subprocess.run(
        ["fuser", str(resolved)], capture_output=True, text=True, timeout=3, check=False
    )
    getty = subprocess.run(
        ["systemctl", "is-active", f"serial-getty@{resolved.name}.service"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    tty = Path("/sys/class/tty") / resolved.name / "device" / "of_node"
    return {
        "model": model,
        "device": device,
        "resolved": str(resolved),
        "exists": selected.exists() and selected.is_char_device(),
        "access": os.access(selected, os.R_OK | os.W_OK),
        "pinmux": pinmux,
        "holders": holder.stdout.strip(),
        "holder_check_ok": holder.returncode == 1 and not holder.stderr.strip(),
        "getty": getty.stdout.strip(),
        "device_tree": str(tty.resolve()) if tty.exists() else "UNKNOWN",
    }


def command(config: dict, executable: str) -> list[str]:
    return [
        executable,
        "--master=" + config["device"],
        "--baudrate=115200",
        "--mavversion=2.0",
        "--dialect=ardupilotmega",
        "--out=udp:127.0.0.1:14550",
        "--out=udp:127.0.0.1:14552",
        "--out=udp:127.0.0.1:14553",
        "--source-system=252",
        "--heartbeat-rate=0",
        "--streamrate=-1",
        "--default-modules=link,log",
        "--daemon",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    try:
        with args.config.open("rb") as stream:
            c = tomllib.load(stream)
        if not shutil.which("fuser"):
            raise RuntimeError("fuser (psmisc) is required")
        if not c["device"].startswith("/dev/"):
            raise RuntimeError("Router requires an explicit UART device")
        evidence = inspect(c["device"])
        print(json.dumps(evidence, indent=2), flush=True)
        if args.inspect:
            return 0
        if os.geteuid() == 0:
            raise RuntimeError("Run router as an unprivileged serial-group user")
        if c.get("hardware_confirmed") is not True:
            raise RuntimeError("Review wiring/device evidence before hardware_confirmed=true")
        if not evidence["model"].startswith(("Raspberry Pi 4 Model B", "Raspberry Pi 5 Model B")):
            raise RuntimeError("Unsupported/unverified Pi model")
        if (
            not evidence["exists"]
            or not evidence["access"]
            or evidence["holders"]
            or not evidence["holder_check_ok"]
        ):
            raise RuntimeError("UART missing, inaccessible, busy or ownership check unavailable")
        if "TXD" not in evidence["pinmux"].upper() or "RXD" not in evidence["pinmux"].upper():
            raise RuntimeError("UART GPIO14/15 pinmux unverified")
        if "Pi 5" in evidence["model"] and evidence["resolved"].endswith("ttyAMA10"):
            raise RuntimeError("Pi5 debug UART is not the GPIO14/15 header UART")
        if evidence["getty"] not in ("inactive", "failed", "unknown"):
            raise RuntimeError("Serial getty state unsafe/unknown")
        # Same per-user canonical-device lock convention as pi_flight/flightcheck.
        lockdir = Path(f"/tmp/pi-flight-{os.getuid()}")
        lockdir.mkdir(mode=0o700, exist_ok=True)
        if (
            lockdir.is_symlink()
            or lockdir.stat().st_uid != os.getuid()
            or lockdir.stat().st_mode & 0o077
        ):
            raise RuntimeError("Unsafe UART lock directory")
        fd = os.open(
            lockdir / f"serial-{Path(evidence['resolved']).name}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        working = Path(c.get("working_directory", "/var/lib/drone-router"))
        working.mkdir(parents=True, exist_ok=True)
        for path in (
            Path.home() / ".mavinit.scr",
            Path.home() / ".mavproxy" / "mavinit.scr",
            working / "mavinit.scr",
            working / ".mavinit.scr",
        ):
            if path.exists():
                raise RuntimeError(f"Unexpected MAVProxy startup script: {path}")
        executable = str(Path(sys.executable).parent / "mavproxy.py")
        if not Path(executable).exists():
            executable = shutil.which("mavproxy.py") or ""
        if not executable:
            raise RuntimeError("Install the router optional dependencies")
        env = dict(os.environ, MAVLINK20="1")
        process = subprocess.Popen(command(c, executable), cwd=working, env=env)

        def terminate(*_):
            process.terminate()

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        try:
            return process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            os.close(fd)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ROUTER REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
