#!/usr/bin/env python3
"""Enable the existing drone services and persistent recovery without restarting them."""
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess

UNITS = (
    'mavproxy.service', 'drone-oled.service', 'drone-oled-web.service',
    'drone-mavlink-hub.service', 'drone-mavproxy-web.service', 'drone-video-gallery.service',
    'drone-qr-web.service', 'drone-web-dashboard.service',
    'ssh.service', 'tailscaled.service',
)
RECOVERY_UNITS = (
    'mavproxy.service', 'drone-oled.service', 'drone-oled-web.service',
    'ssh.service', 'tailscaled.service',
)
RECOVERY = '''# Managed by all_drone/scripts/web/ensure_startup.py
[Unit]
StartLimitIntervalSec=0

[Service]
RestartSec=3
'''


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run: sudo python3 scripts/web/ensure_startup.py')
    # Verify that this is an existing installation before changing configuration.
    for unit in UNITS:
        state = subprocess.check_output(
            ['systemctl', 'show', unit, '--property=LoadState', '--value'], text=True).strip()
        if state != 'loaded':
            raise SystemExit(f'{unit} is not installed; install the drone services first')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for unit in RECOVERY_UNITS:
        folder = Path('/etc/systemd/system') / (unit + '.d')
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / '90-drone-startup.conf'
        if path.exists() and path.read_text() == RECOVERY:
            continue
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + '.before-' + stamp))
        path.write_text(RECOVERY)
        path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemd-analyze', 'verify', *UNITS], check=True)
    # 'enable --now' starts inactive units and preserves already-running processes.
    subprocess.run(['systemctl', 'enable', '--now', *UNITS], check=True)
    for unit in UNITS:
        subprocess.run(['systemctl', 'is-enabled', '--quiet', unit], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
        print(f'{unit}: running, enabled at boot')
    print('Existing processes preserved. No flight, motor test or recording was started.')


if __name__ == '__main__':
    main()
