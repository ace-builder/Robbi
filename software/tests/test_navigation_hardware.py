"""Control telemetry and fresh range pairing, with no real clocks or motors."""
from concurrent.futures import Future
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robbi.navigation.hardware import Hardware,usable_scan_turn
from robbi.navigation.model import Halt,Pose,Ray,Settings,angle
from robbi.orchestration import Intent,current_intent,intent_scope
from robot.motion import MotionManager


@pytest.fixture
def rig(monkeypatch):
    clock=SimpleNamespace(now=100.)
    def advance(dt):clock.now+=dt
    monkeypatch.setattr('robbi.navigation.hardware.time',SimpleNamespace(monotonic=lambda:clock.now,sleep=advance))
    intent=Intent('navigation',80);intent.wait=lambda dt:advance(dt)
    motor=SimpleNamespace(start=clock.now,stopped=False,intent_id=None,command=None)
    sensor=SimpleNamespace(stale=False,distance=100.,no_return=False)
    class Completion:
        def done(self):return motor.stopped or clock.now-motor.start>=8
        def result(self):
            turn=motor.command.get('angle_deg',0)+getattr(motor,'offset',0)
            distance=motor.command.get('distance_cm',20)*min(1,(clock.now-motor.start)/8)
            return {'ok':not motor.stopped and not getattr(motor,'reason',''),
                    'reason':getattr(motor,'reason','') or ('stopped' if motor.stopped else ''),'control':'settled_turn','final':{'left':distance,'right':distance},
                    'heading':{'source':'imu_encoder','heading_deg':turn}}
    def drive(spec,**kwargs):
        motor.start=clock.now;motor.command=spec;motor.intent_id=current_intent().id
        assert spec['require_encoders'] and not spec.get('ignore_obstacles')
        assert kwargs['cancel_event'] is intent
        return Completion()
    def feedback():
        progress=min(1,(clock.now-motor.start)/8)
        return {'intent_id':motor.intent_id,'timestamp':clock.now,
                'distance_cm':progress*motor.command.get('distance_cm',20),'heading_deg':progress*(motor.command.get('angle_deg',0)+getattr(motor,'offset',0))}
    def stop(**kw):
        assert kw['owner'] is intent;motor.stopped=True
    motion=SimpleNamespace(status=lambda:{'available':True,'encoder_enabled':True,'heading':{'available':True,'enabled':True}},
                           drive=drive,navigation_feedback=feedback,stop=stop,cm_per_count=lambda:1)
    def range_status():
        return {'available':True,'fresh':not sensor.stale,'range_state':'no_return' if sensor.no_return else 'ready',
                'distance_cm':None if sensor.no_return else sensor.distance,
                'last_read_ts':clock.now-1 if sensor.stale else math.floor(clock.now*10)/10,'poll_hz':10,'timing_budget_ms':20}
    controller=SimpleNamespace(motion=motion,tof=SimpleNamespace(status=range_status),_can_run_slam_motion=lambda:(True,''))
    return Hardware(controller),intent,clock,motor,sensor


def test_continuous_scan_uses_measured_travel_and_fused_heading(rig):
    hardware,intent,clock,motor,sensor=rig
    published=[]
    end,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,published.append)
    assert len(rays)>=45
    assert math.hypot(end.x_cm,end.y_cm)<.1 # 20 cm wheel mean over a full turn closes an arc
    assert abs(end.theta_deg)<.1
    assert len(published)>100
    # Sensor origins vary with measured movement, rather than a fixed scan center.
    assert max(r.x_cm for r in rays)-min(r.x_cm for r in rays)>6


@pytest.mark.parametrize('failure',['stale','obstacle','cancel'])
def test_faults_stop_owned_motor_and_wait_for_completion(rig,failure):
    hardware,intent,clock,motor,sensor=rig
    def check():
        if clock.now-motor.start>.5:
            if failure=='stale':sensor.stale=True
            elif failure=='obstacle':sensor.distance=5
            elif failure=='no_return':sensor.no_return=True
            else:raise Halt('Stopped by user')
    with pytest.raises(Halt):hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),check,lambda p:None)
    assert motor.stopped and clock.now-motor.start<(3 if failure=='stale' else 1)


def test_no_return_is_unknown_not_free_and_stale_feedback_is_not_reused(rig):
    hardware,intent,clock,motor,sensor=rig
    sensor.no_return=True
    assert hardware.range_sample(allow_no_return=True)['distance_cm'] is None
    with pytest.raises(Halt):hardware.range_sample()
    sensor.stale=True
    with pytest.raises(Halt):hardware.range_sample(allow_no_return=True)


