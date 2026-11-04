"""Efficiency regressions that run without robot hardware or external services."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robbi.errors import BusyError
from robbi.jobs import BoundedExecutor
from robot import camera
from robot.config import settings


def test_idle_executor_blocks_until_work_or_shutdown(monkeypatch):
    executor = BoundedExecutor(max_workers=1)
    real_get = executor._queue.get
    entered_idle_wait = threading.Event()
    calls = []

    def observed_get(block=True, timeout=None):
        calls.append((block, timeout))
        if len(calls) == 2:
            entered_idle_wait.set()
        return real_get(block, timeout)

    monkeypatch.setattr(executor._queue, "get", observed_get)
    assert executor.submit(lambda: 42).result(1) == 42
    assert entered_idle_wait.wait(1)
    assert calls == [(True, None), (True, None)]
    executor.shutdown()
    assert all(not thread.is_alive() for thread in executor._threads)
    assert executor._queue.unfinished_tasks == 0


def test_full_executor_shutdown_never_waits_for_running_tasks():
    workers = 3
    entered = threading.Barrier(workers + 1)
    release = threading.Event()
    executor = BoundedExecutor(max_workers=workers, max_pending=2)

    def blocked():
        entered.wait(2)
        release.wait(3)

    running = [executor.submit(blocked) for _ in range(workers)]
    entered.wait(2)
    pending = [executor.submit(lambda: "queued") for _ in range(2)]
    try:
        with pytest.raises(BusyError):
            executor.submit(lambda: None)
        started_at = time.monotonic()
        executor.shutdown(wait=False, cancel_futures=True)
        assert time.monotonic() - started_at < 0.25
        assert all(future.cancelled() for future in pending)
        assert not any(future.done() for future in running)
    finally:
        release.set()
        executor.shutdown()
    assert executor._queue.unfinished_tasks == 0


def test_shutdown_drains_accepted_work_and_repeated_shutdown_is_safe():
    release = threading.Event()
    entered = threading.Event()
    executor = BoundedExecutor(max_workers=1, max_pending=2)
    running = executor.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(1)
    pending = executor.submit(lambda: 99)
    executor.shutdown(wait=False)
    executor.shutdown(wait=False)
    with pytest.raises(RuntimeError, match="stopped"):
        executor.submit(lambda: None)
    release.set()
    executor.shutdown()
    assert running.done()
    assert pending.result() == 99
    executor.shutdown()
    assert executor._queue.unfinished_tasks == 0


def test_later_shutdown_can_cancel_pending_and_callbacks_can_reenter():
    release = threading.Event()
    entered = threading.Event()
    executor = BoundedExecutor(max_workers=1, max_pending=2)
    executor.submit(lambda: (entered.set(), release.wait(3)))
    assert entered.wait(1)
    pending = executor.submit(lambda: "must not run")
    callback_ran = threading.Event()

    def callback(_):
        executor.shutdown(wait=False)
        with pytest.raises(RuntimeError, match="stopped"):
            executor.submit(lambda: None)
        callback_ran.set()

    pending.add_done_callback(callback)
    executor.shutdown(wait=False)
    shutdown = threading.Thread(target=lambda: executor.shutdown(wait=False, cancel_futures=True), daemon=True)
    shutdown.start()
    try:
        shutdown.join(1)
        assert not shutdown.is_alive()
        assert pending.cancelled()
        assert callback_ran.is_set()
    finally:
        release.set()
        executor.shutdown()
    assert executor._queue.unfinished_tasks == 0


def test_concurrent_submit_and_shutdown_resolve_every_accepted_future():
    for _ in range(12):
        executor = BoundedExecutor(max_workers=2, max_pending=6)
        start = threading.Barrier(3)
        accepted = []

        def submit():
            start.wait(2)
            for number in range(20):
                try:
                    accepted.append(executor.submit(lambda value=number: value))
                except (BusyError, RuntimeError):
                    pass

        def shutdown():
            start.wait(2)
            executor.shutdown(wait=False, cancel_futures=True)

        threads = [threading.Thread(target=submit), threading.Thread(target=shutdown)]
        for thread in threads:
            thread.start()
        start.wait(2)
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
        executor.shutdown()
        assert all(future.done() for future in accepted)
        assert executor._queue.unfinished_tasks == 0


def test_camera_applies_sensor_rate_after_configure_before_start(monkeypatch):
    monkeypatch.setattr(settings, "camera_enabled", True)
    monkeypatch.setattr(settings, "camera_fps", 3)
    monkeypatch.setattr(settings, "camera_warmup_s", 0)
    monkeypatch.setattr(camera, "PICAMERA2_AVAILABLE", True)
    monkeypatch.setattr(camera, "cv2", Mock())
    monkeypatch.setattr(camera, "np", Mock())
    device = Mock(camera_controls={"FrameDurationLimits": (10000, 1000000, None)})
    monkeypatch.setattr(camera, "Picamera2", Mock(return_value=device))
    service = camera.CameraService()
    monkeypatch.setattr(service, "_capture_loop", lambda: None)
    try:
        assert service.start()
        names = [call[0] for call in device.mock_calls]
        assert names.index("configure") < names.index("set_controls") < names.index("start")
        device.set_controls.assert_called_once_with({"FrameDurationLimits": (333333, 333333)})
        assert service.health()["sensor_fps"] == 3
    finally:
        service.stop()


@pytest.mark.parametrize("limits,expected", [((100000, 200000, None), (200000, 200000)),
                                            ((500000, 1000000, None), (500000, 500000))])
def test_camera_rate_is_clamped_to_sensor_limits(monkeypatch, limits, expected):
    monkeypatch.setattr(settings, "camera_fps", 3)
    service = camera.CameraService()
    service._camera = Mock(camera_controls={"FrameDurationLimits": limits})
    service._configure_capture_rate()
    service._camera.set_controls.assert_called_once_with({"FrameDurationLimits": expected})
    assert "clamped" in service.health()["rate_limit_warning"]


@pytest.mark.parametrize("failure", ["missing", "rejected"])
def test_optional_sensor_rate_failure_retains_software_capture(monkeypatch, failure):
    service = camera.CameraService()
    service._camera = Mock(camera_controls={} if failure == "missing" else
                           {"FrameDurationLimits": (10000, 1000000, None)})
    service._camera.set_controls.side_effect = RuntimeError("unsupported control")
    service._configure_capture_rate()
    assert service.health()["sensor_fps"] is None
    assert service.health()["rate_limit_warning"]
    assert not service.health()["last_error"]


def test_camera_sensor_wait_counts_toward_capture_interval(monkeypatch):
    monkeypatch.setattr(settings, "camera_fps", 4)
    times = iter([10.0, 10.2])
    monkeypatch.setattr(camera.time, "monotonic", lambda: next(times))
    service = camera.CameraService()
    service._running = True
    service._camera = object()
    monkeypatch.setattr(service, "_capture_array", lambda: "frame")
    monkeypatch.setattr(service, "_store_frame", lambda frame: None)
    waited = []

    def wait(seconds):
        waited.append(seconds)
        service._running = False

    service._stop_event = SimpleNamespace(wait=wait)
    service._capture_loop()
    assert waited == pytest.approx([0.05])
