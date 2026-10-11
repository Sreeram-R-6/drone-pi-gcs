import unittest
from flightcheck.checks import Check, all_pass, stream_health
from flightcheck.telemetry import Store, process_lock, LinkLost


class GatingTests(unittest.TestCase):
    def test_required_nonpass_blocks(self):
        self.assertFalse(all_pass([]))
        self.assertFalse(all_pass([Check("optional", "PASS", 1, "optional", required=False)]))
        for state in ("WARN", "FAIL", "UNKNOWN"):
            self.assertFalse(all_pass([Check("critical", state, None, "required")]))
        self.assertTrue(all_pass([Check("critical", "PASS", 1, "required")]))

    def test_missing_stale_gap_and_rate(self):
        self.assertEqual(stream_health([], 0, 3, 1, 1)[0], "UNKNOWN")
        for times in ([0,1], [0,.1], [.1,.2,2.8], [1,1,2,3]):
            self.assertEqual(stream_health([(t,{}) for t in times], 0, 3, 1, 1)[0], "FAIL")
        self.assertEqual(stream_health([(t,{}) for t in (0,.5,1,1.5,2,2.5,3)], 0,3,1,1)[0], "PASS")

    def test_old_packet_does_not_count(self):
        self.assertEqual(stream_health([(-1,{})], 0,3,1,1)[0], "UNKNOWN")

    def test_origin_and_boot_reset_latched(self):
        s = Store()
        s.put("GPS_GLOBAL_ORIGIN", {"latitude":1,"longitude":2,"altitude":3}, 1)
        s.put("GPS_GLOBAL_ORIGIN", {"latitude":1,"longitude":2,"altitude":4}, 2)
        self.assertTrue(s.origin_reset)
        s.put("LOCAL_POSITION_NED", {"time_boot_ms":100}, 1)
        s.put("LOCAL_POSITION_NED", {"time_boot_ms":50}, 2)
        self.assertTrue(s.boot_reset)

    def test_statustext_failure_is_not_cleared_by_age(self):
        s = Store()
        s.put("STATUSTEXT", {"severity":6,"text":"PreArm: Compass inconsistent"}, 0)
        s.put("STATUSTEXT", {"severity":6,"text":"Ready"}, 100)
        self.assertEqual(len(s.faults), 1)
        with self.assertRaises(LinkLost):
            s.latest("HEARTBEAT", 100, 2)

    def test_duplicate_mission_lock(self):
        with process_lock("unit-test.lock"):
            with self.assertRaises(RuntimeError):
                with process_lock("unit-test.lock"):
                    self.fail("Duplicate owner permitted")
