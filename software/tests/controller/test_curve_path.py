from concurrent.futures import Future
import logging
import math
import threading
from unittest.mock import Mock

import pytest

from robot import motion as motion_module
from robot.config import settings
from robot.curve_path import CurveFollower, CurvePath
from robot.motion import MotionManager


CM_PER_COUNT = math.pi * 6.2 / 2800


def test_arc_positions_and_wheel_ratio_agree_throughout_path():
    path = CurvePath.arc(20, 90, 9.8)
    assert path.targets(CM_PER_COUNT) == (3410, 5623)
    for fraction in (0.1, 0.25, 0.5, 0.75, 1):
        left, right = path.wheel_cm(path.length_cm * fraction)
        assert left / right == pytest.approx(15.1 / 24.9)
        assert (right - left) / 9.8 == pytest.approx(math.pi / 2 * fraction)
    end = path.at(path.length_cm)
    assert (end.x, end.y) == pytest.approx((20, 20))
    assert CurvePath.arc(20, -90, 9.8).targets(CM_PER_COUNT) == (5623, 3410)


def test_sine_is_continuous_with_zero_end_heading_and_bounded_curvature():
    path = CurvePath.sine(15, 45, 1, 9.8)
    assert len(path.points) == 129
    assert path.points[0].heading == 0
    assert path.points[-1].heading == pytest.approx(0, abs=1e-10)
    assert path.points[-1].y == pytest.approx(0, abs=1e-10)
    assert path.points[-1].x == pytest.approx(4 * 15 * math.sin(math.pi / 4))
    assert max(p.y for p in path.points) > 5
    assert max(abs(p.curvature) for p in path.points) <= 1 / 15 + 1e-9
    assert any(p.heading > 0.2 for p in path.points)
    assert any(p.heading < -0.2 for p in path.points)
    assert max(abs(b.curvature - a.curvature) for a, b in zip(path.points, path.points[1:])) < 0.006
    left, right = path.targets(CM_PER_COUNT)
    assert left == right


def test_sine_respects_explicit_length_amplitude_cycles_and_zero():
    path = CurvePath.sine(10, 45, 2, 9.8, distance_cm=80, amplitude_cm=1)
    assert path.points[-1].x == 80
    assert max(p.y for p in path.points) == pytest.approx(2)
    assert path.targets(CM_PER_COUNT)[0] == path.targets(CM_PER_COUNT)[1]
    assert CurvePath.sine(15, 45, 1, 9.8, distance_cm=0).targets(CM_PER_COUNT) == (0, 0)
    with pytest.raises(ValueError, match="amplitude"):
        CurvePath.sine(15, 45, 1, 9.8, distance_cm=10, amplitude_cm=10)
    with pytest.raises(ValueError):
        CurvePath.arc(20, float("nan"), 9.8)


def test_curve_feedforward_ratio_exists_before_any_encoder_motion():
    path = CurvePath.arc(20, 90, 9.8)
    follower = CurveFollower(path, CM_PER_COUNT)
    left, right = follower.command((0, 0), 70, 30, 70, 0.125, 0, 0, 300, 0.02, 200)
    assert 0 < left < right <= 4
    left, right = follower.command((0, 0), 70, 30, 70, .125, 0, 0, 300, .02, 10000)
    # Under the wheel model's rolling deadband, initial speeds should already
    # follow the requested arc, without waiting for heading error to build.
    assert (left-20)/(right-20) == pytest.approx(15.1/24.9, abs=.02)


