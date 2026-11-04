"""Fault injection without GPIO, camera, network or motor activity."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from robot import camera, media, microphone, motion, slam, tof
from robot.config import settings


def test_optional_native_modules_do_not_prevent_hardware_import(tmp_path):
    script = '''
import builtins
original = builtins.__import__
def without_optional(name, *args, **kwargs):
    if name.split('.')[0] in {'numpy', 'cv2', 'PIL', 'picamera2', 'lgpio', 'st7789'}:
        raise ImportError('injected optional dependency failure')
    return original(name, *args, **kwargs)
builtins.__import__ = without_optional
from robot.camera import CameraService
from robot.media import MediaManager
from robot.microphone import MicrophoneService
assert not CameraService().start()
mic = MicrophoneService()
assert not mic.health()['local_wake']['available']
manager = MediaManager()
assert not manager.health()['display']['available']
manager.shutdown()
'''
    env = dict(os.environ, ROBBI_DATA_DIR=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_missing_camera_is_reported_and_requests_are_nonblocking(monkeypatch):
    if hasattr(settings, "camera_enabled"):
        monkeypatch.setattr(settings, "camera_enabled", True)
    monkeypatch.setattr(camera, "PICAMERA2_AVAILABLE", False)
    service = camera.CameraService()
    assert service.start() is False
    assert "dependencies" in service.health()["last_error"]
    service.restart = Mock(side_effect=AssertionError("request must not restart hardware"))
    assert service.get_frame() is None
    assert service.get_jpeg_frame() is None


def test_stale_camera_frame_is_never_returned(monkeypatch):
    service = camera.CameraService()
    service._running = True
    service._latest_frame = Mock()
    service._last_frame_ts = time.monotonic() - 10
    monkeypatch.setattr(camera.time, "time", lambda: 0)
    assert service.get_frame(max_age_s=2) is None
    assert service.health()["fresh"] is False
    service.stop()
    assert service._latest_frame is None


def test_face_normalization_and_corrupt_image_keep_previous_display(tmp_path, monkeypatch):
    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(media, "LCD_AVAILABLE", False)
    display = media.Display()
    display._disp = Mock()
    path = tmp_path / "wide.png"
    pil.new("RGB", (1000, 100), "white").save(path)
    assert display.show_image(path)
    frame = display._disp.display.call_args.args[0]
    assert frame.size == (280, 240)
    assert frame.getpixel((0, 0)) == (0, 0, 0)
    assert frame.getpixel((140, 120)) == (255, 255, 255)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert display.show_image(broken) is False
    assert display.health()["last_asset_error"]
    assert display._disp.display.call_count == 1
    assert display.show_image(path)
    assert not display.health()["last_asset_error"]


def test_face_cache_is_bounded(tmp_path, monkeypatch):
    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(media, "LCD_AVAILABLE", False)
    display = media.Display()
    for i in range(20):
        path = tmp_path / f"face-{i}.png"
        pil.new("RGB", (10 + i, 10)).save(path)
        display.show_image(path)
    assert display.health()["cached_faces"] == 12


def test_audio_queue_rejects_overload_and_shutdown_cancels(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    def hold_playback(self, path, generation=None):
        entered.set()
        release.wait(2)
        return -1
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", hold_playback)
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    player = media.AudioPlayer()
    path = tmp_path / "sound.wav"
    path.write_bytes(b"fixture")
    first = player.play_file(path)
    assert entered.wait(1)
    queued = [player.play_file(path) for _ in range(8)]
    rejected = player.play_file(path)
    assert rejected.wait(0) == -1
    assert player.health()["queue_size"] == 8
    player.stop_all()
    assert all(handle.wait(0) == -1 for handle in queued)
    release.set()
    player.shutdown()
    assert first.wait(1) == -1
    assert not player._worker.is_alive()
    assert player.play_file(path).wait(0) == -1


def test_stopped_audio_never_retries_another_device(tmp_path, monkeypatch):
    player = media.AudioPlayer()
    generation = player._generation
    player.stop_all()
    monkeypatch.setattr(media.subprocess, "Popen", Mock(side_effect=AssertionError("stale playback")))
    assert player._play_with_fallbacks(tmp_path / "sound.wav", generation) == -1
    player.shutdown()


def test_microphone_status_never_launches_subprocess(monkeypatch):
    mic = microphone.MicrophoneService()
    monkeypatch.setattr(microphone.subprocess, "run", Mock(side_effect=AssertionError("blocking status")))
    assert mic.health()["capture_available"] is False


@pytest.mark.parametrize("distance", [None, float("nan"), float("inf"), -1])
def test_missing_invalid_or_stale_range_stops_forward_even_with_override(monkeypatch, distance):
    manager = motion.MotionManager.__new__(motion.MotionManager)
    manager._distance_getter = lambda: distance
    monkeypatch.setattr(settings, "motion_obstacle_stop_enabled", True)
    assert manager._should_stop_for_obstacle(50, 50, ignore_obstacles=True)
    assert not manager._should_stop_for_obstacle(-50, -50, ignore_obstacles=False)
    assert not manager._should_stop_for_obstacle(-50, 50, ignore_obstacles=False)
    assert manager._obstacle_result("forward", time.monotonic())["reason"] == "range_unavailable"


def test_tof_freshness_is_independent_of_wall_clock(monkeypatch):
    monkeypatch.setattr(settings, "tof_enabled", True)
    sensor = tof.ToFService()
    sensor._set_status("h1", available=True, distance_cm=50, last_read_ts=time.monotonic())
    monkeypatch.setattr(tof.time, "time", lambda: 9e12)
    assert sensor.distance_cm() == 50
    assert sensor.status()["fresh"]
    sensor._set_status("h1", last_read_ts=time.monotonic() - 20)
    assert sensor.distance_cm() is None


def test_mapping_capacity_halts_without_erasing_prior_obstacles():
    grid = slam.OccupancyGrid(max_cells=100)
    for i in range(100):
        grid._add((i, 0), 1)
    with pytest.raises(slam.MappingHalted, match="capacity"):
        grid._add((101, 0), -1)
    assert len(grid.cells) == 100
    assert grid.cells[(0, 0)] == 1
    assert grid.capacity_reached


def test_mapping_write_failure_leaves_existing_snapshot_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(slam, "MAPS_DIR", tmp_path)
    monkeypatch.setattr(slam.time, "strftime", lambda pattern: "fixed")
    service = slam.SlamService(Mock(), lambda: None, lambda: (True, ""))
    path = tmp_path / "map_fixed.json"
    path.write_text("previous snapshot")
    monkeypatch.setattr(slam.os, "replace", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        service.save()
    assert path.read_text() == "previous snapshot"
    assert list(tmp_path.glob(".map-*.tmp")) == []


def test_constructing_display_and_motion_never_opens_hardware(monkeypatch):
    monkeypatch.setattr(settings, "enable_lcd", True)
    monkeypatch.setattr(settings, "enable_motors", True)
    driver = Mock()
    monkeypatch.setattr(media, "LCD_AVAILABLE", True)
    monkeypatch.setattr(media, "st7789", driver)
    motor = Mock()
    monkeypatch.setattr(motion, "MOTOR_AVAILABLE", True)
    monkeypatch.setattr(motion, "motor", motor)
    display, manager = media.Display(), motion.MotionManager()
    driver.ST7789.assert_not_called()
    motor.initialize.assert_not_called()
    display.start()
    manager.start()
    driver.ST7789.assert_called_once()
    motor.initialize.assert_called_once()


def test_disabled_microphone_never_launches_capture(monkeypatch):
    monkeypatch.setattr(settings, "voice_enabled", False)
    mic = microphone.MicrophoneService()
    monkeypatch.setattr(microphone.subprocess, "Popen", Mock(side_effect=AssertionError("disabled microphone")))
    mic.start()
    assert mic._thread is None
    assert not mic.health()["enabled"]


@pytest.mark.parametrize("reason", ["expired", "restarted", "stopped"])
def test_stale_voice_result_never_executes_command(monkeypatch, reason):
    mic = microphone.MicrophoneService()
    mic._running = reason != "stopped"
    mic._generation = 3
    mic._mode = "processing_command"
    command = Mock()
    mic.configure(transcribe_callback=lambda *args: "drive forward", command_callback=command)
    generation = 2 if reason == "restarted" else 3
    deadline = time.monotonic() - 1 if reason == "expired" else time.monotonic() + 10
    mic._process_command_capture(b"wav", generation=generation, deadline=deadline)
    command.assert_not_called()
    if reason == "expired":
        assert mic._mode == "wake"


def test_camera_jpeg_cache_encodes_once_and_is_bounded(monkeypatch):
    fake_cv2 = Mock()
    fake_cv2.IMWRITE_JPEG_QUALITY = 1
    fake_cv2.imencode.return_value = (True, Mock(tobytes=lambda: b"jpeg"))
    monkeypatch.setattr(camera, "cv2", fake_cv2)
    service = camera.CameraService()
    service._running = True
    frame = Mock(ndim=3, shape=(240, 280, 3))
    frame.copy.return_value = frame
    service._store_frame(frame)
    assert service.get_jpeg_frame() == service.get_jpeg_frame() == b"jpeg"
    assert fake_cv2.imencode.call_count == 1
    for quality in range(30, 50):
        service.get_llm_image_bytes(quality=quality)
    assert len(service._jpeg_cache) <= 4
    service.stop()


def test_speech_tempfile_is_cleaned_after_playback(tmp_path, monkeypatch):
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", lambda *args: 0)
    player = media.AudioPlayer()
    path = tmp_path / "temporary.wav"
    path.write_bytes(b"fixture")
    handle = player.play_file(path, delete_after=True)
    assert handle.wait(1) == 0
    player.shutdown()
    assert not path.exists()


def test_failed_volume_conversion_does_not_play_full_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 5)
    monkeypatch.setattr(media.AudioPlayer, "_scale_wav", lambda *args: None)
    play = Mock(side_effect=AssertionError("wrong-volume playback"))
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", play)
    player = media.AudioPlayer()
    path = tmp_path / "unconvertible.wav"
    path.write_bytes(b"fixture")
    assert player.play_file(path).wait(1) == -1
    play.assert_not_called()
    assert "volume" in player.health()["last_error"]
    player.shutdown()


def test_cancelled_audio_cannot_enqueue_or_mark_hardware_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda command: "/fake/aplay")
    player = media.AudioPlayer()
    path = tmp_path / "speech.wav"
    path.write_bytes(b"fixture")
    event = threading.Event()
    event.set()
    assert player.play_file(path, cancel_event=event, delete_after=True).wait(0) == -1
    assert not path.exists()
    assert player.health()["queue_size"] == 0
    assert player.health()["last_error"] == ""
    player.shutdown()


def test_video_discards_frame_decoded_after_stop(monkeypatch):
    event = threading.Event()
    capture = Mock()
    capture.isOpened.return_value = True
    capture.get.return_value = 25
    def read():
        event.set()
        return True, Mock()
    capture.read.side_effect = read
    fake_cv2 = Mock()
    fake_cv2.VideoCapture.return_value = capture
    monkeypatch.setattr(media, "cv2", fake_cv2)
    manager = media.MediaManager()
    manager.display.show_frame = Mock()
    manager.play_video(Path("fixture.mp4"), cancel_event=event)
    manager.display.show_frame.assert_not_called()
    capture.release.assert_called_once()
    manager.shutdown()


def test_active_audio_observes_cancellation_without_another_stop_call(tmp_path, monkeypatch):
    event = threading.Event()
    process = Mock()
    process.returncode = 0
    calls = []
    def communicate(timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            event.set()
            raise subprocess.TimeoutExpired("aplay", timeout)
        process.returncode = -1
        return None, ""
    process.communicate.side_effect = communicate
    monkeypatch.setattr(media.subprocess, "Popen", lambda *args, **kwargs: process)
    player = media.AudioPlayer()
    assert player._play_with_device(tmp_path / "fixture.wav", "fake-device", cancel_event=event) == -1
    process.terminate.assert_called_once()
    assert len(calls) == 2
    player.shutdown()
