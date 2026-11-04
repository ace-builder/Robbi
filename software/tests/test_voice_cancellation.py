import threading
import time
from unittest.mock import Mock

import pytest

from robot.lifecycle import LifecycleMixin
from robot.microphone import MicrophoneService
from robbi.audio.inbox import AudioInbox
from robbi.jobs import BoundedExecutor


class Controller(LifecycleMixin):
    def __init__(self, mic):
        self._init_lifecycle()
        self.microphone = mic
        self._transcribe_microphone_audio = Mock(return_value="go")
        self._handle_wake_phrase_detected = Mock()
        self._handle_voice_command_transcript = Mock()
        self._handle_voice_listening_finished = Mock()
        self._can_listen_for_voice = lambda: True
        self._configure_microphone()


@pytest.mark.parametrize("method", ["_process_command_capture", "_process_wake_capture"])
def test_stop_discards_stt_response_already_in_flight(method):
    mic = MicrophoneService()
    mic._running = True
    controller = Controller(mic)
    entered, release = threading.Event(), threading.Event()
    def transcribe(*args):
        entered.set()
        assert release.wait(2)
        return "go"
    mic._transcribe = transcribe
    token = controller._action_token()
    mic._processing_local.context = token
    completed = threading.Event()
    original = getattr(mic, method)
    def callback(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            completed.set()
    mic._submit_transcription(callback, b"wav", "test-stt")
    assert entered.wait(1)
    controller.cancel_actions()
    assert mic._running
    release.set()
    assert completed.wait(1)
    controller._handle_voice_command_transcript.assert_not_called()
    controller._handle_wake_phrase_detected.assert_not_called()


def test_callback_that_passed_guard_before_stop_inherits_old_token():
    mic = MicrophoneService()
    controller = Controller(mic)
    entered, release = threading.Event(), threading.Event()
    executed = []
    futures = []
    executor = BoundedExecutor(1, 1, "test-voice-cancel")
    def callback(*_):
        entered.set()
        assert release.wait(2)
        futures.append(controller._submit_guarded(executor, executed.append, "old command"))
    controller._handle_voice_command_transcript = callback
    controller._configure_microphone()
    token = controller._action_token()
    thread = threading.Thread(target=mic._command_callback, args=("go", b"wav"), kwargs={"context": token})
    thread.start()
    try:
        assert entered.wait(1)
        controller.cancel_actions()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert futures[0].result(timeout=1) is None
        assert executed == []
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("method", ["_process_command_capture", "_process_wake_capture"])
def test_cancelled_stt_never_starts_provider_request(method):
    mic = MicrophoneService()
    mic._running = True
    generation = (mic._generation, mic._audio_input.epoch)
    mic.cancel_pending_input()
    mic._transcribe = Mock()
    getattr(mic, method)(b"wav", generation=generation, deadline=time.monotonic() + 5)
    mic._transcribe.assert_not_called()
    # Stop cancels old work, but leaves capture capable of accepting new input.
    assert mic._audio_input.push(b"new")
    assert mic._audio_input.current(mic._audio_input.receive())


def test_audio_packet_preserves_capture_context():
    inbox = AudioInbox()
    token = (7, threading.Event())
    inbox.push(b"audio", context=token)
    assert inbox.receive().context is token


def test_wake_reset_failure_does_not_kill_audio_consumer():
    mic = MicrophoneService()
    mic._running = True
    waiting = threading.Event()
    receive = mic._audio_input.receive
    def observed_receive():
        waiting.set()
        return receive()
    mic._audio_input.receive = observed_receive
    mic._local_wake.reset_stream = Mock(side_effect=RuntimeError("bad optional model"))
    mic._local_wake.unload = Mock()
    handled = threading.Event()
    mic._handle_chunk = lambda _: handled.set()
    thread = threading.Thread(target=mic._processing_loop)
    thread.start()
    try:
        assert waiting.wait(1)
        mic._audio_input.reset()
        mic._audio_input.push(b"new")
        assert handled.wait(1)
        assert "reset failed" in mic._last_error
        mic._local_wake.unload.assert_called_once()
    finally:
        mic._running = False
        mic._audio_input.close()
        thread.join(2)
        assert not thread.is_alive()
