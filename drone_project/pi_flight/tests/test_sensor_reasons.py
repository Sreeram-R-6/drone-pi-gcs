from pathlib import Path
import unittest
from flightcheck.checks import evaluate
from flightcheck.config import load
from flightcheck.telemetry import Store
from reviewed_fixture import reviewed_config


class SensorReasonTests(unittest.TestCase):
    def test_gps_failure_explains_bad_sample_not_just_latest_lock(self):
        config = reviewed_config()
        store = Store()
        for stamp,sats in [(0,4),(1,8),(2,8),(3,8)]:
            store.put('GPS_RAW_INT',{'lat':120000000,'lon':770000000,'vel':0,
                      'fix_type':3,'satellites_visible':sats,'eph':180},stamp)
        check = {c.name:c for c in evaluate(store,config,0,3)}['GPS quality/stability']
        self.assertEqual(check.state,'FAIL')
        self.assertIn('Satellites below 5',check.explanation)
        self.assertEqual(check.measured['satellites_min'],4)
    def test_diagnostic_ekf_relaxation_does_not_relax_mission_flags(self):
        config = reviewed_config()
        store = Store()
        store.put('EKF_STATUS_REPORT',{'flags':367,'velocity_variance':1.5,
            'pos_horiz_variance':0.1,'pos_vert_variance':0.1,'compass_variance':0.1},1)
        diagnostic = {c.name:c for c in evaluate(store,config,0,3,mission_readiness=False)}
        mission = {c.name:c for c in evaluate(store,config,0,3)}
        self.assertEqual(diagnostic['EKF [diagnostic]'].state,'PASS')
        self.assertEqual(mission['EKF'].state,'FAIL')
        store.samples['EKF_STATUS_REPORT'][-1][1]['flags'] |= 32768
        diagnostic = {c.name:c for c in evaluate(store,config,0,3,mission_readiness=False)}
        self.assertEqual(diagnostic['EKF [diagnostic]'].state,'FAIL')
    def setUp(self):
        self.c = load(Path(__file__).parents[1]/'config.example.toml')
        self.store = Store()

    def checks(self):
        return {c.name:c for c in evaluate(self.store,self.c,0,3)}

    def test_unset_limits_are_unknown_not_hardware_failure(self):
        checks = self.checks()
        for name in ('GPS quality/stability','EKF','battery','attitude/heading','local position/origin continuity'):
            self.assertEqual(checks[name].state,'UNKNOWN')
            self.assertIn('Limits unset:',checks[name].explanation)

    def test_valid_rc_channels_with_unavailable_rssi_are_not_unbound(self):
        rc = {'chancount':8,'rssi':255,**{f'chan{i}_raw':1500 for i in range(1,5)}}
        self.store.put('RC_CHANNELS',rc,1)
        checks = self.checks()
        self.assertEqual(checks['RC receiver'].state,'PASS')
        self.assertNotIn('CRSF link quality',checks)
        self.assertNotIn('RC signal strength',checks)
        rc['chan3_raw'] = 65535
        self.assertEqual(self.checks()['RC receiver'].state,'FAIL')

    def test_legacy_flow_stream_and_quality_without_false_rad_failure(self):
        self.c['flow'].update(sensor_id=0,min_quality=50,min_hz=1,max_gap_s=2,
                              max_integration_us=0,max_compensated_rad_s=0,max_compensated_m_s=1)
        for now in (0,1,2,3):
            self.store.put('OPTICAL_FLOW',{'sensor_id':0,'quality':100,
                'flow_comp_m_x':0.2,'flow_comp_m_y':-0.1},now)
        checks = self.checks()
        self.assertNotIn('OPTICAL_FLOW_RAD stream',checks)
        self.assertEqual(checks['OPTICAL_FLOW stream'].state,'PASS')
        self.assertEqual(checks['optical flow'].state,'PASS')
        self.assertNotIn('flow calibration/fusion review',checks)
        self.store.samples['OPTICAL_FLOW'][-1][1]['quality'] = 10
        self.assertEqual(self.checks()['optical flow'].state,'FAIL')
        self.store.samples['OPTICAL_FLOW'][-1][1].update(quality=100,flow_comp_m_x=2)
        self.assertEqual(self.checks()['optical flow'].state,'FAIL')
        self.store.samples['OPTICAL_FLOW'][-1][1].update(flow_comp_m_x=float('nan'))
        self.assertEqual(self.checks()['optical flow'].state,'FAIL')

    def test_compensated_flow_remains_source_when_rad_also_arrives(self):
        self.c['flow'].update(sensor_id=0, min_quality=50, min_hz=1,
                              max_gap_s=2, max_compensated_m_s=1)
        for now in (0, 1, 2, 3):
            self.store.put('OPTICAL_FLOW', {'sensor_id':0, 'quality':100,
                'flow_comp_m_x':0.2, 'flow_comp_m_y':-0.1}, now)
            self.store.put('OPTICAL_FLOW_RAD', {'sensor_id':0, 'quality':0,
                'integration_time_us':0}, now)
        checks = self.checks()
        self.assertEqual(checks['optical flow'].state, 'PASS')
        self.assertEqual(checks['optical flow'].measured['source'], 'OPTICAL_FLOW')
        self.assertNotIn('OPTICAL_FLOW_RAD stream', checks)

    def test_unset_sensor_id_shows_observed_id(self):
        self.store.put('OPTICAL_FLOW',{'sensor_id':7},1)
        check = self.checks()['OPTICAL_FLOW stream']
        self.assertEqual(check.state,'UNKNOWN')
        self.assertEqual(check.measured,{'observed_ids':[7]})
