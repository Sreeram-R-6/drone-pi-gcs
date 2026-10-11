#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -ne 0 ]] || { echo 'Run tmux as normal user'; exit 2; }
[[ -x "$base/.venv/bin/python" ]] || { echo "Run bash $base/install_pi.sh first"; exit 2; }
exec "$base/.venv/bin/python" "$base/tmux_session.py" "$@"
