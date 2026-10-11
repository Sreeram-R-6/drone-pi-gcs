#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -ne 0 ]] || { echo 'Refusing GCS as root'; exit 2; }
exec "$base/.venv/bin/python" "$base/terminal_gcs.py" --config "$base/config.toml" "$@"
