"""Whole directed trips recover without replay, scan loops or masked stops."""
import math
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace as NS

import pytest

from test_navigation import service,idle,Matcher
from test_navigation_transit import corridor
from test_navigation_hardware import rig
from robbi.navigation.model import Pose,Settings,Halt
from robbi.navigation.recovery import RouteWait,SensorWait
from robbi.navigation.route import RouteRunner
from robbi.navigation.planning import PlanningLimit,NoRoute,StartBlocked
from robbi.orchestration import Intent,intent_scope


def until(check):
    deadline=time.monotonic()+2
    while not check():
        assert time.monotonic()<deadline
        time.sleep(.005)


def test_twelve_meter_trip_has_only_midpoint_and_arrival_checks(service):
    corridor(service)
    service.hardware.wait_for_range=lambda *args,**kw:{'distance_cm':None}
    service.navigate(goal=(1200,0));status=idle(service)
    assert not status['last_error'],status
    assert [row.get('distance_cm') for row in service.hardware.moves]==[500.,100.,None,500.,100.,None]
    assert status['route_progress']['position_checks']==2
    assert math.dist(service._position(),(1200,0))<1


def test_failed_midpoint_match_continues_without_extra_scans(service):
    corridor(service);checks=[]
    def scan(**kwargs):
        checks.append(1);return False
    service._scan=scan
    service.navigate(goal=(300,0));status=idle(service)
    assert not status['last_error'] and len(checks)==2
    assert math.dist(service._position(),(300,0))<1
    assert 'inconclusive' in status['message']


@pytest.mark.parametrize('failure',[PlanningLimit,NoRoute,StartBlocked])
def test_map_planning_problem_uses_live_range_without_extra_scans(service,failure):
    corridor(service)
    def route(*args,**kw):raise failure('Map connection uncertain')
    service._planner=lambda:NS(radius=20,route=route)
    service.navigate(goal=(200,0));status=idle(service)
    assert not status['last_error'],status
    assert math.dist(service._position(),(200,0))<1
    assert status['route_progress']['position_checks']==1


def test_scan_rejected_pose_is_never_applied_or_inserted(service,monkeypatch):
    service.start_scan();idle(service);before=dict(service.grid.cells);pose=service.pose.to_dict()
    count=service.hardware.moves.count({'scan':True})
    monkeypatch.setattr(Matcher,'search',lambda *args,**kw:{'accepted':False,'pose':Pose(900,900,90),
        'score':.8,'margin':.01,'reason':'Ambiguous'})
    service._launch('check',lambda:service._scan(best_effort=True),physical=True)
    status=idle(service)
    assert not status['last_error'] and status['localized']
    assert service.grid.cells==before and service.pose.to_dict()==pose
    assert service.hardware.moves.count({'scan':True})==count+1
    assert service.match['mode']=='odometry_only'


def test_partial_stall_resumes_only_remaining_distance(service,monkeypatch):
    corridor(service);monkeypatch.setattr(RouteRunner,'RETRY_S',.001)
    original=service.hardware.move;commands=[]
    def move(spec,pose,intent,settings,check,publish,**kw):
        commands.append(dict(spec))
        if len(commands)==1:
            partial=pose.moved(50,0);publish(partial)
            fault=RouteWait('encoder_stall','Waiting before retrying');fault.pose=partial
            raise fault
        return original(spec,pose,intent,settings,check,publish,**kw)
    service.hardware.move=move
    service.navigate(goal=(200,0));status=idle(service)
    assert not status['last_error'],status
    assert [row['distance_cm'] for row in commands]==[200,150]
    assert service.travel==pytest.approx(200)
    assert status['route_progress']['warnings']==1


def test_obstacle_wait_is_active_and_resumes_automatically_when_clear(service,monkeypatch):
    corridor(service);monkeypatch.setattr(RouteRunner,'RETRY_S',.01)
    clear=threading.Event()
    service.hardware.wait_for_range=lambda *args,**kw:{'distance_cm':350 if clear.is_set() else 5}
    service.navigate(goal=(200,0))
    until(lambda:service.status()['phase']=='waiting')
    status=service.status()
    assert status['active'] and not status['last_error'] and not service.hardware.moves
    clear.set();status=idle(service)
    assert not status['last_error'] and math.dist(service._position(),(200,0))<1


