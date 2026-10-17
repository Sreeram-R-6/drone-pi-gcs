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

    def fake_flight(self, camera_error=False, takeoff_error=False, hover_error=False, nav_alt=0, auto_fence=0):
        events = []
        link = MagicMock()
        link.cached.return_value = SimpleNamespace()
        link.is_armed.return_value = False
        link.get_parameter.side_effect = lambda names: mission.flight.SavedParameter(names[0], nav_alt if names[0]=="WP_NAVALT_MIN" else auto_fence, 9)
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
        def hover(*args):
            events.append("hover")
            if hover_error:
                raise mission.flight.FlightError("hover did not stabilize")
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
            stack.enter_context(patch.object(mission, "stabilize_launch", side_effect=hover))
            stack.enter_context(patch.object(mission, "move_forward", side_effect=lambda *a: events.append("forward")))
            stack.enter_context(patch.object(mission, "return_launch", side_effect=lambda *a: events.append("return")))
            for name in ("configure_streams", "collect_preflight", "run_prearm_checks"):
                stack.enter_context(patch.object(mission.flight, name))
            stack.enter_context(patch.object(mission.flight, "speed_parameter_values", return_value=(MagicMock(), .3, MagicMock(), .3)))
            restore = stack.enter_context(patch.object(mission.flight, "restore_parameters", return_value=[]))
            stack.enter_context(patch.object(mission.flight, "arm", side_effect=lambda *a: events.append("arm")))
            stack.enter_context(patch.object(mission.flight, "land_if_owned", side_effect=lambda *a: events.append("LAND")))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            result = mission.main()
            if nav_alt and not camera_error:
                self.assertTrue(any(call.args[0].name == "WP_NAVALT_MIN" and call.args[1] == 0
                                    for call in link.set_parameter.call_args_list))
                self.assertTrue(any(parameter.name == "WP_NAVALT_MIN" and parameter.value == nav_alt
                                    for parameter in restore.call_args.args[1]))
        return result, events

    def test_video_spans_arm_takeoff_destination_return_land(self):
        result, events = self.fake_flight()
        self.assertEqual(result, 0)
        self.assertEqual(events, ["record", "GUIDED", "arm", "reference", "takeoff", "hover", "forward", "return", "LAND", "stop_record"])

    def test_camera_failure_prevents_arming(self):
        result, events = self.fake_flight(camera_error=True)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["record", "stop_record"])

    def test_takeoff_failure_lands_and_stops_recording(self):
        result, events = self.fake_flight(takeoff_error=True)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["record", "GUIDED", "arm", "reference", "takeoff", "LAND", "stop_record"])

    def test_unstable_hover_lands_without_forward_travel(self):
        result, events = self.fake_flight(hover_error=True)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["record", "GUIDED", "arm", "reference", "takeoff", "hover", "LAND", "stop_record"])

    def test_delayed_navigation_is_temporarily_removed_and_restored(self):
        result, events = self.fake_flight(nav_alt=2)
        self.assertEqual(result, 0)
        self.assertIn("hover", events)

    def test_takeoff_only_fence_activation_is_not_silently_skipped(self):
        result,events=self.fake_flight(auto_fence=1)
        self.assertEqual(result,1);self.assertEqual(events,[])

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

    def run_range_takeoff(self, centimetres, z, vz=0):
        link, values = self.range_link(centimetres, z)
        values["LOCAL_POSITION_NED"].vz = vz
        start = mission.TakeoffReference(0,0,0,.06)
        tick = iter(i / 10 for i in range(3000))
        def acknowledge(*args):
            link.last_takeoff_ack = (SimpleNamespace(result=mission.mav.MAV_RESULT_ACCEPTED), mission.time.monotonic())
        link.master.mav.command_long_send.side_effect = acknowledge
        with patch.object(mission.time, "monotonic", side_effect=lambda: next(tick)), \
                patch.object(mission.flight, "ensure_guided_health"), contextlib.redirect_stdout(io.StringIO()):
            return mission.verified_takeoff(link, SimpleNamespace(), start)

    def test_takeoff_confirmed_by_range_even_when_local_z_has_not_changed(self):
        self.assertEqual(self.run_range_takeoff(156, z=0), 0)

    def test_local_altitude_cannot_confirm_takeoff_when_range_stays_on_ground(self):
        with self.assertRaisesRegex(mission.flight.FlightError, "rangefinder height timeout"):
            self.run_range_takeoff(6, z=-2)

    def test_height_alone_does_not_confirm_while_climbing(self):
        with self.assertRaisesRegex(mission.flight.FlightError, "rangefinder height timeout"):
            self.run_range_takeoff(156, z=-2, vz=-.3)

    def test_takeoff_velocity_feedback_corrects_xy_and_stops_at_buffered_height(self):
        link,values=self.range_link(6,z=.2)
        local=values['LOCAL_POSITION_NED'];clock=[0.0];velocity=[None];first_height=[];maximum=[0]
        local.x=.15
        def pump(dt):
            clock[0]+=dt
            if velocity[0] is None:
                local.vz=-.2;local.vx=.04;local.vy=0
            else:
                # Simple controller lag, not a certified dynamics simulation.
                for name,request in zip(('vx','vy','vz'),velocity[0]):
                    setattr(local,name,getattr(local,name)+.5*(request-getattr(local,name)))
            local.z+=local.vz*dt;local.x+=local.vx*dt;local.y+=local.vy*dt
            values['DISTANCE_SENSOR'].current_distance=round((.06+.2-local.z)*100)
            maximum[0]=max(maximum[0],values['DISTANCE_SENSOR'].current_distance/100)
        def command(*args):
            height=mission.rangefinder_height(link,mission.TakeoffReference(0,0,.2,.06))
            first_height.append(height);velocity[0]=args[8:11]
            self.assertEqual(args[4],0x09C7)
            self.assertLessEqual(math.hypot(args[8],args[9]),.2+1e-9)
            self.assertLessEqual(abs(args[10]),.15+1e-9)
        def ack(*args):
            self.assertEqual(args[-1],.8)
            link.last_takeoff_ack=(SimpleNamespace(result=mission.mav.MAV_RESULT_ACCEPTED),clock[0])
        link.pump.side_effect=pump;link.master.mav.command_long_send.side_effect=ack
        link.master.mav.set_position_target_local_ned_send.side_effect=command
        with patch.object(mission.time,'monotonic',side_effect=lambda:clock[0]),patch.object(mission.flight,'ensure_guided_health'),contextlib.redirect_stdout(io.StringIO()):
            mission.verified_takeoff(link,SimpleNamespace(),mission.TakeoffReference(0,0,.2,.06),.3)
        self.assertLess(first_height[0],1)
        self.assertLess(abs(local.x),.15)
        self.assertLess(maximum[0],1.85)
        self.assertLess(abs(mission.rangefinder_height(link,mission.TakeoffReference(0,0,.2,.06))-1.5),.1)

    def test_hover_correction_converges_from_both_drift_directions(self):
        for direction in (-1,1):
            with self.subTest(direction=direction):
                link,values=self.range_link(156,z=-.4)
                local=values['LOCAL_POSITION_NED'];local.x=direction*.35;local.y=-direction*.15
                clock=[0.0];velocity=[(0,0,0)]
                def command(*args):
                    self.assertEqual(args[3:5],(mission.mav.MAV_FRAME_LOCAL_NED,0x09C7));self.assertEqual(args[14],.7)
                    velocity[0]=args[8:11]
                def pump(dt):
                    clock[0]+=dt
                    local.vx,local.vy,local.vz=velocity[0]
                    local.x+=local.vx*dt;local.y+=local.vy*dt
                link.master.mav.set_position_target_local_ned_send.side_effect=command;link.pump.side_effect=pump
                with patch.object(mission.time,'monotonic',side_effect=lambda:clock[0]),patch.object(mission.flight,'ensure_guided_health'),contextlib.redirect_stdout(io.StringIO()):
                    mission.stabilize_launch(link,SimpleNamespace(),mission.TakeoffReference(0,0,0,.06),.7,-.4)
                self.assertLess(math.hypot(local.x,local.y),.15)
                self.assertGreater(clock[0],2)

    def test_range_feedback_ignores_wrong_local_z_and_brakes_before_ceiling(self):
        link,values=self.range_link(126,z=100)
        start=mission.TakeoffReference(0,0,0,.06)
        tracker=mission.RangeTracking()
        tracker.command(link,start,0,0,0)
        self.assertLess(link.master.mav.set_position_target_local_ned_send.call_args.args[10],0)
        values['DISTANCE_SENSOR'].current_distance=176
        tracker=mission.RangeTracking();tracker.command(link,start,0,0,0)
        self.assertGreater(link.master.mav.set_position_target_local_ned_send.call_args.args[10],0)
        link.master.mav.set_position_target_local_ned_send.reset_mock()
        values['DISTANCE_SENSOR'].current_distance=186
        with patch.object(mission.flight,'ensure_guided_health'), self.assertRaisesRegex(mission.flight.FlightError,'ceiling'):
            mission.guard(link,SimpleNamespace(),start)
        link.master.mav.set_position_target_local_ned_send.assert_not_called()

    def test_predictive_ceiling_stops_even_below_abort_height(self):
        link,values=self.range_link(166,z=-1.6)
        values['LOCAL_POSITION_NED'].vz=-.5
        with self.assertRaisesRegex(mission.flight.FlightError,'Predictive ceiling'):
            mission.RangeTracking().command(link,mission.TakeoffReference(0,0,0,.06),0,0,0)
        link.master.mav.set_position_target_local_ned_send.assert_not_called()

    def test_destination_gps_conversion_and_distance_rejection(self):
        link,values=self.range_link(6)
        values['GLOBAL_POSITION_INT']=SimpleNamespace(lat=100000000,lon=760000000)
        values['GPS_RAW_INT']=SimpleNamespace(fix_type=3)
        start=mission.TakeoffReference(4,5,0,.06)
        target=mission.destination(link,start,0,10.000009,76)
        self.assertAlmostEqual(target[0]-4,1.00075,places=3);self.assertEqual(target[1],5)
        with self.assertRaisesRegex(mission.flight.FlightError,'within 2 m'):mission.destination(link,start,0,10.001,76)
        values['GPS_RAW_INT'].fix_type=2
        with self.assertRaisesRegex(mission.flight.FlightError,'GPS'):mission.destination(link,start,0,10,76)

    def test_hover_refuses_large_drift_stale_range_or_pilot_override(self):
        for failure in ('drift','range','override'):
            with self.subTest(failure=failure):
                link,values=self.range_link(156,z=-2)
                if failure=='drift':values['LOCAL_POSITION_NED'].x=.51
                if failure=='range':values['DISTANCE_SENSOR']=None
                health=mission.flight.PilotOverride('Pilot changed mode') if failure=='override' else None
                with patch.object(mission.flight,'ensure_guided_health',side_effect=health),contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(mission.flight.FlightError):
                        mission.stabilize_launch(link,SimpleNamespace(),mission.TakeoffReference(0,0,0,.06),0,-2)
                link.master.mav.set_position_target_local_ned_send.assert_not_called()

    def test_hover_does_not_accept_stationary_offset_or_high_speed(self):
        for x,vx in ((.25,0),(0,.25)):
            link,values=self.range_link(156,z=-2)
            values['LOCAL_POSITION_NED'].x=x;values['LOCAL_POSITION_NED'].vx=vx
            clock=[0.0];link.pump.side_effect=lambda dt:clock.__setitem__(0,clock[0]+dt)
            with patch.object(mission.time,'monotonic',side_effect=lambda:clock[0]),patch.object(mission.flight,'ensure_guided_health'),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(mission.flight.FlightError,'did not stabilize'):
                    mission.stabilize_launch(link,SimpleNamespace(),mission.TakeoffReference(0,0,0,.06),0,-2)

    def test_range_height_rejects_missing_wrong_orientation_and_invalid_readings(self):
        link, values = self.range_link(156)
        start = mission.TakeoffReference(0,0,0,.06)
        self.assertAlmostEqual(mission.rangefinder_height(link,start),1.5)
        values["DISTANCE_SENSOR"].current_distance=65535
        with self.assertRaisesRegex(mission.flight.FlightError,"invalid"):
            mission.rangefinder_height(link,start)
        values["DISTANCE_SENSOR"].current_distance=156
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

    def test_coordinate_travel_uses_range_feedback_when_local_z_is_wrong(self):
        link,values=self.range_link(156,z=-.4)
        values['LOCAL_POSITION_NED'].x=2
        clock=[0.0];link.pump.side_effect=lambda dt:clock.__setitem__(0,clock[0]+dt)
        with patch.object(mission.time,'monotonic',side_effect=lambda:clock[0]),patch.object(mission.flight,'ensure_guided_health'),contextlib.redirect_stdout(io.StringIO()):
            mission.move_forward(link,SimpleNamespace(),mission.TakeoffReference(0,0,0,.06),0,hold_down=-.4)
        args=link.master.mav.set_position_target_local_ned_send.call_args.args
        self.assertEqual(args[4],0x09C7);self.assertAlmostEqual(args[10],0)

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
