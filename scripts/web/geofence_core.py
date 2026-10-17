"""Validated fence geometry and MAVLink 2 fence transactions, independent of HTTP."""
import math
import time

FENCE = 1
POLYGON_IN, POLYGON_OUT, CIRCLE_IN, CIRCLE_OUT, RETURN = 5001, 5002, 5003, 5004, 5000
MAX_ITEMS = 100


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def coordinate(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:raise ValueError('Coordinates must be [latitude, longitude]')
    lat, lon = number(value[0], 'Latitude'), number(value[1], 'Longitude')
    if not -85 <= lat <= 85 or not -180 <= lon <= 180:raise ValueError('Coordinate outside the supported map range')
    return [round(lat, 7), round(lon, 7)]


def cross(a, b, c):return (b[1]-a[1])*(c[0]-a[0])-(b[0]-a[0])*(c[1]-a[1])


def intersects(a,b,c,d):
    def on(a,b,p):return abs(cross(a,b,p))<1e-15 and min(a[0],b[0])-1e-12<=p[0]<=max(a[0],b[0])+1e-12 and min(a[1],b[1])-1e-12<=p[1]<=max(a[1],b[1])+1e-12
    return (cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0) or any((on(a,b,c),on(a,b,d),on(c,d,a),on(c,d,b)))


def validate(plan, allow_empty=False):
    if not isinstance(plan,dict) or not isinstance(plan.get('fences'),list):raise ValueError('Expected a fence plan')
    if not plan['fences'] and not allow_empty:raise ValueError('Draw at least one boundary; an empty upload would erase the fence')
    if len(plan['fences'])>MAX_ITEMS:raise ValueError('Too many boundaries')
    result={'version':1,'fences':[]};total=0
    for fence in plan['fences']:
        if not isinstance(fence,dict) or type(fence.get('inclusion')) is not bool:raise ValueError('Choose inclusion or exclusion for each boundary')
        item={'kind':fence.get('kind'),'inclusion':fence['inclusion']}
        if item['kind']=='circle':
            item['center']=coordinate(fence.get('center'))
            item['radius']=number(fence.get('radius'),'Radius')
            if not 1<=item['radius']<=10000:raise ValueError('Circle radius must be 1–10000 metres')
            total+=1
        elif item['kind']=='polygon':
            vertices=fence.get('vertices')
            if not isinstance(vertices,list):raise ValueError('Polygon vertices are missing')
            points=[coordinate(p) for p in vertices]
            if len(points)>1 and points[0]==points[-1]:points.pop()
            if not 3<=len(points)<=MAX_ITEMS or len({tuple(p) for p in points})!=len(points):raise ValueError('Polygon needs at least three distinct vertices')
            if max(p[1] for p in points)-min(p[1] for p in points)>180:raise ValueError('Polygons crossing the date line are not supported')
            area=sum(points[i][1]*points[(i+1)%len(points)][0]-points[(i+1)%len(points)][1]*points[i][0] for i in range(len(points)))
            if abs(area)<1e-12:raise ValueError('Polygon has no usable area')
            for i in range(len(points)):
                for j in range(i+1,len(points)):
                    if j==i+1 or (i==0 and j==len(points)-1):continue
                    if intersects(points[i],points[(i+1)%len(points)],points[j],points[(j+1)%len(points)]):raise ValueError('Polygon edges must not cross or touch')
            # QGC .plan polygons use clockwise winding.
            if area>0:points.reverse()
            item['vertices']=points;total+=len(points)
        else:raise ValueError('Choose circle or polygon')
        result['fences'].append(item)
    if plan.get('return_point') is not None:result['return_point']=coordinate(plan['return_point']);total+=1
    if total>MAX_ITEMS:raise ValueError(f'Plan exceeds {MAX_ITEMS} fence items')
    return result


def encode(plan):
    plan=validate(plan,allow_empty=True);items=[]
    def add(command,point,p1=0):items.append({'command':command,'param1':p1,'x':round(point[0]*1e7),'y':round(point[1]*1e7)})
    if 'return_point' in plan:add(RETURN,plan['return_point'])
    for fence in plan['fences']:
        if fence['kind']=='circle':add(CIRCLE_IN if fence['inclusion'] else CIRCLE_OUT,fence['center'],fence['radius'])
        else:
            for point in fence['vertices']:add(POLYGON_IN if fence['inclusion'] else POLYGON_OUT,point,len(fence['vertices']))
    return items


def decode(items):
    plan={'version':1,'fences':[]};index=0
    while index<len(items):
        item=items[index];command=int(item.command)
        if any(abs(float(getattr(item,f'param{n}',0)))>1e-6 for n in (2,3,4)) or abs(float(item.z))>1e-6:
            raise ValueError('Fence uses advanced fields unsupported by this editor; keep the controller plan unchanged')
        point=[item.x/1e7,item.y/1e7]
        if command==RETURN:
            if 'return_point' in plan:raise ValueError('Controller returned duplicate return points')
            plan['return_point']=point;index+=1
        elif command in (CIRCLE_IN,CIRCLE_OUT):
            plan['fences'].append({'kind':'circle','inclusion':command==CIRCLE_IN,'center':point,'radius':float(item.param1)});index+=1
        elif command in (POLYGON_IN,POLYGON_OUT):
            count=int(item.param1)
            if count!=item.param1 or not 3<=count<=MAX_ITEMS or index+count>len(items):raise ValueError('Invalid polygon vertex count from controller')
            group=items[index:index+count]
            if any(p.command!=command or p.param1!=count or any(float(getattr(p,f'param{n}',0))!=0 for n in (2,3,4)) or float(p.z)!=0 for p in group):raise ValueError('Inconsistent polygon group from controller')
            plan['fences'].append({'kind':'polygon','inclusion':command==POLYGON_IN,'vertices':[[p.x/1e7,p.y/1e7] for p in group]});index+=count
        else:raise ValueError(f'Unsupported fence command {command}; no replacement was made')
    return validate(plan,allow_empty=True)


def matches(expected,actual):
    if len(expected)!=len(actual):return False
    return all(a['command']==b.command and a['x']==b.x and a['y']==b.y and abs(a['param1']-b.param1)<=max(.001,abs(a['param1'])*1e-6) and all(abs(float(getattr(b,f'param{n}',0)))<1e-6 for n in (2,3,4)) and abs(float(b.z))<1e-6 for a,b in zip(expected,actual))


class FenceProtocol:
    def __init__(self,link,target,source=241,component=191,timeout=2,retries=4,progress=lambda message:None):
        self.link,self.target,self.source,self.component=link,target,source,component
        self.timeout,self.retries,self.progress=timeout,retries,progress
        self.deadline=time.monotonic()+120
        self.require_disarmed=False

    def receive(self,kinds):
        end=min(self.deadline,time.monotonic()+self.timeout)
        while time.monotonic()<end:
            message=self.link.recv_match(blocking=True,timeout=min(.2,max(.001,end-time.monotonic())))
            if message is None:continue
            if message.get_srcSystem()!=self.target[0] or message.get_srcComponent()!=self.target[1]:continue
            kind=message.get_type()
            if self.require_disarmed and kind=='HEARTBEAT' and message.base_mode&128:raise RuntimeError('Aircraft armed during fence operation; transfer stopped')
            if kind not in kinds or getattr(message,'mission_type',0)!=FENCE:continue
            if getattr(message,'target_system',0) not in (0,self.source) or getattr(message,'target_component',0) not in (0,self.component):continue
            if kind=='MISSION_ACK' and message.type!=0:raise RuntimeError(f'Controller rejected fence transfer (MAV_MISSION_RESULT {message.type})')
            return message
        return None

    def request(self,send,kinds):
        for _ in range(self.retries):
            if time.monotonic()>=self.deadline:break
            send();message=self.receive(kinds+['MISSION_ACK'])
            if message is not None:
                if message.get_type()=='MISSION_ACK':raise RuntimeError('Unexpected fence acknowledgement')
                return message
        raise TimeoutError('Fence transfer timed out; controller contents are not verified')

    def download(self):
        mav=self.link.mav
        count=self.request(lambda:mav.mission_request_list_send(*self.target,FENCE),['MISSION_COUNT'])
        if not 0<=count.count<=MAX_ITEMS:raise ValueError('Controller fence is too large for this editor; no changes made')
        result=[]
        for seq in range(count.count):
            send=lambda seq=seq:mav.mission_request_int_send(*self.target,seq,FENCE)
            for attempt in range(self.retries):
                item=self.request(send,['MISSION_ITEM_INT','MISSION_ITEM'])
                if item.seq==seq:break
            else:raise RuntimeError('Controller repeatedly returned the wrong fence sequence')
            if item.get_type()!='MISSION_ITEM_INT':raise RuntimeError('Controller returned legacy floating-point fence coordinates; integer transfer required')
            if item.frame not in (0,5):raise ValueError('Unsupported fence coordinate frame')
            result.append(item)
        mav.mission_ack_send(*self.target,0,FENCE)
        return result

    def upload(self,plan):
        self.require_disarmed=True
        items=encode(validate(plan));mav=self.link.mav;served=set()
        last=lambda:mav.mission_count_send(*self.target,len(items),FENCE)
        last();failures=0
        while time.monotonic()<self.deadline:
            message=self.receive(['MISSION_REQUEST_INT','MISSION_REQUEST','MISSION_ACK'])
            if message is None:
                failures+=1
                if failures>=self.retries:raise TimeoutError('No fence upload acknowledgement; upload is not verified')
                last();continue
            failures=0
            if message.get_type()=='MISSION_ACK':
                if len(served)!=len(items):raise RuntimeError('Premature fence acknowledgement; upload is not verified')
                break
            seq=message.seq
            if not 0<=seq<len(items):raise RuntimeError('Controller requested an invalid fence item')
            item=items[seq]
            last=lambda seq=seq,item=item:mav.mission_item_int_send(*self.target,seq,5,item['command'],0,0,item['param1'],0,0,0,item['x'],item['y'],0,FENCE)
            last();served.add(seq);self.progress(f'Uploading fence item {seq+1}/{len(items)}')
        else:raise TimeoutError('Fence upload deadline exceeded')
        self.progress('Upload accepted; reading fence back for verification')
        downloaded=self.download()
        if not matches(items,downloaded):raise RuntimeError('Fence readback does not match the upload; enforcement was not enabled')
        return decode(downloaded)
