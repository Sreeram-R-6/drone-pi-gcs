#!/usr/bin/env bash
# Dependency/config setup only: no FC connection and no service restart.
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID -ne 0 ]] || { echo 'Run as your normal user, not root.'; exit 2; }
if [[ ${1:-} == --help ]]; then
    echo 'Usage: bash scripts/setup_pi.sh [--skip-apt]'; exit 0
fi
[[ $# -eq 0 || ( $# -eq 1 && $1 == --skip-apt ) ]] || { echo 'Unknown option'; exit 2; }
if [[ ${1:-} != --skip-apt ]]; then
    [[ -r /proc/device-tree/model ]] || { echo 'This installer targets a Raspberry Pi.'; exit 2; }
    model=$(tr -d '\0' < /proc/device-tree/model)
    [[ $model == 'Raspberry Pi 4 Model B'* ]] || { echo "Unverified model: $model"; exit 2; }
    sudo apt-get update
    sudo apt-get install -y python3-venv python3-dev python3-pip build-essential \
        libxml2-dev libxslt1-dev zlib1g-dev psmisc git tmux
fi
cd "$base"
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
python3 -m venv drone_project/pi_flight/.venv
python=drone_project/pi_flight/.venv/bin/python
"$python" -m pip install --upgrade pip
"$python" -m pip install -r requirements.txt
"$python" -m pip check
"$python" scripts/create_local_config.py
echo 'GCS dependencies ready. No FC commands sent or services changed.'
echo 'Next: ./gcs --offline; see README for OLED/shared-router installation.'
