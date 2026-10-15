#!/usr/bin/env python3
"""Forward localhost websites and shared MAVLink privately over Tailscale, persistently."""
import ipaddress
import os
import subprocess

PORTS = (8070, 8071, 8080, 8090, 5000, 5760)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run: sudo python3 scripts/web/enable_tailscale.py")
    address = subprocess.check_output(["tailscale", "ip", "-4"], text=True).strip()
    if ipaddress.ip_address(address) not in ipaddress.ip_network("100.64.0.0/10"):
        raise SystemExit("Expected an active Tailscale IPv4 address")
    for port in PORTS:
        subprocess.run(["tailscale", "serve", "--bg", "--yes", f"--tcp={port}",
                        f"tcp://127.0.0.1:{port}"], check=True)
    print(f"Dashboard: http://{address}:8070")
    print("Tailscale forwarding is persistent; existing localhost access remains available.")


if __name__ == "__main__":
    main()
