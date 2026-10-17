#!/usr/bin/env python3
"""MAVLink worker launched in the flight venv. Monitoring never writes FC settings."""
import argparse
import ctypes
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time

os.environ['MAVLINK20']='1'
from pymavlink import mavutil
from geofence_core import FenceProtocol, validate, decode
from geofence_settings import SCHEMA, validate_changes, validate_combined

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'drone_project/pi_flight'))
from flightcheck.telemetry import process_lock
PARAMETERS=('FENCE_ENABLE','FENCE_TYPE','FENCE_ACTION','FENCE_MARGIN','FENCE_ALT_MAX','FENCE_RADIUS','FENCE_OPTIONS','FENCE_TOTAL')+tuple(name for name in SCHEMA if name not in ('FENCE_TYPE','FENCE_ACTION','FENCE_MARGIN','FENCE_ALT_MAX','FENCE_RADIUS','FENCE_OPTIONS'))


def emit(value):print(json.dumps(value,allow_nan=False),flush=True)


class Vehicle:
    def __init__(self,source,endpoint='tcp:127.0.0.1:5760'):
        self.link=mavutil.mavlink_connection(endpoint,source_system=source,source_component=191,dialect='ardupilotmega',autoreconnect=False)
        self.target=None;self.params={};self.cache={};self.source=source
        end=time.monotonic()+8
        while time.monotonic()<end:
            self.pump(.3)
            if self.target:break
        else:raise RuntimeError('No flight-controller heartbeat from the shared MAVLink hub')
    def close(self):self.link.close()
    def pump(self,timeout=.2):
        message=self.link.recv_match(blocking=True,timeout=timeout)
        if message is None:return None
        if self.target is None:
            if message.get_type()!='HEARTBEAT' or message.autopilot==8:return None
            self.target=(message.get_srcSystem(),message.get_srcComponent())
        if (message.get_srcSystem(),message.get_srcComponent())!=self.target:return None
        self.cache[message.get_type()]=(time.monotonic(),message)
        if message.get_type()=='PARAM_VALUE':self.params[str(message.param_id).rstrip('\x00')]=(float(message.param_value),int(message.param_type),time.monotonic())
        return message
    def latest(self,kind,age=3):
        cached=self.cache.get(kind)
        return cached[1] if cached and time.monotonic()-cached[0]<=age else None
    def request_message(self,message_id):
        self.link.mav.command_long_send(*self.target,512,0,message_id,0,0,0,0,0,0)
    def read_param(self,name):
        for _ in range(3):
            self.link.mav.param_request_read_send(*self.target,name.encode(),-1)
            end=time.monotonic()+1
            while time.monotonic()<end:
                message=self.pump(.1)
                if message and message.get_type()=='PARAM_VALUE' and str(message.param_id).rstrip('\x00')==name:return self.params[name][0]
        raise RuntimeError(f'Controller did not provide {name}')
    def settings(self):return {name:self.params[name][0] for name in PARAMETERS if name in self.params}
    def disarmed(self):
        heartbeat=self.latest('HEARTBEAT')
        end=time.monotonic()+2
        while heartbeat is None and time.monotonic()<end:
            self.pump(.1);heartbeat=self.latest('HEARTBEAT')
        if heartbeat is None:raise RuntimeError('Fresh flight-controller heartbeat required')
        if heartbeat.base_mode&128:raise RuntimeError('Land and disarm before changing the geofence')
    def write_param(self,name,value):
        self.disarmed();self.read_param(name)
        for _ in range(3):
            self.disarmed()
            self.link.mav.param_set_send(*self.target,name.encode(),float(value),self.params[name][1])
            end=time.monotonic()+1.5
            while time.monotonic()<end:
                message=self.pump(.1)
                if message and message.get_type()=='PARAM_VALUE' and str(message.param_id).rstrip('\x00')==name and math.isclose(float(message.param_value),value,rel_tol=1e-6,abs_tol=1e-5):return
        if not math.isclose(self.read_param(name),value,rel_tol=1e-6,abs_tol=1e-5):raise RuntimeError(f'{name} readback did not confirm the requested value')
    def capabilities(self):
        self.request_message(148)
        end=time.monotonic()+4
        while time.monotonic()<end:
            self.pump(.2)
            if 'AUTOPILOT_VERSION' in self.cache:
                capabilities=self.cache['AUTOPILOT_VERSION'][1].capabilities
                if not capabilities&16384:raise RuntimeError('Controller does not advertise MAVLink fence support')
                return
        raise RuntimeError('Controller did not report fence capabilities')


def inside(point,fence):
    lat,lon=point
    if fence['kind']=='circle':
        a,b=fence['center'];dlat=math.radians(lat-a);dlon=math.radians(lon-b)
        distance=2*6371000*math.asin(min(1,math.sqrt(math.sin(dlat/2)**2+math.cos(math.radians(lat))*math.cos(math.radians(a))*math.sin(dlon/2)**2)))
        return distance<=fence['radius']
    points=fence['vertices'];found=False
    for a,b in zip(points,points[1:]+points[:1]):
        if (a[0]>lat)!=(b[0]>lat) and lon<(b[1]-a[1])*(lat-a[0])/(b[0]-a[0])+a[1]:found=not found
    return found


