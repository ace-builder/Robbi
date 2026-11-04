"""Real worker handovers with fake motors: no GPIO or robot connection."""
import threading
import time
from unittest.mock import Mock

import pytest

from robbi.errors import BusyError, UnavailableError
from robbi.orchestration import APP, AUTONOMY, MANUAL, Intent, Orchestrator, current_intent, intent_scope
from robot import motion
from robot.config import settings
from robot.imu import IMUService
from robot.slam import SlamService, MappingHalted


@pytest.fixture
def rig(monkeypatch):
    driver = Mock()
    driver.counts.return_value = driver.cached_counts.return_value = (0, 0)
    driver.physical_counts.return_value = driver.cached_levels.return_value = driver.driver_status.return_value = {}
    monkeypatch.setattr(motion, "motor", driver)
    monkeypatch.setattr(motion, "MOTOR_AVAILABLE", True)
    for key, value in dict(enable_motors=True, motion_encoder_enabled=False,
                           motion_brake_s=0, motion_obstacle_stop_enabled=False,
                           motion_control_interval_s=.02).items():
        monkeypatch.setattr(settings, key, value)
    manager, broker = motion.MotionManager(), Orchestrator()
    manager.bind_orchestrator(broker)
    yield manager, broker, driver
    manager.stop()
    thread = manager._motion_thread
    if thread:
        thread.join(2)


@pytest.mark.parametrize("kind", ["timed", "raw", "encoder", "turn"])
def test_running_motion_observes_app_cancellation(rig, monkeypatch, kind):
    manager, broker, driver = rig
    running, cancel = threading.Event(), threading.Event()
    driver.set_wheel_pwm.side_effect = lambda *_: running.set()
    driver.set_wheel_pwm_raw.side_effect = lambda *_: running.set()
    driver.set_wheel_pwm_with_brake.side_effect = lambda *_, **__: running.set()
    monkeypatch.setattr(settings, "motion_encoder_enabled", kind in {"encoder", "turn"})
    owner = Intent("app:test", APP, cancelled=cancel)
    with intent_scope(owner):
        if kind == "raw":
            future = manager.raw_wheel_drive(20, 20, duration=2, cancel_event=cancel)
        elif kind == "turn":
            future = manager.drive({"mode": "turn", "angle_deg": 90, "duration": 2}, cancel_event=cancel)
        else:
            future = manager.drive({"direction": "forward", "distance_cm": 30 if kind == "encoder" else None,
                                    "duration": 2}, cancel_event=cancel)
    assert running.wait(1)
    cancel.set()
    result = future.result(timeout=1)
    assert result["ok"] is False and result["reason"] == "stopped"
    assert not manager.is_running()
    assert "motion" not in broker.snapshot()["resources"]
    driver.stop.assert_called()


def test_equal_priority_apps_cannot_replace_each_others_motion(rig):
    manager, _, driver = rig
    running = threading.Event()
    driver.set_wheel_pwm.side_effect = lambda *_: running.set()
    first = Intent("app:first", APP)
    with intent_scope(first):
        future = manager.move(duration=2)
    assert running.wait(1)
    with intent_scope(Intent("app:second", APP)), pytest.raises(BusyError):
        manager.move(duration=.1)
    assert not first.is_set() and not future.done()
    manager.stop(owner=first)
    future.result(timeout=1)


def test_manual_preemption_waits_for_actual_braking_before_handover(rig):
    manager, broker, driver = rig
    braking, release = threading.Event(), threading.Event()
    def brake():
        braking.set()
        assert release.wait(2)
    manager._brake_then_stop = brake
    first = Intent("app:first", APP)
    with intent_scope(first):
        future = manager.move(duration=0)
    assert braking.wait(1)
    assert not future.done()  # Inner runner already finished; brake still owns it.
    assert broker.snapshot()["resources"]["motion"]["intent_id"] == first.id
    started = time.monotonic()
    try:
        with intent_scope(Intent("manual:drive", MANUAL)), pytest.raises(BusyError):
            manager.move(duration=.01)
        assert time.monotonic() - started < .5
        assert first.is_set() and driver.arm.call_count == 1
    finally:
        release.set()
    future.result(timeout=1)
    assert "motion" not in broker.snapshot()["resources"]
    manager._brake_then_stop = driver.stop
    with intent_scope(Intent("manual:new", MANUAL)):
        manager.move(duration=.01, wait=True)
    assert driver.arm.call_count == 2


def test_manual_control_preempts_active_app_and_old_owner_cannot_stop_it(rig):
    manager, _, driver = rig
    running = threading.Event()
    driver.set_wheel_pwm.side_effect = lambda *_: running.set()
    old = Intent("app:drive", APP)
    with intent_scope(old):
        previous = manager.move(duration=2)
    assert running.wait(1)
    new = Intent("manual:drive", MANUAL)
    with intent_scope(new):
        replacement = manager.move(duration=2)
    assert old.is_set() and previous.result(timeout=1)["reason"] == "stopped"
    before = driver.inhibit.call_count
    assert manager.stop(owner=old) is False
    assert driver.inhibit.call_count == before and not replacement.done()
    # Identical labels are still distinct invocations, never cleanup authority.
    assert manager.stop(owner=Intent("manual:drive", MANUAL)) is False
    manager.stop(owner=new)
    replacement.result(timeout=1)


