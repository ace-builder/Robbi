import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError
from robbi.jobs import BoundedExecutor
from robot.action_executor import ActionExecutorMixin
from robot.lifecycle import LifecycleMixin


class Host(ActionExecutorMixin, LifecycleMixin):
    def __init__(self):
        self._init_lifecycle()
        self.events = []
        self.drives = []
        self.presentation = {}
        self._log = logging.getLogger("test")
        self._publish_event = lambda kind, payload: self.events.append((kind, payload))
        self._set_presentation = lambda **updates: self.presentation.update(updates)
        self._execute_drive_action = lambda drive: self.drives.append(drive)
        self._defer_focus_timer_for_action = lambda action: None
        self._collect_spoken_text = lambda action: action.get("speak", "")
        self._resolve_asset = lambda category, name, **kwargs: Path(name)
        self.behavior = SimpleNamespace(snapshot=lambda: SimpleNamespace(current_face="neutral"))
        self.animations = SimpleNamespace(play=lambda *args, **kwargs: None, play_filming_sequence=lambda *args, **kwargs: None)
        self._speak_text_with_overlay = lambda *args: None
        self._play_video_action = lambda *args: None


def test_stop_releases_presentation_lane_while_prefetch_provider_remains_blocked():
    host = Host()
    entered = threading.Event()
    release = threading.Event()
    steps = []

    def synthesize(text):
        entered.set()
        assert release.wait(2)
        return {"wav": b"late"}

    host._prepare_tts_audio = synthesize
    host._run_sequence_step = lambda *args, **kwargs: steps.append(args)
    prefetch = BoundedExecutor(1, 0)
    presentation = BoundedExecutor(1, 1)
    host._tts_prefetch_executor = prefetch
    try:
        old = host._submit_guarded(presentation, host._run_sequence, [{"speak": "old"}])
        assert entered.wait(1)
        host.cancel_actions()
        old.result(timeout=0.5)
        # The new sequence can proceed without waiting for the old TTS call.
        host._submit_guarded(presentation, host._run_sequence, [{"face": "happy"}]).result(timeout=0.5)
        assert len(steps) == 1
        assert steps[0][0] == {"face": "happy"}
    finally:
        release.set()
        prefetch.shutdown()
        presentation.shutdown()


@pytest.mark.parametrize("presentation_action, skipped", [
    ({"speak": "moving"}, {"speech"}),
    ({"animation": "dance", "video": "clip.mp4"}, {"animation", "video"}),
])
def test_presentation_overload_does_not_report_already_applied_motion_as_failed(presentation_action, skipped):
    host = Host()

    def busy(*args, **kwargs):
        raise BusyError("presentation capacity reached")

    host._audio_executor = host._effect_executor = SimpleNamespace(submit=busy)
    action = {"drive": {"direction": "forward", "duration": 0.1}, **presentation_action}
    assert host.execute_action(action) == action
    assert host.drives == [action["drive"]]
    assert {payload["feature"] for kind, payload in host.events if kind == "presentation.skipped"} == skipped
    assert host.presentation == {}  # No claim that skipped speech/video played.


def test_optional_presentation_keeps_original_stop_signal_even_if_submit_observes_new_generation():
    host = Host()
    cancelled = host._action_token()[1]
    dispatched = []
    saved = []
    host._submit_guarded = lambda executor, callback: saved.append(callback) or object()
    host._submit_optional_presentation(None, "video", lambda: dispatched.append(True), cancellation=cancelled)
    host.cancel_actions()
    saved[0]()
    assert dispatched == []
