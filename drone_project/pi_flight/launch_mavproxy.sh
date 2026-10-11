#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -ne 0 ]] || { echo 'Refusing MAVProxy as root'; exit 2; }
[[ -x "$base/.venv/bin/python" ]] || { echo "Run bash $base/install_pi.sh first"; exit 2; }
exec "$base/.venv/bin/python" "$base/proxy.py" "$@"