def enable(vehicle,plan):
    vehicle.disarmed()
    end=time.monotonic()+3
    while time.monotonic()<end:
        vehicle.pump(.1)
        position=vehicle.latest('GLOBAL_POSITION_INT');gps=vehicle.latest('GPS_RAW_INT',5)
        if position and gps and gps.fix_type>=3:break
    else:raise RuntimeError('Fresh global position and 3D GPS fix are required to enable the fence')
    point=[position.lat/1e7,position.lon/1e7]
    inclusions=[inside(point,f) for f in plan['fences'] if f['inclusion']]
    union=bool(int(vehicle.read_param('FENCE_OPTIONS'))&2)
    if inclusions and not (any(inclusions) if union else all(inclusions)):raise RuntimeError('Aircraft is outside the drawn inclusion boundary')
    if any(inside(point,f) for f in plan['fences'] if not f['inclusion']):raise RuntimeError('Aircraft is inside an exclusion boundary')
    previous=int(vehicle.read_param('FENCE_TYPE'))
    if not previous&4:vehicle.write_param('FENCE_TYPE',previous|4)
    vehicle.write_param('FENCE_ENABLE',1)


def monitor(vehicle):
    vehicle.request_message(148);vehicle.request_message(242)
    last=0;next_params=0;next_home=0;queue=[]
    while True:
        vehicle.pump(.1);now=time.monotonic()
        if now>=next_params and not queue:
            queue=list(PARAMETERS);next_params=now+10
            if 'AUTOPILOT_VERSION' not in vehicle.cache:vehicle.request_message(148)
        if now>=next_home:
            vehicle.request_message(242);next_home=now+10
        if queue and now-last>.25:
            name=queue.pop(0);vehicle.link.mav.param_request_read_send(*vehicle.target,name.encode(),-1);last=now
        if now-getattr(monitor,'last_emit',0)<1:continue
        monitor.last_emit=now
        heartbeat=vehicle.latest('HEARTBEAT');position=vehicle.latest('GLOBAL_POSITION_INT');gps=vehicle.latest('GPS_RAW_INT',5)
        home=vehicle.cache.get('HOME_POSITION');version=vehicle.cache.get('AUTOPILOT_VERSION')
        fix=int(gps.fix_type) if gps else None
        emit({'connected':bool(heartbeat),'armed':bool(heartbeat.base_mode&128) if heartbeat else None,
              'target_system':vehicle.target[0],'target_component':vehicle.target[1],
              'position':{'lat':position.lat/1e7,'lon':position.lon/1e7,'relative_alt_m':position.relative_alt/1000} if position and fix is not None and fix>=3 else None,
              'home':{'lat':home[1].latitude/1e7,'lon':home[1].longitude/1e7} if home and now-home[0]<=30 and (home[1].latitude or home[1].longitude) else None,
              'runtime_fence':{'enabled':bool(vehicle.latest('SYS_STATUS').onboard_control_sensors_enabled&1048576),'healthy':bool(vehicle.latest('SYS_STATUS').onboard_control_sensors_health&1048576)} if vehicle.latest('SYS_STATUS') and vehicle.latest('SYS_STATUS').onboard_control_sensors_present&1048576 else None,
              'gps_fix':fix,'satellites':int(gps.satellites_visible) if gps and gps.satellites_visible!=255 else None,
              'fence_supported':bool(version[1].capabilities&16384) if version else None,'settings':vehicle.settings(),
              'settings_observed_at':min((vehicle.params[name][2] for name in PARAMETERS if name in vehicle.params),default=0)})
        if not heartbeat:raise RuntimeError('Flight-controller heartbeat lost')



@contextlib.contextmanager
def transfer_lock(operation):
    with contextlib.ExitStack() as stack:
        stack.enter_context(process_lock('fence-transfer.lock'))
        if operation!='read':stack.enter_context(process_lock('mission.lock'))
        yield