class CurveWheels:
    """Simple asymmetric friction model; not a physical validation of Robbi."""
    def __init__(self, clock, cm_per_count, track_cm, right_gain=1.0, dead_right=False, reverse_right=False, delay_s=0):
        self.clock = clock
        self.cm_per_count = cm_per_count
        self.track_cm = track_cm
        self.right_gain = right_gain
        self.dead_right = dead_right
        self.reverse_right = reverse_right
        self.delay_s = delay_s
        self.position = [0.0, 0.0]
        self.duty = (0, 0)
        self.history = []
        self.resets = 0
        self.brakes = 0
        self.brake_flags = []
        self.x = self.y = self.heading = 0.0

    def reset_counts(self):
        self.resets += 1
        self.position = [0.0, 0.0]

    def counts(self):
        if self.delay_s:
            delayed = [row for row in self.history if row[0] <= self.clock[0] - self.delay_s]
            return tuple(round(v) for v in delayed[-1][1:3]) if delayed else (0, 0)
        return tuple(round(p) for p in self.position)

    def set_wheel_pwm_with_brake(self, left, right, **brakes):
        self.brake_flags.append((brakes['left_brake'],brakes['right_brake']))
        self.duty = (0 if brakes["left_brake"] else left, 0 if brakes["right_brake"] else right)

    def stop(self):
        self.duty = (0, 0)

    def brake(self, seconds):
        self.brakes += 1
        self.stop()

    def wait(self, timeout):
        delta = [math.copysign(max(0, abs(duty) - 20) * 65 * timeout, duty) for duty in self.duty]
        delta[1] *= 0 if self.dead_right else self.right_gain * (-1 if self.reverse_right else 1)
        self.position = [position + step for position, step in zip(self.position, delta)]
        left_cm, right_cm = [d * self.cm_per_count for d in delta]
        angle = (right_cm - left_cm) / self.track_cm
        distance = (left_cm + right_cm) / 2
        self.x += distance * math.cos(self.heading + angle / 2)
        self.y += distance * math.sin(self.heading + angle / 2)
        self.heading += angle
        self.clock[0] += timeout
        self.history.append((self.clock[0], *self.position, self.x, self.y, self.heading, *self.duty))


@pytest.fixture
def curve_run(monkeypatch):
    tuning = dict(wheel_diameter_cm=6.2, wheel_base_cm=9.8, encoder_counts_per_rev=2800,
                  pwm_max_duty=70.0, pwm_min_duty=30.0, pid_kp=0.125, pid_ki=0.0, pid_kd=0.0,
                  pid_integral_limit=300.0, curve_pwm_slew_per_s=200.0, encoder_enabled=True,
                  encoder_tolerance_counts=40, control_interval_s=0.02, encoder_timeout_s=8,
                  stall_timeout_s=0.75, stop_margin_per_duty=0.5, stop_margin_ratio=0.1,
                  obstacle_stop_enabled=True, obstacle_stop_cm=5, brake_s=0.08)
    for name, value in tuning.items():
        monkeypatch.setattr(settings, "motion_" + name, value)

    def run(path, *, gain=1.0, dead_right=False, reverse_right=False, obstacle_at=None, stop_at=None, delay_s=0, speed=70, plant=CurveWheels, imu=False):
        clock = [0.0]
        wheels = plant(clock, CM_PER_COUNT, 9.8, gain, dead_right, reverse_right, delay_s)
        monkeypatch.setattr(motion_module, "motor", wheels)
        monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
        manager = MotionManager.__new__(MotionManager)
        manager._enabled = True
        manager._log = logging.getLogger(__name__)
        manager._distance_getter = lambda: 3 if obstacle_at is not None and clock[0] >= obstacle_at else 100
        manager._stop_event = Mock()
        manager._stop_event.is_set.side_effect = lambda: stop_at is not None and clock[0] >= stop_at
        manager._stop_event.wait.side_effect = wheels.wait
        manager._lock = threading.Lock()
        manager._motion_thread = None
        manager._last_result = {}
        if imu:
            monkeypatch.setattr(settings, 'motion_imu_heading_enabled', True)
            manager._heading_getter = lambda: {'ready':True,'epoch':1,'sample_time':clock[0],
                'angle_deg':math.degrees(wheels.heading),'rate_deg_s':0}
        future = Future()
        manager._run_target_sequence("curve", speed, 0.7, [path.targets(CM_PER_COUNT)], False, future, path)
        future.result()
        return manager._last_result, wheels
    return run


