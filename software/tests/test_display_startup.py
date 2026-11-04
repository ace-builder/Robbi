"""Deterministic LCD initialization races without SPI or timing sleeps."""
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from robbi.orchestration import BACKGROUND, Intent, Orchestrator, intent_scope
from robot import media
from robot.config import settings


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_lcd", True)
    monkeypatch.setattr(media, "LCD_AVAILABLE", True)
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Driver:
        def __init__(self, **kwargs):
            calls.append("construct")
        def reset(self):
            calls.append("reset-start")
            entered.set()
            assert release.wait(2)
            calls.append("reset-end")
        def _init(self):
            calls.append("init")
        def display(self, frame):
            calls.append(("frame", frame.getpixel((140, 120))))
        def close(self):
            calls.append("close")
    monkeypatch.setattr(media, "st7789", SimpleNamespace(ST7789=Driver))
    paths = []
    for color in ("red", "blue"):
        path = tmp_path / f"{color}.png"
        Image.new("RGB", (280, 240), color).save(path)
        paths.append(path)
    display = media.Display(Orchestrator())
    worker = threading.Thread(target=display.start)
    yield display, worker, entered, release, calls, paths
    release.set()
    if worker.ident is not None:
        worker.join(2)
        assert not worker.is_alive()


def test_first_frame_waits_for_complete_reset_and_latest_startup_face_wins(rig):
    display, worker, entered, release, calls, (red, blue) = rig
    # The controller can schedule its first face before the media worker runs.
    assert not display.show_image(red, defer_until_ready=True)
    worker.start()
    assert entered.wait(1)
    assert display._disp is None and not display.health()["available"]
    assert not display.start()  # A second lifecycle call cannot open another SPI owner.
    assert not display.show_image(blue, defer_until_ready=True)
    assert calls == ["construct", "reset-start"]
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert calls == ["construct", "reset-start", "reset-end", "init", ("frame", (0, 0, 255))]
    assert display.health()["available"]
    assert display._pending_image is None


def test_regular_unavailable_display_request_is_not_replayed(rig):
    display, worker, entered, release, calls, (red, _) = rig
    worker.start()
    assert entered.wait(1)
    assert not display.show_image(red)
    release.set()
    worker.join(1)
    assert not any(isinstance(call, tuple) for call in calls)
    assert display.show_image(red)
    assert calls[-1] == ("frame", (255, 0, 0))


def test_stop_cancels_deferred_startup_face_in_original_controller_context(rig):
    display, worker, entered, release, calls, (red, _) = rig
    stop = threading.Event()
    worker.start()
    assert entered.wait(1)
    with intent_scope(Intent("startup-ready", BACKGROUND, cancelled=stop)):
        assert not display.show_image(red, defer_until_ready=True)
    display.orchestrator.cancel_all()
    stop.set()  # Lifecycle.cancel_actions invalidates the controller action token.
    release.set()
    worker.join(1)
    assert display.health()["available"]
    assert not any(isinstance(call, tuple) for call in calls)


def test_shutdown_during_initialization_discards_driver_and_pending_face(rig):
    display, worker, entered, release, calls, (red, _) = rig
    worker.start()
    assert entered.wait(1)
    assert not display.show_image(red, defer_until_ready=True)
    display.shutdown()  # Must return while the native reset remains blocked.
    assert worker.is_alive()
    assert display._pending_image is None
    release.set()
    worker.join(1)
    assert not display.health()["available"]
    assert not display.start()
    assert calls[-1] == "close"
    assert not any(isinstance(call, tuple) for call in calls)


def test_initialization_failure_never_publishes_partial_driver(rig, monkeypatch):
    display, worker, entered, release, calls, _ = rig
    def failed_init(self):
        raise OSError("SPI initialization failed")
    monkeypatch.setattr(media.st7789.ST7789, "_init", failed_init)
    release.set()
    assert not display.start()
    assert display._disp is None
    assert display.health()["last_error"] == "SPI initialization failed"
    assert calls[-1] == "close"
