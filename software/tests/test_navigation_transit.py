"""Directed travel is continuous; scans verify arrival, not every probe."""
import math

import pytest

from test_navigation import service,idle
from test_navigation_hardware import rig
from robbi.navigation.grid import Grid
from robbi.navigation.model import Halt,Pose,Settings


def corridor(service):
    service.grid=Grid(cells={(x,y):-8 for x in range(-12,71) for y in range(-5,6)})
    service.grid.inferred_free.update(service.grid.cells)
    service.pose=Pose(sigma_cm=15);service.localized=True
    service.match={'mode':'odometry_only','accepted':False}
    def scan(**kwargs):
        service._check();service.hardware.moves.append({'scan':True})
    service._scan=scan


@pytest.mark.parametrize('empty',[False,True])
def test_two_meter_goal_is_one_measured_move_then_one_scan_despite_uncertainty(service,empty):
    corridor(service)
    if empty:service.hardware.wait_for_range=lambda *args,**kw:{'distance_cm':None,'range_state':'no_return'}
    service.navigate(goal=(200,0));status=idle(service)
    assert not status['last_error'],status
    assert service.hardware.moves==[{'mode':'distance','distance_cm':200.,'speed':100.},{'scan':True}]
    assert math.dist(service._position(),(200,0))<1


def test_arrival_scan_corrects_significant_drift_without_probing_along_the_way(service):
    corridor(service);captures=[]
    def scan(**kwargs):
        captures.append(1);service.hardware.moves.append({'scan':True})
        if len(captures)==1:service.pose.x_cm+=20
    service._scan=scan
    service.navigate(goal=(200,0));status=idle(service)
    assert not status['last_error'],status
    drives=[row['distance_cm'] for row in service.hardware.moves if row.get('mode')=='distance']
    assert drives==pytest.approx([200,20]) and len(captures)==1
    assert math.dist(service._position(),(200,0))<1


def test_arrival_check_is_not_repeated_after_one_correction(service):
    corridor(service);captures=[]
    def scan(**kwargs):
        captures.append(1);service.pose.x_cm+=20
    service._scan=scan
    service.navigate(goal=(200,0));status=idle(service)
    assert len(captures)==1 and not status['last_error']
    assert math.dist(service._position(),(200,0))<1
    assert not service.controller.orchestrator.snapshot()['resources']


def test_fresh_no_echo_never_splits_a_directed_two_meter_command(rig):
    hardware,intent,clock,motor,sensor=rig;sensor.no_return=True
    end,_=hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),lambda:None,lambda pose:None,transit=True)
    assert motor.command['distance_cm']==200 and end.x_cm==pytest.approx(200)
    assert not motor.stopped and not hardware.last_motion_metrics['partial']


def test_longer_trip_has_one_midpoint_check_and_one_arrival_check(service):
    corridor(service)
    service.navigate(goal=(300,0));status=idle(service)
    assert not status['last_error'],status
    assert [row.get('distance_cm') for row in service.hardware.moves]==[150.,None,150.,None]


def test_mid_leg_no_echo_does_not_force_a_stop(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now
    def check():sensor.no_return=clock.now-began>.3
    end,_=hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),check,lambda pose:None,transit=True)
    assert end.x_cm==pytest.approx(200) and not motor.stopped


def test_sensor_recovery_retains_continuous_mode_and_resumes_only_remaining_distance(rig):
    hardware,intent,clock,motor,sensor=rig;began=clock.now;commands=[]
    drive=hardware.controller.motion.drive
    def start(spec,**kw):
        motor.stopped=False;commands.append(dict(spec));return drive(spec,**kw)
    hardware.controller.motion.drive=start
    def check():
        sensor.stale=.3<clock.now-began<.8
        sensor.no_return=clock.now-began>.8
    end,_=hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),check,lambda pose:None,transit=True)
    assert len(commands)==2 and 180<commands[1]['distance_cm']<200
    assert end.x_cm==pytest.approx(200) and hardware.last_motion_metrics['retries']==1


@pytest.mark.parametrize('failure',['obstacle','stale','cancel'])
def test_continuous_travel_still_checks_live_sensor_and_stop(rig,failure):
    hardware,intent,clock,motor,sensor=rig;sensor.no_return=True;began=clock.now
    def check():
        if clock.now-began>.3:
            if failure=='obstacle':sensor.no_return=False;sensor.distance=5
            elif failure=='stale':sensor.stale=True
            else:raise Halt('Stopped by user')
    with pytest.raises(Halt):
        hardware.move({'mode':'distance','distance_cm':200},Pose(),intent,Settings(),check,lambda pose:None,transit=True)
    assert motor.stopped