@pytest.mark.parametrize('path', [CurvePath.arc(50,-20,9.8),CurvePath.sine(20,2,1,9.8)])
def test_filming_curves_ignore_close_tof_without_changing_other_driving(curve_run,path):
    from robbi.orchestration import Intent,intent_scope
    with intent_scope(Intent('filming-shot',90)):
        result,wheels=curve_run(path,obstacle_at=0)
    assert result['ok'] and len(wheels.history)>5
    result,_=curve_run(path,obstacle_at=0)
    assert not result['ok'] and result['reason']=='obstacle'


@pytest.mark.parametrize("angle", [-90, 90])
@pytest.mark.parametrize("gain", [0.8, 1.0, 1.2])
def test_arc_turns_throughout_motion_not_only_at_endpoint(curve_run, angle, gain):
    path = CurvePath.arc(20, angle, 9.8)
    result, wheels = curve_run(path, gain=gain)
    assert result["ok"], result
    for fraction in (0.25, 0.5, 0.75):
        sample = min(wheels.history, key=lambda row: abs((row[1] + row[2]) * CM_PER_COUNT / 2 - path.length_cm * fraction))
        assert math.degrees(sample[5]) == pytest.approx(angle * fraction, abs=8)
    assert math.degrees(wheels.heading) == pytest.approx(angle, abs=5)
    assert wheels.x == pytest.approx(20, abs=2)
    assert wheels.y == pytest.approx(math.copysign(20, angle), abs=2)
    assert wheels.duty == (0, 0)
    assert all(0 <= duty <= 70 for row in wheels.history for duty in row[-2:])


@pytest.mark.parametrize("gain", [0.8, 1.0, 1.2])
def test_sine_tracks_a_wave_without_intermediate_brakes_or_count_resets(curve_run, gain):
    path = CurvePath.sine(15, 45, 1, 9.8)
    result, wheels = curve_run(path, gain=gain)
    assert result["ok"], result
    assert len(result["segments"]) == 1
    assert result["control"] == "curve_tracking"
    assert wheels.resets == 1
    assert wheels.brakes == 2  # Endpoint and final cleanup only.
    assert max(row[4] for row in wheels.history) > 4.5
    assert wheels.y == pytest.approx(0, abs=2)
    assert wheels.x == pytest.approx(path.points[-1].x, abs=2)
    assert math.degrees(wheels.heading) == pytest.approx(0, abs=4)
    for row in wheels.history:
        progress = (row[1] + row[2]) * CM_PER_COUNT / 2
        desired = path.at(progress)
        assert abs(row[4] - desired.y) < 2


@pytest.mark.parametrize('gain', [0.8, 1.0, 1.2])
def test_authored_reverse_curve_tracks_body_heading_and_measured_odometry(curve_run, gain):
    arc = CurvePath.arc(20, 90, 9.8)
    path = CurvePath.authored([[p.s, -p.x, -p.y, p.heading, p.curvature] for p in arc.points], -1, 9.8)
    result, wheels = curve_run(path, gain=gain)
    assert result['ok'], result
    assert wheels.x == pytest.approx(-20, abs=2)
    assert wheels.y == pytest.approx(-20, abs=2)
    assert math.degrees(wheels.heading) == pytest.approx(90, abs=5)
    assert result['odometry']['x_cm'] == pytest.approx(wheels.x, abs=.03)
    assert result['odometry']['y_cm'] == pytest.approx(wheels.y, abs=.03)
    assert result['odometry']['heading_rad'] == pytest.approx(wheels.heading, abs=.002)
    assert all(-70 <= duty <= 0 for row in wheels.history for duty in row[-2:])
    assert wheels.resets == 1 and wheels.duty == (0, 0)


@pytest.mark.parametrize('options,reason', [({'dead_right': True}, 'encoder_stall'),
    ({'reverse_right': True}, 'encoder_direction_mismatch'), ({'stop_at': .4}, 'stopped')])
