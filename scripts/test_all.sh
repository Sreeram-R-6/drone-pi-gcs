#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python="$base/drone_project/pi_flight/.venv/bin/python"
[[ -x $python ]] || { echo 'Run scripts/setup_pi.sh first.'; exit 2; }
"$python" "$base/scripts/test_tools.py"
for script in "$base"/scripts/*.sh "$base"/drone_project/pi_flight/*.sh; do
    bash -n "$script"
done
cd "$base/drone_project/pi_flight"
# Isolate legacy test mission locks from a running real GCS. Tests use mocked
# transports, PTYs and local sockets, never the real FC device.
"$python" - <<'PY'
import tempfile, unittest
from unittest.mock import patch
import preflight_hover
from flightcheck import monitor, telemetry
original = telemetry.process_lock
with tempfile.TemporaryDirectory(prefix='gcs-tests-') as root:
    def isolated(name):
        return original('test-' + root.rsplit('/',1)[-1] + '-' + name)
    with patch.object(preflight_hover,'process_lock',isolated), \
         patch.object(monitor,'process_lock',isolated):
        result = unittest.TextTestRunner(verbosity=1).run(
            unittest.defaultTestLoader.discover('tests'))
raise SystemExit(not result.wasSuccessful())
PY
cd "$base/drone_project/drone_oled"
oled_python="$python"
if ! "$oled_python" -c 'import pytest' 2>/dev/null; then
    oled_python="$base/drone_project/drone_oled/.venv/bin/python"
    [[ -x $oled_python ]] || { echo 'Install requirements-dev.txt in the GCS venv first.'; exit 2; }
fi
"$oled_python" -m pytest -q