def test_stop_during_recovery_cancels_route_without_automatic_restart(service,monkeypatch):
    corridor(service);monkeypatch.setattr(RouteRunner,'RETRY_S',.01);attempts=[]
    def unavailable(*args,**kw):attempts.append(1);raise RouteWait('motors_unavailable','Waiting for the driver')
    service.hardware.move=unavailable
    service.navigate(goal=(200,0));until(lambda:service.status()['phase']=='waiting')
    assert not service.status()['last_error']
    service.stop();status=idle(service);count=len(attempts);time.sleep(.03)
    assert status['last_error']=='Stopped by user' and len(attempts)==count
    assert not service.controller.orchestrator.snapshot()['resources']


def test_final_position_check_can_request_only_one_modest_correction(service):
    corridor(service);checks=[]
    def scan(**kw):checks.append(1);service.pose.x_cm+=300;return True
    service._scan=scan
    service.navigate(goal=(200,0));status=idle(service)
    assert not status['last_error'] and len(checks)==1
    assert status['route_progress']['correction_attempted']
    assert status['route_progress']['remaining_cm']==pytest.approx(200)
    assert '200 cm' in status['message']


def test_stop_inside_position_check_prevents_any_correction(service):
    corridor(service)
    def scan(**kw):service.pose.x_cm+=20;service.stop();raise RouteWait('cancelled','Stopped')
    service._scan=scan
    service.navigate(goal=(200,0));status=idle(service)
    assert status['last_error']=='Stopped by user'
    assert len(service.hardware.moves)==1


def test_directed_turn_keeps_actual_pose_after_a_large_accuracy_miss(rig):
    hardware,intent,clock,motor,sensor=rig;motor.reason='turn_accuracy_not_reached';motor.offset=-20
    end,_=hardware.move({'mode':'turn','angle_deg':90},Pose(),intent,Settings(),lambda:None,lambda p:None,transit=True)
    assert end.theta_deg==pytest.approx(70)
    assert hardware.last_motion_metrics['accuracy_reached'] is False


def test_hardware_pause_carries_partial_pose_for_remaining_distance(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now;motor.reason='encoder_stall'
    def check():
        if clock.now-began>.5:motor.stopped=True
    with pytest.raises(RouteWait) as caught:
        hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),check,lambda p:None,transit=True)
    assert caught.value.code=='encoder_stall' and 10<caught.value.pose.x_cm<15


def test_directed_hardware_can_use_encoders_while_imu_recovers(rig):
    hardware,intent,clock,motor,sensor=rig
    hardware.controller.motion.status=lambda:{'available':True,'encoder_enabled':True,'heading':{'available':False,'enabled':True}}
    with pytest.raises(SensorWait):hardware.ready()
    assert hardware.ready(allow_encoder_fallback=True)['distance_cm']==100


def test_motor_driver_start_failure_is_a_route_pause_and_releases_range_context(rig):
    hardware,intent,clock,motor,sensor=rig
    def failed(*args,**kw):raise RuntimeError('motor gpio shut down')
    hardware.controller.motion.drive=failed
    with pytest.raises(RouteWait,match='motor driver'):
        hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),lambda:None,lambda p:None,transit=True)
    assert hardware._range_context is None


def test_motor_worker_exception_keeps_route_recoverable(rig):
    hardware,intent,clock,motor,sensor=rig;drive=hardware.controller.motion.drive
    def failed(spec,**kw):
        drive(spec,**kw);future=Future();future.set_exception(OSError('driver unavailable'));return future
    hardware.controller.motion.drive=failed
    with pytest.raises(RouteWait) as caught:
        hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),lambda:None,lambda p:None,transit=True)
    assert caught.value.pose is not None and caught.value.code=='motion_unavailable'
    assert hardware._range_context is None


def test_missing_motor_future_is_a_stationary_route_pause(rig):
    hardware,intent,clock,motor,sensor=rig
    hardware.controller.motion.drive=lambda *args,**kw:None
    with pytest.raises(RouteWait):
        hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),lambda:None,lambda p:None,transit=True)
    assert hardware._range_context is None


def test_navigation_fallback_is_explicit_and_passes_native_turn_feedback_policy(monkeypatch):
    from robbi.engine import motion
    manager=motion.MotionManager();calls=[]
    monkeypatch.setattr(manager,'_heading_feedback',lambda *args:(None,'imu_heading_unavailable'))
    monkeypatch.setattr(motion,'run_turn',lambda *args,**kw:calls.append(kw) or {'ok':True})
    with intent_scope(Intent('navigation',80)):
        result=manager._run_target_blocking('turn',100,1,(-100,100),False,allow_encoder_fallback=True)
    assert result['ok'] and result['heading']['source']=='encoders'
    assert calls[0]['best_effort'] and calls[0]['feedback'] is None
