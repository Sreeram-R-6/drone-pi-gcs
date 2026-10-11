import os
from pathlib import Path
import pty
import select
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from flightcheck.config import load
from flightcheck.telemetry import Rejected, Transport, serial_owner
from reviewed_fixture import reviewed_config


class UartTransportTests(unittest.TestCase):
    def test_deployed_gcs_shares_router_without_opening_uart(self):
        c = reviewed_config()
        self.assertEqual(c['link']['endpoint'], 'udpin:127.0.0.1:14550')
        self.assertEqual(c['link']['baud'], 115200)
        with patch('flightcheck.telemetry.serial_owner') as owner, \
             patch('pymavlink.mavutil.mavlink_connection') as connect:
            t = Transport(c, Mock(), receive_only=True)
            try:
                owner.assert_not_called()
                self.assertFalse(t.direct_uart)
                self.assertEqual(connect.call_args.args[0], c['link']['endpoint'])
                self.assertEqual(connect.call_args.kwargs['source_system'], 254)
            finally:
                t.close()

    def test_saved_default_is_direct_uart(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        self.assertEqual(c['link']['endpoint'],'/dev/serial0')
        self.assertEqual(c['link']['baud'],115200)

    def test_busy_uart_blocks_before_pymavlink_opens_it(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        master,slave = pty.openpty()
        c['link']['endpoint'] = os.ttyname(slave)
        os.close(slave)
        try:
            with patch('flightcheck.telemetry.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='1234',stderr='')),patch('pymavlink.mavutil.mavlink_connection') as connect:
                with self.assertRaisesRegex(Rejected,'UART busy'):
                    Transport(c,Mock(),receive_only=True)
                connect.assert_not_called()
        finally:
            os.close(master)

    def test_real_uart_bytes_receive_without_udp_or_transmission(self):
        from pymavlink.dialects.v20 import ardupilotmega as mav
        c = load(Path(__file__).parents[1]/'config.example.toml')
        master,slave = pty.openpty()
        endpoint = os.ttyname(slave)
        os.close(slave)
        c['link']['endpoint'] = endpoint
        transport = None
        try:
            # Sandbox fuser cannot enumerate network namespaces; simulate an idle
            # ownership check while exercising actual PTY bytes and device locks.
            with patch('flightcheck.telemetry.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='')):
                transport = Transport(c,SimpleNamespace(event=Mock()),receive_only=True)
            self.assertEqual(transport.connection.port.baudrate,115200)
            packet = mav.MAVLink_heartbeat_message(2,3,1,0,3,3).pack(mav.MAVLink(None,srcSystem=1,srcComponent=1))
            os.write(master,packet)
            transport.connect(.5)
            self.assertFalse(transport.armed())
            transport.heartbeat()
            self.assertFalse(select.select([master],[],[],.1)[0])
            transport.request_monitor_telemetry()
            self.assertTrue(select.select([master],[],[],.2)[0])
            parser = mav.MAVLink(None)
            messages = parser.parse_buffer(os.read(master,65536))
            self.assertEqual(messages[0].get_type(),'HEARTBEAT')
            self.assertEqual(messages[1].get_type(),'REQUEST_DATA_STREAM')
            self.assertTrue(all(m.get_type() == 'PARAM_REQUEST_READ' for m in messages[2:4]))
            self.assertTrue(all(m.command == 511 for m in messages[4:]))
            transport.request_monitor_telemetry()
            self.assertFalse(select.select([master],[],[],.1)[0])
            with self.assertRaises(Rejected):
                transport.command(400,[1,0],flight=True)
            # The canonical lock protects aliases/cooperating checkouts.
            with self.assertRaisesRegex(RuntimeError,'Duplicate owner'):
                with serial_owner(endpoint):
                    pass
            transport.close()
            transport = None
            # Closing releases both the UART and the canonical lock.
            with patch('flightcheck.telemetry.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='')):
                with serial_owner(endpoint):
                    pass
        finally:
            if transport:
                transport.close()
            os.close(master)


if __name__ == '__main__':
    unittest.main()
