from robot.config import settings
from robot.motion import MotionManager
from robot import motion as motion_module
from unittest.mock import Mock
import threading


def test_obstacle_stop_blocks_forward_motion(monkeypatch):
    motion = MotionManager.__new__(MotionManager)
    motion._distance_getter = lambda: 4.0
    monkeypatch.setattr(settings, "motion_obstacle_stop_enabled", True)
    monkeypatch.setattr(settings, "motion_obstacle_stop_cm", 5.0)

    assert motion._should_stop_for_obstacle(100, 100, ignore_obstacles=False)


def test_pickup_interlock_blocks_manual_motion_before_motor_arming(monkeypatch):
    motor = Mock()
    monkeypatch.setattr(motion_module, 'motor', motor)
    monkeypatch.setattr(motion_module, 'MOTOR_AVAILABLE', True)
    monkeypatch.setattr(settings, 'enable_motors', True)
    manager = MotionManager()
    manager.handling_interlock = lambda: True
    try:
        future = manager.move('forward', duration=.2)
        assert future.result()['reason'] == 'picked_up'
        assert not manager.is_running()
        motor.arm.assert_not_called()
        motor.set_wheel_pwm.assert_not_called()
        motor.inhibit.assert_called()
    finally:
        manager.shutdown()


def test_measured_gesture_cannot_fall_back_to_timed_motion_if_encoder_setting_changes(monkeypatch):
    motor = Mock()
    motor.cached_counts.return_value = (0, 0)
    monkeypatch.setattr(motion_module, 'motor', motor)
    monkeypatch.setattr(motion_module, 'MOTOR_AVAILABLE', True)
    monkeypatch.setattr(settings, 'enable_motors', True)
    monkeypatch.setattr(settings, 'motion_encoder_enabled', False)
    manager = MotionManager()
    try:
        future = manager.drive({'mode': 'distance', 'distance_cm': 3,
                                'speed': 30, 'duration': .3, 'require_encoders': True})
        assert future.done()
        assert manager.status()['last_result']['reason'] == 'encoders_required'
        motor.arm.assert_not_called()
        motor.set_wheel_pwm.assert_not_called()
    finally:
        manager.shutdown()


def test_obstacle_stop_allows_turn_but_rejects_command_override(monkeypatch):
    motion = MotionManager.__new__(MotionManager)
    motion._distance_getter = lambda: 4.0
    monkeypatch.setattr(settings, "motion_obstacle_stop_enabled", True)
    monkeypatch.setattr(settings, "motion_obstacle_stop_cm", 5.0)

    assert not motion._should_stop_for_obstacle(-100, 100, ignore_obstacles=False)
    # V2 deliberately closes the per-command safety bypass.
    assert motion._should_stop_for_obstacle(100, 100, ignore_obstacles=True)


def test_motion_geometry_estimates_sequence_delta(monkeypatch):
    motion = MotionManager.__new__(MotionManager)
    monkeypatch.setattr(settings, "motion_wheel_diameter_cm", 6.2)
    monkeypatch.setattr(settings, "motion_encoder_counts_per_rev", 700)
    monkeypatch.setattr(settings, "motion_wheel_base_cm", 10.5)

    left_cm, right_cm = motion.estimate_wheel_delta_cm(
        {"mode": "wiggle", "angle_deg": 10, "cycles": 1},
        {"segments": [{"final": {"left": -100, "right": 100}}, {"final": {"left": 200, "right": -200}}]},
    )

    assert left_cm != 0
    assert right_cm != 0
    assert motion.turn_angle_deg_from_wheel_cm(left_cm, right_cm) != 0


def test_obstacle_stop_does_not_latch_or_break_repeated_commands(monkeypatch):
    motor = Mock()
    monkeypatch.setattr(motion_module, "motor", motor)
    monkeypatch.setattr(motion_module, "MOTOR_AVAILABLE", True)
    monkeypatch.setattr(settings, "enable_motors", True)
    monkeypatch.setattr(settings, "motion_encoder_enabled", False)
    monkeypatch.setattr(settings, "motion_brake_s", 0)
    monkeypatch.setattr(settings, "motion_obstacle_stop_enabled", True)
    monkeypatch.setattr(settings, "motion_obstacle_stop_cm", 5)
    motion = MotionManager()
    distance = [3.5]
    motion.set_obstacle_distance_getter(lambda: distance[0])
    motion.move("forward", duration=0.005, wait=True)
    assert motion._last_result["reason"] == "obstacle"
    motor.set_wheel_pwm.assert_not_called()
    distance[0] = 120
    for _ in range(12):
        motion.move("forward", duration=0.005, wait=True)
        assert motion._last_result["ok"]
    assert motor.set_wheel_pwm.call_count == 12
    motion.shutdown()


def test_one_stalled_wheel_stops_even_when_other_keeps_counting(monkeypatch):
    motor = Mock()
    counts = [0, 0]
    def read_counts():
        counts[1] += 1
        return tuple(counts)
    motor.counts.side_effect = read_counts
    monkeypatch.setattr(motion_module, "motor", motor)
    monkeypatch.setattr(motion_module, "MOTOR_AVAILABLE", True)
    monkeypatch.setattr(settings, "enable_motors", True)
    monkeypatch.setattr(settings, "motion_encoder_enabled", True)
    monkeypatch.setattr(settings, "motion_brake_s", 0)
    monkeypatch.setattr(settings, "motion_stall_timeout_s", 0.05)
    monkeypatch.setattr(settings, "motion_obstacle_stop_enabled", False)
    motion = MotionManager()
    motion.move("forward", distance_cm=20, wait=True)
    assert motion._last_result["reason"] == "encoder_stall"
    assert counts[1] > 1
    motor.stop.assert_called()
    motion.shutdown()


def test_cancelled_mapping_cannot_clear_stop_and_start_a_new_move(monkeypatch):
    motor = Mock()
    monkeypatch.setattr(motion_module, "motor", motor)
    monkeypatch.setattr(motion_module, "MOTOR_AVAILABLE", True)
    monkeypatch.setattr(settings, "enable_motors", True)
    manager = MotionManager()
    cancel = threading.Event()
    cancel.set()
    future = manager.move(mode="turn", angle_deg=10, cancel_event=cancel)
    assert future.done()
    assert manager._last_result["reason"] == "stopped"
    motor.arm.assert_not_called()
    motor.inhibit.assert_called()
    assert not manager.is_running()


def test_stop_racing_start_invalidates_command_ticket(monkeypatch):
    motor = Mock()
    monkeypatch.setattr(motion_module, "motor", motor)
    monkeypatch.setattr(motion_module, "MOTOR_AVAILABLE", True)
    monkeypatch.setattr(settings, "enable_motors", True)
    manager = MotionManager()
    original = manager._request_stop

    def interrupted():
        original()
        manager.stop()  # User STOP arrives while the old worker is being joined.

    manager._request_stop = interrupted
    future = manager.move(mode="turn", angle_deg=10)
    assert future.done()
    motor.arm.assert_not_called()
    assert manager._last_result["reason"] == "stopped"
