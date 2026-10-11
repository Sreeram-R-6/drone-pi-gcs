import hashlib
import contextlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from flightcheck.maintenance import download_one, list_logs, sensor_details, verify_backup
from flightcheck.maintenance import run
from flightcheck.config import load
from flightcheck.telemetry import Rejected, Store
from flightcheck.terminal import Terminal
import terminal_gcs


class MaintenanceTests(unittest.TestCase):
    def test_armed_fc_blocks_erasure_before_any_erase_request(self):
        c = load(Path(__file__).parents[1]/'config.example.toml')
        t = Mock()
        t.target = (1,1)
        t.armed.return_value = True
        t.mode.return_value = 'STABILIZE'
        ui = SimpleNamespace(draw=Mock(),observe=Mock())
        with tempfile.TemporaryDirectory() as base:
            c['_base'] = base
            with patch('flightcheck.maintenance.Transport',return_value=t),patch('flightcheck.maintenance.process_lock',return_value=contextlib.nullcontext()):
                self.assertEqual(run(c,ui,'erase-logs'),3)
        t.connection.mav.log_erase_send.assert_not_called()
        self.assertEqual(ui.view_state,'MAINTENANCE BLOCKED')
    def test_hotkeys_only_request_service(self):
        for key,action in [('F','diagnostics'),('L','download-logs'),('X','erase-logs')]:
            keys = iter([ord(key),10,-1])
            screen = SimpleNamespace(keypad=lambda _:None,nodelay=lambda _:None,getch=lambda:next(keys))
            with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
                ui = Terminal(screen)
            ui.receive_only = True
            ui.keys()
            self.assertEqual(ui.service_request,action)
            self.assertIsNone(ui.manual_action)
            self.assertIsNone(ui.request)
            self.assertEqual(screen.getch(),10)
            args = terminal_gcs.service_args(['--monitor'],action)
            self.assertNotIn('--flight',args)
            self.assertNotIn('--arm-only',args)

    def test_sensor_bit_failure_is_named(self):
        mask = 1|2|4|8|32|64|65536|268435456
        detail = sensor_details({'onboard_control_sensors_present':mask,
            'onboard_control_sensors_enabled':mask & ~64,
            'onboard_control_sensors_health':mask & ~268435456})
        self.assertEqual(detail['disabled_required'],['optical flow'])
        self.assertEqual(detail['unhealthy_required'],['pre-arm check'])

    def transfer(self,payload,missing=False):
        clock = [0.]
        store = Store()
        mav = Mock()
        queued = []
        def request(system,component,log_id,offset,count):
            chunks = []
            for start in range(offset,offset+count,90):
                raw = payload[start:min(start+90,offset+count)]
                if missing and start == 90:
                    continue
                chunks.append({'id':log_id,'ofs':start,'count':len(raw),'data':list(raw)})
            queued.extend(reversed(chunks))
        mav.log_request_data_send.side_effect = request
        def pump():
            clock[0] += .1
            while queued:
                store.put('LOG_DATA',queued.pop(),clock[0])
        t = SimpleNamespace(target=(1,1),store=store,connection=SimpleNamespace(mav=mav),pump=pump,heartbeat=Mock())
        return t,clock

    def test_download_complete_and_checksum(self):
        payload = bytes(range(256))*150
        t,clock = self.transfer(payload)
        with tempfile.TemporaryDirectory() as base,patch('flightcheck.maintenance.time.monotonic',side_effect=lambda:clock[0]):
            path = Path(base)/'log-1.bin'
            sha = download_one(t,{'id':1,'size':len(payload)},path,Mock(),Mock())
            self.assertEqual(path.read_bytes(),payload)
            self.assertEqual(sha,hashlib.sha256(payload).hexdigest())
            self.assertFalse(path.with_suffix('.bin.part').exists())
        t.connection.mav.log_erase_send.assert_not_called()

    def test_missing_packet_retains_partial_and_does_not_erase(self):
        t,clock = self.transfer(bytes(range(180)),missing=True)
        with tempfile.TemporaryDirectory() as base,patch('flightcheck.maintenance.time.monotonic',side_effect=lambda:clock[0]):
            path = Path(base)/'log-1.bin'
            with self.assertRaises(Rejected):
                download_one(t,{'id':1,'size':180},path,Mock(),Mock())
            self.assertFalse(path.exists())
            self.assertTrue(path.with_suffix('.bin.part').exists())
        t.connection.mav.log_erase_send.assert_not_called()

    def test_erase_backup_checks_identity_inventory_and_hash(self):
        raw = b'flight evidence'
        entry = {'id':1,'size':len(raw),'time_utc':100}
        manifest = {'uid':123,'logs':[{**entry,'sha256':hashlib.sha256(raw).hexdigest()}]}
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root/'log-1.bin').write_bytes(raw)
            verify_backup(root,manifest,[entry],123)
            for identity,entries in [(None,[entry]),(456,[entry]),(123,[])]:
                with self.assertRaises(Rejected):
                    verify_backup(root,manifest,entries,identity)
            (root/'log-1.bin').write_bytes(b'x'*len(raw))
            with self.assertRaises(Rejected):
                verify_backup(root,manifest,[entry],123)

    def test_log_list_requires_complete_inventory(self):
        t,clock = self.transfer(b'')
        t.connection.mav.log_request_list_send.side_effect = lambda *args:t.store.put('LOG_ENTRY',
            {'id':1,'size':10,'num_logs':2,'time_utc':0},clock[0])
        with patch('flightcheck.maintenance.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(Rejected):
                list_logs(t,Mock())
