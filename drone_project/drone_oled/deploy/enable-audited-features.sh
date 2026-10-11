#!/usr/bin/env bash
# Applies local feature permissions only. Does not write FC parameters or arm.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run sudo bash deploy/enable-audited-features.sh' >&2
  exit 2
fi
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python=/opt/drone-oled/venv/bin/python
config=/etc/drone-oled/config.toml
[[ -s "$base/commissioning/profile.live.json" && -x "$python" && -f "$config" ]]
"$python" -m pip install --upgrade 'pip>=24' 'setuptools>=68' wheel
"$python" -m pip install --no-build-isolation --no-deps --upgrade "$base"
cp -a "$config" "$config.before-audited-features.$(date +%Y%m%d-%H%M%S)"
install -m 644 "$base/commissioning/profile.live.json" /etc/drone-oled/profile.live.json
"$python" - <<'PY'
from pathlib import Path
import re
p=Path('/etc/drone-oled/config.toml');s=p.read_text()
start=s.index('[safety]');end=s.find('\n[',start+1)
if end<0:end=len(s)
section=s[start:end]
values={key:'true' for key in ('commands_enabled','arm_enabled','mission_enabled','motor_test_enabled','calibration_enabled','parameter_write_enabled')}
values.update(arm_policy='"fc_managed"',firmware_profile='"/etc/drone-oled/profile.live.json"',abort_mode='"RTL"',abort_fallback='"LAND"')
for key,value in values.items():
 pattern=rf'(?m)^{key}\s*=.*$'
 section=re.sub(pattern,f'{key} = {value}',section) if re.search(pattern,section) else section+f'\n{key} = {value}\n'
p.write_text(s[:start]+section.rstrip()+'\n'+s[end:])
PY
runuser -u drone-oled -- "$python" -m drone_oled.app --config "$config" --validate
systemctl restart drone-oled.service
sleep 2
if ! systemctl is-active --quiet drone-oled.service; then
  journalctl -u drone-oled.service -n 50 --no-pager
  exit 1
fi
echo 'Audited feature permissions enabled. No FC parameters changed; no actions executed.'
echo 'Motor tests: REMOVE PROPS, confirm each test. A=FR B=RR C=RL D=FL.'
echo 'Failsafe editing: OK to edit; PREV/NEXT or UP/DOWN change; OK confirms; BACK cancels.'
echo 'Missions still require executable plans, fresh position/home and readiness.'
echo 'Accel six-axis and optical-flow calibration remain GCS-guided.'
