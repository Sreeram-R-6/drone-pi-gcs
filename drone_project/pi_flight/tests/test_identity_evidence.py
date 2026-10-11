from pathlib import Path
from types import SimpleNamespace
import unittest
import json
from unittest.mock import Mock, patch

from flightcheck.config import load
from flightcheck.telemetry import Store, parameter_audit
from flightcheck.telemetry import Rejected
from flightcheck.startup import START_MODES, confirmation_mode, check_confirmation_state
from preflight_hover import identity_checks
from reviewed_fixture import reviewed_config


class IdentityEvidenceTests(unittest.TestCase):
    def test_supported_pilot_start_modes_ignore_legacy_stabilize_preference(self):
        for mode in START_MODES:
            c,t = self.fixture()
            t.mode = lambda:mode
            checks = {x.name:x for x in identity_checks(t,c)}
            self.assertEqual(checks['initial mode'].state,'PASS',mode)

    def test_automatic_unknown_and_changed_start_modes_do_not_pass(self):
        for mode in ('AUTO','RTL','LAND','GUIDED','UNKNOWN'):
            c,t = self.fixture()
            t.mode = lambda:mode
            self.assertEqual(next(x for x in identity_checks(t,c) if x.name == 'initial mode').state,'FAIL')
        c,t = self.fixture()
        t.preflight_mode = 'LOITER'
        self.assertEqual(next(x for x in identity_checks(t,c) if x.name == 'initial mode').state,'FAIL')

    def test_confirmation_accepts_loiter_but_rejects_mode_change_and_arming(self):
        t = SimpleNamespace(mode=lambda:'LOITER',armed=lambda:False)
        mode = confirmation_mode(t)
        check_confirmation_state(t,mode)
        t.mode = lambda:'STABILIZE'
        with self.assertRaises(Rejected):
            check_confirmation_state(t,mode)
        t.mode = lambda:'LOITER'
        t.armed = lambda:True
        with self.assertRaises(Rejected):
            check_confirmation_state(t,mode)

    def test_requested_climb_mapping_is_metres_per_second_not_legacy_centimetres(self):
        c,_ = self.fixture()
        f = c['firmware']
        self.assertEqual(f['climb_parameter'],'WP_SPD_UP')
        self.assertEqual(f['climb_parameter_type'],9)
        self.assertEqual(c['mission']['climb_m_s']*f['climb_parameter_units_per_m_s'],0.3)
        self.assertEqual(c['battery']['capacity_mah'],5000)
        self.assertFalse(c['commissioning']['firmware_behavior_reviewed'])

    def fixture(self):
        c = reviewed_config()
        t = SimpleNamespace(version={
            'flight_sw_version':67568127,'board_version':76218368,'uid':0,
            'uid2':[1] * 18},
            store=Store(),mode=lambda:'STABILIZE')
        return c,t

    def test_saved_identity_matches_using_uid2_despite_legacy_zero(self):
        c,t = self.fixture()
        checks = {x.name:x for x in identity_checks(t,c)}
        for key in ('flight_sw_version','board_version','uid','initial mode'):
            self.assertEqual(checks[key].state,'PASS',key)
        self.assertEqual(checks['firmware target / real hardware'].state,'PASS')
        self.assertEqual(checks['firmware behavior review'].state,'UNKNOWN')

    def test_changed_firmware_board_or_uid_fails(self):
        for key in ('flight_sw_version','board_version','uid2'):
            c,t = self.fixture()
            if key == 'uid2':
                t.version[key][0] ^= 1
            else:
                t.version[key] += 1
            checks = {x.name:x for x in identity_checks(t,c)}
            self.assertEqual(checks['uid' if key == 'uid2' else key].state,'FAIL')
            self.assertEqual(checks['firmware target / real hardware'].state,'FAIL')

    def test_missing_uid2_is_not_accepted_with_legacy_zero(self):
        c,t = self.fixture()
        del t.version['uid2']
        checks = {x.name:x for x in identity_checks(t,c)}
        self.assertEqual(checks['uid'].state,'UNKNOWN')

    def test_audit_uses_firmware_correct_arming_parameter_and_rejects_skip_mask(self):
        for version,name,good,bad in ((67568127,'ARMING_SKIPCHK',0,4),
                                      (0x040607ff,'ARMING_CHECK',1,0)):
            for value,state in ((good,'PASS'),(bad,'FAIL')):
                c,_ = self.fixture()
                c['aircraft']['flight_sw_version'] = version
                c['parameter_audit']['profile_path'] = 'synthetic-profile.json'
                names = ('arming','serial_pi','serial_flow','ekf','flow','range','battery',
                         'rc_failsafe','gcs_failsafe','ekf_failsafe','fence','rtl','landing','climb')
                profile = {'flight_sw_version':version,'categories':{
                    category:{'FIXTURE':{'value':1,'type':2}} for category in names}}
                for group,category,protocol in (('SERIAL4','serial_pi',2),('SERIAL5','serial_flow',1)):
                    profile['categories'][category] = {
                        group+'_PROTOCOL':{'value':protocol,'type':2},
                        group+'_BAUD':{'value':115,'type':6}}
                params = {key:(v['value'],v['type']) for category in profile['categories'].values()
                          for key,v in category.items()}
                params[name] = (value,6)
                t = SimpleNamespace(read_param=Mock(side_effect=lambda key:params[key]),
                                    report=SimpleNamespace(save=Mock()))
                with patch('pathlib.Path.read_text',return_value=json.dumps(profile)):
                    checks = parameter_audit(t,c)
                check = next(x for x in checks if x.name == 'all arming checks')
                self.assertEqual(check.state,state)
                self.assertIn(name,check.requirement)
                t.read_param.assert_any_call(name)

