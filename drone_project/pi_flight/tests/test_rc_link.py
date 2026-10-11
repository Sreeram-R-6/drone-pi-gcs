import unittest
from flightcheck.rc_link import link_quality
from flightcheck.telemetry import Store


class LinkQualityTests(unittest.TestCase):
    def store(self,options=2048,rssi_type=3):
        s = Store()
        s.put('PARAM_VALUE',{'param_id':'RC_OPTIONS','param_value':options},10)
        s.put('PARAM_VALUE',{'param_id':'RSSI_TYPE','param_value':rssi_type},10)
        return s

    def test_verified_mapping_uses_crsf_scale(self):
        for raw,expected in [(0,0),(125,50),(250,100)]:
            self.assertEqual(link_quality(self.store(),{'rssi':raw},11)[0],expected)

    def test_missing_wrong_and_stale_mapping_never_labels_rssi_as_lq(self):
        for store,now in [(Store(),11),(self.store(options=0),11),
                          (self.store(rssi_type=0),11),(self.store(),30)]:
            self.assertIsNone(link_quality(store,{'rssi':125},now)[0])

    def test_unavailable_value_is_not_a_full_quality_link(self):
        self.assertIsNone(link_quality(self.store(),{'rssi':255},11)[0])
        self.assertIsNone(link_quality(self.store(),{},11)[0])
