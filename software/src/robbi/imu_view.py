"""Display-axis alignment and shared upright reference; no gyro filter reset."""
import json
import math
from pathlib import Path
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from .errors import BusyError, ValidationError
from .orchestration import Intent
from .storage import write_json


def unit(v):
    length=math.sqrt(sum(x*x for x in v))
    if length<1e-6:raise ValidationError('These poses do not define distinct axes. Capture them again.')
    return [x/length for x in v]

def dot(a,b):return sum(x*y for x,y in zip(a,b, strict=True))
def cross(a,b):return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
def project(v,up):return unit([x-dot(v,up)*z for x,z in zip(v,up, strict=True)])


def fit_mount(poses):
    if not isinstance(poses,dict) or set(poses)!={'upright','left','right','front'}:
        raise ValidationError('Capture upright, left side, right side and front poses.')
    vectors={}
    for name,value in poses.items():
        if not isinstance(value,list) or len(value)!=3 or any(type(x) not in (int,float) or not math.isfinite(x) for x in value):
            raise ValidationError('Every pose must contain three finite acceleration values.')
        magnitude=math.sqrt(dot(value,value))
        if not 7.8<magnitude<11.8:raise ValidationError('Keep Robbi still while capturing the '+name+' pose.')
        vectors[name]=unit(value)
    up,left,right,front=(vectors[k] for k in ['upright','left','right','front'])
    if dot(left,right)>-.7 or max(abs(dot(up,left)),abs(dot(up,right)))>.55:
        raise ValidationError('Left/right poses are not opposite sides. Lay Robbi fully on each side and capture again.')
    if abs(dot(front,up))>.85:
        raise ValidationError('The front pose is too close to upright. Gently rest Robbi on its front.')
    side=project([r-l for r,l in zip(right,left, strict=True)],up)
    forward_from_sides=unit(cross(side,up))
    forward_from_front=project([-x for x in front],up)
    agreement=dot(forward_from_sides,forward_from_front)
    if agreement<.7:
        raise ValidationError('The front pose conflicts with the side poses. Use Robbi’s own left and right, and the face/camera side as front.')
    forward=unit([a+b for a,b in zip(forward_from_sides,forward_from_front, strict=True)])
    side=unit(cross(up,forward))
    # Columns are robot forward/left/up expressed in sensor coordinates.
    m=[[forward[i],side[i],up[i]] for i in range(3)]
    trace=sum(m[i][i] for i in range(3))
    if trace>0:
        s=math.sqrt(trace+1)*2
        q=[s/4,(m[2][1]-m[1][2])/s,(m[0][2]-m[2][0])/s,(m[1][0]-m[0][1])/s]
    else:
        i=max(range(3),key=lambda k:m[k][k]);j=(i+1)%3;k=(i+2)%3
        s=math.sqrt(1+m[i][i]-m[j][j]-m[k][k])*2
        q=[(m[k][j]-m[j][k])/s,0,0,0]
        q[i+1]=s/4;q[j+1]=(m[j][i]+m[i][j])/s;q[k+1]=(m[k][i]+m[i][k])/s
    return {'mount_wxyz':unit(q),'axis_agreement_deg':round(math.degrees(math.acos(min(1,max(-1,agreement)))),2)}


