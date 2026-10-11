#!/usr/bin/env bash
# Enables only deliberate arm/disarm requests; never sends an arm command.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run sudo bash deploy/enable-fc-managed-arm.sh' >&2
  exit 2
fi
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python=/opt/drone-oled/venv/bin/python
config=/etc/drone-oled/config.toml
[[ -f "$config" && -x "$python" ]]
"$python" -m pip install --upgrade 'pip>=24' 'setuptools>=68' wheel
"$python" -m pip install --no-build-isolation --no-deps --upgrade "$base"
cp -a "$config" "$config.before-fc-managed-arm.$(date +%Y%m%d-%H%M%S)"
"$python" - <<'PY'
from pathlib import Path
import re
p = Path('/etc/drone-oled/config.toml')
s = p.read_text()
start = s.index('[safety]')
end = s.find('\n[', start + 1)
if end < 0:
    end = len(s)
section = s[start:end]
values = dict(commands_enabled='true', arm_enabled='true', arm_policy='"fc_managed"',
              firmware_profile='""', mission_enabled='false', motor_test_enabled='false',
              calibration_enabled='false', parameter_write_enabled='false')
for key, value in values.items():
    pattern = rf'(?m)^{key}\s*=.*$'
    if re.search(pattern, section):
        section = re.sub(pattern, f'{key} = {value}', section)
    else:
        section += f'\n{key} = {value}\n'
p.write_text(s[:start] + section.rstrip() + '\n' + s[end:])
PY
runuser -u drone-oled -- "$python" -m drone_oled.app --config "$config" --validate
systemctl reset-failed drone-oled.service
systemctl restart drone-oled.service
sleep 2
if ! systemctl is-active --quiet drone-oled.service; then
  journalctl -u drone-oled.service -n 40 --no-pager
  exit 1
fi
echo 'Arm/disarm enabled: confirm on OLED. FC arming checks remain authoritative.'
echo 'No arm command or FC parameter write was sent by this setup.'
echo 'Mission starts, motor tests, calibration and parameter writes remain disabled.'