def test_authored_reverse_curve_still_observes_stop_and_encoder_guards(curve_run, options, reason):
    arc = CurvePath.arc(20, 90, 9.8)
    path = CurvePath.authored([[p.s, -p.x, -p.y, p.heading, p.curvature] for p in arc.points], -1, 9.8)
    result, wheels = curve_run(path, **options)
    assert not result['ok'] and result['reason'] == reason
    assert wheels.duty == (0, 0)


@pytest.mark.parametrize("options,reason", [
    ({"dead_right": True}, "encoder_stall"),
    ({"reverse_right": True}, "encoder_direction_mismatch"),
    ({"obstacle_at": 0.4}, "obstacle"),
    ({"stop_at": 0.4}, "stopped"),
    ({"stop_at": 0}, "stopped"),
])
def test_curve_retains_safety_stops(curve_run, options, reason):
    result, wheels = curve_run(CurvePath.arc(20, 90, 9.8), **options)
    assert not result["ok"]
    assert result["reason"] == reason
    assert wheels.duty == (0, 0)
    assert wheels.clock[0] < 0.8


@pytest.mark.parametrize('filming',[False,True])
def test_drive_routes_sine_to_single_continuous_curve(monkeypatch,filming):
    from robbi.orchestration import Intent,intent_scope
    manager = MotionManager.__new__(MotionManager)
    manager._enabled = True
    manager._stop_event = threading.Event()
    manager._lock = threading.Lock()
    manager._start_lock = threading.Lock()
    manager._stop_generation = 0
    manager._request_stop = Mock(side_effect=lambda: setattr(manager, "_stop_generation", manager._stop_generation+1))
    monkeypatch.setattr(motion_module, "motor", Mock())
    called = []
    monkeypatch.setattr(manager, '_launch', lambda target,args,*rest:called.append((target,args)))
    monkeypatch.setattr(settings, "motion_encoder_enabled", True)
    with intent_scope(Intent('filming-shot' if filming else 'manual:drive')):
        manager.drive({"mode": "sine", "cycles": 1})
    target, args = called[0]
    assert target == manager._run_target_sequence
    assert len(args[3]) == 1
    assert isinstance(args[-2], CurvePath)
    assert args[-1] is filming  # Filming includes gestures; regular driving remains strict.


def test_curves_refuse_to_run_without_encoder_feedback(monkeypatch):
    manager = MotionManager.__new__(MotionManager)
    manager._enabled = True
    manager._request_stop = Mock()
    manager._lock = threading.Lock()
    manager._log = logging.getLogger(__name__)
    monkeypatch.setattr(settings, "motion_encoder_enabled", False)
    future = manager.drive({"mode": "arc", "radius_cm": 20, "angle_deg": 90})
    assert future.done()
    assert manager._last_result["reason"] == "encoders_required"
    manager._request_stop.assert_called_once()


@pytest.mark.parametrize("radius", [5, 6, 10])
def test_tight_arcs_remain_forward_and_stop_at_target(curve_run, radius):
    path = CurvePath.arc(radius, 90, 9.8)
    result, wheels = curve_run(path)
    assert result["ok"], result
    assert math.degrees(wheels.heading) == pytest.approx(90, abs=5)
    assert all(duty >= 0 for row in wheels.history for duty in row[-2:])


@pytest.mark.parametrize("delay", [0.04, 0.08])
@pytest.mark.parametrize("kind", ["arc", "sine"])
def test_batched_encoder_feedback_still_tracks_curve(curve_run, delay, kind):
    path = CurvePath.arc(20, 90, 9.8) if kind == "arc" else CurvePath.sine(15, 45, 1, 9.8)
    result, wheels = curve_run(path, gain=0.9, delay_s=delay)
    assert result["ok"], result
    assert wheels.x == pytest.approx(path.points[-1].x, abs=3)
    assert wheels.y == pytest.approx(path.points[-1].y, abs=3)
    assert math.degrees(wheels.heading) == pytest.approx(math.degrees(path.points[-1].heading), abs=8)
    assert wheels.duty == (0, 0)


