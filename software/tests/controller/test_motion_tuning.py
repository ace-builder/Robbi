import json
import logging
import math
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from robot import motion as motion_module
from robot.config import settings
from robot.motion import MotionManager
from robot.settings_schema import validated_number, NUMERIC_FIELDS


# Deterministic simulation benchmark, never installed as robot calibration.
# Keep the simulated motor model and its controller tuning together.
PROFILE = {'motion': {
    'default_step_cm':30, 'default_turn_deg':90, 'wheel_diameter_cm':6.2,
    'wheel_base_cm':9.8, 'encoder_counts_per_rev':1400,
    'encoder_tolerance_counts':20, 'pwm_min_duty':50, 'pid_kp':0.25,
    'stop_margin_per_duty':0.25, 'stop_margin_ratio':0.1, 'control_interval_s':0.02,
    'encoder_timeout_s':8, 'arc_radius_cm':20, 'sine_radius_cm':15,
    'sine_angle_deg':45, 'wiggle_angle_deg':45, 'pwm_max_duty':100,
    'left_motor_invert':False}}
DOUBLE_RESOLUTION = {'encoder_counts_per_rev':2800, 'encoder_tolerance_counts':40,
                     'pid_kp':0.125, 'stop_margin_per_duty':0.5}


@pytest.fixture
def tuned(monkeypatch):
    for name, value in PROFILE["motion"].items():
        monkeypatch.setattr(settings, "motion_" + name, value)
    return MotionManager.__new__(MotionManager)


def test_profile_is_valid(tuned):
    for name, value in PROFILE["motion"].items():
        if "motion." + name in NUMERIC_FIELDS:
            assert validated_number("motion." + name, value) == value


def test_synthetic_geometry(tuned):
    ticks = tuned._distance_to_ticks(30)
    assert ticks == 2156
    assert tuned._target_for_spec({"mode": "forward"}) == (ticks, ticks)
    assert tuned._target_for_spec({"mode": "backward"}) == (-ticks, -ticks)
    assert tuned._turn_ticks(90) == (-553, 553)
    assert tuned._turn_ticks(-90) == (553, -553)
    angle = tuned.turn_angle_deg_from_wheel_cm(-553*tuned.cm_per_count(), 553*tuned.cm_per_count())
    assert angle == pytest.approx(90, abs=0.1)


def test_arc_uses_measured_track_and_midline_radius(tuned):
    left, right = tuned._arc_ticks(20, 90)
    assert left*tuned.cm_per_count() == pytest.approx((20-4.9)*math.pi/2, abs=0.01)
    assert right*tuned.cm_per_count() == pytest.approx((20+4.9)*math.pi/2, abs=0.01)
    assert tuned._arc_ticks(20, -90) == (right, left)


@pytest.mark.parametrize("mode,key", [("forward", "distance_cm"), ("backward", "distance_cm"),
                                     ("turn_left", "angle_deg"), ("turn_right", "angle_deg"),
                                     ("arc", "angle_deg")])
def test_explicit_zero_never_becomes_default_motion(tuned, mode, key):
    assert tuned._target_for_spec({"mode": mode, key: 0}) == (0, 0)


@pytest.mark.parametrize("maximum,requested,expected", [(100,70,70), (55,70,55), (55,35,35), (100,0,0), (0,70,0)])
def test_duty_is_clipped_once_and_minimum_never_overrides_request(tuned, monkeypatch, maximum, requested, expected):
    monkeypatch.setattr(settings, "motion_pwm_max_duty", maximum)
    cap, minimum = tuned._duty_limits(requested)
    assert cap == expected
    assert 0 <= minimum <= cap


def test_dashboard_turns_have_starting_torque_and_small_brake_lead(tuned):
    cap, minimum = tuned._duty_limits(70)
    assert tuned._pid_duty(553, 0, 0, 553, 0, 20, cap, minimum) == 70
    assert tuned._pid_duty(100, 0, 0, 553, 453, 20, cap, minimum) == 50
    assert tuned._stop_margin(553, -553, 20, 70) == 20


def test_sync_output_cannot_reverse_or_exceed_request_or_restart_finished_wheel(tuned):
    for sign in (-1, 1):
        assert tuned._bounded_wheel_duty(sign*95, sign*500, False, 70) == sign*70
        assert tuned._bounded_wheel_duty(-sign*10, sign*500, False, 70) == 0
        assert tuned._bounded_wheel_duty(sign*20, sign*500, True, 70) == 0
    assert tuned._bounded_wheel_duty(20, 0, False, 70) == 0


def test_zero_speed_does_not_start_worker(monkeypatch):
    motor = Mock()
    monkeypatch.setattr(motion_module, "motor", motor)
    monkeypatch.setattr(motion_module, "MOTOR_AVAILABLE", True)
    monkeypatch.setattr(settings, "enable_motors", True)
    manager = MotionManager()
    manager.drive({"mode": "distance", "distance_cm": 30, "speed": 0}, wait=True)
    assert not manager.is_running()
    motor.set_wheel_pwm_with_brake.assert_not_called()
    motor.set_wheel_pwm.assert_not_called()