def test_missing_echo_and_stale_sensor_have_distinct_errors(rig):
    hardware,intent,clock,motor,sensor=rig
    sensor.no_return=True
    with pytest.raises(Halt,match='updating but has no usable distance'):hardware.range_sample()
    sensor.stale=True
    with pytest.raises(Halt,match='stale.*1000 ms old'):hardware.range_sample(allow_no_return=True)
    hardware.controller.tof.status=lambda:{'available':False,'last_error':'I2C bus error'}
    with pytest.raises(Halt,match='unavailable.*I2C bus error'):hardware.range_sample()


def test_mapped_rotation_allows_fresh_unknown_returns_but_never_stale_data(rig):
    hardware,intent,clock,motor,sensor=rig
    sensor.no_return=True
    end,rays=hardware.move({'mode':'turn','angle_deg':90},Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert end.theta_deg==pytest.approx(90) and not rays
    sensor.stale=True
    with pytest.raises(Halt,match='automatic retries'):
        hardware.move({'mode':'turn','angle_deg':90},end,intent,Settings(),lambda:None,lambda p:None)


def test_distance_waits_for_echo_with_motors_stopped_and_still_honors_stop(rig):
    hardware,intent,clock,motor,sensor=rig
    status=hardware.controller.tof.status;began=clock.now
    def intermittent():
        sensor.no_return=clock.now-began<.08
        if sensor.no_return:assert motor.command is None
        return status()
    hardware.controller.tof.status=intermittent
    sample=hardware.wait_for_range(intent,lambda:None)
    assert sample['distance_cm']==100 and motor.command is None
    assert clock.now>=began+.08


def test_missing_echo_wait_is_bounded_and_cancellable_before_start(rig):
    hardware,intent,clock,motor,sensor=rig
    sensor.no_return=True;began=clock.now
    with pytest.raises(Halt,match='no usable distance'):
        hardware.wait_for_range(intent,lambda:None)
    assert motor.command is None and clock.now-began<.62
    began=clock.now
    def cancel():
        if clock.now-began>.03:raise Halt('Stopped by user')
    with pytest.raises(Halt,match='Stopped by user'):hardware.wait_for_range(intent,cancel)
    assert motor.command is None and clock.now-began<.05


def test_wait_for_echo_never_waits_while_motors_are_running(rig):
    hardware,intent,clock,motor,sensor=rig
    hardware.controller.motion.status=lambda:{'running':True}
    with pytest.raises(Halt,match='Stop movement'):hardware.wait_for_range(intent,lambda:None)


def held_range_rig(rig):
    hardware,intent,clock,motor,sensor=rig
    sample={'available':True,'enabled':True,'range_state':'ready','distance_cm':100.,
            'last_read_ts':100.,'sample_time':100.,'sample_count':100,'timing_budget_ms':20}
    feedback={'intent_id':intent.id,'timestamp':100.,'distance_cm':0.,'heading_deg':0.}
    hardware.controller.tof.status=lambda:dict(sample)
    hardware.controller.motion.navigation_feedback=lambda:dict(feedback)
    hardware.controller.motion.navigation_samples=lambda after:[dict(feedback)] if feedback['timestamp']>after else []
    hardware._range_context={'intent_id':intent.id,'started':100.}
    hardware.range_sample(allow_recent=True)
    clock.now=100.06
    feedback.update(timestamp=clock.now,distance_cm=4.)
    sample.update(range_state='no_return',distance_cm=None,last_read_ts=100.05,sample_count=102)
    return hardware,intent,clock,sample,feedback


def test_few_missing_returns_use_short_lived_distance_with_travel_subtracted(rig):
    hardware,intent,clock,sample,feedback=held_range_rig(rig)
    measured=hardware.range_sample(allow_recent=True)
    assert measured['distance_cm']==pytest.approx(94) # 100 - 4 traveled - 2 uncertainty.
    assert measured['held_range'] and measured['skipped_returns']==2
    assert hardware.controller.tof.status()['distance_cm'] is None
    with pytest.raises(Halt,match='no usable distance'):hardware.range_sample()


@pytest.mark.parametrize('fault',['too_old','too_many','too_far','turned','wrong_owner','stale_odometry','disconnected','stale_sensor'])
def test_missing_return_tolerance_never_becomes_blind_driving(rig,fault):
    hardware,intent,clock,sample,feedback=held_range_rig(rig)
    if fault=='too_old':clock.now=100.16;sample['last_read_ts']=clock.now;feedback['timestamp']=clock.now
    elif fault=='too_many':sample['sample_count']=104
    elif fault=='too_far':feedback['distance_cm']=5.01
    elif fault=='turned':feedback['heading_deg']=3.01
    elif fault=='wrong_owner':feedback['intent_id']='different movement'
    elif fault=='stale_odometry':feedback['timestamp']=99.9
    elif fault=='disconnected':sample['available']=False
    elif fault=='stale_sensor':sample['range_state']='stale'
    with pytest.raises(Halt):hardware.range_sample(allow_recent=True)


def test_new_close_obstacle_cannot_be_hidden_by_previous_far_distance(rig):
    hardware,intent,clock,sample,feedback=held_range_rig(rig)
    sample.update(range_state='ready',distance_cm=5.,last_read_ts=clock.now,sample_time=clock.now,sample_count=103)
    assert hardware.range_sample(allow_recent=True)['distance_cm']==5
    sample.update(range_state='no_return',distance_cm=None,last_read_ts=100.07,sample_count=104)
    clock.now=100.08;feedback['timestamp']=clock.now
    # With no exposure-aligned odometry for the new close sample, old far
    # evidence must be discarded rather than reused.
    with pytest.raises(Halt):hardware.range_sample(allow_recent=True)


def test_motor_worker_uses_same_navigation_filter_without_changing_other_owners(monkeypatch):
    manager=MotionManager();manager.set_obstacle_distance_getter(lambda:None)
    manager.set_navigation_distance_getter(lambda:25.)
    with intent_scope(Intent('navigation',80)):
        assert manager._obstacle_distance_cm()==25
    with intent_scope(Intent('app:drive',80)):
        assert manager._obstacle_distance_cm() is None
    assert manager._obstacle_distance_cm() is None


@pytest.mark.parametrize('period,burst',[(.02,3),(.035,2)])
def test_distance_motion_skips_short_bursts_of_empty_sensor_samples(rig,period,burst):
    hardware,intent,clock,motor,sensor=rig
    began=clock.now;empty=set()
    def status():
        count=math.floor((clock.now-began+.000001)/period)
        missing=5<=count%18<5+burst
        if missing:empty.add(count)
        stamp=began+count*period
        return {'available':True,'enabled':True,'range_state':'no_return' if missing else 'ready',
                'distance_cm':None if missing else 100.,'last_read_ts':stamp,
                'sample_time':stamp-.01,'sample_count':count,'timing_budget_ms':20}
    def rows(after):
        if motor.command is None:return []
        return [{'intent_id':intent.id,'timestamp':began+i*.02,'distance_cm':i*.02*2.5,'heading_deg':0.}
                for i in range(max(0,math.floor((after-began)/.02)),math.floor((clock.now-began)/.02)+1)
                if began+i*.02>after]
    hardware.controller.tof.status=status
    hardware.controller.tof.samples_since=lambda seq:{'samples':[],'dropped':0}
    hardware.controller.motion.navigation_samples=rows
    end,rays=hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert empty and end.x_cm==pytest.approx(20)
    assert not motor.stopped and not rays
    assert hardware._range_context is None


def test_navigation_never_silently_falls_back_to_encoders(monkeypatch):
    import robot.motion as module
    manager=MotionManager()
    monkeypatch.setattr(manager,'_heading_feedback',lambda *args:(None,'imu_heading_unavailable'))
    motor=SimpleNamespace(stop=Mock());monkeypatch.setattr(module,'motor',motor)
    with intent_scope(Intent('navigation',80)):
        result=manager._run_target_blocking('turn',30,1,(-100,100),False)
    assert not result['ok'] and result['reason']=='imu_heading_unavailable'
    motor.stop.assert_called_once()


def test_scan_keeps_near_complete_rotation_and_actual_pose(rig):
    hardware,intent,clock,motor,sensor=rig
    motor.reason='turn_accuracy_not_reached';motor.offset=-3
    end,rays=hardware.move({'mode':'turn','angle_deg':360},Pose(),intent,Settings(),lambda:None,lambda p:None,collect=True)
    assert len(rays)>=30
    assert abs(end.theta_deg+3)<.01
    end,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert len(rays)>=30
    # Ordinary route turns still require their original precision contract.
    with pytest.raises(Halt,match='turn_accuracy_not_reached'):
        hardware.move({'mode':'turn','angle_deg':90},Pose(),intent,Settings(),lambda:None,lambda p:None)


@pytest.mark.parametrize('error,reason,allowed',[(10,'turn_accuracy_not_reached',True),
                                               (13,'turn_accuracy_not_reached',False),
                                               (3,'encoder_stall',False)])
def test_route_turn_can_correct_only_bounded_settled_accuracy_misses(rig,error,reason,allowed):
    hardware,intent,clock,motor,sensor=rig
    motor.reason=reason;motor.offset=-error
    if allowed:
        end,_=hardware.move({'mode':'turn','angle_deg':45},Pose(),intent,Settings(),lambda:None,lambda p:None,route_turn=True)
        assert end.theta_deg==pytest.approx(45-error)
    else:
        with pytest.raises(Halt):
            hardware.move({'mode':'turn','angle_deg':45},Pose(),intent,Settings(),lambda:None,lambda p:None,route_turn=True)


@pytest.mark.parametrize('actual,reason,source,accepted',[
    (357,'turn_accuracy_not_reached','imu_encoder',True),
    (365,'turn_accuracy_not_reached','imu_encoder',True),
    (0,'turn_accuracy_not_reached','imu_encoder',False),
    (320,'turn_accuracy_not_reached','imu_encoder',False),
    (-360,'turn_accuracy_not_reached','imu_encoder',False),
    (360,'encoder_stall','imu_encoder',False),
    (360,'stopped','imu_encoder',False),
    (360,'imu_heading_unavailable','encoders',False),
    (360,'turn_accuracy_not_reached','encoders',False),
    (float('nan'),'turn_accuracy_not_reached','imu_encoder',False),
])
def test_scan_relaxes_only_bounded_settled_accuracy(actual,reason,source,accepted):
    receipt={'control':'settled_turn','reason':reason,'heading':{'source':source,'heading_deg':actual}}
    assert usable_scan_turn({'mode':'turn','angle_deg':360},receipt) is accepted


def test_scan_drains_50hz_ranges_after_delayed_consumer_ticks(rig):
    hardware,intent,clock,motor,sensor=rig
    motion=hardware.controller.motion
    start=clock.now
    def row(i):
        return {'intent_id':motor.intent_id,'timestamp':start+i*.02,
                'distance_cm':0,'heading_deg':i*1.8}
    def count():return min(200,round((clock.now-start)/.02))
    motion.navigation_samples=lambda after:[row(i) for i in range(1,count()+1) if row(i)['timestamp']>after]
    class Done:
        def done(self):return clock.now-start>=4
        def result(self):return {'ok':True,'final':{'left':-20,'right':20},
                                'heading':{'source':'imu_encoder','heading_deg':360},'control_hz':50}
    def drive(spec,**kw):
        motor.intent_id=intent.id;motor.command=spec
        return Done()
    motion.drive=drive
    def sample(i):
        return {'available':True,'fresh':True,'distance_cm':100,'last_read_ts':start+i*.02,
                'sample_time':start+i*.02-.0125,'sample_count':i,'poll_hz':200,'timing_budget_ms':20,
                'measured_hz':50}
    hardware.controller.tof.status=lambda:sample(count())
    hardware.controller.tof.samples_since=lambda seq:{'samples':[sample(i) for i in range(seq+1,count()+1)],'dropped':0}
    # Simulate 80ms scheduling stalls: four sensor/odometry samples per tick.
    intent.wait=lambda dt:setattr(clock,'now',round(clock.now+.08,8))
    end,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert len(rays)==200
    assert end.theta_deg==pytest.approx(0)
    assert rays[0].angle_deg==pytest.approx(.675)
    assert hardware.last_scan_metrics['largest_gap_deg']==pytest.approx(1.8)
    assert hardware.last_scan_metrics['buffer_dropped']==0
    assert hardware.last_scan_metrics['unmatched_timestamps']==0
    assert motor.command['turn_rate_deg_s']==90


@pytest.mark.parametrize('hz,valid,expected',[(50,100,90),(20,100,80),(20,50,40),(2,100,10),(0,100,90)])
def test_scan_rate_adapts_to_usable_sensor_rate_without_exceeding_ceiling(rig,hz,valid,expected):
    hardware,intent,clock,motor,sensor=rig
    status=hardware.controller.tof.status
    hardware.controller.tof.status=lambda:{**status(),'measured_hz':hz,'valid_return_percent':valid}
    hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert motor.command['turn_rate_deg_s']==expected
    assert hardware.last_scan_metrics['target_rate_deg_s']==expected


@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),True,-1])
def test_nonfinite_or_invalid_sensor_returns_never_reach_mapping(rig,invalid):
    hardware,intent,clock,motor,sensor=rig
    sensor.distance=invalid
    with pytest.raises(Halt):hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert motor.command is None


