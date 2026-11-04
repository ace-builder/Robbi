"""Apps only receive completion when output actually reached the display."""
import threading
from types import SimpleNamespace

import pytest

from robbi.orchestration import APP, Intent, Orchestrator, intent_scope
from robot import media


@pytest.fixture
def video(monkeypatch):
    class Capture:
        opened = True

        def __init__(self):
            self.frames = [object(), object()]
            self.released = False
            self.on_read = lambda: None

        def isOpened(self):
            return self.opened

        def get(self, property):
            return 25

        def read(self):
            self.on_read()
            return (True, self.frames.pop(0)) if self.frames else (False, None)

        def grab(self):
            return bool(self.frames.pop(0)) if self.frames else False

        def release(self):
            self.released = True

    capture = Capture()
    cv2 = SimpleNamespace(VideoCapture=lambda path: capture, CAP_PROP_FPS=1,
                          COLOR_BGR2RGB=2, cvtColor=lambda frame, format: frame)
    monkeypatch.setattr(media, "cv2", cv2)
    manager = media.MediaManager(Orchestrator())
    manager.display._disp = SimpleNamespace()
    manager.display.show_frame = lambda frame, **options: True
    try:
        with intent_scope(Intent("app:video-test", APP)):
            yield manager, capture
    finally:
        manager.shutdown()


def test_video_reports_actual_visual_completion(video, tmp_path):
    manager, capture = video
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result == {"ok": True, "state": "completed", "reason": "",
                      "frames_displayed": 2, "audio_state": "not_requested"}
    assert capture.released


def test_video_does_not_decode_early_while_waiting_for_frame_deadline(video, tmp_path, monkeypatch):
    manager, _ = video
    clock = [1000.]
    shown_at = []
    monkeypatch.setattr(media, "time", SimpleNamespace(
        perf_counter=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda duration: clock.__setitem__(0, clock[0] + duration)))
    manager.display.show_frame = lambda *args, **kwargs: (shown_at.append(clock[0]) or True)
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result["ok"]
    assert shown_at[1] - shown_at[0] == pytest.approx(1 / 25)


def test_missing_opencv_does_not_report_completion(video, tmp_path, monkeypatch):
    manager, _ = video
    monkeypatch.setattr(media, "cv2", None)
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result["ok"] is False and result["state"] == "unavailable"
    assert result["frames_displayed"] == 0


def test_missing_display_does_not_report_completion(video, tmp_path):
    manager, _ = video
    manager.display._disp = None
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result["ok"] is False and result["state"] == "unavailable"
    assert result["frames_displayed"] == 0


def test_unopenable_video_is_failed_and_decoder_is_closed(video, tmp_path):
    manager, capture = video
    capture.opened = False
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result["ok"] is False and result["state"] == "failed"
    assert capture.released


def test_video_with_no_decoded_frames_is_failed(video, tmp_path):
    manager, capture = video
    capture.frames = []
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result["ok"] is False and result["frames_displayed"] == 0


def test_display_rejection_is_failed(video, tmp_path):
    manager, _ = video
    manager.display.show_frame = lambda *args, **kwargs: False
    result = manager.play_video(tmp_path / "fixture.mp4")
    assert result["ok"] is False and result["state"] == "failed"
    assert result["frames_displayed"] == 0


def test_cancellation_during_decode_is_reported_as_cancelled(video, tmp_path):
    manager, capture = video
    cancelled = threading.Event()
    capture.on_read = cancelled.set
    manager.display.show_frame = lambda *args, **kwargs: pytest.fail("Cancelled frame must not display")
    result = manager.play_video(tmp_path / "fixture.mp4", cancel_event=cancelled)
    assert result["ok"] is False and result["state"] == "cancelled"
    assert result["frames_displayed"] == 0


def test_explicit_missing_soundtrack_is_partial(video, tmp_path):
    manager, _ = video
    result = manager.play_video(tmp_path / "fixture.mp4", tmp_path / "missing.wav")
    assert result["ok"] is False and result["state"] == "partial"
    assert result["frames_displayed"] == 2 and result["audio_state"] == "missing"


@pytest.mark.parametrize("returncode, ok, state", [(0, True, "completed"), (-1, False, "partial")])
def test_soundtrack_result_is_included(video, tmp_path, returncode, ok, state):
    manager, _ = video
    sound = tmp_path / "sound.wav"
    sound.write_bytes(b"fixture")
    handle = media.PlaybackHandle()
    handle.set_result(returncode)
    manager.audio.play_file = lambda *args, **kwargs: handle
    result = manager.play_video(tmp_path / "fixture.mp4", sound)
    assert result["ok"] is ok and result["state"] == state
    assert result["audio_state"] == ("completed" if returncode == 0 else "failed")
