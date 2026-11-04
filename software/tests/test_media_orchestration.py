"""Resource ownership must survive real asynchronous/native output lifetimes."""
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from robbi.orchestration import APP, BACKGROUND, MANUAL, Intent, Orchestrator, ResourceBusy, intent_scope
from robot import media


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "face.png"
    Image.new("RGB", (40, 30), "white").save(path)
    return path


def test_blocked_display_retains_ownership_until_native_write_returns(image_path):
    coordinator = Orchestrator()
    display = media.Display(coordinator)
    entered, finish = threading.Event(), threading.Event()
    writes, result = [], []

    def native_write(frame):
        writes.append(frame)
        if len(writes) == 1:
            entered.set()
            assert finish.wait(2)

    display._disp = SimpleNamespace(display=native_write)
    old, new = Intent("app:old", APP), Intent("manual:new", MANUAL)

    def first():
        with intent_scope(old):
            result.append(display.show_image(image_path))

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(1)
        with intent_scope(new), pytest.raises(ResourceBusy):
            display.show_image(image_path)
        assert len(writes) == 1
        assert worker.is_alive()
    finally:
        finish.set()
        worker.join(2)
    assert result == [False]
    with intent_scope(new):
        assert display.show_image(image_path)
    assert len(writes) == 2


def test_static_display_hold_blocks_ambient_but_allows_explicit_replacement(image_path):
    coordinator = Orchestrator()
    display = media.Display(coordinator)
    writes = []
    display._disp = SimpleNamespace(display=lambda image: writes.append(image))
    with intent_scope(Intent("app:image", APP)):
        assert display.show_image(image_path, hold_s=30)
    with intent_scope(Intent("ambient:face", BACKGROUND)), pytest.raises(ResourceBusy):
        display.show_image(image_path)
    with intent_scope(Intent("app:replacement", APP)):
        assert display.show_image(image_path)
    assert len(writes) == 2