def test_sensor_driver_stale_state_overrides_recent_timestamp(rig):
    hardware,intent,clock,motor,sensor=rig
    status=hardware.controller.tof.status
    hardware.controller.tof.status=lambda:{**status(),'range_state':'stale'}
    with pytest.raises(Halt):hardware.range_sample()


def test_obstacle_present_before_motion_never_starts_motors(rig):
    hardware,intent,clock,motor,sensor=rig
    sensor.distance=5
    with pytest.raises(Halt,match='obstacle is too close'):
        hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert motor.command is None


@pytest.mark.parametrize('rate,target,expected',[(10,1800,57),(5,3600,90)])
def test_slow_scan_timeout_accounts_for_requested_rate_and_stays_bounded(monkeypatch,rate,target,expected):
    import robot.motion as module
    from robot.config import settings
    manager=MotionManager()
    heading=SimpleNamespace(degrees_per_count=.1,status=lambda target:{'source':'imu_encoder'})
    monkeypatch.setattr(manager,'_heading_feedback',lambda *args:(heading,''))
    monkeypatch.setattr(manager,'cm_per_count',lambda:math.radians(.1)*settings.motion_wheel_base_cm)
    monkeypatch.setattr(settings,'motion_encoder_timeout_s',8)
    monkeypatch.setattr(settings,'motion_encoder_counts_per_rev',1000)
    runner=Mock(return_value={'ok':True})
    monkeypatch.setattr(module,'run_turn',runner)
    with intent_scope(Intent('navigation',80)):
        manager._run_target_blocking('turn',40,1,(-target,target),False,turn_rate_deg_s=rate)
    assert runner.call_args.kwargs['timeout']==pytest.approx(expected)
    assert runner.call_args.kwargs['stall_timeout']==settings.motion_stall_timeout_s


