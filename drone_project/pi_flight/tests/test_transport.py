import contextlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from flightcheck.telemetry import Rejected, Store, Transport
from test_state_machine import scenario
from mavproxy_flightwatch import FlightWatch


class TransportTests(unittest.TestCase):
    def test_monitor_heartbeat_does_not_wait_for_stream_retry(self):
        t = object.__new__(Transport)
        t.require_live = lambda: None
        t.connection = Mock()
        t.target = (1, 1)
        t.report = Mock()
        t.last_sent_heartbeat = 0
        t.last_monitor_request = None
        with patch('flightcheck.telemetry.time.monotonic', return_value=100):
            t.request_monitor_telemetry()
        with patch('flightcheck.telemetry.time.monotonic', return_value=101):
            t.request_monitor_telemetry()
        self.assertEqual(t.connection.mav.heartbeat_send.call_count, 2)
        self.assertEqual(t.connection.mav.request_data_stream_send.call_count, 1)
        t.connection.mav.param_set_send.assert_not_called()

    def test_parameter_export_ignores_nonindexed_runtime_update(self):
        t = object.__new__(Transport)
        t.target = (1, 1)
        t.connection = Mock()
        t.report = SimpleNamespace(save=Mock())
        t.store = Store()
        for index, name in ((0, "FIRST"), (0xFFFF, "STAT_RUNTIME"), (1, "SECOND")):
            t.store.put("PARAM_VALUE", {
                "param_count": 2, "param_index": index, "param_id": name,
                "param_value": 1.0, "param_type": 9,
            }, 101)
        t.require_live = lambda: None
        t.pump = lambda: None
        t.heartbeat = lambda: None
        with patch("flightcheck.telemetry.time.monotonic", side_effect=[100, 101]):
            t.export_parameters(timeout=60)
        saved_name, saved_entries = t.report.save.call_args.args
        self.assertEqual(saved_name, "complete-parameters.json")
        self.assertEqual([entry["param_id"] for entry in saved_entries], ["FIRST", "SECOND"])

    def test_mode_adapts_stored_heartbeat_dict(self):
        t = object.__new__(Transport)
        t.require_live = lambda: {"base_mode": 1, "custom_mode": 7}
        captured = []
        t.mavutil = SimpleNamespace(mode_string_v10=lambda message: (
            captured.append((message.get_type(), message.base_mode, message.custom_mode)) or "STABILIZE"))
        self.assertEqual(t.mode(), "STABILIZE")
        self.assertEqual(captured, [("HEARTBEAT", 1, 7)])
    def test_position_mask_with_installed_dialect(self):
        from pymavlink.dialects.v20 import common as mav
        ignored = (mav.POSITION_TARGET_TYPEMASK_VX_IGNORE | mav.POSITION_TARGET_TYPEMASK_VY_IGNORE |
                   mav.POSITION_TARGET_TYPEMASK_VZ_IGNORE | mav.POSITION_TARGET_TYPEMASK_AX_IGNORE |
                   mav.POSITION_TARGET_TYPEMASK_AY_IGNORE | mav.POSITION_TARGET_TYPEMASK_AZ_IGNORE |
                   mav.POSITION_TARGET_TYPEMASK_YAW_IGNORE | mav.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE)
        self.assertEqual(ignored,3576)
        self.assertEqual(mav.MAV_FRAME_LOCAL_NED,1)

    def test_parameter_readback_requires_value_and_type(self):
        t = object.__new__(Transport)
        t.c = {"firmware": {"climb_parameter":"WPNAV_SPEED_UP", "climb_parameter_type":9,
                           "climb_parameter_min":10,"climb_parameter_max":1000}}
        t.flight_authorized = True
        t.armed = lambda: False
        t.target = (1,1)
        t.connection = Mock()
        for result in ((40,9),(30,6)):
            t.read_param = lambda name:result
            with self.assertRaises(Rejected):
                t.set_climb_parameter("WPNAV_SPEED_UP",30,9)
        t.read_param = lambda name:(30,9)
        t.set_climb_parameter("WPNAV_SPEED_UP",30,9)
        t.armed = lambda:True
        with self.assertRaises(Rejected):
            t.set_climb_parameter("WPNAV_SPEED_UP",30,9)

    def test_write_succeeded_readback_failed_still_restores(self):
        m,t,r = scenario()
        original = t.set_climb_parameter
        calls = 0
        def change(name,value,kind):
            nonlocal calls
            calls += 1
            original(name,value,kind)
            if calls == 1:
                raise Rejected("readback dropped")
        t.set_climb_parameter = change
        self.assertFalse(m.run())
        self.assertEqual(t.parameter,(250,9))
        self.assertFalse(t.is_armed)

    def test_mavproxy_deadline_and_socket_binding_without_socket(self):
        port = Mock()
        port.getsockname.return_value = ("0.0.0.0",0)
        state = SimpleNamespace(settings=SimpleNamespace(heartbeat=1),
                                status=SimpleNamespace(exit=False),
                                mav_master=[SimpleNamespace(autoreconnect=True)],
                                mav_outputs=[SimpleNamespace(port=port)])
        with tempfile.TemporaryDirectory() as base:
            env = {"PI_FLIGHT_READY":str(Path(base)/"ready.json"), "PI_FLIGHT_TIMEOUT":"5",
                   "PI_FLIGHT_SYSID":"1", "PI_FLIGHT_COMPID":"1", "PI_FLIGHT_MAX_AGE":"2"}
            with patch.dict(os.environ,env), patch("mavproxy_flightwatch.time.monotonic",return_value=0):
                watcher = FlightWatch(state)
                self.assertEqual(state.settings.heartbeat,0)
                self.assertFalse(state.mav_master[0].autoreconnect)
                port.bind.assert_called_once_with(("127.0.0.1",0))
            with patch.dict(os.environ,env), patch("mavproxy_flightwatch.time.monotonic",return_value=6), \
                    contextlib.redirect_stdout(io.StringIO()):
                watcher.idle_task()
                self.assertTrue(state.status.exit)

    def test_mavproxy_rejects_wildcard_bound_command_socket(self):
        port = Mock()
        port.getsockname.return_value = ("0.0.0.0",14550)
        state = SimpleNamespace(settings=SimpleNamespace(heartbeat=1),
                                status=SimpleNamespace(exit=False),mav_master=[],
                                mav_outputs=[SimpleNamespace(port=port)])
        env = {"PI_FLIGHT_READY":"/tmp/unused", "PI_FLIGHT_TIMEOUT":"5",
               "PI_FLIGHT_SYSID":"1", "PI_FLIGHT_COMPID":"1", "PI_FLIGHT_MAX_AGE":"2"}
        with patch.dict(os.environ,env), self.assertRaises(RuntimeError):
            FlightWatch(state)