def test_audio_wait_timeout_and_preemption_do_not_release_blocked_playback(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def native_playback(self, path, generation=None, cancelled=None):
        calls.append(path)
        if len(calls) == 1:
            entered.set()
            assert finish.wait(2)
        return 0

    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", native_playback)
    player = media.AudioPlayer(coordinator)
    path = tmp_path / "sound.wav"
    path.write_bytes(b"fixture")
    new = Intent("manual:sound", MANUAL)
    try:
        with intent_scope(Intent("app:sound", APP)):
            first = player.play_file(path)
        assert entered.wait(1)
        assert first.wait(.01) is None
        with intent_scope(new), pytest.raises(ResourceBusy):
            player.play_file(path)
        assert len(calls) == 1
        finish.set()
        assert first.wait(1) == -1
        wait_until(lambda: player.health()["playing"] is False)
        with intent_scope(new):
            second = player.play_file(path)
        assert second.wait(1) == 0
    finally:
        finish.set()
        player.shutdown()


def test_scoped_audio_stop_cannot_cancel_other_owner_or_other_resources(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    entered = threading.Event()
    tokens = []

    def cooperative_playback(self, path, generation=None, cancelled=None):
        tokens.append(cancelled)
        entered.set()
        cancelled.wait(2)
        return -1

    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", cooperative_playback)
    player = media.AudioPlayer(coordinator)
    path = tmp_path / "sound.wav"
    path.write_bytes(b"fixture")
    owner, stranger = Intent("conversation:one", APP), Intent("conversation:old", APP)
    try:
        with intent_scope(owner), coordinator.acquire(("display", "audio")) as reservation:
            handle = player.play_file(path)
            assert entered.wait(1)
            player.stop_owner(stranger)
            assert not tokens[0].is_set()
            player.stop_owner(owner)
            assert handle.wait(1) == -1
            assert not reservation.is_set()
            wait_until(lambda: player.health()["playing"] is False)
        entered.clear()
        with intent_scope(stranger):
            next_handle = player.play_file(path)
        assert entered.wait(1)
        player.stop_owner(owner)
        assert not tokens[-1].is_set()
        player.stop_owner(stranger)
        assert next_handle.wait(1) == -1
    finally:
        player.shutdown()


def test_draining_audio_queue_keeps_active_native_owner(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    entered, finish = threading.Event(), threading.Event()

    def blocked_playback(self, path, generation=None, cancelled=None):
        entered.set()
        assert finish.wait(2)
        return 0

    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", blocked_playback)
    player = media.AudioPlayer(coordinator)
    path = tmp_path / "sound.wav"
    path.write_bytes(b"fixture")
    try:
        with intent_scope(Intent("app:queue", APP)):
            running = player.play_file(path)
            assert entered.wait(1)
            queued = player.play_file(path)
        player.stop_all()
        assert queued.wait(0) == -1
        assert running.wait(0) is None
        with intent_scope(Intent("manual:next", MANUAL)), pytest.raises(ResourceBusy):
            coordinator.acquire(("audio",), wait_s=0)
        finish.set()
        assert running.wait(1) == -1
        wait_until(lambda: not player._jobs)
        with intent_scope(Intent("manual:later", MANUAL)), coordinator.acquire(("audio",), wait_s=0):
            pass
    finally:
        finish.set()
        player.shutdown()


def test_video_reserves_audio_and_display_atomically(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    manager = media.MediaManager(coordinator)
    monkeypatch.setattr(media, "cv2", SimpleNamespace())
    manager.display._disp = SimpleNamespace()
    sound = tmp_path / "video.wav"
    sound.write_bytes(b"fixture")
    owner = Intent("app:audio", APP)
    try:
        with intent_scope(owner), coordinator.acquire(("audio",)):
            with intent_scope(Intent("app:video", APP)), pytest.raises(ResourceBusy):
                manager.play_video(tmp_path / "video.mp4", sound)
            with intent_scope(Intent("app:display", APP)), coordinator.acquire(("display",), wait_s=0):
                pass
    finally:
        manager.shutdown()


def test_stop_does_not_wait_for_native_audio_process_creation(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    entered, finish = threading.Event(), threading.Event()

    class Process:
        returncode = None

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def communicate(self, timeout=None):
            self.returncode = self.returncode if self.returncode is not None else 0
            return None, ""

        def wait(self):
            return self.returncode

    def popen(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return Process()

    monkeypatch.setattr(media.subprocess, "Popen", popen)
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    player = media.AudioPlayer(coordinator)
    path = tmp_path / "sound.wav"
    path.write_bytes(b"fixture")
    try:
        with intent_scope(Intent("app:starting-audio", APP)):
            handle = player.play_file(path)
        assert entered.wait(1)
        stopped = threading.Event()
        stopper = threading.Thread(target=lambda: (player.stop_all(), stopped.set()))
        stopper.start()
        assert stopped.wait(.2)
        stopper.join(1)
        assert handle.wait(0) is None
        with intent_scope(Intent("manual:next-audio", MANUAL)), pytest.raises(ResourceBusy):
            coordinator.acquire(("audio",), wait_s=0)
        finish.set()
        assert handle.wait(1) == -1
    finally:
        finish.set()
        player.shutdown()


def test_audio_error_keeps_lease_until_failed_process_is_reaped(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    reaping, finish = threading.Event(), threading.Event()

    class Process:
        returncode = None

        def communicate(self, timeout=None):
            raise OSError("broken audio pipe")

        def kill(self):
            self.returncode = -9

        def wait(self):
            reaping.set()
            assert finish.wait(2)
            return self.returncode

    monkeypatch.setattr(media.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    player = media.AudioPlayer(coordinator)
    path = tmp_path / "sound.wav"
    path.write_bytes(b"fixture")
    try:
        with intent_scope(Intent("app:failed-audio", APP)):
            handle = player.play_file(path)
        assert reaping.wait(1)
        with intent_scope(Intent("manual:next-audio", MANUAL)), pytest.raises(ResourceBusy):
            coordinator.acquire(("audio",), wait_s=0)
        assert handle.wait(0) is None
        finish.set()
        assert handle.wait(1) == -1
        wait_until(lambda: not player._jobs)
        with intent_scope(Intent("manual:later-audio", MANUAL)), coordinator.acquire(("audio",), wait_s=0):
            pass
    finally:
        finish.set()
        player.shutdown()
