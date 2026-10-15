"""Offline mission ordering and fail-closed regression tests; no MAVLink link."""
import contextlib
import importlib.util
import io
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("flight_2m", Path(__file__).with_name("flight_2m.py"))
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


class MissionTests(unittest.TestCase):
    def test_mtf01_legacy_flow_preferred_over_unused_rad(self):
        legacy = SimpleNamespace(quality=180, flow_comp_m_x=0.1, flow_comp_m_y=-0.2)
        rad = SimpleNamespace(quality=0)
        link = MagicMock()
        link.cached.side_effect = lambda kind, age: {"OPTICAL_FLOW": legacy, "OPTICAL_FLOW_RAD": rad}.get(kind)
        message, kind = mission.flight.optical_flow(link)
        self.assertIs(message, legacy)
        self.assertEqual(kind, "OPTICAL_FLOW")
        self.assertTrue(mission.flight.flow_values_finite(message, kind))
        legacy.flow_comp_m_x = math.nan
        self.assertFalse(mission.flight.flow_values_finite(message, kind))

    def test_rad_fallback_and_missing_flow(self):
        rad = SimpleNamespace(quality=100, integrated_x=0.01, integrated_y=-0.02)
        link = MagicMock()
        link.cached.side_effect = lambda kind, age: rad if kind == "OPTICAL_FLOW_RAD" else None
        message, kind = mission.flight.optical_flow(link)
        self.assertIs(message, rad)
        self.assertTrue(mission.flight.flow_values_finite(message, kind))
        link.cached.side_effect = None
        link.cached.return_value = None
        self.assertIsNone(mission.flight.optical_flow(link)[0])

    def test_plan_opens_no_connection(self):
        with patch.object(mission, "Link") as link, patch("sys.argv", ["flight_2m.py"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mission.main(), 0)
            link.assert_not_called()

    def test_existing_different_fence_is_preserved(self):
        link = MagicMock()
        home = SimpleNamespace(latitude=100, longitude=200)
        with patch.object(mission, "fence_count", return_value=2):
            with self.assertRaises(mission.flight.FlightError):
                mission.configure_fence(link, home)
        link.master.mav.mission_count_send.assert_not_called()
        link.set_parameter.assert_not_called()

    def test_uploaded_circle_must_match_center_and_radius(self):
        home = SimpleNamespace(latitude=100, longitude=200)
        item = SimpleNamespace(command=mission.mav.MAV_CMD_NAV_FENCE_CIRCLE_INCLUSION,
                               frame=mission.mav.MAV_FRAME_GLOBAL_INT,
                               x=100, y=200, param1=3)
        self.assertTrue(mission.matches_home(item, home))
        item.param1 = 30
        self.assertFalse(mission.matches_home(item, home))
        item.param1, item.x = 3, 101
        self.assertFalse(mission.matches_home(item, home))

    def run_fake_mission(self, fence_error=False):
        events = []
        home = SimpleNamespace(latitude=100000000, longitude=200000000)
        position = SimpleNamespace(lat=home.latitude, lon=home.longitude)
        start = SimpleNamespace(x=5, y=7, z=-1)
        link = MagicMock()
        link.cached.side_effect = lambda name, age: {
            "HOME_POSITION": home, "GLOBAL_POSITION_INT": position,
            "HEARTBEAT": SimpleNamespace(),
        }.get(name)
        link.wait_for.side_effect = lambda kind, timeout: (
            start if kind == "LOCAL_POSITION_NED" else SimpleNamespace(yaw=0)
        )
        link.is_armed.return_value = False
        link.set_mode.side_effect = lambda mode: events.append(mode)
        def configure(*args):
            events.append("fence")
            if fence_error:
                raise mission.flight.FlightError("Fence rejected")
        def move(link, args, home, start, north, east, label):
            events.append((label, north, east))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["flight_2m.py", "--execute"]))
            stack.enter_context(patch.object(mission, "Link", return_value=link))
            stack.enter_context(patch.object(mission, "configure_fence", side_effect=configure))
            stack.enter_context(patch.object(mission, "takeoff", side_effect=lambda *a: events.append("takeoff")))
            stack.enter_context(patch.object(mission, "move", side_effect=move))
            for name in ("configure_streams", "collect_preflight", "run_prearm_checks"):
                stack.enter_context(patch.object(mission.flight, name))
            stack.enter_context(patch.object(mission.flight, "arm", side_effect=lambda *a: events.append("arm")))
            stack.enter_context(patch.object(mission.flight, "monitor_rtl", side_effect=lambda *a: events.append("disarm")))
            stack.enter_context(patch.object(mission.flight, "restore_parameters", return_value=[]))
            stack.enter_context(patch.object(mission.flight, "speed_parameter_values", return_value=(MagicMock(), .3, MagicMock(), .3)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            result = mission.main()
        return result, events

    def test_out_back_then_rtl_order(self):
        result, events = self.run_fake_mission()
        self.assertEqual(result, 0)
        self.assertEqual(events, ["fence", "GUIDED", "arm", "takeoff",
                                 ("Forward point", 7, 7), ("Launch point", 5, 7),
                                 "RTL", "disarm"])

    def test_fence_failure_cannot_arm(self):
        result, events = self.run_fake_mission(fence_error=True)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["fence"])


if __name__ == "__main__":
    unittest.main()
