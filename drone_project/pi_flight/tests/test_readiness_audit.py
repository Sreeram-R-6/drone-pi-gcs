import contextlib
import unittest
from unittest.mock import Mock, patch
import readiness_audit


class AuditTests(unittest.TestCase):
    def connection(self, armed=False):
        c, hb = Mock(), Mock()
        hb.get_srcSystem.return_value = 1
        hb.get_srcComponent.return_value = 1
        hb.get_type.return_value = 'HEARTBEAT'
        hb.to_dict.return_value = {'base_mode': 128 if armed else 0}
        c.recv_match.return_value = hb
        return c

    def test_audit_sends_only_reads_and_message_requests(self):
        c = self.connection()
        with patch('pymavlink.mavutil.mavlink_connection', return_value=c), \
             patch('flightcheck.telemetry.process_lock', return_value=contextlib.nullcontext()), \
             patch('readiness_audit.time.monotonic', return_value=0):
            readiness_audit.run(seconds=0)
        self.assertEqual([call.args[2] for call in c.mav.command_long_send.call_args_list],
                         [512, 512, 512, 511, 511, 511])
        self.assertEqual(c.mav.param_request_read_send.call_count, len(readiness_audit.PARAMETERS))
        c.mav.param_set_send.assert_not_called()
        c.mav.set_mode_send.assert_not_called()
        c.close.assert_called_once()

    def test_armed_vehicle_stops_before_any_request(self):
        c = self.connection(armed=True)
        with patch('pymavlink.mavutil.mavlink_connection', return_value=c), \
             patch('flightcheck.telemetry.process_lock', return_value=contextlib.nullcontext()):
            with self.assertRaisesRegex(RuntimeError, 'FC armed'):
                readiness_audit.run(seconds=0)
        c.mav.command_long_send.assert_not_called()
        c.mav.param_request_read_send.assert_not_called()
        c.close.assert_called_once()
