"""Offline tests: mocked aircraft; recorder integration uses generated frames."""
import contextlib
import io
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flight_2m import mission, recorder


class FlightTests(unittest.TestCase):
    def test_plan_opens_neither_camera_nor_vehicle(self):
        with patch.object(mission, "Link") as link, patch.object(mission, "VideoRecorder") as video:
            with patch("sys.argv", ["flight_2m.py"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mission.main(), 0)
            link.assert_not_called()
            video.from_options.assert_not_called()

    def fake_flight(self, camera_error=False, takeoff_error=False):
        events = []
        link = MagicMock()
        link.cached.return_value = SimpleNamespace()
        link.is_armed.return_value = False
        link.wait_for.return_value = SimpleNamespace(x=0, y=0, z=0, yaw=0)
        link.set_mode.side_effect = lambda mode: events.append(mode)
        video = MagicMock()
        video.check.return_value = True
        def start():
            events.append("record")
            if camera_error:
                raise RuntimeError("camera unavailable")
        video.start.side_effect = start
        video.stop.side_effect = lambda: events.append("stop_record")
        def takeoff(*args):
            events.append("takeoff")
            if takeoff_error:
                raise mission.flight.FlightError("takeoff rejected")
        def reference(*args):
            events.append("reference")
            return SimpleNamespace(x=0, y=0, z=0), 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(mission, "process_lock", side_effect=lambda name: contextlib.nullcontext()))
            stack.enter_context(patch("sys.argv", ["flight_2m.py", "--execute"]))
            stack.enter_context(patch.object(mission, "Link", return_value=link))
            stack.enter_context(patch.object(mission.VideoRecorder, "from_options", return_value=video))
            stack.enter_context(patch.object(mission, "takeoff_reference", side_effect=reference))
            stack.enter_context(patch.object(mission, "verified_takeoff", side_effect=takeoff))
            stack.enter_context(patch.object(mission, "move_forward", side_effect=lambda *a: events.append("forward")))
            for name in ("configure_streams", "collect_preflight", "run_prearm_checks"):
                stack.enter_context(patch.object(mission.flight, name))
            stack.enter_context(patch.object(mission.flight, "speed_parameter_values", return_value=(MagicMock(), .3, MagicMock(), .3)))
            stack.enter_context(patch.object(mission.flight, "restore_parameters", return_value=[]))
            stack.enter_context(patch.object(mission.flight, "arm", side_effect=lambda *a: events.append("arm")))
            stack.enter_context(patch.object(mission.flight, "land_if_owned", side_effect=lambda *a: events.append("LAND")))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            result = mission.main()
        return result, events

    def test_video_spans_arm_takeoff_forward_rtl(self):
        result, events = self.fake_flight()
        self.assertEqual(result, 0)
        self.assertEqual(events, ["record", "GUIDED", "arm", "reference", "takeoff", "forward", "RTL", "stop_record"])

    def test_camera_failure_prevents_arming(self):
        result, events = self.fake_flight(camera_error=True)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["record", "stop_record"])

    def test_takeoff_failure_lands_and_stops_recording(self):
        result, events = self.fake_flight(takeoff_error=True)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["record", "GUIDED", "arm", "reference", "takeoff", "LAND", "stop_record"])

    def test_legacy_mtf_flow_preferred(self):
        link = MagicMock()
        legacy = SimpleNamespace(flow_comp_m_x=.1, flow_comp_m_y=.2, quality=100)
        link.cached.side_effect = lambda kind, age: legacy if kind == "OPTICAL_FLOW" else SimpleNamespace(quality=0)
        message, kind = mission.flight.optical_flow(link)
        self.assertIs(message, legacy)
        self.assertTrue(mission.flight.flow_values_finite(message, kind))

    def test_takeoff_reference_uses_fresh_post_arm_position(self):
        stale = SimpleNamespace(x=5, y=5, z=0, vx=0, vy=0, vz=0)
        fresh = SimpleNamespace(x=5.8, y=5, z=0, vx=0, vy=0, vz=0)
        heading = SimpleNamespace(yaw=.4, roll=0, pitch=0)
        values = {"LOCAL_POSITION_NED": stale, "ATTITUDE": heading,
                  "DISTANCE_SENSOR": SimpleNamespace(orientation=25, current_distance=6)}
        link = MagicMock()
        link.is_armed.return_value = True
        link.cached.side_effect = lambda kind, age: values.get(kind)
        def receive(kind, timeout, newer_than, predicate=None):
            self.assertGreater(newer_than, 0)
            if kind == "LOCAL_POSITION_NED":
                values[kind] = fresh
            return values[kind]
        link.wait_for.side_effect = receive
        with patch.object(mission.flight, "ensure_guided"), contextlib.redirect_stdout(io.StringIO()):
            start, yaw = mission.takeoff_reference(link)
        self.assertEqual(start.x, fresh.x)
        self.assertEqual(start.y, fresh.y)
        self.assertEqual(start.ground_range_m, .06)
        self.assertEqual(yaw, .4)
        self.assertEqual([call.args[0] for call in link.wait_for.call_args_list],
                         ["LOCAL_POSITION_NED", "ATTITUDE", "DISTANCE_SENSOR"])

    def range_link(self, centimetres, z=0):
        link = MagicMock()
        link.is_armed.return_value = True
        values = {
            "HEARTBEAT": SimpleNamespace(),
            "LOCAL_POSITION_NED": SimpleNamespace(x=0,y=0,z=z,vx=0,vy=0,vz=0),
            "ATTITUDE": SimpleNamespace(roll=0,pitch=0,yaw=0),
            "DISTANCE_SENSOR": SimpleNamespace(orientation=25,current_distance=centimetres,
                                               min_distance=1,max_distance=700),
        }
        link.cached.side_effect = lambda kind, age: values.get(kind)
        return link, values

    def run_range_takeoff(self, centimetres, z):
        link, values = self.range_link(centimetres, z)
        start = mission.TakeoffReference(0,0,0,.06)
        tick = iter(i / 10 for i in range(3000))
        def acknowledge(*args):
            link.last_takeoff_ack = (SimpleNamespace(result=mission.mav.MAV_RESULT_ACCEPTED), mission.time.monotonic())
        link.master.mav.command_long_send.side_effect = acknowledge
        with patch.object(mission.time, "monotonic", side_effect=lambda: next(tick)), \
                patch.object(mission.flight, "ensure_guided_health"), contextlib.redirect_stdout(io.StringIO()):
            return mission.verified_takeoff(link, SimpleNamespace(), start)

    def test_takeoff_confirmed_by_range_even_when_local_z_has_not_changed(self):
        self.assertEqual(self.run_range_takeoff(206, z=0), 0)

    def test_local_altitude_cannot_confirm_takeoff_when_range_stays_on_ground(self):
        with self.assertRaisesRegex(mission.flight.FlightError, "rangefinder height timeout"):
            self.run_range_takeoff(6, z=-2)

    def test_range_height_rejects_missing_wrong_orientation_and_invalid_readings(self):
        link, values = self.range_link(206)
        start = mission.TakeoffReference(0,0,0,.06)
        self.assertAlmostEqual(mission.rangefinder_height(link,start),2)
        values["DISTANCE_SENSOR"].current_distance=65535
        with self.assertRaisesRegex(mission.flight.FlightError,"invalid"):
            mission.rangefinder_height(link,start)
        values["DISTANCE_SENSOR"].current_distance=206
        values["DISTANCE_SENSOR"].orientation=0
        with self.assertRaisesRegex(mission.flight.FlightError,"unavailable"):
            mission.rangefinder_height(link,start)
        values["DISTANCE_SENSOR"]=None
        with self.assertRaisesRegex(mission.flight.FlightError,"unavailable"):
            mission.rangefinder_height(link,start)

    def test_rangefinder_is_tilt_corrected(self):
        link, values = self.range_link(200)
        values["ATTITUDE"].roll=math.radians(10)
        self.assertAlmostEqual(mission.downward_range(link),2*math.cos(math.radians(10)))

    def test_forward_preserves_local_height_at_range_confirmed_takeoff(self):
        link, values = self.range_link(206,z=-.4)
        values["LOCAL_POSITION_NED"].x=2
        tick=iter(i / 10 for i in range(1000))
        with patch.object(mission.time,"monotonic",side_effect=lambda:next(tick)), \
                patch.object(mission.flight,"ensure_guided_health"), \
                patch.object(mission,"send_target_hold_yaw") as target, contextlib.redirect_stdout(io.StringIO()):
            mission.move_forward(link,SimpleNamespace(),mission.TakeoffReference(0,0,0,.06),0,hold_down=-.4)
        self.assertEqual(target.call_args.args[3],-.4)

    def test_takeoff_reference_rejects_disarm(self):
        link = MagicMock()
        link.is_armed.return_value = False
        with patch.object(mission.flight, "ensure_guided"):
            with self.assertRaisesRegex(mission.flight.FlightError, "Disarmed"):
                mission.takeoff_reference(link)

    def test_opt_qua_disabled_airborne_but_missing_flow_rejected(self):
        flow = SimpleNamespace(quality=0, flow_comp_m_x=0, flow_comp_m_y=0)
        values = {
            "EKF_STATUS_REPORT": SimpleNamespace(flags=mission.flight.REQUIRED_EKF_FLAGS),
            "OPTICAL_FLOW": flow,
            "DISTANCE_SENSOR": SimpleNamespace(orientation=25, current_distance=200),
            "SYS_STATUS": SimpleNamespace(voltage_battery=16000, battery_remaining=80),
        }
        link = MagicMock()
        link.cached.side_effect = lambda kind, age: values.get(kind)
        args = SimpleNamespace(min_flow_quality=None, min_battery_voltage=14.4, min_battery_remaining=30)
        with patch.object(mission.flight, "ensure_guided"):
            mission.flight.ensure_guided_health(link, args)
            args.min_flow_quality = 50
            with self.assertRaisesRegex(mission.flight.FlightError, "quality degraded"):
                mission.flight.ensure_guided_health(link, args)
            args.min_flow_quality = None
            del values["OPTICAL_FLOW"]
            with self.assertRaisesRegex(mission.flight.FlightError, "telemetry became stale"):
                mission.flight.ensure_guided_health(link, args)

    def test_opt_qua_disabled_in_preflight(self):
        def message(kind, **kwargs):
            return SimpleNamespace(get_type=lambda: kind, **kwargs)
        gps = message("GPS_RAW_INT", fix_type=3, lat=100000000, lon=200000000, satellites_visible=12, eph=100)
        flow = message("OPTICAL_FLOW", quality=0, flow_comp_m_x=0, flow_comp_m_y=0)
        rangefinder = message("DISTANCE_SENSOR", orientation=25, current_distance=15)
        values = {
            "HEARTBEAT": SimpleNamespace(base_mode=0), "OPTICAL_FLOW": flow,
            "LOCAL_POSITION_NED": SimpleNamespace(), "GLOBAL_POSITION_INT": SimpleNamespace(),
            "ATTITUDE": SimpleNamespace(roll=0, pitch=0),
            "EKF_STATUS_REPORT": SimpleNamespace(flags=mission.flight.REQUIRED_EKF_FLAGS | mission.flight.EKF_ABSOLUTE_HORIZONTAL_POSITION),
            "HOME_POSITION": SimpleNamespace(latitude=100000000, longitude=200000000),
            "SYS_STATUS": SimpleNamespace(voltage_battery=16000, battery_remaining=80),
        }
        args = SimpleNamespace(sample_seconds=1, min_satellites=10, max_eph_cm=150,
                               max_gps_p95=.6, min_flow_quality=None, max_preflight_tilt=8,
                               min_battery_voltage=14.4, min_battery_remaining=30)
        for threshold in (None, 50):
            args.min_flow_quality = threshold
            link = MagicMock(status_text=[])
            link.cached.side_effect = lambda kind, age: values.get(kind)
            samples = iter([gps] * 12 + [flow, rangefinder])
            link.pump.side_effect = lambda timeout: next(samples, None)
            tick = iter(i / 100 for i in range(1000))
            with patch.object(mission.flight.time, "monotonic", side_effect=lambda: next(tick)), \
                    patch.object(mission.flight, "run_prearm_checks"), contextlib.redirect_stdout(io.StringIO()):
                if threshold is None:
                    mission.flight.collect_preflight(link, args)
                else:
                    with self.assertRaisesRegex(mission.flight.FlightError, "flow quality"):
                        mission.flight.collect_preflight(link, args)


