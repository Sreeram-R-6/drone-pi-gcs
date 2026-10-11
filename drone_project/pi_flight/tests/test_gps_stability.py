import math
import unittest
from flightcheck.checks import gps_statistics, ne_offset


class GPSStatisticsTests(unittest.TestCase):
    def test_wgs84_one_degree_equator(self):
        north, east = ne_offset(0, 1, 0, 0)
        self.assertEqual(north, 0)
        self.assertAlmostEqual(east, 111319.490793, places=5)

    def test_dateline_small_distance(self):
        north, east = ne_offset(0, -179.99999, 0, 179.99999)
        self.assertEqual(north, 0)
        self.assertAlmostEqual(east, 2.22639, places=4)

    def test_known_symmetric_track_and_no_outlier_removal(self):
        values = [{"lat": 0, "lon": lon, "vel": 10} for lon in (-100, 0, 100)]
        result = gps_statistics(values, 3)
        radius = 1.11319490793
        self.assertAlmostEqual(result["rms_m"], radius*math.sqrt(2/3), places=6)
        self.assertAlmostEqual(result["p95_m"], radius, places=6)
        self.assertAlmostEqual(result["radius_m"], radius, places=6)
        self.assertAlmostEqual(result["drift_m"], 2*radius, places=6)
        self.assertEqual(result["count"], 3)
        values.append({"lat": 0, "lon": 10000, "vel": 10})
        self.assertGreater(gps_statistics(values, 4)["radius_m"], 100)

    def test_missing_and_nonfinite_rejected(self):
        for values in ([], [{"lat": 0, "lon": 0, "vel": 0}],
                       [{"lat": float("nan"), "lon": 0, "vel": 0}]*2):
            with self.assertRaises(ValueError):
                gps_statistics(values, 30)
