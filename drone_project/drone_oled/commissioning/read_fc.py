"""Read-only FC audit over a spare MAVProxy UDP output; no parameter/mission writes."""
import json,time
from pathlib import Path
from pymavlink import mavutil

names=['FRAME_CLASS','FRAME_TYPE','ARMING_CHECK','ARMING_OPTIONS','ARMING_REQUIRE','BATT_MONITOR','BATT_CAPACITY','BATT_LOW_VOLT','BATT_CRT_VOLT','BATT_LOW_MAH','BATT_CRT_MAH','BATT_FS_LOW_ACT','BATT_FS_CRT_ACT','BATT_ARM_VOLT','BATT_ARM_MAH','FS_THR_ENABLE','FS_THR_VALUE','FS_GCS_ENABLE','FS_EKF_ACTION','FS_EKF_THRESH','FENCE_ENABLE','FENCE_TYPE','FENCE_ACTION','FENCE_RADIUS','FENCE_ALT_MAX','FENCE_ALT_MIN','RTL_ALT','RTL_ALT_FINAL','RTL_LOIT_TIME','LAND_SPEED','LAND_SPEED_HIGH','WPNAV_SPEED','WPNAV_ACCEL','WPNAV_RADIUS','PILOT_THR_BHV','MOT_SPIN_ARM','MOT_SPIN_MIN','MOT_SPIN_MAX','MOT_PWM_TYPE','MOT_SAFE_DISARM','MOT_SPOOL_TIME','INS_GYR_CAL','COMPASS_ENABLE','COMPASS_USE','COMPASS_USE2','COMPASS_USE3','COMPASS_DEV_ID','COMPASS_DEV_ID2','COMPASS_DEV_ID3','COMPASS_OFS_X','COMPASS_OFS_Y','COMPASS_OFS_Z','INS_ACC_ID','INS_ACCOFFS_X','INS_ACCOFFS_Y','INS_ACCOFFS_Z','GPS_TYPE','GPS1_TYPE','EK3_SRC1_POSXY','EK3_SRC1_POSZ','EK3_SRC1_VELXY','EK3_SRC1_YAW','AHRS_EKF_TYPE','SERIAL4_PROTOCOL','SERIAL4_BAUD','MIS_TOTAL']+[f'SERVO{i}_FUNCTION' for i in range(1,17)]
c=mavutil.mavlink_connection('udpin:127.0.0.1:14552',source_system=251,source_component=191,dialect='ardupilotmega')
report={'read_only':True,'parameters':{},'messages':{},'missing':[]}
end=time.monotonic()+10
while time.monotonic()<end:
 m=c.recv_match(type='HEARTBEAT',blocking=True,timeout=1)
 if m and m.get_srcSystem()==1 and m.get_srcComponent()==1:
  report['messages']['HEARTBEAT']=m.to_dict();break
else:raise SystemExit('No FC heartbeat')
c.mav.command_long_send(1,1,512,0,148,0,0,0,0,0,0)
c.mav.mission_request_list_send(1,1)
for name in names:c.mav.param_request_read_send(1,1,name.encode(),-1)
end=time.monotonic()+18
while time.monotonic()<end:
 m=c.recv_match(blocking=True,timeout=.3)
 if not m or m.get_srcSystem()!=1 or m.get_srcComponent()!=1:continue
 d=m.to_dict();kind=m.get_type()
 if kind=='PARAM_VALUE':report['parameters'][d['param_id']]={'value':d['param_value'],'type':d['param_type']}
 elif kind in ('HEARTBEAT','AUTOPILOT_VERSION','SYS_STATUS','GPS_RAW_INT','BATTERY_STATUS','EKF_STATUS_REPORT','EXTENDED_SYS_STATE','HOME_POSITION','GLOBAL_POSITION_INT','MISSION_COUNT','MISSION_CURRENT','STATUSTEXT'):report['messages'][kind]=d
report['missing']=[n for n in names if n not in report['parameters']]
count=report['messages'].get('MISSION_COUNT',{}).get('count',0)
report['mission_items']=[]
for seq in range(min(count,99)):
 c.mav.mission_request_int_send(1,1,seq)
 deadline=time.monotonic()+1
 while time.monotonic()<deadline:
  m=c.recv_match(type='MISSION_ITEM_INT',blocking=True,timeout=.2)
  if m and m.get_srcSystem()==1 and m.get_srcComponent()==1 and m.seq==seq:
   report['mission_items'].append(m.to_dict());break
c.close()
Path('commissioning/fc-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