class RecorderTests(unittest.TestCase):
    def test_invalid_options(self):
        for kwargs in ({"fps": 0}, {"fps": 121}, {"size": "bad"}, {"camera": "https://example.com"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                recorder.VideoRecorder(**kwargs)

    def test_missing_ffmpeg(self):
        video = recorder.VideoRecorder()
        with patch.object(recorder.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                video.start()
        self.assertIsNone(video.process)

    def test_camera_process_failure_closes_log(self):
        with tempfile.TemporaryDirectory() as folder:
            video = recorder.VideoRecorder(output_dir=folder, lock_dir=Path(folder) / "locks")
            process = MagicMock(returncode=1)
            process.poll.return_value = 1
            with patch.object(recorder.shutil, "which", return_value="ffmpeg"), patch.object(recorder.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "recording failed"):
                    video.start()
            self.assertIsNone(video.log)
            self.assertTrue(video.failed)

    def test_stop_escalates_after_timeout(self):
        video = recorder.VideoRecorder()
        video.process = MagicMock()
        video.process.poll.return_value = None
        video.process.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 5), 0]
        self.assertFalse(video.stop())
        video.process.send_signal.assert_called_once()
        video.process.terminate.assert_called_once()
        video.process.kill.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg integration tools missing")
    def test_real_encoder_writes_playable_video_and_stops(self):
        class SyntheticRecorder(recorder.VideoRecorder):
            def command(self, executable):
                command = super().command(executable)
                return [executable, "-hide_banner", "-nostdin", "-n", "-loglevel", "warning",
                        "-re", "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30"] + command[command.index("-an"):]
        with tempfile.TemporaryDirectory() as folder:
            video = SyntheticRecorder(output_dir=folder, lock_dir=Path(folder) / "locks")
            try:
                video.start()
                self.assertTrue(video.check())
            finally:
                self.assertTrue(video.stop())
            self.assertIsNotNone(video.process.poll())
            result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                     "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0",
                                     str(video.path)], capture_output=True, text=True, check=True)
            self.assertIn("h264,640,480", result.stdout)


if __name__ == "__main__":
    unittest.main()
