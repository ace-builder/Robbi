import json
import math
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError,ValidationError
from robbi.imu_view import Alignment,fit_mount
from robbi.orchestration import Orchestrator,Intent,ResourceBusy
from robot.orientation import multiply


def rotate(q,v):
    return multiply(multiply(q,(0,*v)),(q[0],-q[1],-q[2],-q[3]))[1:]

def poses(mount=(1,0,0,0),front_angle=90):
    a=math.radians(front_angle)
    vectors={'upright':(0,0,1),'left':(0,-1,0),'right':(0,1,0),'front':(-math.sin(a),0,math.cos(a))}
    return {k:[x*9.80665 for x in rotate(mount,v)] for k,v in vectors.items()}

@pytest.mark.parametrize('angle',[85,90,105,120])
@pytest.mark.parametrize('mount',[(1,0,0,0),(.5,.5,.5,.5),(0,0,1,0),(.2,-.4,.8,.4)])
def test_pose_fit_recovers_mount_even_with_front_past_ninety(angle,mount):
    n=math.sqrt(sum(x*x for x in mount));mount=[x/n for x in mount]
    result=fit_mount(poses(mount,angle))['mount_wxyz']
    assert abs(sum(x*y for x,y in zip(result,mount)))==pytest.approx(1,abs=1e-7)

def test_bad_poses_are_rejected_with_actionable_errors():
    data=poses();data['left']=data['right']
    with pytest.raises(ValidationError,match='opposite'):fit_mount(data)
    data=poses();data['front']=[9.8,0,0]
    with pytest.raises(ValidationError,match='conflicts'):fit_mount(data)
    data=poses();data['front']=data['upright']
    with pytest.raises(ValidationError,match='too close'):fit_mount(data)
    data=poses();data['front']=[float('nan'),0,0]
    with pytest.raises(ValidationError,match='finite'):fit_mount(data)

@pytest.fixture
def alignment(tmp_path):
    motion=SimpleNamespace(stop=lambda:None,is_running=lambda:False)
    controller=SimpleNamespace(imu=SimpleNamespace(status=lambda:{'fresh':True,'calibration':'complete'}),motion=motion,orchestrator=Orchestrator())
    service=Alignment(tmp_path/'alignment.json',SimpleNamespace(controller=lambda:controller))
    yield service,controller
    if service.session:service.end(service.session['id'])

def test_session_excludes_other_motion_and_persists_only_view_alignment(alignment):
    service,c=alignment
    session=service.start()
    with pytest.raises(BusyError):service.start()
    with pytest.raises(ResourceBusy):c.orchestrator.acquire(('motion',),intent=Intent('autonomy',10))
    assert service.poll(session['id'])['imu']['fresh']
    result=service.save({'session_id':session['id'],'poses':poses()})
    assert result==service.load() and service.session is None
    with c.orchestrator.acquire(('motion',),intent=Intent('autonomy',10)):pass
    assert set(json.loads(service.path.read_text()))=={'version','mount_wxyz','axis_agreement_deg','saved_at'}

def test_invalid_or_expired_session_does_not_overwrite_previous_alignment(alignment):
    service,c=alignment
    s=service.start();service.save({'session_id':s['id'],'poses':poses()});original=service.path.read_bytes()
    s=service.start()
    with pytest.raises(ValidationError):service.save({'session_id':s['id'],'poses':{}})
    assert service.path.read_bytes()==original
    service.session['heartbeat']-=9
    with pytest.raises(ValidationError,match='expired'):service.poll(s['id'])
    service.end(s['id']);assert service.session is None

def test_corrupt_saved_alignment_does_not_break_dashboard(alignment):
    service,_=alignment;service.path.write_text('{bad json')
    result=service.load();assert result['calibration'] is None and result['error']

def test_failed_motor_stop_releases_reservation(alignment):
    service,c=alignment
    c.motion.is_running=lambda:True
    with pytest.raises(BusyError,match='stopping'):service.start()
    assert service.session is None
    with c.orchestrator.acquire(('motion',),intent=Intent('other',10)):pass

def test_http_calibration_lifecycle_uses_the_saved_profile(tmp_path):
    from flask import Flask
    from robbi.imu_view import blueprint
    from robbi.web import install_error_handlers
    controller=SimpleNamespace(imu=SimpleNamespace(status=lambda:{'fresh':True,'calibration':'complete'}),motion=SimpleNamespace(stop=lambda:None,is_running=lambda:False),orchestrator=Orchestrator())
    runtime=SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path),controller=lambda:controller)
    app=Flask(__name__);install_error_handlers(app);app.register_blueprint(blueprint(runtime));client=app.test_client()
    assert client.get('/api/imu-view/calibration').json=={'calibration':None}
    session=client.post('/api/imu-view/session',json={}).json
    assert client.get('/api/imu-view/session/'+session['id']).json['imu']['fresh']
    assert client.post('/api/imu-view/calibration',json={'session_id':'wrong','poses':poses()}).status_code==400
    result=client.post('/api/imu-view/calibration',json={'session_id':session['id'],'poses':poses()})
    assert result.status_code==200
    assert client.get('/api/imu-view/calibration').json==result.json
    assert client.get('/api/imu-view/session/'+session['id']).status_code==400
