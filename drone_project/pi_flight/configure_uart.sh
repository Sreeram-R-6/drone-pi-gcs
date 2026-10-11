#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=python3
[[ ! -x "$base/.venv/bin/python" ]] || python="$base/.venv/bin/python"
exec "$python" "$base/uart.py" configure "$@"
