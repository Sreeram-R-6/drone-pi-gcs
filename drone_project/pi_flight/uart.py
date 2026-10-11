#!/usr/bin/env python3
"""Read-only Pi UART discovery; this implementation never opens serial or UDP."""
import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

BASE = Path(__file__).resolve().parent
TREE = Path("/proc/device-tree")


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def text(path):
    return Path(path).read_bytes().rstrip(b"\0").decode()


def config():
    sys.path.insert(0, str(BASE))
    from flightcheck.config import load
    return load(BASE / "config.toml")


def inspect():
    c = config()
    model = text(TREE / "model") if (TREE / "model").exists() else "UNKNOWN"
    if not model.startswith("Raspberry Pi 4 Model B"):
        raise RuntimeError("Only the detected Pi 4B/BCM2711 profile is supported")
    profiles = {
        "serial0_gpio14": ("serial0", "uart1_gpio14", "TXD1", "RXD1", "/dev/serial0"),
        "uart3_gpio4": ("uart3", "uart3_pins", "TXD3", "RXD3", None),
    }
    interface = c["link"]["pi_interface"]
    if interface not in profiles:
        raise RuntimeError(f"Unsupported Pi interface {interface}")
    uart_symbol, pin_symbol, tx_name, rx_name, stable_alias = profiles[interface]
    node = (text(TREE / "aliases" / uart_symbol) if uart_symbol == "serial0"
            else text(TREE / "__symbols__" / uart_symbol))
    pin_node = TREE / text(TREE / "__symbols__" / pin_symbol).lstrip("/")
    pins = (pin_node / "brcm,pins").read_bytes()
    function = (pin_node / "brcm,function").read_bytes()
    resolved_device = None
    for tty in [*Path("/sys/class/tty").glob("ttyAMA*"), *Path("/sys/class/tty").glob("ttyS*")]:
        of_node = tty / "device" / "of_node"
        if of_node.exists() and str(of_node.resolve()).endswith(node):
            resolved_device = "/dev/" + tty.name
            break
    device = stable_alias if stable_alias and Path(stable_alias).exists() else resolved_device
    result = {
        "model": model, "interface": interface, "uart_node": node,
        "selected_device": device, "resolved_device": resolved_device, "device_tree_pins": list(struct.unpack(
            ">" + "I" * (len(pins) // 4), pins)),
        "device_tree_function": struct.unpack(">I", function)[0],
        "pinmux": "UNKNOWN",
        "boot": "/boot/firmware/config.txt" if Path("/boot/firmware/config.txt").exists()
        else "/boot/config.txt",
    }
    if result["device_tree_pins"] != [c["link"]["gpio_tx"], c["link"]["gpio_rx"]]:
        raise RuntimeError("Configured GPIO pins do not match active device-tree profile")
    for tool in ("pinctrl", "raspi-gpio"):
        if shutil.which(tool):
            status, output, error = run(tool, "get", str(c["link"]["gpio_tx"]), str(c["link"]["gpio_rx"]))
            result["pinmux_output"] = output or error
            if status == 0 and tx_name in output and rx_name in output:
                result["pinmux"] = "PASS"
            break
    if device:
        result["character_device"] = Path(device).is_char_device()
        result["permissions"] = os.access(device, os.R_OK | os.W_OK)
        result["getty"] = run("systemctl", "is-active", f"serial-getty@{Path(device).name}.service")[1]
        result["holders"] = "UNKNOWN"
        if shutil.which("fuser"):
            status, output, error = run("fuser", device)
            result["holders"] = output if status == 0 else ("none visible" if status == 1 and not error else error)
    return result


def configure(result, apply=False):
    c = config()
    if result["selected_device"] != c["link"]["device"] or not c["link"]["device_confirmed"]:
        raise RuntimeError(f"UART unconfirmed: discovered {result['selected_device']}, expected {c['link']['device']}. No changes.")
    if result.get("getty") == "active":
        raise RuntimeError("Exact selected UART has a getty; stop only that service after explicit review")
    if c["link"]["pi_interface"] == "serial0_gpio14":
        print("GPIO14/15 serial0 is active; no boot configuration change is required.")
        return
    raise RuntimeError("This build intentionally supports no automatic overlay edits")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("inspect", "configure"))
    p.add_argument("--offline", action="store_true", help="metadata only (also default)")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    try:
        result = inspect()
        print(json.dumps(result, indent=2))
        if args.action == "configure":
            configure(result, args.apply)
        c = config()
        if result["selected_device"] != c["link"]["device"]:
            print("FAIL: configured device differs from live device-tree mapping.")
            return 3
        if not c["link"]["device_confirmed"]:
            print("FAIL: physical device/wiring confirmation required.")
            return 3
        return 0 if result["pinmux"] == "PASS" else 3
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
