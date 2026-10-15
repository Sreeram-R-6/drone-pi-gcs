from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from flightcheck.mission_policy import policy_checks
from flightcheck.telemetry import Rejected
from reviewed_fixture import reviewed_config
from test_state_machine import scenario
from flightcheck.telemetry import parameter_audit


class PolicyTests(unittest.TestCase):
    def test_draft_parameter_profile_cannot_pass_or_read_unfilled_values(self):
        c = reviewed_config()
        c['parameter_audit']['profile_path'] = 'parameter-profile.example.json'
        t = SimpleNamespace(read_param=Mock())
        checks = parameter_audit(t, c)
        self.assertEqual(checks[0].state, 'UNKNOWN')
        t.read_param.assert_not_called()

    def fixture(self):
        c = reviewed_config()
        c['firmware'].update(mission_policy='home_relative_land_v1',
                             takeoff_altitude_reference='relative_home')
        values = {'WP_RFND_USE': 0, 'FS_GCS_ENABLE': 5, 'FS_GCS_TIMEOUT': 5,
                  'MAV_GCS_SYSID': 254, 'MAV_GCS_SYSID_HI': 0, 'FS_OPTIONS': 16}
        t = SimpleNamespace(read_param=Mock(side_effect=lambda name:(values[name], 6)))
        return c, t, values

    def test_exact_reviewed_readbacks_pass_without_writes(self):
        c, t, _ = self.fixture()
        self.assertTrue(all(x.state == 'PASS' for x in policy_checks(t,c)))
        self.assertEqual(t.read_param.call_count, 6)

    def test_terrain_following_disabled_failsafe_or_wrong_source_block(self):
        for name, bad in (('WP_RFND_USE',1), ('FS_GCS_ENABLE',0),
                          ('MAV_GCS_SYSID',255), ('FS_OPTIONS',1),
                          ('FS_GCS_TIMEOUT',float('nan'))):
            c, t, values = self.fixture()
            values[name] = bad
            self.assertTrue(any(x.state == 'FAIL' for x in policy_checks(t,c)), name)

    def test_missing_readback_stays_unknown(self):
        c, t, _ = self.fixture()
        t.read_param.side_effect = Rejected('missing readback')
        self.assertTrue(all(x.state == 'UNKNOWN' for x in policy_checks(t,c)))

    def test_different_firmware_cannot_inherit_policy(self):
        c, t, _ = self.fixture()
        c['aircraft']['flight_sw_version'] += 1
        self.assertEqual(policy_checks(t,c)[0].state, 'FAIL')
        t.read_param.assert_not_called()

    def test_policy_failure_precedes_parameter_writes_modes_and_arming(self):
        m, t, _ = scenario()
        c, _, _ = self.fixture()
        m.c['firmware'] = c['firmware']
        m.c['aircraft'] = c['aircraft']
        self.assertFalse(m.run())
        self.assertFalse(any(call[0] in ('parameter',176,400,22) for call in t.calls))
