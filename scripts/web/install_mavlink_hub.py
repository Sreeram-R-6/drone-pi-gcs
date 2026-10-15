#!/usr/bin/env python3
"""Add shared read/write MAVLink access without restarting the UART router or web terminals."""
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def main():
    if os.geteuid()!=0:raise SystemExit('Run: sudo python3 scripts/web/install_mavlink_hub.py')
    subprocess.run([sys.executable,str(ROOT/'scripts/web/install_services.py'),
                    '--user','user','--only','drone-mavlink-hub.service'],check=True)
    folder=Path('/etc/systemd/system/drone-mavproxy-web.service.d')
    folder.mkdir(exist_ok=True)
    (folder/'90-mavlink-hub.conf').write_text(
        '[Unit]\nWants=drone-mavlink-hub.service\nAfter=drone-mavlink-hub.service\n')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['tailscale','serve','--bg','--yes','--tcp=5760','tcp://127.0.0.1:5760'],check=True)
    print('Shared MAVLink: tcp:127.0.0.1:5760 locally; TCP 5760 on the Pi Tailscale address remotely.')
    print('UART router, OLED and existing terminal processes were preserved.')


if __name__=='__main__':main()
