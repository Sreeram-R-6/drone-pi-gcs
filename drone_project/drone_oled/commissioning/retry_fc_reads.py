import json,time
from pathlib import Path
from pymavlink import mavutil
p=Path('commissioning/fc-audit.json');r=json.loads(p.read_text());names=r['missing']
c=mavutil.mavlink_connection('udpin:127.0.0.1:14552',source_system=251,source_component=191,dialect='ardupilotmega')
for first in range(0,len(names),3):
 pending=set(names[first:first+3])
 for name in pending:c.mav.param_request_read_send(1,1,name.encode(),-1)
 until=time.monotonic()+1.8
 while pending and time.monotonic()<until:
  m=c.recv_match(blocking=True,timeout=.15)
  if not m or m.get_srcSystem()!=1 or m.get_srcComponent()!=1:continue
  if m.get_type()=='PARAM_VALUE':
   d=m.to_dict();r['parameters'][d['param_id']]={'value':d['param_value'],'type':d['param_type']};pending.discard(d['param_id'])
  elif m.get_type() in ('GPS_RAW_INT','SYS_STATUS','BATTERY_STATUS','HEARTBEAT','EKF_STATUS_REPORT','HOME_POSITION','STATUSTEXT','EXTENDED_SYS_STATE'):r['messages'][m.get_type()]=m.to_dict()
r['missing']=[name for name in names if name not in r['parameters']]
p.write_text(json.dumps(r,indent=2)+'\n');c.close()
print(json.dumps({'parameters':r['parameters'],'missing':r['missing']},indent=2))
