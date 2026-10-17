"""Offline geofence geometry, wire protocol and HTTP tests; never opens the real hub."""
import http.client
import json
from pathlib import Path
import sys
import socket
import time
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).parent/'web'))
from geofence_core import validate,encode,decode,matches,FenceProtocol
import web_dashboard

PLAN={'fences':[{'kind':'circle','inclusion':True,'center':[12.3456789,76.1234567],'radius':30},
                {'kind':'polygon','inclusion':False,'vertices':[[12.346,76.123],[12.346,76.124],[12.347,76.124]]}]}


def message(kind,**values):
    defaults={'target_system':241,'target_component':191,'mission_type':1}
    defaults.update(values)
    return SimpleNamespace(**defaults,get_type=lambda:kind,get_srcSystem=lambda:1,get_srcComponent=lambda:1)


def item(seq,raw):return message('MISSION_ITEM_INT',seq=seq,frame=5,param2=0,param3=0,param4=0,z=0,**raw)


class Link:
    def __init__(self,items):
        self.items=items;self.queue=[];self.calls=[];self.mav=self;self.uploaded={};self.duplicate=False;self.rejection=None;self.wrong_readback=False;self.premature=False;self.armed=False
    def recv_match(self,**kwargs):return self.queue.pop(0) if self.queue else None
    def mission_count_send(self,sysid,compid,count,mission_type):
        self.calls.append(('count',count,mission_type));self.count=count
        if self.rejection is not None:self.queue.append(message('MISSION_ACK',type=self.rejection))
        elif self.premature:self.queue.append(message('MISSION_ACK',type=0))
        else:
            if self.armed:self.queue.append(message('HEARTBEAT',base_mode=128))
            self.queue.extend([message('MISSION_ACK',type=0,mission_type=0),message('MISSION_REQUEST_INT',seq=0,target_system=200),message('MISSION_REQUEST_INT',seq=0)])
    def mission_item_int_send(self,*args):
        sysid,compid,seq,frame,command,current,autocontinue,p1,p2,p3,p4,x,y,z,mission_type=args
        self.calls.append(('item',seq,mission_type,x,y));self.uploaded[seq]={'command':command,'param1':p1,'x':x,'y':y}
        if self.duplicate and seq==0:self.duplicate=False;self.queue.append(message('MISSION_REQUEST_INT',seq=0));return
        if seq+1<self.count:self.queue.append(message('MISSION_REQUEST_INT',seq=seq+1))
        else:
            self.items=[self.uploaded[i] for i in range(self.count)];self.queue.append(message('MISSION_ACK',type=0))
    def mission_request_list_send(self,sysid,compid,mission_type):
        self.calls.append(('list',mission_type));self.queue.append(message('MISSION_COUNT',count=len(self.items)))
    def mission_request_int_send(self,sysid,compid,seq,mission_type):
        raw=dict(self.items[seq])
        if self.wrong_readback:raw['x']+=100
        self.queue.append(item(seq,raw))
    def mission_ack_send(self,*args):self.calls.append(('ack',args[-2],args[-1]))


