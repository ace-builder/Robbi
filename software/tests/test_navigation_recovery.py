"""Sensor interruptions and empty optical returns, without physical movement."""
import math
import time
from types import SimpleNamespace

import pytest

from test_navigation_hardware import rig
from test_navigation import service,idle
from test_heading import sample
from robbi.navigation.model import Pose,Settings,Halt
from robbi.navigation.recovery import SensorWait,RecoveryMotion
from robbi.engine.motion import MotionManager
from robbi.orchestration import Intent,intent_scope


def test_fresh_empty_range_starts_short_step_without_inventing_map_evidence(rig):
    hardware,intent,clock,motor,sensor=rig;sensor.no_return=True
    end,rays=hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert end.x_cm==pytest.approx(10) and motor.command['distance_cm']==10
    assert not rays and hardware.controller.tof.status()['distance_cm'] is None
    assert hardware.last_motion_metrics['unknown_range'] and not motor.stopped


def test_empty_returns_mid_move_stop_at_checkpoint_instead_of_aborting(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now
    def check():sensor.no_return=clock.now-began>.5
    end,_=hardware.move({'mode':'distance','distance_cm':30},Pose(),intent,Settings(),check,lambda p:None)
    assert 10<end.x_cm<13 and motor.stopped
    assert hardware.last_motion_metrics['partial'] and hardware.last_motion_metrics['unknown_range']


def test_transient_stale_range_pauses_one_second_and_resumes_only_remainder(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now;commands=[]
    drive=hardware.controller.motion.drive
    def start(spec,**kwargs):
        motor.stopped=False;commands.append(dict(spec));return drive(spec,**kwargs)
    hardware.controller.motion.drive=start
    def check():sensor.stale=.5<clock.now-began<1.2
    end,_=hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),check,lambda p:None)
    assert len(commands)==2 and 18<commands[1]['distance_cm']<20
    assert end.x_cm==pytest.approx(20)
    assert hardware.last_motion_metrics['retries']==1


def test_stop_during_recovery_never_restarts_the_wheels(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now;commands=[]
    drive=hardware.controller.motion.drive
    def start(spec,**kwargs):commands.append(spec);return drive(spec,**kwargs)
    hardware.controller.motion.drive=start
    def check():
        sensor.stale=clock.now-began>.5
        if clock.now-began>.8:raise Halt('Stopped by user')
    with pytest.raises(Halt,match='Stopped by user'):
        hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),check,lambda p:None)
    assert len(commands)==1 and motor.stopped and clock.now-began<.85


def test_persistent_sensor_failure_has_finite_recovery_budget(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now;sensor.stale=True
    with pytest.raises(Halt,match='2 automatic retries'):
        hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),lambda:None,lambda p:None)
    assert motor.command is None and clock.now-began==pytest.approx(2)