@pytest.mark.parametrize('angle',[-20,20])
@pytest.mark.parametrize('gain',[.8,1,1.2])
def test_filming_shallow_arc_steers_during_travel_without_final_pivot(curve_run,angle,gain):
    path=CurvePath.arc(50,angle,9.8)
    result,wheels=curve_run(path,gain=gain,speed=40)
    assert result['ok'], result
    for fraction in (.25,.5,.75):
        sample=min(wheels.history,key=lambda r:abs((r[1]+r[2])*CM_PER_COUNT/2-path.length_cm*fraction))
        assert math.degrees(sample[5]) == pytest.approx(angle*fraction,abs=2)
    assert wheels.y == pytest.approx(path.points[-1].y,abs=.6)
    assert all(left==right for left,right in wheels.brake_flags)
    assert result['path_tracking']['stop']=='coupled'
    assert len(result['path_tracking']['checkpoints'])==3


@pytest.mark.parametrize('kind',['arc','sine','reverse'])
def test_all_continuous_paths_stop_wheels_together(curve_run,kind):
    path=CurvePath.arc(20,90,9.8) if kind!='sine' else CurvePath.sine(15,45,1,9.8)
    if kind=='reverse':
        path=CurvePath.authored([[p.s,-p.x,-p.y,p.heading,p.curvature] for p in path.points],-1,9.8)
    result,wheels=curve_run(path,gain=.8,speed=40)
    assert result['ok'],result
    assert all(left==right for left,right in wheels.brake_flags)