class SimulatedWheels:
    """Deterministic friction/asymmetry model, not a claim of physical calibration."""
    def __init__(self, clock, reversed_left=False):
        self.clock = clock
        self.position = [0.0, 0.0]
        self.outputs = []
        self.duty = (0, 0)
        self.reversed_left = reversed_left
        self.friction = 35

    def reset_counts(self):
        self.position = [0.0, 0.0]

    def counts(self):
        return tuple(round(p) for p in self.position)

    def set_wheel_pwm_with_brake(self, left, right, **brakes):
        self.duty = (0 if brakes["left_brake"] else left, 0 if brakes["right_brake"] else right)
        self.outputs.append(self.duty)

    def stop(self):
        self.duty = (0, 0)

    def brake(self, seconds):
        self.stop()

    def wait(self, timeout):
        self.clock[0] += timeout
        for side, duty in enumerate(self.duty):
            # ~1400 counts/s at duty 70; left/right differ in gain.
            speed = max(0, abs(duty)-self.friction) * (40 if side == 0 else 35)
            sign = -1 if self.reversed_left and side == 0 else 1
            self.position[side] += math.copysign(speed, duty) * timeout * sign


@pytest.mark.parametrize("mode", ["forward", "backward", "turn_left", "turn_right", "arc_left", "arc_right", "wiggle", "sine"])
def test_tuned_moves_complete_with_asymmetric_wheels(tuned, monkeypatch, mode):
    clock = [0.0]
    wheels = SimulatedWheels(clock)
    monkeypatch.setattr(motion_module, "motor", wheels)
    monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
    tuned._enabled = True
    tuned._log = logging.getLogger(__name__)
    tuned._distance_getter = lambda: 100
    tuned._stop_event = Mock()
    tuned._stop_event.is_set.return_value = False
    tuned._stop_event.wait.side_effect = wheels.wait
    for target in tuned._targets_for_spec({"mode": mode, "cycles": 1}):
        result = tuned._run_target_blocking(mode, 70, 0.7, target, False)
        assert result["ok"], result
        for side, expected in zip(("left", "right"), target):
            assert result["final"][side] == pytest.approx(expected, abs=50)
    assert max(abs(d) for pair in wheels.outputs for d in pair) <= 70


def test_bouncing_right_count_stops_before_full_motion_timeout(tuned, monkeypatch):
    clock = [0.0]
    wheels = SimulatedWheels(clock)
    def noisy_counts():
        return round(wheels.position[0]), -(len(wheels.outputs) % 2)
    wheels.counts = noisy_counts
    monkeypatch.setattr(motion_module, "motor", wheels)
    monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "motion_stall_timeout_s", 0.75)
    tuned._enabled = True
    tuned._log = logging.getLogger(__name__)
    tuned._distance_getter = lambda: 100
    tuned._stop_event = Mock()
    tuned._stop_event.is_set.return_value = False
    tuned._stop_event.wait.side_effect = wheels.wait
    result = tuned._run_target_blocking("forward", 70, 0.7, (2156, 2156), False)
    assert not result["ok"]
    assert result["reason"] == "encoder_stall"
    assert result["stalled_wheels"] == ["right"]
    assert 0.75 <= result["elapsed_s"] < 0.8
    assert wheels.duty == (0, 0)


def test_encoder_resolution_rescales_counts_not_physical_geometry(tuned, monkeypatch):
    old_kp = settings.motion_pid_kp
    old_count = tuned._distance_to_ticks(30)
    old_margin_cm = tuned._stop_margin(553, 553, 20, 70) * tuned.cm_per_count()
    for key, value in DOUBLE_RESOLUTION.items():
        monkeypatch.setattr(settings, "motion_" + key, value)
    assert tuned._distance_to_ticks(30) == 4313
    assert tuned._turn_ticks(90) == (-1106, 1106)
    assert settings.motion_pid_kp * tuned._distance_to_ticks(30) == pytest.approx(old_kp * old_count, abs=0.2)
    assert tuned._stop_margin(1106, 1106, 40, 70) * tuned.cm_per_count() == pytest.approx(old_margin_cm)


@pytest.mark.parametrize("mode", ["forward", "backward", "turn_left", "turn_right"])
@pytest.mark.parametrize("inverted", [False, True])
def test_mirrored_right_encoder_stops_until_sign_is_corrected(tuned, monkeypatch, mode, inverted):
    for key, value in DOUBLE_RESOLUTION.items():
        monkeypatch.setattr(settings, "motion_" + key, value)
    clock = [0.0]
    wheels = SimulatedWheels(clock)

    def counts():
        left, right_raw = round(wheels.position[0]), -round(wheels.position[1])
        return left, -right_raw if inverted else right_raw

    wheels.counts = counts
    monkeypatch.setattr(motion_module, "motor", wheels)
    monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
    tuned._enabled = True
    tuned._log = logging.getLogger(__name__)
    tuned._distance_getter = lambda: 100
    tuned._stop_event = Mock()
    tuned._stop_event.is_set.return_value = False
    tuned._stop_event.wait.side_effect = wheels.wait
    target = tuned._target_for_spec({"mode": mode})
    result = tuned._run_target_blocking(mode, 70, 0.7, target, False)
    assert result["ok"] == inverted, result
    if inverted:
        assert result["final"]["left"] == pytest.approx(target[0], abs=50)
        assert result["final"]["right"] == pytest.approx(target[1], abs=50)
    else:
        assert result["reason"] == "encoder_direction_mismatch"
    assert wheels.duty == (0, 0)


