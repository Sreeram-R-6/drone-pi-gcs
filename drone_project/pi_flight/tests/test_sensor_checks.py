from pathlib import Path
import unittest
from flightcheck.checks import all_pass, evaluate
from flightcheck.config import load
from flightcheck.telemetry import Store


def healthy_fixture():
    c = load(Path(__file__).parents[1]/"config.example.toml")
    c["gps"].update(min_satellites=10,max_hdop=1.5,max_rms_m=.5,max_p95_m=1.,
        max_radius_m=1.5,max_drift_m=1.,max_speed_m_s=.3,max_step_m=.5)
    c["flow"].update(sensor_id=0,min_quality=100,min_hz=2,max_gap_s=1.,
        max_compensated_rad_s=.1,max_integration_us=200000)
    c["range"].update(sensor_id=0,usable_min_m=.05,usable_max_m=3.,max_covariance_cm2=10.)
    c["battery"].update(min_voltage_v=15.,min_remaining_pct=80.,max_current_a=10.)
    c["attitude"].update(max_stationary_tilt_deg=3.,max_heading_span_deg=5.,
        max_altitude_span_m=.2,max_ekf_variance=.5)
    c["commissioning"]["manual_hover_log_reviewed"] = True
    c["commissioning"]["flow_axes_range_calibration_reviewed"] = True # Synthetic approved fixture only.
    # SYNTHETIC LIMITS ONLY: no values here are approved for the real vehicle.
    mask = 1|2|4|8|32|64|65536|268435456
    data = {
        "HEARTBEAT":dict(autopilot=3,type=2,system_status=3,base_mode=0),
        "GPS_RAW_INT":dict(lat=120000000,lon=770000000,vel=0,fix_type=3,satellites_visible=14,eph=100),
        "GLOBAL_POSITION_INT":dict(relative_alt=0),
        "LOCAL_POSITION_NED":dict(x=0.,y=0.,z=0.,vx=0.,vy=0.,vz=0.),
        "ATTITUDE":dict(roll=0.,pitch=0.,yaw=0.),
        "EKF_STATUS_REPORT":dict(flags=55,velocity_variance=.1,pos_horiz_variance=.1,
            pos_vert_variance=.1,compass_variance=.1),
        "SYS_STATUS":dict(onboard_control_sensors_present=mask,onboard_control_sensors_enabled=mask,
            onboard_control_sensors_health=mask),
        "BATTERY_STATUS":dict(id=0,voltages=[4000]*4+[65535]*6,current_battery=100,battery_remaining=95),
        "RC_CHANNELS":dict(chancount=8,rssi=100,chan1_raw=1500,chan2_raw=1500,chan3_raw=1000,chan4_raw=1500),
        "OPTICAL_FLOW_RAD":dict(sensor_id=0,quality=150,integration_time_us=100000,
            integrated_x=0.,integrated_y=0.,integrated_xgyro=0.,integrated_ygyro=0.,integrated_zgyro=0.),
        "DISTANCE_SENSOR":dict(id=0,orientation=25,min_distance=1,max_distance=800,current_distance=20,covariance=1),
    }
    s = Store()
    for i in range(151):
        for kind,d in data.items():
            s.put(kind,dict(d),i*.2)
    return c,s


class SensorChecks(unittest.TestCase):
    def test_flow_calibration_review_removed_without_disabling_flow_health(self):
        c,s = healthy_fixture()
        c['commissioning']['flow_axes_range_calibration_reviewed'] = False
        for readiness in (True,False):
            checks = evaluate(s,c,0,30,mission_readiness=readiness)
            self.assertNotIn('flow calibration/fusion review',[x.name for x in checks])
            self.assertIn('optical flow',[x.name for x in checks])
            self.assertTrue(all_pass(checks))

    def test_ekf_reason_names_missing_position_and_unsafe_state(self):
        c,s = healthy_fixture()
        for _,packet in s.samples['EKF_STATUS_REPORT']:
            packet['flags'] = 367
        check = next(x for x in evaluate(s,c,0,30) if x.name == 'EKF')
        self.assertEqual(check.state,'FAIL')
        self.assertIn('absolute horizontal position',check.explanation)
        self.assertNotIn('forbidden',check.explanation)
        self.assertNotIn('Unsafe estimator state',check.explanation)
        s.samples['EKF_STATUS_REPORT'][-1][1]['flags'] |= 32768
        check = next(x for x in evaluate(s,c,0,30) if x.name == 'EKF')
        self.assertIn('EKF uninitialized',check.explanation)
        self.assertEqual(check.state,'FAIL')

    def test_vibration_review_is_not_a_check_or_mission_gate(self):
        c,s = healthy_fixture()
        c['commissioning']['manual_hover_log_reviewed'] = False
        s.put('VIBRATION',{'vibration_x':0.01,'clipping_0':0},15)
        for readiness in (True,False):
            checks = evaluate(s,c,0,30,mission_readiness=readiness)
            self.assertNotIn('powered vibration/log review',[x.name for x in checks])
            self.assertTrue(all_pass(checks))

    def test_synthetic_stationary_samples_pass(self):
        c,s = healthy_fixture()
        checks = evaluate(s,c,0,30)
        self.assertTrue(all_pass(checks),[x for x in checks if x.state != "PASS"])

    def test_each_missing_stream_blocks(self):
        c,s = healthy_fixture()
        for key in list(s.samples):
            with self.subTest(stream=key):
                c,s = healthy_fixture()
                del s.samples[key]
                self.assertFalse(all_pass(evaluate(s,c,0,30)))

    def test_unknown_or_invalid_measurements_block(self):
        cases = [("GPS_RAW_INT","eph",65535),("GPS_RAW_INT","satellites_visible",255),
                 ("OPTICAL_FLOW_RAD","quality",0),("OPTICAL_FLOW_RAD","integration_time_us",0),
                 ("OPTICAL_FLOW_RAD","integrated_x",float("nan")),
                 ("DISTANCE_SENSOR","current_distance",0),("DISTANCE_SENSOR","covariance",255),
                 ("DISTANCE_SENSOR","orientation",0),("BATTERY_STATUS","battery_remaining",-1),
                 ("BATTERY_STATUS","current_battery",-1),("EKF_STATUS_REPORT","flags",183),
                 ("RC_CHANNELS","chan1_raw",65535),("ATTITUDE","pitch",None)]
        for kind,key,value in cases:
            with self.subTest(kind=kind,key=key):
                c,s = healthy_fixture()
                s.samples[kind][-1][1][key] = value
                self.assertFalse(all_pass(evaluate(s,c,0,30)))

    def test_other_sensor_cannot_supply_rate(self):
        c,s = healthy_fixture()
        for timestamp,data in s.samples["DISTANCE_SENSOR"]:
            data["id"] = 2
        s.samples["DISTANCE_SENSOR"][-1][1]["id"] = 0
        self.assertFalse(all_pass(evaluate(s,c,0,30)))
