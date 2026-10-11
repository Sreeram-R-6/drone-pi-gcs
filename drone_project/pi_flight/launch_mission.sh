#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -ne 0 ]] || { echo 'Refusing mission as root'; exit 2; }
[[ -x "$base/.venv/bin/python" ]] || { echo "Run bash $base/install_pi.sh first"; exit 2; }
source "$base/.venv/bin/activate"
exec python "$base/preflight_hover.py" --config "$base/config.toml" "$@"
