"""Manual dashboard admission/recovery with no GPIO or real robot calls."""
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
from flask import Flask

from robbi.manual_motion import blueprint, validate_command
from robbi.orchestration import current_intent, MANUAL
from robbi.web import install_error_handlers
from robot.motion import MotionManager


@pytest.fixture
def rig():
    status = dict(enabled=True, available=True, running=False, encoder_enabled=True,
                  heading=dict(enabled=True, available=True, source='imu_encoder'))
    tof = dict(enabled=True, available=True, fresh=True, distance_cm=50)
    future = Future()
    cancel = threading.Event()
    def drive(spec, **kw):
        assert spec['require_encoders'] is True
        assert current_intent().priority == MANUAL
        assert kw['cancel_event'] is cancel
        return future
    controller = SimpleNamespace(
        motion=SimpleNamespace(status=lambda: dict(status), drive=Mock(side_effect=drive)),
        tof=SimpleNamespace(status=lambda: dict(tof)),
        imu=SimpleNamespace(status=lambda: dict(available=True, fresh=True)),
        slam=SimpleNamespace(track_drive_action=Mock()), _action_token=lambda: (1, cancel))
    app = Flask(__name__)
    install_error_handlers(app)
    app.register_blueprint(blueprint(SimpleNamespace(controller=lambda: controller)))
    return app.test_client(), controller, status, tof, future


def test_tof_loss_is_advisory_and_recovery_never_replays(rig):
    client, controller, status, tof, future = rig
    tof.update(fresh=False, distance_cm=None)
    response=client.post('/api/manual-motion', json={'mode':'distance'})
    assert response.status_code==200
    assert response.json['command']['warnings']==['front_range_unavailable']
    assert controller.motion.drive.call_args.args[0]['distance_cm']==20
    assert controller.motion.drive.call_args.args[0]['speed']==100
    tof.update(fresh=True,distance_cm=55)
    future.set_result({'ok':True})
    assert client.get('/api/manual-motion').json['command']['state']=='completed'
    controller.motion.drive.assert_called_once()


@pytest.mark.parametrize('spec',[{'mode':'turn','angle_deg':90},{'mode':'distance','distance_cm':-20}])
def test_front_range_loss_does_not_lock_turns_or_reverse(rig,spec):
    client,controller,_,tof,_=rig
    tof.update(fresh=False,distance_cm=None,range_state='no_return')
    assert client.post('/api/manual-motion',json=spec).status_code==200
    controller.motion.drive.assert_called_once()


def test_commands_report_actual_completion_and_block_double_submission(rig):
    client, controller, status, tof, future = rig
    first = client.post('/api/manual-motion', json={'mode':'turn', 'angle_deg':-90}).json
    assert client.post('/api/manual-motion', json={'mode':'turn'}).status_code == 429
    result = dict(ok=True, heading={'source':'imu_encoder','imu_deg':-89.7})
    future.set_result(result)
    last = client.get('/api/manual-motion').json['command']
    assert last['id'] == first['command']['id']
    assert last['state'] == 'completed' and last['result'] == result
    controller.motion.drive.assert_called_once()


@pytest.mark.parametrize('key,value', [('enabled',False),('available',False),('encoder_enabled',False),('running',True)])
def test_hardware_and_busy_gates_cannot_be_bypassed_by_http(rig,key,value):
    client, controller, status, *_ = rig
    status[key] = value
    assert client.post('/api/manual-motion', json={'mode':'distance'}).status_code in (429,503)
    controller.motion.drive.assert_not_called()


@pytest.mark.parametrize('payload', [
    {'mode':'turn','ignore_obstacles':True}, {'mode':'distance','speed':101},
    {'mode':'distance','distance_cm':0}, {'mode':'turn','angle_deg':181},
    {'mode':'wiggle','cycles':1.5}, {'mode':'wiggle','angle_deg':True},
    {'mode':'raw'}, {'mode':[]}, {'mode':'turn','angle_deg':float('nan')},
    {'mode':'distance','require_encoders':False},
])
def test_invalid_commands_never_start_motors(rig,payload):
    client, controller, *_ = rig
    assert client.post('/api/manual-motion',json=payload).status_code == 400
    controller.motion.drive.assert_not_called()


@pytest.mark.parametrize('failure', ['range_unavailable','imu_heading_stale','picked_up','encoder_stall'])
def test_stop_reasons_reach_dashboard_and_do_not_auto_restart(rig,failure):
    client, controller, _, _, future = rig
    client.post('/api/manual-motion',json={'mode':'sine'})
    future.set_result(dict(ok=False,reason=failure))
    for _ in range(3):
        command = client.get('/api/manual-motion').json['command']
        assert command['state'] == 'stopped' and command['result']['reason'] == failure
    controller.motion.drive.assert_called_once()


