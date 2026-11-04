"""No late ready tone or presentation state after a blocked voice callback."""
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from robot.domains.voice import VoiceDomain
from robot.lifecycle import LifecycleMixin
from robbi.resources import ExecutionResources


class Controller(LifecycleMixin, VoiceDomain):
    def __init__(self):
        self._init_lifecycle()
        self._log = Mock()
        self._touch_activity = Mock()
        self._publish_event = Mock()
        self._conversation_lock = threading.Lock()
        self._set_presentation = Mock()
        self.assets = Mock()
        self.assets.get.return_value = Path("happy.png")
        self._pick_listening_sound = lambda: Path("ready.wav")
        self.media = Mock()
        self.media.duration_s.return_value = 0.5
        self.microphone = Mock()
        self.behavior = Mock()


def run_blocked_callback(controller, method, blocked_target, return_value):
    entered, release = threading.Event(), threading.Event()
    def block(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return return_value
    blocked_target.side_effect = block
    thread = threading.Thread(target=method)
    thread.start()
    try:
        assert entered.wait(1)
        controller.cancel_actions()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        release.set()


def test_stop_during_ready_asset_lookup_prevents_face_and_tone():
    controller = Controller()
    run_blocked_callback(controller, lambda: controller._handle_wake_phrase_detected("wake"),
                         controller.assets.get, Path("happy.png"))
    controller.media.show_face.assert_not_called()
    controller.media.play_sound.assert_not_called()


def test_stop_during_ready_duration_lookup_prevents_tone():
    controller = Controller()
    run_blocked_callback(controller, lambda: controller._handle_wake_phrase_detected("wake"),
                         controller.media.duration_s, 0.5)
    controller.media.play_sound.assert_not_called()
    controller.microphone.extend_post_wake_block.assert_not_called()


def test_ready_tone_retains_cancellation_through_audio_admission():
    controller = Controller()
    initial_token = controller._action_token()
    accepted = []
    def admit(path, wait=False, *, cancel_event=None):
        controller.cancel_actions()
        assert cancel_event is initial_token[1]
        if not cancel_event.is_set():
            accepted.append(path)
    controller.media.play_sound.side_effect = admit
    controller._handle_wake_phrase_detected("wake")
    assert accepted == []
    assert not any("last_sound" in call.kwargs for call in controller._set_presentation.call_args_list)


@pytest.mark.parametrize("method", ["wake", "finished"])
def test_stop_during_display_io_prevents_following_effects_and_state(method):
    controller = Controller()
    callback = (lambda: controller._handle_wake_phrase_detected("wake")) if method == "wake" else controller._handle_voice_listening_finished
    run_blocked_callback(controller, callback, controller.media.show_face, None)
    controller.media.play_sound.assert_not_called()
    controller.behavior.set_current_face.assert_not_called()
    controller._set_presentation.assert_not_called()


def test_stop_during_listening_finished_asset_lookup_skips_face():
    controller = Controller()
    run_blocked_callback(controller, controller._handle_voice_listening_finished,
                         controller.assets.get, Path("happy.png"))
    controller.media.show_face.assert_not_called()


def test_previously_cancelled_inherited_context_has_no_effect():
    controller = Controller()
    controller._action_local.token = controller._action_token()
    controller.cancel_actions()
    controller._handle_wake_phrase_detected("wake")
    controller._handle_voice_listening_finished()
    controller.assets.get.assert_not_called()
    controller.media.play_sound.assert_not_called()


@pytest.mark.parametrize("saturation", ["lane", "shared_budget", "shutdown"])
def test_optional_thinking_sound_rejection_does_not_abort_conversation(saturation):
    controller = Controller()
    resources = ExecutionResources(task_limit=1 if saturation == "shared_budget" else 16)
    controller._effect_executor = resources.lane("effects")
    release = threading.Event()
    blockers = []
    try:
        if saturation == "shutdown":
            controller._effect_executor.shutdown(wait=False, cancel_futures=True)
        elif saturation == "shared_budget":
            blockers.append(resources.lane("research").submit(release.wait, 2))
        else:
            # Two running slots plus two pending slots fill only the cosmetic
            # lane; foreground provider capacity remains available.
            blockers = [controller._effect_executor.submit(release.wait, 2) for _ in range(4)]
        controller._play_delayed_thinking_sound(Path("thinking.wav"), request_id=17)
        controller._publish_event.assert_called_once_with("presentation.optional_skipped", {
            "kind": "thinking_sound", "reason": "stopping" if saturation == "shutdown" else "busy",
            "request_id": 17,
        })
        controller.media.play_sound.assert_not_called()
        if saturation == "shared_budget":
            budget_released = threading.Event()
            blockers[0].add_done_callback(lambda _: budget_released.set())
            release.set()
            assert budget_released.wait(1)
        # The caller continues to the substantive provider stage; rejected
        # cosmetic work does not leak an admission permit or executor error.
        provider = Mock(return_value={"speak": "answer"})
        assert resources.lane("llm-live").submit(provider).result(timeout=1) == {"speak": "answer"}
        provider.assert_called_once_with()
    finally:
        release.set()
        resources.shutdown()
