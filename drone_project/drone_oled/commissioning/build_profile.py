"""Build local feature settings from the captured FC audit, without writing the FC."""
import json
from pathlib import Path
from drone_oled.config import load, load_profile

root=Path(__file__).resolve().parent
report=json.loads((root/'fc-audit.json').read_text())
p=report['parameters'];version=report['messages']['AUTOPILOT_VERSION']
def value(name):return p[name]['value']
assert value('FRAME_CLASS')==1 and value('FRAME_TYPE')==1, 'Only audited quad-X mapping supported'
servo={f'SERVO{i}_FUNCTION':value(f'SERVO{i}_FUNCTION') for i in range(1,5)}
assert sorted(servo.values())==[33,34,35,36], 'Unique four-motor assignment required'
assert 0<value('BATT_CRT_VOLT')<value('BATT_LOW_VOLT')<=value('BATT_ARM_VOLT')
mask=sum(1<<i for i,suffix in enumerate(('', '2','3'))
         if value('COMPASS_USE'+suffix)>0 and value('COMPASS_DEV_ID'+suffix)>0)
assert mask, 'No configured compass found'
profile=dict(approved=True,flight_sw_version=version['flight_sw_version'],board_version=version['board_version'],
 uid2=bytes(version['uid2']).hex(),min_voltage_v=round(value('BATT_ARM_VOLT'),3),
 max_ekf_variance=round(value('FS_EKF_THRESH'),3),motor_positions=['FR','RR','RL','FL'],
 motor_configuration=dict(FRAME_CLASS=1,FRAME_TYPE=1,**servo),
 arming_mode='GUIDED',recovery_reviewed=True,calibrations=['GYRO','BARO ZERO','COMPASS'],
 compass_mask=mask,failsafe_texts={},parameters={},
 provenance=dict(audit='fc-audit.json',calibration='USER CONFIRMED COMPLETE',
 motor_mapping='CONFIGURED QUAD-X; PHYSICAL WIRING NOT OBSERVED',
 recovery='UI DEFAULT RTL WHEN POSITION/HOME VALID, OTHERWISE LAND; FC AUTHORITATIVE'))
ranges={'FS_THR_ENABLE':(1,7,1),'FENCE_ENABLE':(0,1,1),'FENCE_RADIUS':(30,10000,10),
        'FENCE_ALT_MAX':(10,1000,1),
        'BATT_LOW_VOLT':(round(value('BATT_LOW_VOLT'),3),round(value('BATT_ARM_VOLT'),3),.1)}
for name,(lo,hi,step) in ranges.items():
 profile['parameters'][name]=dict(min=lo,max=hi,step=step,type=p[name]['type'])
profile['parameters']['FS_THR_ENABLE']['values']=[1,3,4,5,6,7]
profile['parameters']['FENCE_ENABLE']['values']=[0,1]
out=root/'profile.live.json';out.write_text(json.dumps(profile,indent=2)+'\n')
c=load();c['safety']['firmware_profile']=str(out)
assert load_profile(c)==profile
print('Verified profile:',out)
print('No FC writes, motor tests, calibration or arming performed.')