def sparse_scan(rig,monkeypatch,*,error=0,count=None):
    hardware,intent,clock,motor,sensor=rig
    calls=[]
    def move(spec,pose,intent,settings,check,publish,**kwargs):
        check();calls.append((spec,kwargs))
        if kwargs.get('collect'):
            # A healthy full rotation with one 30-degree sampling gap.
            rays=[Ray(0,0,a,100) for a in range(0,360,10) if a not in (40,50)]
            if count is not None:rays=rays[:count]
            hardware.last_scan_metrics={'raw_points':len(rays),'elapsed_s':2}
            return pose.moved(0,360),rays
        measured=pose.moved(2,spec['angle_deg']+error)
        publish(measured)
        return measured,[]
    monkeypatch.setattr(hardware,'move',move)
    return calls


@pytest.mark.parametrize('limit',[0,1,8])
def test_partial_scan_skips_empty_measurements_without_demanding_full_coverage(rig,monkeypatch,limit):
    hardware,intent,clock,motor,sensor=rig
    calls=sparse_scan(rig,monkeypatch);sensor.no_return=True
    end,rays=hardware.scan(Pose(),intent,Settings(scan_max_fill_turns=limit),lambda:None,lambda p:None)
    assert len(calls)==1 and len(rays)==34
    assert hardware.last_scan_metrics['phase']=='partial'
    assert hardware.last_scan_metrics['partial_coverage']
    assert hardware.last_scan_metrics['fill_turns']==0
    assert all(ray.angle_deg not in (40,50) for ray in rays)