class Tests(unittest.TestCase):
    def test_geometry_roundtrip_and_integer_coordinates(self):
        normalized=validate(PLAN);items=encode(normalized)
        self.assertEqual(items[0]['x'],123456789);self.assertEqual(items[0]['y'],761234567)
        decoded=decode([item(i,value) for i,value in enumerate(items)])
        self.assertEqual(decoded,normalized)
        self.assertEqual([p['param1'] for p in items[1:]],[3,3,3])
    def test_invalid_geometry_never_uploads(self):
        invalid=[{'fences':[]},{'fences':[{'kind':'circle','inclusion':True,'center':[True,0],'radius':30}]},
                 {'fences':[{'kind':'circle','inclusion':True,'center':[0,0],'radius':float('nan')}]},
                 {'fences':[{'kind':'polygon','inclusion':True,'vertices':[[0,0],[1,1],[0,1],[1,0]]}]}]
        for plan in invalid:
            link=Link([])
            with self.assertRaises(ValueError):FenceProtocol(link,(1,1)).upload(plan)
            self.assertEqual(link.calls,[])
    def test_upload_verifies_readback_and_ignores_other_clients(self):
        link=Link([]);link.duplicate=True
        result=FenceProtocol(link,(1,1),timeout=.01).upload(PLAN)
        self.assertEqual(result,validate(PLAN))
        self.assertEqual(len([call for call in link.calls if call[0]=='item']),len(encode(PLAN))+1)
        self.assertTrue(all(call[2]==1 for call in link.calls if call[0] in ('count','item')))
    def test_rejected_premature_and_wrong_readback_do_not_report_success(self):
        for setting,value in [('rejection',4),('premature',True),('wrong_readback',True),('armed',True)]:
            link=Link([]);setattr(link,setting,value)
            with self.assertRaises(RuntimeError):FenceProtocol(link,(1,1),timeout=.01).upload(PLAN)
    def test_empty_download_is_not_an_empty_upload(self):
        link=Link([]);protocol=FenceProtocol(link,(1,1),timeout=.01)
        self.assertEqual(decode(protocol.download()),{'version':1,'fences':[]})
        self.assertFalse(any(call[0]=='count' for call in link.calls))
    def test_timeout_is_bounded_and_retries_fence_count(self):
        link=Link([]);link.mission_count_send=lambda *args:link.calls.append(args)
        with self.assertRaises(TimeoutError):FenceProtocol(link,(1,1),timeout=.001,retries=2).upload(PLAN)
        self.assertEqual(len(link.calls),2)
    def test_advanced_groups_cannot_be_silently_removed(self):
        data=item(0,encode(PLAN)[0]);data.param2=1
        with self.assertRaises(ValueError):decode([data])
    def test_enabling_preserves_other_parameters_and_checks_current_position(self):
        try:
            import geofence_worker as worker
        except ImportError:self.skipTest('Run in flight venv')
        actor=Mock();actor.params={};actor.read_param.side_effect=lambda name:{'FENCE_OPTIONS':0,'FENCE_TYPE':3}[name]
        position=SimpleNamespace(lat=123456789,lon=761234567)
        actor.latest.side_effect=lambda kind,*args:position if kind=='GLOBAL_POSITION_INT' else SimpleNamespace(fix_type=3)
        worker.enable(actor,validate(PLAN))
        self.assertEqual(actor.write_param.call_args_list,[unittest.mock.call('FENCE_TYPE',7),unittest.mock.call('FENCE_ENABLE',1)])
        actor.write_param.reset_mock();position.lat=130000000
        with self.assertRaisesRegex(RuntimeError,'outside'):worker.enable(actor,validate(PLAN))
        actor.write_param.assert_not_called()
    def test_enabling_without_3d_fix_never_writes_parameters(self):
        try:
            import geofence_worker as worker
        except ImportError:self.skipTest('Run in flight venv')
        import itertools
        actor=Mock();actor.latest.side_effect=lambda kind,*args:SimpleNamespace(lat=123456789,lon=761234567) if kind=='GLOBAL_POSITION_INT' else SimpleNamespace(fix_type=2)
        with patch.object(worker.time,'monotonic',side_effect=itertools.count()):
            with self.assertRaisesRegex(RuntimeError,'3D GPS'):worker.enable(actor,validate(PLAN))
        actor.write_param.assert_not_called()

    def test_real_mavlink2_wire_upload_against_simulated_controller(self):
        try:
            from pymavlink.dialects.v20 import ardupilotmega as mav
            from geofence_worker import Vehicle
        except ImportError:self.skipTest('Run in the flight venv for wire-level test')
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        address=listener.getsockname();stopping=threading.Event();stored=[];errors=[]
        def controller():
            peer=None
            try:
                peer,_=listener.accept();peer.settimeout(.05)
                class Writer:
                    def write(self,data):peer.sendall(data)
                encoder=mav.MAVLink(Writer(),srcSystem=1,srcComponent=1);decoder=mav.MAVLink(None)
                last=0;count=0
                while not stopping.is_set():
                    now=time.monotonic()
                    if now-last>.2:encoder.heartbeat_send(2,3,0,0,4);last=now
                    try:data=peer.recv(65535)
                    except socket.timeout:continue
                    if not data:break
                    for packet in decoder.parse_buffer(data) or []:
                        kind=packet.get_type()
                        if kind.startswith('MISSION_'):self.assertEqual(packet.mission_type,1)
                        if kind=='MISSION_COUNT':
                            count=packet.count;stored.clear();encoder.mission_request_int_send(241,191,0,1)
                        elif kind=='MISSION_ITEM_INT':
                            self.assertEqual(packet.seq,len(stored));stored.append(packet)
                            if len(stored)<count:encoder.mission_request_int_send(241,191,len(stored),1)
                            else:encoder.mission_ack_send(241,191,0,1)
                        elif kind=='MISSION_REQUEST_LIST':encoder.mission_count_send(241,191,len(stored),1)
                        elif kind=='MISSION_REQUEST_INT':
                            p=stored[packet.seq];encoder.mission_item_int_send(241,191,p.seq,p.frame,p.command,0,0,p.param1,p.param2,p.param3,p.param4,p.x,p.y,p.z,1)
            except Exception as error:errors.append(error)
            finally:
                if peer:peer.close()
        thread=threading.Thread(target=controller,daemon=True);thread.start();vehicle=None
        try:
            vehicle=Vehicle(241,endpoint=f'tcp:{address[0]}:{address[1]}')
            result=FenceProtocol(vehicle.link,vehicle.target,timeout=.5).upload(PLAN)
            self.assertEqual(result,validate(PLAN));self.assertEqual(len(stored),4)
        finally:
            stopping.set()
            if vehicle:vehicle.close()
            listener.close();thread.join(2)
        self.assertEqual(errors,[])

    def test_http_page_and_actions_require_token_and_origin(self):
        fences=Mock();fences.status.return_value={'telemetry':{'connected':False},'job':{'state':'idle'}};fences.launch.return_value={'accepted':True}
        server=web_dashboard.DashboardServer(('127.0.0.1',0),{'sites':[]},fences=fences)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
            connection.request('POST' if body is not None else 'GET',path,body=json.dumps(body) if body is not None else None,headers=headers or {})
            response=connection.getresponse();result=response.status,response.read(),response.getheader('Content-Security-Policy');connection.close();return result
        try:
            code,page,csp=request('/geofence');self.assertEqual(code,200);self.assertIn(b'Draw polygon',page);self.assertIn('https://tile.openstreetmap.org',csp)
            self.assertEqual(request('/api/geofence/upload',{'plan':PLAN})[0],403);fences.launch.assert_not_called()
            headers={'X-Dashboard-Token':server.token,'Origin':f'http://127.0.0.1:{server.server_port}'}
            self.assertEqual(request('/api/geofence/read',{},headers)[0],202);fences.launch.assert_called_once_with('read',{})
            self.assertEqual(request('/map-assets/../README.md')[0],404)
            self.assertEqual(request('/map-assets/leaflet.js')[0],200)
        finally:server.shutdown();thread.join();server.server_close()


if __name__=='__main__':unittest.main()
