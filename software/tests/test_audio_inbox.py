import threading
import time
from unittest.mock import Mock

from robbi.audio.inbox import AudioInbox


def test_capture_chunk_contains_complete_pcm_frames_at_nonstandard_rate(monkeypatch):
    from robot import microphone
    monkeypatch.setattr(microphone.settings, "mic_sample_rate", 8030)
    monkeypatch.setattr(microphone.settings, "voice_chunk_ms", 20)
    mic = microphone.MicrophoneService()
    assert mic._chunk_bytes == 322
    assert mic._chunk_bytes % 2 == 0


def test_overrun_discards_entire_old_utterance_and_invalidates_inflight():
    inbox = AudioInbox(capacity=2)
    inbox.push(b"inflight")
    inflight = inbox.receive()
    inbox.push(b"old1")
    inbox.push(b"old2")
    inbox.push(b"fresh")
    assert not inbox.current(inflight)
    assert inbox.receive().data == b"fresh"
    assert inbox.snapshot()["dropped_chunks"] == 2
    assert inbox.snapshot()["overruns"] == 1


def test_conditional_push_rejects_read_crossing_reset_without_dropping_fresh_input():
    inbox = AudioInbox()
    old_epoch = inbox.epoch
    new_epoch = inbox.reset("Stop")
    assert inbox.push(b"fresh", expected_epoch=new_epoch)
    assert not inbox.push(b"read-across-stop", expected_epoch=old_epoch)
    packet = inbox.receive()
    assert packet.data == b"fresh" and inbox.current(packet)
    assert inbox.snapshot()["dropped_chunks"] == 1


def test_stale_audio_is_not_replayed_and_close_wakes_consumer():
    now = [0]
    inbox = AudioInbox(clock=lambda: now[0])
    inbox.push(b"old")
    now[0] = 1
    result = []
    thread = threading.Thread(target=lambda: result.append(inbox.receive()))
    thread.start()
    deadline = time.monotonic() + 1
    while not inbox.snapshot()["overruns"] and time.monotonic() < deadline:
        threading.Event().wait(.001)
    inbox.close()
    thread.join(1)
    assert result == [None]
    assert not inbox.push(b"after stop")


def test_blocked_wake_processing_does_not_block_capture_or_deliver_stale_detection(monkeypatch):
    from robot import microphone
    monkeypatch.setattr(microphone.settings, "voice_enabled", True)
    monkeypatch.setattr(microphone.settings, "voice_wake_engine", "local_wake")
    mic = microphone.MicrophoneService()
    mic._running = True
    mic._wake_prepared = True
    mic._wake_configured = "local_wake"
    entered, release = threading.Event(), threading.Event()
    detector = Mock()
    detector.available.return_value = True
    def delayed(*_):
        entered.set()
        release.wait(2)
        return {"wakeword": "old"}
    detector.process_chunk.side_effect = delayed
    mic._local_wake._instance = detector
    mic._local_wake._attempted = True
    mic._wake_callback = Mock()
    mic._processing_thread = threading.Thread(target=mic._processing_loop)
    mic._processing_thread.start()
    try:
        mic._audio_input.push(b"\0\0" * 1600)
        assert entered.wait(1)
        for _ in range(10):
            mic._audio_input.push(b"\0\0" * 1600)
        assert mic._audio_input.snapshot()["overruns"]
        assert not mic._transcription_current((mic._generation, 0), time.monotonic() + 1)
        # Closing invalidates the in-flight detector result before releasing it.
        mic._running = False
        mic._audio_input.close()
        release.set()
        mic._processing_thread.join(1)
        mic._wake_callback.assert_not_called()
    finally:
        release.set()
        mic.stop()


def test_capture_loop_only_enqueues_audio(monkeypatch):
    from robot import microphone
    mic = microphone.MicrophoneService()
    mic._running = True
    mic._cached_hardware_probe = lambda: None
    mic._stream_proc = Mock()
    mic._start_stream = lambda: True
    mic._stop_stream = lambda: None
    mic._handle_chunk = Mock(side_effect=AssertionError("Inference on capture thread"))
    def read(*_):
        mic._running = False
        return b"\0" * mic._chunk_bytes
    mic._read_chunk = read
    mic._audio_input.push = Mock(return_value=True)
    mic._capture_loop()
    mic._audio_input.push.assert_called_once()
    mic._handle_chunk.assert_not_called()


def test_capture_read_spanning_stop_is_rejected_before_wake_history():
    from robot.microphone import MicrophoneService
    from robot.lifecycle import LifecycleMixin

    mic = MicrophoneService()
    controller = LifecycleMixin()
    controller._init_lifecycle()
    controller.microphone = mic
    mic._input_context_callback = controller._action_token
    mic._running = True
    mic._cached_hardware_probe = lambda: None
    mic._stream_proc = Mock()
    mic._start_stream = lambda: True
    mic._stop_stream = lambda: None
    reads = []
    accepted = []
    push = mic._audio_input.push
    def observed_push(data, **kwargs):
        result = push(data, **kwargs)
        if result:
            accepted.append(mic._audio_input.receive())
        return result
    mic._audio_input.push = observed_push
    def read(*_):
        reads.append(True)
        if len(reads) == 1:
            controller.cancel_actions()  # In the middle of the blocking read.
            return b"\1\0" * (mic._chunk_bytes // 2)
        mic._running = False
        return b"\2\0" * (mic._chunk_bytes // 2)
    mic._read_chunk = read
    mic._capture_loop()
    assert len(reads) == 2
    assert len(accepted) == 1
    assert accepted[0].data == b"\2\0" * (mic._chunk_bytes // 2)
    assert accepted[0].context == controller._action_token()
    assert not accepted[0].context[1].is_set()


def test_slow_wake_reset_resumes_fresh_audio_without_reloading_models():
    from robot.microphone import MicrophoneService, _LazyWakeEngine

    now = [0.0]
    mic = MicrophoneService()
    mic._running = True
    mic._audio_input = AudioInbox(clock=lambda: now[0])
    resets = []
    class Model:
        def reset(self):
            resets.append(True)
            now[0] += 0.8  # Longer than the queue's 500 ms freshness limit.
            mic._audio_input.push(b"captured-during-reset")
    def factory():
        raise AssertionError("A capture gap must never construct another model")
    mic._nano_wake = _LazyWakeEngine("nano", factory)
    mic._nano_wake._instance = Model()
    mic._nano_wake._attempted = True
    initial_wait, waiting_after_reset, handled = threading.Event(), threading.Event(), threading.Event()
    receive = mic._audio_input.receive
    def observed_receive():
        if resets:
            waiting_after_reset.set()
        else:
            initial_wait.set()
        return receive()
    mic._audio_input.receive = observed_receive
    seen = []
    def handle(data):
        seen.append(data)
        handled.set()
    mic._handle_chunk = handle
    thread = threading.Thread(target=mic._processing_loop)
    thread.start()
    try:
        assert initial_wait.wait(1)
        mic._audio_input.reset("Simulated capture gap")
        mic._audio_input.push(b"first-after-gap")
        assert waiting_after_reset.wait(1)
        assert mic._audio_input.snapshot()["queued_chunks"] == 0
        mic._audio_input.push(b"fresh-after-model-reset")
        assert handled.wait(1)
        assert len(resets) == 1
        assert seen == [b"fresh-after-model-reset"]
    finally:
        mic._running = False
        mic._audio_input.close()
        thread.join(2)
        assert not thread.is_alive()