def test_bootstrap_can_combine_an_individually_sparse_sweep_without_repair_turns(rig,monkeypatch):
    hardware,intent,clock,motor,sensor=rig
    calls=sparse_scan(rig,monkeypatch,count=23)
    _,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None,allow_sparse=True)
    assert len(rays)==23 and len(calls)==1


def test_heading_diagnostics_report_cached_imu_freshness(rig):
    hardware,intent,clock,motor,sensor=rig
    sample={'ready':True,'sample_time':clock.now}
    hardware.controller.imu=SimpleNamespace(heading_sample=lambda:sample)
    assert hardware.heading_status()['fresh']
    assert hardware.heading_status()['source']=='imu_encoder'
    clock.now+=.2
    assert not hardware.heading_status()['fresh']


def test_insufficient_scan_repairs_only_until_partial_geometry_is_usable(rig,monkeypatch):
    hardware,intent,clock,motor,sensor=rig
    calls=sparse_scan(rig,monkeypatch,error=3,count=23)
    start=Pose(100,50,70)
    end,rays=hardware.scan(start,intent,Settings(sensor_yaw_deg=10),lambda:None,lambda p:None)
    assert len(calls)==2 # One sweep, one measured repair, not another full sweep.
    assert calls[0][0]['angle_deg']==360 and calls[1][0]['angle_deg']==pytest.approx(-70)
    assert all(spec['speed']==100 for spec,_ in calls)
    assert calls[1][1]['scan_turn']
    assert rays[-1].angle_deg==pytest.approx(-57) # Actual heading, not the ideal target.
    assert angle(end.theta_deg-start.theta_deg)==pytest.approx(-67)
    assert hardware.last_scan_metrics['initial_largest_gap_deg']==120
    assert hardware.last_scan_metrics['largest_gap_deg']==63
    assert hardware.last_scan_metrics['fill_turns']==1
    assert hardware.last_scan_metrics['phase']=='partial'
    # Stationary rays also use measured translation and the sensor offset.
    expected_x=math.cos(math.radians(-start.theta_deg))*(end.x_cm-start.x_cm+4*math.cos(math.radians(end.theta_deg)))-math.sin(math.radians(-start.theta_deg))*(end.y_cm-start.y_cm+4*math.sin(math.radians(end.theta_deg)))
    assert rays[-1].x_cm==pytest.approx(expected_x)


