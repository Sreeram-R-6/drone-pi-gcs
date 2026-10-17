"""Offline safety editor tests. All controller operations are mocked."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'web'))
from geofence_settings import SCHEMA,validate_changes,validate_combined
import geofence_service
try:
    import geofence_worker as worker
except ImportError:worker=None


class SettingsTests(unittest.TestCase):
    def test_allowlist_units_and_ranges(self):
        self.assertEqual(SCHEMA['WP_SPD']['unit'],'m/s')
        for changes,expected in [({'ARMING_CHECK':0},{'ARMING_CHECK':1}),({'WP_SPD':True},{'WP_SPD':6}),({'WP_SPD':float('nan')},{'WP_SPD':6}),({'WP_SPD':21},{'WP_SPD':6}),({'WP_SPD':1},{}),({'FENCE_ACTION':6},{'FENCE_ACTION':1}),({'FENCE_TYPE':16},{'FENCE_TYPE':7})]:
            with self.assertRaises(ValueError):validate_changes(changes,expected)
        self.assertEqual(validate_changes({'WP_SPD':.3},{'WP_SPD':6}),{'WP_SPD':.3})
        self.assertEqual(validate_changes({'FENCE_ALT_MAX':3},{'FENCE_ALT_MAX':100}),{'FENCE_ALT_MAX':3})
        with self.assertRaises(ValueError):validate_changes({'FENCE_ALT_MAX':0},{'FENCE_ALT_MAX':100})
    def test_vertical_margin_validation(self):
        values={'FENCE_TYPE':15,'FENCE_MARGIN':2,'FENCE_ALT_MIN':10,'FENCE_ALT_MAX':20,'FENCE_ALT_MIN_TP':1,'FENCE_ALT_MAX_TP':1,'FENCE_RADIUS':30}
        validate_combined(values)
        for edits in ({'FENCE_ALT_MAX':12},{'FENCE_ALT_MIN_TP':3},{'FENCE_MARGIN_XY':31}):
            with self.assertRaises(ValueError):validate_combined({**values,**edits})
    def test_api_requires_confirmation(self):
        service=geofence_service.GeofenceService()
        with self.assertRaises(ValueError):service.launch('settings',{'changes':{'WP_SPD':1},'expected':{'WP_SPD':6}})
        self.assertEqual(service.job['state'],'idle')
    def test_service_preserves_partial_failure(self):
        service=geofence_service.GeofenceService()
        result={'ok':False,'error':'readback failure','upload_verified':True,'applied':{'WP_SPD':1},'settings':{'FENCE_ENABLE':0}}
        process=Mock(stdin=io.StringIO(),stdout=io.StringIO(json.dumps(result)+'\n'));process.wait.return_value=1
        with patch.object(geofence_service.subprocess,'Popen',return_value=process):service._run('test','settings',{})
        self.assertEqual(service.job['state'],'error');self.assertTrue(service.job['upload_verified']);self.assertEqual(service.job['applied'],{'WP_SPD':1})

    @unittest.skipIf(worker is None,'Run in flight venv')
    def test_mock_controller_success_and_failures(self):
        scenarios=['success','armed','enabled','stale','partial','wrong_readback','firmware']
        for scenario in scenarios:
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as directory:
                values={name:float(spec['min'] or 0) if spec['min'] is not None else float(next(iter(spec['choices']))) for name,spec in SCHEMA.items() if not spec['choices'] or 'bits' not in spec['choices']}
                values.update(FENCE_TYPE=7,FENCE_OPTIONS=0,FENCE_ENABLE=0,FENCE_ALT_MAX=100,FENCE_ALT_MIN=-10,FENCE_ALT_MAX_TP=1,FENCE_ALT_MIN_TP=1,FENCE_RADIUS=300,WP_SPD=6,WP_SPD_UP=.3)
                if scenario=='enabled':values['FENCE_ENABLE']=1
                actor=Mock();actor.target=(1,1);actor.cache={'AUTOPILOT_VERSION':(0,SimpleNamespace(flight_sw_version=(4<<24)|((6 if scenario=='firmware' else 7)<<16)))};actor.latest.return_value=SimpleNamespace(type=2)
                actor.read_param.side_effect=lambda name:values.get(name,0)
                actor.settings.side_effect=lambda:dict(values)
                if scenario=='armed':actor.disarmed.side_effect=RuntimeError('Armed')
                def write(name,value):
                    if scenario=='partial' and name=='WP_SPD_UP':raise RuntimeError('Controller link lost; earlier settings may have changed')
                    if scenario!='wrong_readback':values[name]=value
                actor.write_param.side_effect=write
                output=[];payload={'changes':{'WP_SPD':1,'WP_SPD_UP':.5},'expected':{'WP_SPD':5 if scenario=='stale' else 6,'WP_SPD_UP':.3}}
                with patch.object(worker,'Vehicle',return_value=actor),patch.object(worker,'transfer_lock',return_value=contextlib.nullcontext()),patch.object(worker,'ROOT',Path(directory)),patch.object(worker,'emit',side_effect=output.append),patch.object(sys,'argv',['worker','settings']),patch.object(sys,'stdin',io.StringIO(json.dumps(payload))):
                    if scenario=='success':worker.main()
                    else:
                        with self.assertRaises(SystemExit):worker.main()
                self.assertEqual(output[-1]['ok'],scenario=='success')
                if scenario in ('armed','enabled','stale','firmware'):actor.write_param.assert_not_called()
                if scenario=='success':
                    self.assertEqual(output[-1]['applied'],{'WP_SPD':1,'WP_SPD_UP':.5});self.assertEqual(values['FENCE_ENABLE'],0)
                    backup=Path(directory)/output[-1]['backup'];self.assertEqual(json.loads(backup.read_text())['before']['WP_SPD'],6);self.assertEqual(backup.stat().st_mode&0o777,0o600)
                if scenario=='partial':self.assertEqual(output[-1]['applied'],{'WP_SPD':1})

if __name__=='__main__':unittest.main()
