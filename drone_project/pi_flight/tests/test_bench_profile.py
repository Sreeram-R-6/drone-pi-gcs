import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import preflight_hover
from flightcheck.config import load, bench_profile
from flightcheck.checks import evaluate
from flightcheck.telemetry import Store, Transport, Rejected
from reviewed_fixture import reviewed_config


class BenchTests(unittest.TestCase):
    def test_synthetic_review_has_limits_but_no_firmware_approval(self):
        config = reviewed_config()
        checks = evaluate(Store(),config,0,3)
        self.assertFalse(any(c.explanation.startswith('Limits unset:') for c in checks))
        self.assertTrue(config['commissioning']['thresholds_approved']) # Synthetic test input only.
        self.assertFalse(config['commissioning']['firmware_behavior_reviewed'])
        self.assertFalse(config['commissioning']['manual_hover_log_reviewed'])
    def test_overlay_is_positive_and_does_not_modify_flight_config(self):
        original = load(Path(__file__).parents[1]/'config.example.toml')
        bench = bench_profile(original)
        self.assertTrue(bench['_bench_only'])
        self.assertEqual(original['gps']['max_hdop'],0)
        self.assertEqual(bench['gps']['max_hdop'],4)
        self.assertEqual(bench['flow']['sensor_id'],0)
        checks = evaluate(Store(),bench,0,3)
        self.assertFalse(any(c.explanation.startswith('Limits unset:') for c in checks))

    def test_bench_blocks_motor_mission_and_erase_before_opening_uart(self):
        for flag in (['--flight'],['--arm-only'],['--disarm-only'],['--maintenance','erase-logs']):
            ui = SimpleNamespace()
            with patch('preflight_hover.load', return_value=load(Path(__file__).parents[1]/'config.example.toml')), \
                 patch('preflight_hover.Transport') as transport,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight_hover.main(['--bench','--check-once']+flag,ui=ui),3)
            transport.assert_not_called()
            self.assertIn('BENCH ONLY',ui.workflow_status)

    def test_transport_cannot_bypass_bench_motor_lock(self):
        t = object.__new__(Transport)
        t.receive_only = False
        t.c = {'_bench_only':True}
        t.connection = Mock()
        t.flight_authorized = True
        with self.assertRaises(Rejected):
            t.command(400,[1,0],flight=True)
        t.connection.mav.command_long_send.assert_not_called()
