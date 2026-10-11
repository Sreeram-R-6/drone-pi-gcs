#!/usr/bin/env bash
# Install the local mirror without changing MAVProxy or vehicle-control settings.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run sudo bash deploy/install-web.sh' >&2
  exit 2
fi
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python=/opt/drone-oled/venv/bin/python
config=/etc/drone-oled/config.toml
[[ -x "$python" && -f "$config" ]]
"$python" -m pip install --upgrade 'pip>=24' 'setuptools>=68' wheel
"$python" -m pip install --no-build-isolation --no-deps --upgrade "$base"
cp -a "$config" "$config.before-web.$(date +%Y%m%d-%H%M%S)"
"$python" - <<'PY'
from pathlib import Path
import re
p=Path('/etc/drone-oled/config.toml')
s=p.read_text()
section='[web]\nenabled = true\nsocket = "/run/drone-oled/ui.sock"\n'
if re.search(r'(?m)^\[web\]\s*$',s):
    s=re.sub(r'(?ms)^\[web\]\s*\n.*?(?=^\[|\Z)',section+'\n',s)
else:
    s+='\n'+section
p.write_text(s)
PY
runuser -u drone-oled -- "$python" -m drone_oled.app --config "$config" --validate
install -m 644 "$base/deploy/drone-oled.service" "$base/deploy/drone-oled-web.service" /etc/systemd/system/
systemctl daemon-reload
for unit in drone-oled.service drone-oled-web.service; do
  # A new unit may not be loaded yet; reset-failed would abort a first install.
  if systemctl is-failed --quiet "$unit"; then
    systemctl reset-failed "$unit"
  fi
done
systemctl enable drone-oled.service drone-oled-web.service
systemctl restart drone-oled.service
systemctl restart drone-oled-web.service
sleep 2
if ! systemctl is-active --quiet drone-oled.service || ! systemctl is-active --quiet drone-oled-web.service; then
  journalctl -u drone-oled.service -u drone-oled-web.service -n 50 --no-pager
  exit 1
fi
"$python" - <<'PY'
import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:8090/api/frame',timeout=3) as response:
    frame=json.load(response)
assert frame.get('frame') and frame['age']<1, frame
print('Verified live OLED frames. Open http://localhost:8090 on the Pi.')
PY
echo 'Local web service installed, running, and enabled at boot.'
echo 'For an SSH browser tunnel: ssh -L 8090:127.0.0.1:8090 user@PI_ADDRESS'
