"""Bounded Pi-only health sampling. Never requires root or an FC connection."""

import json
import ipaddress
import logging
from pathlib import Path
import re
import subprocess
from threading import Event, Thread
import time
from typing import Any
from .model import TelemetryStore

LOG = logging.getLogger(__name__)


def lan_address(addresses: list, config: dict, ethernet_up: bool) -> tuple[str, str]:
    names = ([config["ethernet_interface"]] if ethernet_up else []) + [config["wifi_interface"]]
    for name in names:
        if name.startswith(("tailscale", "tun", "wg", "lo")):
            continue
        for interface in addresses:
            if interface.get("ifname") != name or interface.get("operstate") == "DOWN":
                continue
            if "NO-CARRIER" in interface.get("flags", []):
                continue
            for entry in interface.get("addr_info", []):
                if entry.get("scope") != "global":
                    continue
                try:
                    address = ipaddress.ip_address(entry.get("local", ""))
                except ValueError:
                    continue
                if (
                    address.version != 4
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_unspecified
                    or address.is_multicast
                ):
                    continue
                if address in ipaddress.ip_network("100.64.0.0/10"):
                    continue
                return str(address), name
    return "--", ""


def run(argv: list[str], timeout: float) -> str:
    try:
        # systemctl is-system-running deliberately returns nonzero for starting/degraded.
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


class PiMonitor(Thread):
    def __init__(self, config: dict, store: TelemetryStore):
        super().__init__(name="pi-monitor", daemon=True)
        self.c, self.store = config, store
        self.stop_event = Event()
        self.previous_cpu: tuple[int, int] | None = None

    def sample(self) -> dict:
        c, timeout = self.c, self.c["command_timeout_s"]
        values: dict[str, Any] = {
            "pi_state": run(["systemctl", "is-system-running"], timeout) or None,
            "wifi": None,
            "ethernet": None,
            "ip": "--",
            "ip_interface": "",
            "cpu": None,
            "pi_temp": None,
        }
        wifi = run(["iw", "dev", c["wifi_interface"], "link"], timeout)
        match = re.search(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", wifi)
        if "Connected to" in wifi and match:
            values["wifi"] = float(match.group(1))
        try:
            values["ethernet"] = (
                Path("/sys/class/net") / c["ethernet_interface"] / "carrier"
            ).read_text().strip() == "1"
        except OSError:
            pass
        try:
            addresses = json.loads(run(["ip", "-j", "-4", "address", "show"], timeout) or "[]")
            values["ip"], values["ip_interface"] = lan_address(
                addresses, c, values["ethernet"] is True
            )
        except (ValueError, KeyError):
            pass
        try:
            cpu = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
            idle, total = cpu[3] + cpu[4], sum(cpu)
            if self.previous_cpu:
                old_idle, old_total = self.previous_cpu
                delta = total - old_total
                values["cpu"] = 100 * (1 - (idle - old_idle) / delta) if delta > 0 else None
            self.previous_cpu = idle, total
        except (OSError, ValueError, IndexError):
            pass
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                if zone.joinpath("type").read_text().strip() in (
                    "cpu-thermal",
                    "cpu_thermal",
                    "soc_thermal",
                ):
                    values["pi_temp"] = int(zone.joinpath("temp").read_text()) / 1000
                    break
            except (OSError, ValueError):
                continue
        return values

    def run(self):
        while not self.stop_event.is_set():
            try:
                values = self.sample()
                now = time.monotonic()
                for key, value in values.items():
                    self.store.put(key, value, now)
            except Exception:
                LOG.exception("Pi monitor sampling failed")
            self.stop_event.wait(1 / self.c["sample_hz"])

    def close(self):
        self.stop_event.set()
        self.join(timeout=4)