def test_real_obstacle_after_no_echo_still_stops_without_retry(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now;sensor.no_return=True
    def check():
        if clock.now-began>.3:sensor.no_return=False;sensor.distance=5
    with pytest.raises(Halt,match='obstacle'):
        hardware.move({'mode':'distance','distance_cm':20},Pose(),intent,Settings(),check,lambda p:None)
    assert motor.stopped and hardware.last_motion_metrics['retries']==0


def test_navigation_unknown_policy_is_fresh_and_owner_scoped(rig):
    hardware,intent,clock,motor,sensor=rig;sensor.no_return=True
    manager=MotionManager();manager.set_obstacle_distance_getter(lambda:None)
    manager.set_navigation_distance_getter(hardware._motion_range_cm)
    manager.set_navigation_unknown_getter(hardware._fresh_unknown_range)
    from robbi.configuration.settings import settings
    original=settings.motion_obstacle_stop_enabled;settings.motion_obstacle_stop_enabled=True
    try:
        with intent_scope(intent):
            assert not manager._should_stop_for_obstacle(20,20,False)
            sensor.stale=True
            assert manager._should_stop_for_obstacle(20,20,False)
        sensor.stale=False
        with intent_scope(Intent('app:background',50)):
            assert manager._should_stop_for_obstacle(20,20,False)
    finally:settings.motion_obstacle_stop_enabled=original


def test_navigation_fuses_continuous_imu_despite_wheel_slip():
    manager=MotionManager();now=time.monotonic();current=sample(time=now)
    manager.set_heading_getter(lambda:dict(current))
    with intent_scope(Intent('navigation',80)):
        feedback,reason=manager._heading_feedback()
    assert feedback and feedback.recover
    stride=2/(2*feedback.degrees_per_count)
    for i in range(1,101):
        current.update(sample(angle=i,time=now+.02*i,rate=50))
        feedback.counts((-stride*i,stride*i),now+.02*i)
    assert 'imu_encoder_disagreement' in feedback.status()['warnings']
    assert feedback.status()['source']=='imu_encoder'
    assert abs(feedback.angle-100)<3


def test_retry_preserves_unwrapped_360_degree_remainder_and_scan_frame(rig):
    hardware,intent,clock,motor,sensor=rig;commands=[]
    def once(spec,pose,*args,**kwargs):
        commands.append(dict(spec))
        if len(commands)==1:
            fault=SensorWait('imu_heading_stale','temporary')
            fault.pose=pose.moved(0,100);fault.rotation=100
            raise fault
        return pose.moved(0,spec['angle_deg']),[]
    hardware._move_once=once
    end,_=hardware.move({'mode':'turn','angle_deg':360},Pose(),intent,Settings(),lambda:None,lambda p:None,collect=True)
    assert commands[1]['angle_deg']==260 and end.theta_deg==pytest.approx(0)


def test_exploration_no_echo_step_returns_for_another_scan(service):
    service.start_scan();assert idle(service)['localized']
    service.hardware.wait_for_range=lambda *args,**kwargs:{'distance_cm':None,'range_state':'no_return'}
    before=len(service.hardware.moves)
    service._launch('test_leg',lambda:service._follow_leg((50,0),100),physical=True)
    status=idle(service)
    assert not status['last_error']
    drives=[m for m in service.hardware.moves[before:] if m.get('mode')=='distance']
    assert len(drives)==1 and drives[0]['distance_cm']<=10
    assert 'Scanning again' in status['message']


def test_start_inside_extra_margin_can_leave_away_from_wall():
    from robbi.navigation.grid import Grid
    from robbi.navigation.planning import Planner
    grid=Grid(cells={(x,y):-8 for x in range(-20,21) for y in range(-20,21)})
    grid.cells.update({(-4,y):12 for y in range(-20,21)})
    before=dict(grid.cells)
    planner=Planner(grid,21,allow_unknown_margin=True,escape_radius=12)
    assert (0,0) not in planner.safe
    route=planner.route((0,0),(50,0))
    assert route and planner.escape and route[-1]==(50.,0.)
    assert planner.visible((0,0),(10,0))
    assert not planner.visible((0,0),(-10,0))
    assert grid.cells==before # No occupancy evidence was erased to make a route.


def test_start_escape_never_crosses_a_wall_or_undersized_passage():
    from robbi.navigation.grid import Grid
    from robbi.navigation.planning import Planner,StartBlocked
    grid=Grid(cells={(x,y):-8 for x in range(-15,16) for y in range(-15,16)})
    grid.cells.update({(-2,y):12 for y in range(-15,16)})
    planner=Planner(grid,21,allow_unknown_margin=True,escape_radius=12)
    with pytest.raises(StartBlocked):planner.route((0,0),(50,0))


def test_inflation_overlap_is_a_short_escape_step_then_rescan(service):
    from robbi.navigation.grid import Grid
    service.grid=Grid(cells={(x,y):-8 for x in range(-20,21) for y in range(-20,21)})
    service.grid.cells.update({(-4,y):12 for y in range(-20,21)})
    service.pose=Pose();service.localized=True
    service._launch('test_leg',lambda:service._follow_leg((50,0),100),physical=True)
    status=idle(service)
    assert not status['last_error']
    drives=[m for m in service.hardware.moves if m.get('mode')=='distance']
    assert len(drives)==1 and drives[0]['distance_cm']<=10


def test_inconsistent_start_map_gets_one_automatic_refresh(service):
    from robbi.navigation.grid import Grid
    service.grid=Grid(cells={(x,y):-8 for x in range(-20,21) for y in range(-20,21)})
    service.grid.cells[0,0]=12;service.pose=Pose();service.localized=True
    refresh=[]
    def scan():
        refresh.append(1);service.grid.cells[0,0]=-8;service.grid.revision+=1
    service._scan=scan
    service._launch('test_replan',lambda:service._plan((50,0)),physical=True)
    assert not idle(service)['last_error'] and refresh==[1]
