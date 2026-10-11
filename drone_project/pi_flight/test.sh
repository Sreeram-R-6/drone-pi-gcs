#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for script in "$base"/*.sh; do bash -n "$script"; done
"$base/.venv/bin/python" -m compileall -q "$base/flightcheck" "$base/tests" "$base"/*.py
cd "$base"
exec "$base/.venv/bin/python" -m unittest discover -s tests -v