def test_recovery_recomputes_actual_gaps_instead_of_using_a_stale_target_list(rig,monkeypatch):
    hardware,intent,clock,motor,sensor=rig
    calls=sparse_scan(rig,monkeypatch,error=9,count=22)
    end,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert len(calls)>=3
    assert len(rays)==24
    assert hardware.last_scan_metrics['largest_gap_deg']<130
    assert hardware.last_scan_metrics['phase']=='partial'


@pytest.mark.parametrize('limit',[0,1,3])
def test_unobservable_sector_remains_unknown_and_recovery_is_bounded(rig,monkeypatch,limit):
    hardware,intent,clock,motor,sensor=rig
    calls=sparse_scan(rig,monkeypatch,count=23)
    sensor.no_return=True
    with pytest.raises(Halt,match='usable bearings'):
        hardware.scan(Pose(),intent,Settings(scan_max_fill_turns=limit),lambda:None,lambda p:None)
    assert len(calls)<=limit+1
    assert hardware.last_scan_metrics['points']==23
    assert hardware.last_scan_metrics['largest_gap_deg']==120
    assert hardware.last_scan_metrics['fill_turns']==limit
    assert hardware.last_scan_metrics['phase']=='insufficient'
    assert clock.now<103 # No indefinite retry on missing returns.


def test_stationary_recovery_rejects_new_reads_from_old_exposures(rig,monkeypatch):
    hardware,intent,clock,motor,sensor=rig
    sparse_scan(rig,monkeypatch,count=23)
    began=clock.now;status=hardware.controller.tof.status
    def sample():
        value=status();value['sample_time']=began-.01 if clock.now<began+.2 else clock.now-.03
        value['sample_count']=round((clock.now-began)*100)
        value['timing_uncertainty_ms']=5
        return value
    hardware.controller.tof.status=sample
    _,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert len(rays)==24
    assert clock.now>=began+.2
    assert hardware.last_scan_metrics['recovery_received_samples']>=20


def test_stop_interrupts_waiting_for_a_stationary_range(rig,monkeypatch):
    hardware,intent,clock,motor,sensor=rig
    calls=sparse_scan(rig,monkeypatch,count=23);sensor.no_return=True
    began=clock.now
    def check():
        if clock.now-began>.12:raise Halt('Stopped by user')
    with pytest.raises(Halt,match='Stopped by user'):
        hardware.scan(Pose(),intent,Settings(),check,lambda p:None)
    assert len(calls)==2
    assert clock.now-began<.15