@pytest.mark.parametrize("mode", ["turn_left", "turn_right", "left", "right", "turn"])
@pytest.mark.parametrize("current_profile", [False, True])
def test_settled_turn_starts_above_friction_and_finishes_within_tolerance(tuned, monkeypatch, mode, current_profile):
    # Exercise this intentionally lower-power profile, independent of defaults.
    monkeypatch.setattr(settings, "motion_turn_start_duty", 38)
    monkeypatch.setattr(settings, "motion_turn_max_duty", 50)
    clock = [0.0]
    wheels = SimulatedWheels(clock)
    if current_profile:
        for key, value in DOUBLE_RESOLUTION.items():
            monkeypatch.setattr(settings, "motion_" + key, value)
        monkeypatch.setattr(settings, "motion_pwm_min_duty", 30)
        monkeypatch.setattr(settings, "motion_stall_timeout_s", 0.75)
        wheels.friction = 20
    monkeypatch.setattr(motion_module, "motor", wheels)
    monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
    tuned._enabled = True
    tuned._log = logging.getLogger(__name__)
    tuned._distance_getter = lambda: 100
    tuned._stop_event = Mock()
    tuned._stop_event.is_set.return_value = False
    tuned._stop_event.wait.side_effect = wheels.wait
    target = tuned._target_for_spec({"mode": mode})
    result = tuned._run_target_blocking(mode, 70, 0.7, target, False)
    assert result["ok"], result
    assert tuple(abs(d) for d in wheels.outputs[0]) == (38, 38)
    assert max(abs(d) for pair in wheels.outputs for d in pair) <= 50
    assert result["control"] == "settled_turn"
    assert result["control_hz"] == pytest.approx(50)
    assert max(abs(e) for e in result["error_counts"].values()) <= 12
    for side in (0, 1):
        nonzero = [abs(pair[side]) for pair in wheels.outputs if pair[side]]
        assert nonzero[-1] < max(nonzero) - 5
        # Initial acceleration starts at breakaway duty, rather than spending
        # much of the ramp whining below static friction.
        assert max(abs(pair[side]) for pair in wheels.outputs[:10]) < 44
    assert wheels.duty == (0, 0)


def test_turn_ramp_does_not_delay_encoder_stall_stop(tuned, monkeypatch):
    clock = [0.0]
    wheels = SimulatedWheels(clock)
    wheels.counts = lambda: (0, 0)
    monkeypatch.setattr(motion_module, "motor", wheels)
    monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "motion_stall_timeout_s", 0.75)
    tuned._enabled = True
    tuned._log = logging.getLogger(__name__)
    tuned._distance_getter = lambda: 100
    tuned._stop_event = Mock()
    tuned._stop_event.is_set.return_value = False
    tuned._stop_event.wait.side_effect = wheels.wait
    result = tuned._run_target_blocking("left", 70, 0.7, tuned._turn_ticks(90), False)
    assert result["reason"] == "encoder_stall"
    assert result["elapsed_s"] < 0.8
    assert wheels.duty == (0, 0)


@pytest.mark.parametrize("direction", ["left", "right"])
@pytest.mark.parametrize("limit", [45, 100, 150])
def test_timed_fallback_cannot_bypass_turn_limit(tuned, monkeypatch, direction, limit):
    clock = [0.0]
    outputs = []
    monkeypatch.setattr(motion_module.time, "monotonic", lambda: clock[0])
    fake_motor = Mock()
    fake_motor.set_wheel_pwm.side_effect = lambda l, r: outputs.append((l, r))
    monkeypatch.setattr(motion_module, "motor", fake_motor)
    monkeypatch.setattr(settings, "motion_turn_max_duty", limit)
    tuned._log = logging.getLogger(__name__)
    tuned._stop_event = Mock()
    tuned._stop_event.is_set.return_value = False
    tuned._stop_event.wait.side_effect = lambda timeout: clock.__setitem__(0, clock[0]+timeout)
    tuned._set_last_result = Mock()
    tuned._brake_then_stop = Mock()
    tuned._clear_thread = Mock()
    future = Future()
    tuned._run_timed_move(direction, 100, .1, False, future)
    assert future.result() is None
    assert outputs and max(abs(d) for pair in outputs for d in pair) == min(limit, 100)


def test_turn_settings_are_exposed_and_limited():
    from robot.config import settings_ui_snapshot
    motion = settings_ui_snapshot()["values"]["motion"]
    assert motion["turn_max_duty"] == 100
    assert motion["turn_start_duty"] == 100
    assert motion["turn_tolerance_counts"] == 12
    with pytest.raises(ValueError):
        validated_number("motion.turn_max_duty", 101)
