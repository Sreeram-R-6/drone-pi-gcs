#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -ne 0 ]] || { echo 'Run as your normal user; sudo is used only for apt/groups.'; exit 2; }
source /etc/os-release
case "$ID" in ubuntu|debian|raspbian) ;; *) echo "Unsupported distribution: $ID"; exit 2;; esac
arch=$(dpkg --print-architecture)
case "$arch" in arm64|armhf) ;; *) echo "Unsupported Pi architecture: $arch"; exit 2;; esac
echo "Installing for $ID $VERSION_ID $arch; no FC connection will be attempted."
needed=()
for package in python3-venv python3-dev python3-pip tmux; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
        needed+=("$package")
    fi
done
if ((${#needed[@]})); then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends "${needed[@]}"
fi
python3 -m venv "$base/.venv"
if grep -q '==' "$base/requirements.lock.txt"; then
    "$base/.venv/bin/python" -m pip install -r "$base/requirements.lock.txt"
else
    "$base/.venv/bin/python" -m pip install -r "$base/requirements.in"
fi
"$base/.venv/bin/python" -m pip check
"$base/.venv/bin/python" -c 'import pymavlink, MAVProxy, yaml, tomli; from importlib.metadata import version; print({p: version(p) for p in ("MAVProxy", "pymavlink", "PyYAML")})'
"$base/.venv/bin/mavproxy.py" --version
# pip freeze is a generated lock artifact, not a manually edited source file.
"$base/.venv/bin/python" -m pip freeze > "$base/requirements.lock.txt"
mkdir -p "$base/wheelhouse"
"$base/.venv/bin/python" -m pip download -r "$base/requirements.lock.txt" -d "$base/wheelhouse"
(cd "$base/wheelhouse" && sha256sum -- * > "$base/requirements.artifacts.sha256")
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    sudo usermod -aG dialout "$USER"
    printf 'Log out and back in. Next command: bash %q --offline\n' "$base/inspect_uart.sh"
fi
echo 'Installation complete; nothing will start at boot.'
printf 'Next: bash %q --offline\n' "$base/launch_mission.sh"
printf 'Future props-off check (only after device/wiring confirmation): bash %q --check-once\n' "$base/start_tmux.sh"