class Alignment:
    def __init__(self,path,runtime):
        self.path=Path(path);self.runtime=runtime;self.lock=threading.RLock();self.session=None

    def load(self):
        try:
            if not self.path.exists():return {'calibration':None}
            if self.path.stat().st_size>4096:raise ValueError()
            value=json.loads(self.path.read_text())
            q=value['mount_wxyz']
            if value.get('version')!=1 or len(q)!=4 or not all(type(x) in (int,float) and math.isfinite(x) for x in q) or abs(dot(q,q)-1)>.01:raise ValueError()
            return {'calibration':value}
        except (OSError,ValueError,KeyError,TypeError):
            return {'calibration':None,'error':'Saved axis alignment could not be read. Run the four-pose calibration again.'}

    def start(self):
        controller=self.runtime.controller()
        imu=controller.imu.status()
        if not imu.get('fresh') or imu.get('calibration')!='complete':
            raise ValidationError('Wait for fresh IMU readings and completed gyro calibration first.')
        with self.lock:
            if self.session:raise BusyError('Another axis calibration is in progress. Finish or cancel it first.')
            lease=controller.orchestrator.acquire(('motion',),intent=Intent('imu-axis-calibration',100),ttl_s=120)
            try:
                controller.motion.stop()
                if controller.motion.is_running():raise BusyError('Motors are still stopping. Try again in a moment.')
                session={'id':uuid.uuid4().hex,'lease':lease,'started':time.monotonic(),'heartbeat':time.monotonic(),'stop':threading.Event()}
                self.session=session
                threading.Thread(target=self._watch,args=(session,),daemon=True,name='imu-alignment').start()
                return {'id':session['id'],'remaining_s':120}
            except Exception:
                lease.release();raise

    def _watch(self,session):
        try:
            while not session['stop'].wait(.5):
                with self.lock:
                    if self.session is not session or session['lease'].is_set() or time.monotonic()-session['heartbeat']>8:return
        finally:self.end(session['id'])

    def active(self,identifier):
        session=self.session
        if not session or session['id']!=identifier or session['lease'].is_set() or time.monotonic()-session['heartbeat']>8:
            raise ValidationError('Calibration session expired or was stopped. Put Robbi upright, then start again.')
        return session

    def poll(self,identifier):
        with self.lock:
            session=self.active(identifier);session['heartbeat']=time.monotonic()
            return {'imu':self.runtime.controller().imu.status(),'remaining_s':round(max(0,120-(time.monotonic()-session['started'])),1)}

    def end(self,identifier):
        with self.lock:
            if self.session and self.session['id']==identifier:
                session=self.session;self.session=None;session['stop'].set();session['lease'].release()

    def save(self,data):
        if not isinstance(data,dict):raise ValidationError('Supply the calibration poses as an object.')
        with self.lock:
            session=self.active(data.get('session_id'))
            fitted=fit_mount(data.get('poses'))
            value={'version':1,**fitted,'saved_at':time.time()}
            write_json(self.path,value)
            setter=getattr(self.runtime.controller().imu,'set_upright_reference',None)
            if setter:setter(data['poses']['upright'])
            self.end(session['id'])
            return {'calibration':value}

    def upright(self):
        """Confirm a stationary owner-selected pose while holding motor ownership."""
        session=self.start()
        try:
            samples=[];last_count=None;deadline=time.monotonic()+1.5
            while time.monotonic()<deadline:
                sample=self.poll(session['id'])['imu']
                accel=sample.get('acceleration_m_s2');gyro=sample.get('gyro_deg_s');bias=sample.get('gyro_bias_deg_s')
                valid=lambda v:isinstance(v,(list,tuple)) and len(v)==3 and all(type(x) in (int,float) and math.isfinite(x) for x in v)
                if (not sample.get('fresh') or sample.get('calibration')!='complete'
                        or not all(valid(v) for v in (accel,gyro,bias))
                        or not 7.8<math.sqrt(dot(accel,accel))<11.8
                        or math.sqrt(sum((g-b)**2 for g,b in zip(gyro,bias, strict=True)))>3):
                    raise ValidationError('Keep Robbi upright and completely still, then set the pose again.')
                count=sample.get('sample_count',sample.get('timestamp'))
                if count!=last_count:
                    if samples and math.sqrt(sum((a-b)**2 for a,b in zip(accel,samples[0], strict=True)))>.35:
                        raise ValidationError('Robbi moved during capture. Hold still and try again.')
                    samples.append(list(accel));last_count=count
                time.sleep(.1)
            if len(samples)<8:raise ValidationError('Not enough fresh IMU samples. Try again when readings recover.')
            mean=[sum(v[i] for v in samples)/len(samples) for i in range(3)]
            result=self.runtime.controller().imu.set_upright_reference(mean)
            return {'ok':True,'reference':result,'message':'Upright pose saved for driving, navigation and handling.'}
        finally:self.end(session['id'])


def blueprint(runtime):
    service=Alignment(runtime.config.data_dir/'imu-view-alignment.json',runtime)
    bp=Blueprint('imu_view',__name__,url_prefix='/api/imu-view')
    @bp.route('/calibration',methods=['GET','POST'])
    def calibration():return jsonify(service.save(request.get_json() or {}) if request.method=='POST' else service.load())
    @bp.post('/session')
    def start():return jsonify(service.start()),201
    @bp.post('/upright')
    def upright():return jsonify(service.upright())
    @bp.route('/session/<identifier>',methods=['GET','DELETE'])
    def session(identifier):
        if request.method=='DELETE':service.end(identifier);return jsonify(ok=True)
        return jsonify(service.poll(identifier))
    return bp