def test_nested_same_owner_motion_cannot_overlap(rig):
    manager, _, driver = rig
    running = threading.Event()
    driver.set_wheel_pwm.side_effect = lambda *_: running.set()
    owner = Intent("app:scene", APP)
    with intent_scope(owner):
        first = manager.move(duration=2)
        assert running.wait(1)
        with pytest.raises(BusyError):
            manager.move(duration=.1)
    assert not owner.is_set() and not first.done()
    manager.stop(owner=owner)
    first.result(timeout=1)


def test_cancelled_intent_cannot_reacquire_motion(rig):
    manager, _, driver = rig
    owner = Intent("mapping:scan", AUTONOMY)
    owner.cancel("preempted")
    with intent_scope(owner), pytest.raises(UnavailableError):
        manager.move(duration=.1)
    driver.arm.assert_not_called()


def test_stop_during_blocked_launch_does_not_wait_or_start_late_motion(rig):
    manager, broker, driver = rig
    entered, release = threading.Event(), threading.Event()
    driver.arm.side_effect = lambda: (entered.set(), release.wait(2))
    futures = []
    def launch():
        with intent_scope(Intent("app:late", APP)):
            futures.append(manager.move(duration=.1))
    thread = threading.Thread(target=launch)
    thread.start()
    assert entered.wait(1)
    started = time.monotonic()
    try:
        manager.stop()
        assert time.monotonic() - started < .1
        assert "motion" in broker.snapshot()["resources"]
    finally:
        release.set()
        thread.join(1)
    assert futures[0].result(timeout=1)["reason"] == "stopped"
    driver.set_wheel_pwm.assert_not_called()


def test_mapping_copies_unique_autonomy_intents_into_each_worker(rig, monkeypatch):
    manager, _, _ = rig
    monkeypatch.setattr(settings, "motion_encoder_enabled", True)
    monkeypatch.setattr(settings, "slam_enabled", True)
    service = SlamService(manager, lambda: 100, lambda: (True, ""))
    owners = []
    for _ in range(2):
        assert service._start_worker("scan", lambda: owners.append(current_intent()))["ok"]
        service._thread.join(1)
    assert owners[0].priority == owners[1].priority == AUTONOMY
    assert owners[0].id != owners[1].id
    owners[1].cancel("manual control")
    with intent_scope(owners[1]), pytest.raises(MappingHalted, match="owner_cancelled"):
        service._check_guard()


def test_inactive_mapping_stop_does_not_stop_manual_motion(rig):
    manager, _, driver = rig
    service = SlamService(manager, lambda: 100, lambda: (True, ""))
    running = threading.Event()
    driver.set_wheel_pwm.side_effect = lambda *_: running.set()
    manual = Intent("manual:drive", MANUAL)
    with intent_scope(manual):
        future = manager.move(duration=2)
        assert running.wait(1)
        service.stop()
    assert not future.done()
    manager.stop(owner=manual)
    future.result(timeout=1)


def test_imu_enable_after_disabled_start_creates_one_sampler(monkeypatch):
    monkeypatch.setattr(settings, "imu_enabled", False)
    entered, finish = threading.Event(), threading.Event()
    service = IMUService()
    service._loop = lambda: (entered.set(), finish.wait(2))
    service.reload_runtime_settings()
    assert service._thread is None
    monkeypatch.setattr(settings, "imu_enabled", True)
    try:
        service.reload_runtime_settings()
        assert entered.wait(1)
        first = service._thread
        service.reload_runtime_settings()
        assert service._thread is first
    finally:
        finish.set()
        service.shutdown()


def test_imu_snapshots_do_not_expose_mutable_worker_data(monkeypatch):
    monkeypatch.setattr(settings, "imu_enabled", True)
    service = IMUService()
    service._publish(available=True, last_sample_monotonic=time.monotonic(),
                     quaternion=[1, 0, 0, 0], orientation_deg={"roll": 1})
    data = service.status()
    data["quaternion"][0] = 0
    data["orientation_deg"]["roll"] = 99
    assert service.status()["quaternion"] == [1, 0, 0, 0]
    assert service.status()["orientation_deg"] == {"roll": 1}


def test_standalone_motion_worker_preserves_manual_sensor_policy(rig):
    manager,broker,driver=rig
    manager._orchestrator=None
    called=[]
    def runner(future):
        called.append((current_intent().owner,manager._manual_command()))
        future.set_result({'ok':True})
    from concurrent.futures import Future
    future=Future()
    with intent_scope(Intent('manual:dashboard',MANUAL)):
        manager._launch(runner,(future,),future)
    assert future.result(timeout=1)=={'ok':True}
    manager._motion_thread.join(1)
    assert called==[('manual:dashboard',True)]