def test_worker_exception_is_reported_as_failure(rig):
    client, _, _, _, future = rig
    client.post('/api/manual-motion',json={'mode':'turn'})
    future.set_exception(RuntimeError('IMU disconnected'))
    command = client.get('/api/manual-motion').json['command']
    assert command['state'] == 'stopped' and command['result']['error'] == 'IMU disconnected'


@pytest.mark.parametrize('mode',['distance','turn','arc','sine','wiggle','wiggle_forward','avoid_obstacle'])
def test_every_manual_mode_plans_encoder_targets(mode):
    manager = MotionManager()
    spec = validate_command({'mode':mode})
    targets = manager._targets_for_spec(spec)
    assert targets and all(any(pair) for pair in targets)
    if mode == 'wiggle_forward':
        # Continuous path returns to its initial heading with 20 cm forward extent.
        assert sum(right-left for left,right in targets) == 0
        travel = sum((left+right)/2 for left,right in targets)*manager.cm_per_count()
        assert travel == pytest.approx(manager._curve_for_spec(spec).length_cm,abs=.02)
        assert len(targets)==1
        assert manager._curve_for_spec(spec).points[-1].x==20


def test_manual_admission_retains_stop_cancellation(rig):
    client, controller, *_ = rig
    controller._action_token()[1].set()
    # The exact signal propagates to the motor launch; cancellation is enforced
    # by the existing orchestrator (covered in test_motion_orchestration).
    client.post('/api/manual-motion',json={'mode':'turn'})
    assert controller.motion.drive.call_args.kwargs['cancel_event'].is_set()


def test_raw_test_accepts_full_duty_two_seconds_and_reports_completion(rig):
    client, controller, status, _, future=rig
    status['encoder_enabled']=False
    controller.motion.raw_wheel_drive=Mock(return_value=future)
    response=client.post('/api/manual-motion/raw',json={'left':100,'right':-100,'duration':2})
    assert response.status_code==200 and response.json['command']['state']=='running'
    assert controller.motion.raw_wheel_drive.call_args.args==(100,-100,2)
    future.set_result({'ok':True,'mode':'raw','left':100,'right':-100,'duration_s':2})
    assert client.get('/api/manual-motion').json['command']['state']=='completed'
    controller.motion.drive.assert_not_called()


@pytest.mark.parametrize('payload',[{'left':101},{'duration':2.01},{'duration':float('nan')},{'right':True}])
def test_raw_test_rejects_out_of_bounds_without_starting(rig,payload):
    client, controller, *_=rig
    controller.motion.raw_wheel_drive=Mock()
    assert client.post('/api/manual-motion/raw',json=payload).status_code==400
    controller.motion.raw_wheel_drive.assert_not_called()


def test_handling_block_is_visible_and_explains_how_to_recover(rig):
    client, controller, *_=rig
    controller.motion.handling_interlock=lambda:True
    controller.dynamics=SimpleNamespace(status=lambda:{'pickup_detected':True,'tilt_deg':35})
    assert client.get('/api/manual-motion').json['handling']['pickup_detected']
    response=client.post('/api/manual-motion',json={'mode':'turn'})
    assert response.status_code==503 and 'upright pose' in response.json['error']
    controller.motion.drive.assert_not_called()


@pytest.mark.parametrize('mode',['sine','wiggle_forward'])
def test_explicit_wave_amplitude_and_continuous_dispatch(mode):
    spec=validate_command({'mode':mode,'distance_cm':40,'radius_cm':15,'amplitude_cm':1})
    path=MotionManager()._curve_for_spec(spec)
    assert max(p.y for p in path.points)==pytest.approx(2)
    assert path.points[-1].x==40
    with pytest.raises(ValueError,match='amplitude'):
        MotionManager()._curve_for_spec({**spec,'amplitude_cm':10})


def test_manual_range_policy_does_not_leak_into_navigation_or_apps():
    from robbi.orchestration import Intent,intent_scope
    from robot.config import settings
    manager=MotionManager()
    manager._distance_getter=lambda:None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings,'motion_obstacle_stop_enabled',True)
        with intent_scope(Intent('manual:dashboard',80)):
            assert not manager._should_stop_for_obstacle(100,100,False)
        for owner in ['navigation','app:drive','voice']:
            with intent_scope(Intent(owner,40)):
                assert manager._should_stop_for_obstacle(100,100,True)
        manager._distance_getter=lambda:1
        with intent_scope(Intent('manual:dashboard',80)):
            assert manager._should_stop_for_obstacle(100,100,False)
