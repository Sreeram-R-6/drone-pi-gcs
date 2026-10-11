#!/usr/bin/env bash
# Explicit Pi deployment only; does not alter UART overlays or FC parameters.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo "Run sudo bash deploy/install.sh on the Raspberry Pi" >&2
  exit 2
fi
if [[ ! -r /proc/device-tree/model ]]; then
  echo "Refusing install: no Raspberry Pi device tree" >&2
  exit 2
fi
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
apt-get update
apt-get install -y python3-venv python3-dev python3-lgpio python3-gpiozero i2c-tools psmisc iw iproute2
for account in drone-oled drone-router; do
  if ! id "$account" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/var/lib/$account" --shell /usr/sbin/nologin "$account"
  fi
done
groupadd --system --force i2c
groupadd --system --force gpio
install -d /etc/udev/rules.d
# Run after Ubuntu's 99-gpio.rules, which otherwise restores group dialout.
rm -f /etc/udev/rules.d/70-drone-oled.rules
cat > /etc/udev/rules.d/99-z-drone-oled.rules <<'RULES'
SUBSYSTEM=="i2c-dev", KERNEL=="i2c-1", GROUP="i2c", MODE="0660"
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
RULES
udevadm control --reload-rules
udevadm trigger --subsystem-match=i2c-dev
udevadm trigger --subsystem-match=gpio
usermod -aG i2c,gpio drone-oled
usermod -aG dialout drone-router
install -d /opt/drone-oled /etc/drone-oled /var/lib/drone-oled/missions
python3 -m venv /opt/drone-oled/venv
# Ubuntu 22.04's setuptools 59 cannot read PEP 621 project metadata. Upgrade
# explicitly and use this backend, rather than inheriting a system backend.
/opt/drone-oled/venv/bin/python -m pip install --upgrade 'pip>=24' 'setuptools>=68' wheel
/opt/drone-oled/venv/bin/python -m pip uninstall -y UNKNOWN
/opt/drone-oled/venv/bin/python -m pip install --no-build-isolation --upgrade "$base[pi,router]"
/opt/drone-oled/venv/bin/python - <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import sys
package = distribution('drone-oled')
assert package.version == '0.1.0', package.version
for executable in ('drone-oled', 'drone-oled-router'):
    assert (Path(sys.executable).parent / executable).is_file(), executable
from drone_oled.app import main
from drone_oled.input import GPIOButtons
from luma.oled.device import ssd1306
print('Verified drone-oled package, entry points and hardware imports')
PY
if [[ ! -e /etc/drone-oled/config.toml ]]; then
  install -m 644 "$base/config.toml" /etc/drone-oled/config.toml
  /opt/drone-oled/venv/bin/python - <<'PY'
from pathlib import Path
p = Path('/etc/drone-oled/config.toml')
s = p.read_text().replace('directory = "missions"', 'directory = "/var/lib/drone-oled/missions"')
s = s.replace('directory = ""', 'directory = "/var/lib/drone-oled/logs"')
# Conservative until the configured physical I2C clock has been verified.
s = s.replace('i2c_hz = 400000', 'i2c_hz = 100000').replace('max_fps = 10', 'max_fps = 5')
p.write_text(s)
PY
fi
if [[ ! -e /etc/drone-oled/router.toml ]]; then
  install -m 644 "$base/deploy/router.toml" /etc/drone-oled/router.toml
fi
for fixture in "$base"/missions/*.json; do
  # Recover empty files left by a failed deployment; preserve nonempty missions.
  [[ -s "/var/lib/drone-oled/missions/$(basename "$fixture")" ]] || install -m 644 "$fixture" /var/lib/drone-oled/missions/
done
chown -R drone-oled:drone-oled /var/lib/drone-oled
runuser -u drone-oled -- /opt/drone-oled/venv/bin/drone-oled --config /etc/drone-oled/config.toml --validate
install -m 644 "$base/deploy/drone-oled.service" "$base/deploy/mavproxy.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable drone-oled.service
if [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]]; then
  preview_uid=$(id -u "$SUDO_USER")
  if [[ -S /run/user/$preview_uid/bus ]]; then
    runuser -u "$SUDO_USER" -- env XDG_RUNTIME_DIR="/run/user/$preview_uid" systemctl --user disable --now drone-oled.service || true
  fi
fi
systemctl reset-failed drone-oled.service
systemctl restart drone-oled.service
sleep 2
if ! systemctl is-active --quiet drone-oled.service; then
  journalctl -u drone-oled.service -n 40 --no-pager
  exit 1
fi
echo "Installed. Configure I2C/UART per README. Router remains hardware_confirmed=false."
echo "System OLED service started; check sudo systemctl status drone-oled.service."
echo "Vehicle commands remain disabled. No FC parameters were changed."
