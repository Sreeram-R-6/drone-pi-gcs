"""Dashboard state and isolated MAVLink workers for the geofence page."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from geofence_core import validate
from geofence_settings import SCHEMA, validate_changes

ROOT=Path(__file__).resolve().parents[2]
PYTHON=ROOT/'drone_project/pi_flight/.venv/bin/python'
WORKER=Path(__file__).with_name('geofence_worker.py')


class GeofenceService:
    def __init__(self):
        self.lock=threading.RLock();self.stop=threading.Event();self.thread=None;self.monitor=None;self.worker=None
        self.telemetry={'connected':False,'position':None,'home':None,'fence_supported':None,'settings':{}}
        self.updated=0;self.job={'state':'idle','message':'Draw a boundary or read the controller fence.'}
    def _monitor(self):
        while not self.stop.is_set():
            try:
                process=subprocess.Popen([str(PYTHON),'-B',str(WORKER),'monitor'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=dict(os.environ,GEOFENCE_PARENT_PID=str(os.getpid())))
                with self.lock:self.monitor=process
                if self.stop.is_set():process.terminate()
                for line in process.stdout:
                    try:data=json.loads(line)
                    except ValueError:continue
                    with self.lock:
                        if 'connected' in data:self.telemetry=data;self.updated=time.monotonic()
                        elif data.get('error'):self.telemetry['error']=data['error'];self.telemetry['connected']=False
                process.wait()
            except OSError as error:
                with self.lock:self.telemetry['error']=str(error)
            finally:
                with self.lock:self.telemetry['connected']=False
            self.stop.wait(3)
    def status(self):
        with self.lock:
            if self.thread is None:
                self.thread=threading.Thread(target=self._monitor,daemon=True);self.thread.start()
            telemetry=dict(self.telemetry)
            age=time.monotonic()-self.updated if self.updated else None
            telemetry['age_s']=round(age,1) if age is not None else None
            if age is None or age>4:telemetry.update(connected=False,position=None,armed=None)
            return {'telemetry':telemetry,'job':dict(self.job),'settings_schema':SCHEMA}
    def launch(self,operation,data):
        if operation not in {'read','upload','enable','disable','settings'}:raise ValueError('Invalid geofence operation')
        payload={}
        if operation=='settings':
            payload={'changes':validate_changes(data.get('changes'),data.get('expected')),'expected':data['expected']}
            if data.get('confirmed') is not True:raise ValueError('Confirm the safety setting changes')
        if operation=='upload':
            payload={'plan':validate(data.get('plan')),'enable_after':data.get('enable_after') is True}
            if data.get('confirmed') is not True:raise ValueError('Confirm replacement of the controller fence')
        if operation in {'enable','disable'} and data.get('confirmed') is not True:raise ValueError('Confirm the fence enforcement change')
        with self.lock:
            if self.job['state']=='running':raise RuntimeError('A fence operation is already running')
            job_id=uuid.uuid4().hex
            self.job={'id':job_id,'state':'running','operation':operation,'message':'Connecting to the flight controller'}
            threading.Thread(target=self._run,args=(job_id,operation,payload),daemon=True).start()
        return {'accepted':True,'job_id':job_id}
    def _run(self,job_id,operation,payload):
        process=None;timer=None;final=None
        try:
            process=subprocess.Popen([str(PYTHON),'-B',str(WORKER),operation],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=dict(os.environ,GEOFENCE_PARENT_PID=str(os.getpid())))
            with self.lock:self.worker=process
            if self.stop.is_set():process.terminate()
            process.stdin.write(json.dumps(payload));process.stdin.close()
            timer=threading.Timer(180,process.kill);timer.daemon=True;timer.start()
            for line in process.stdout:
                try:data=json.loads(line)
                except ValueError:continue
                with self.lock:
                    if 'progress' in data:
                        self.job['message']=data['progress']
                        for name in ('upload_verified','applied','backup'):
                            if name in data:self.job[name]=data[name]
                if 'ok' in data:final=data
            code=process.wait()
            if code!=0 or not final or not final.get('ok'):raise RuntimeError((final or {}).get('error','Fence worker stopped or timed out. Read controller contents before retrying.'))
            with self.lock:self.job={'id':job_id,'state':'complete','operation':operation,'finished_at':time.monotonic(),**final}
        except (OSError,RuntimeError) as error:
            with self.lock:self.job={**self.job,'id':job_id,**(final or {}),'state':'error','operation':operation,'finished_at':time.monotonic(),'error':str(error),'message':str(error)}
        finally:
            if timer:timer.cancel()
            if process and process.stdout:process.stdout.close()
            with self.lock:
                if self.worker is process:self.worker=None
    def close(self):
        self.stop.set()
        with self.lock:processes=[self.monitor,self.worker]
        for process in processes:
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill()