class LoadedWheels(CurveWheels):
    """Inertia, unequal breakaway/rolling friction, and 6ms of work per tick."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.velocity=[0.,0.]
    def set_wheel_pwm_with_brake(self,left,right,**brakes):
        self.wait(.006)
        super().set_wheel_pwm_with_brake(left,right,**brakes)
    def wait(self,timeout):
        steps=max(1,math.ceil(timeout/.002));dt=timeout/steps
        for _ in range(steps):
            delta=[]
            for i,(duty,rolling,starting,gain) in enumerate(zip(self.duty,(18,26),(34,38),(65,52))):
                target=max(0,abs(duty)-rolling)*gain*getattr(self,'motor_gain',1.)*(self.right_gain if i else 1.)
                if abs(self.velocity[i])<1 and abs(duty)<starting:target=0
                target=math.copysign(target,duty)
                self.velocity[i]+=(target-self.velocity[i])*(1-math.exp(-dt/.06))
                delta.append(self.velocity[i]*dt)
            self.position=[p+d for p,d in zip(self.position,delta)]
            left,right=[d*self.cm_per_count for d in delta]
            right*=getattr(self,'traction_right',1.)
            turn=(right-left)/self.track_cm
            distance=(left+right)/2
            self.x+=distance*math.cos(self.heading+turn/2)
            self.y+=distance*math.sin(self.heading+turn/2)
            self.heading+=turn
            self.clock[0]+=dt
        self.history.append((self.clock[0],*self.position,self.x,self.y,self.heading,*self.duty))
    def brake(self,seconds):
        super().brake(seconds)
        self.velocity=[0.,0.]


@pytest.mark.parametrize('path',[CurvePath.arc(20,90,9.8),CurvePath.arc(20,-90,9.8),CurvePath.sine(15,45,1,9.8)])
@pytest.mark.parametrize('speed',[40,50])
@pytest.mark.parametrize('interval',[.01,.02])
def test_loaded_curves_track_during_travel_at_selected_rate(curve_run,path,speed,interval,monkeypatch):
    monkeypatch.setattr(settings,'motion_control_interval_s',interval)
    result,wheels=curve_run(path,plant=LoadedWheels,imu=True,speed=speed,delay_s=.04)
    assert result['ok'],result
    assert result['heading']['source']=='imu_encoder'
    assert result['control_hz']==pytest.approx(1/interval,abs=.2)
    assert result['max_control_gap_ms']<=interval*1000+.1
    assert all(not left and not right for left,right in wheels.brake_flags)
    for checkpoint in result['path_tracking']['checkpoints']:
        assert abs(checkpoint['target_deg']-checkpoint['measured_deg'])<8
    assert abs(result['path_tracking']['end_heading_error_deg'])<.6
    assert wheels.x==pytest.approx(path.points[-1].x,abs=3)
    assert wheels.y==pytest.approx(path.points[-1].y,abs=3)


class SlippingWheels(LoadedWheels):
    traction_right=.9


@pytest.mark.parametrize('path',[CurvePath.arc(20,90,9.8),CurvePath.sine(15,45,1,9.8)])
def test_gyro_feedback_corrects_heading_despite_mild_wheel_slip(curve_run,path):
    result,wheels=curve_run(path,plant=SlippingWheels,imu=True,speed=50)
    assert result['ok'],result
    assert abs(result['heading']['disagreement_deg'])>5
    assert abs(result['path_tracking']['end_heading_error_deg'])<6
    assert all(abs(c['target_deg']-c['measured_deg'])<8 for c in result['path_tracking']['checkpoints'])
    assert wheels.x==pytest.approx(path.points[-1].x,abs=4)


def test_progress_can_extend_curve_timeout_but_never_indefinitely(curve_run):
    class Creeping(LoadedWheels):
        motor_gain=.1
    path=CurvePath.arc(20,90,9.8)
    result,wheels=curve_run(path,plant=Creeping,speed=50)
    assert not result['ok'] and result['reason']=='timeout'
    assert 8<wheels.clock[0]<=24.2
    assert all(c>100 for c in wheels.position)
    assert wheels.duty==(0,0)


@pytest.mark.parametrize('distance',[20,-20])
def test_straight_paths_use_continuous_wheel_balance_and_coupled_stop(curve_run,distance):
    path=CurvePath.straight(distance,9.8)
    result,wheels=curve_run(path,plant=LoadedWheels,imu=True,speed=40)
    assert result['ok'],result
    assert abs(result['path_tracking']['end_heading_error_deg'])<.7
    assert wheels.x==pytest.approx(distance,abs=.7)
    assert wheels.y==pytest.approx(0,abs=.7)
    assert all(not a and not b for a,b in wheels.brake_flags)


def test_curve_feedback_handles_a_load_change_mid_move(curve_run):
    class LoadChange(LoadedWheels):
        def wait(self,timeout):
            if self.clock[0]>2:self.right_gain=.75
            super().wait(timeout)
    result,wheels=curve_run(CurvePath.arc(20,90,9.8),plant=LoadChange,imu=True,speed=50)
    assert result['ok'],result
    assert abs(result['path_tracking']['end_heading_error_deg'])<1.5
    assert max(abs(v) for row in wheels.history for v in row[-2:])<=50


def test_manual_sensor_dropout_during_a_curve_does_not_abort_it(curve_run,monkeypatch):
    from robbi.orchestration import Intent,intent_scope
    original=MotionManager._heading_feedback
    def feedback(manager,allow_encoder_fallback=False):
        getter=manager._heading_getter
        manager._heading_getter=lambda:None if .3<motion_module.time.monotonic()<.6 else getter()
        return original(manager,allow_encoder_fallback)
    monkeypatch.setattr(MotionManager,'_heading_feedback',feedback)
    with intent_scope(Intent('manual:dashboard',80)):
        result,wheels=curve_run(CurvePath.arc(20,90,9.8),plant=LoadedWheels,imu=True,speed=50)
    assert result['ok'],result
    assert result['heading']['imu_recoveries']>=1
    assert 'imu_heading_unavailable' in result['warnings']
    assert abs(result['path_tracking']['end_heading_error_deg'])<1