def main():
    # The dashboard keeps flight jobs across restarts; these helper workers must exit with their parent.
    parent=int(os.environ.get('GEOFENCE_PARENT_PID',os.getppid()))
    ctypes.CDLL(None).prctl(1,signal.SIGTERM,0,0,0)
    if os.getppid()!=parent:return
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=('monitor','read','upload','enable','disable','settings'))
    args=parser.parse_args();vehicle=None;protocol=None;upload_verified=False;transfer_started=False;applied={};backup_path=None
    try:
        data={} if args.operation=='monitor' else json.load(sys.stdin)
        vehicle=Vehicle(240 if args.operation=='monitor' else 241)
        if args.operation=='monitor':monitor(vehicle);return
        vehicle.capabilities()
        with transfer_lock(args.operation):
            protocol=FenceProtocol(vehicle.link,vehicle.target,progress=lambda text:emit({'progress':text}))
            if args.operation=='settings':
                version=vehicle.cache['AUTOPILOT_VERSION'][1].flight_sw_version
                if (version>>24,(version>>16)&255)!=(4,7):raise RuntimeError('This editor supports ArduCopter 4.7 parameter definitions; other firmware needs matching metadata')
                if vehicle.latest('HEARTBEAT').type not in (2,3,4,13,14,15,29,35):raise RuntimeError('Safety settings editor requires an ArduCopter vehicle')
                changes=validate_changes(data.get('changes'),data.get('expected'))
                vehicle.disarmed()
                if vehicle.read_param('FENCE_ENABLE')!=0:raise RuntimeError('Disable enforcement before editing safety settings')
                current={}
                for name in dict.fromkeys(list(changes)+[n for n in SCHEMA if n.startswith('FENCE_')]):
                    try:current[name]=vehicle.read_param(name)
                    except RuntimeError:
                        if name in changes or name in ('FENCE_TYPE','FENCE_MARGIN','FENCE_ALT_MAX','FENCE_ALT_MIN','FENCE_ALT_MAX_TP','FENCE_ALT_MIN_TP','FENCE_RADIUS'):raise
                for name in changes:
                    if not math.isclose(current[name],data['expected'][name],rel_tol=1e-6,abs_tol=1e-5):raise RuntimeError(name+' changed on the controller; refresh and review again')
                validate_combined({**current,**changes})
                backup=ROOT/'video/.geofence';backup.mkdir(parents=True,exist_ok=True)
                path=backup/(time.strftime('%Y%m%dT%H%M%S',time.gmtime())+'-settings-'+str(os.getpid())+'.json')
                path.write_text(json.dumps({'before':current,'requested':changes},indent=2));path.chmod(0o600);backup_path=str(path.relative_to(ROOT))
                for name,value in changes.items():
                    emit({'progress':'Saving and verifying '+name})
                    if vehicle.read_param('FENCE_ENABLE')!=0:raise RuntimeError('Fence was enabled externally; remaining changes stopped')
                    if not math.isclose(vehicle.read_param(name),current[name],rel_tol=1e-6,abs_tol=1e-5):raise RuntimeError(name+' changed externally; remaining changes stopped')
                    vehicle.write_param(name,value);applied[name]=vehicle.read_param(name)
                    if not math.isclose(applied[name],value,rel_tol=1e-6,abs_tol=1e-5):raise RuntimeError(name+' final readback mismatch')
                plan=None
            elif args.operation=='read':
                plan=decode(protocol.download())
            else:
                vehicle.disarmed()
                if args.operation=='upload':
                    plan=validate(data.get('plan'))
                    if vehicle.read_param('FENCE_ENABLE')!=0:raise RuntimeError('Disable fence enforcement before replacing boundaries')
                    # Save the previous boundary before any replacement, for operator recovery.
                    previous=decode(protocol.download())
                    backup=ROOT/'video/.geofence';backup.mkdir(parents=True,exist_ok=True)
                    path=backup/(time.strftime('%Y%m%dT%H%M%S',time.gmtime())+'-'+str(os.getpid())+'.json')
                    path.write_text(json.dumps({'plan':previous,'settings':vehicle.settings()},indent=2));path.chmod(0o600);backup_path=str(path.relative_to(ROOT))
                    vehicle.disarmed()
                    protocol.require_disarmed=True
                    transfer_started=True
                    plan=protocol.upload(plan)
                    upload_verified=True
                    emit({'progress':'Boundaries uploaded and readback verified; checking enforcement settings','upload_verified':True,'backup':backup_path})
                    if data.get('enable_after') is True:enable(vehicle,plan)
                elif args.operation=='enable':
                    plan=validate(decode(protocol.download()));enable(vehicle,plan)
                else:
                    vehicle.write_param('FENCE_ENABLE',0);plan=None
            for name in PARAMETERS:
                try:vehicle.read_param(name)
                except RuntimeError:
                    if name in ('FENCE_ENABLE','FENCE_TYPE'):raise
            if args.operation=='settings':
                if vehicle.read_param('FENCE_ENABLE')!=0:raise RuntimeError('Enforcement changed externally; inspect controller state')
                for name,value in changes.items():
                    if not math.isclose(vehicle.read_param(name),value,rel_tol=1e-6,abs_tol=1e-5):raise RuntimeError(name+' changed before final verification')
            emit({'ok':True,'upload_verified':upload_verified,'applied':applied,'backup':backup_path,'plan':plan,'settings':vehicle.settings(),'message':'Fence upload accepted and readback verified' if args.operation=='upload' else 'Safety settings saved and readback verified' if args.operation=='settings' else 'Controller fence operation complete'})
    except Exception as error:
        if protocol and args.operation=='upload' and transfer_started and not upload_verified:
            try:vehicle.link.mav.mission_ack_send(*vehicle.target,15,1)
            except Exception:pass
        detail=str(error)
        if upload_verified:detail='Fence boundaries were uploaded and verified, but the following step failed: '+detail+'. Read enforcement state before flying.'
        emit({'ok':False,'applied':applied,'backup':backup_path,'settings':vehicle.settings() if vehicle else {},'upload_verified':upload_verified,'error':detail});raise SystemExit(1)
    finally:
        if vehicle:vehicle.close()


if __name__=='__main__':main()
