import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flightcheck.config import load, unresolved
import preflight_hover
import proxy


class OfflineTests(unittest.TestCase):
    def test_offline_never_constructs_transport_or_socket(self):
        c = load(Path(__file__).parents[1]/"config.example.toml")
        with tempfile.TemporaryDirectory() as base:
            c["_base"] = base
            with patch("preflight_hover.load", return_value=c), patch("preflight_hover.Transport") as transport, \
                    patch("socket.socket", side_effect=AssertionError("socket opened")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight_hover.main(["--offline"]), 0)
                self.assertEqual(preflight_hover.main(["--offline", "--flight"]), 3)
                self.assertEqual(proxy.main(["--offline"]), 0)
                transport.assert_not_called()

    def test_missing_confirmation_blocks_before_transport(self):
        c = load(Path(__file__).parents[1]/"config.example.toml")
        with tempfile.TemporaryDirectory() as base:
            c["_base"] = base
            with patch("preflight_hover.load", return_value=c), patch("preflight_hover.Transport") as transport, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight_hover.main(["--check-once"]),3)
                transport.assert_not_called()

    def test_current_config_cannot_authorize_flight(self):
        c = load(Path(__file__).parents[1]/"config.example.toml")
        missing = unresolved(c)
        self.assertIn("UART device confirmation",missing)
        self.assertIn("at least 15 m clear controlled radius",missing)
        self.assertNotIn("commissioning.flow_axes_range_calibration_reviewed",missing)

    def test_wait_requires_deadline(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            preflight_hover.main(["--wait-for-fcu"])
