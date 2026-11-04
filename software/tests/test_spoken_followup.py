"""Attention begins after successful speech, without workers polling playback."""
from concurrent.futures import Future
import logging
import threading
from types import SimpleNamespace

import pytest

from robbi.app_runtime.robot_port import RuntimeRobotPort
from robbi.orchestration import APP, AUTONOMY, Intent, Orchestrator, intent_scope
from robot import media, microphone
from robot.config import settings
from robot.domains.voice import VoiceDomain
from robot.lifecycle import LifecycleMixin
from robot.presentation_pipeline import PresentationPipelineMixin


class SpeechOutput:
    def __init__(self):
        self.handles = []
        self.playing = False

    def speak_wav_bytes(self, wav, **options):
        self.playing = True
        handle = media.PlaybackHandle()
        self.handles.append(handle)
        return handle

    def finish(self, code=0):
        self.playing = False
        self.handles[-1].set_result(code)

    def is_audio_playing(self):
        return self.playing

    def is_video_playing(self):
        return False

    def health(self):
        return {"audio": {"available": True}}


class Immediate:
    def submit(self, callback, *args):
        result = Future()
        try:
            result.set_result(callback(*args))
        except BaseException as exc:
            result.set_exception(exc)
        return result


class Controller(VoiceDomain, PresentationPipelineMixin, LifecycleMixin):
    def __init__(self):
        self._init_lifecycle()
        self._log = logging.getLogger(__name__)
        self._conversation_lock = threading.Lock()
        self._conversation_turns = 0
        self._filming_mode = self._voice_processing = False
        self.focus_timer = SimpleNamespace(is_active=lambda: False)
        self.microphone = microphone.MicrophoneService()
        self.microphone._running = True
        self.microphone.configure(can_listen_callback=self._can_listen_for_voice)
        self.media = SpeechOutput()
        self.llm = SimpleNamespace(synthesize=lambda text: b"wav")
        self.events = []
        self._voice_executor = Immediate()
        self._touch_activity = lambda: None
        self._set_presentation = lambda **kwargs: None
        self._update_pipeline_metrics = lambda *args, **kwargs: None
        self.execute_action = lambda action: self._speak_text(action["speak"])

    def _publish_event(self, name, data):
        self.events.append((name, data))


@pytest.fixture
def subject(monkeypatch):
    monkeypatch.setattr(settings, "voice_enabled", True)
    monkeypatch.setattr(settings, "conversation_followup_enabled", True)
    monkeypatch.setattr(settings, "conversation_followup_timeout_s", 10.)
    monkeypatch.setattr(settings, "conversation_max_turns", 0)
    clock = [100.]
    monkeypatch.setattr(microphone, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    controller = Controller()
    return controller, clock


def tick(subject):
    controller, _ = subject
    mic = controller.microphone
    mic._handle_chunk(b"\0" * mic._chunk_bytes)


def test_long_reply_starts_full_window_only_after_actual_completion(subject):
    controller, clock = subject
    controller._speak_text("A lengthy answer")
    assert controller.microphone._pending_followup is None
    clock[0] += 45
    tick(subject)
    assert controller.microphone._mode == "wake"
    controller.media.finish()
    assert controller.microphone._pending_followup is not None
    assert controller.microphone._armed_until == 0
    tick(subject)
    assert controller.microphone._mode == "armed"
    assert controller.microphone._armed_until == pytest.approx(155.15)


def test_unclear_followup_retries_once_then_invalidates_attention(subject):
    controller, _ = subject
    controller.assets = SimpleNamespace(get=lambda *args, **kwargs: None)
    controller._handle_voice_command_transcript('', b'')
    assert len(controller.media.handles) == 1
    controller.media.finish()
    tick(subject)
    assert controller.microphone._mode == 'armed'
    controller._handle_voice_command_transcript('', b'')
    controller._handle_voice_command_transcript('', b'')
    tick(subject)
    assert len(controller.media.handles) == 1
    assert controller.microphone._mode == 'wake'
    assert controller.microphone._pending_followup is None
    controller._pick_listening_sound = lambda: None
    controller._handle_wake_phrase_detected('hey robbi')
    controller._handle_voice_command_transcript('', b'')
    assert len(controller.media.handles) == 2


def test_disabled_followup_stays_quiet_on_empty_input(subject, monkeypatch):
    controller, _ = subject
    monkeypatch.setattr(settings, 'conversation_followup_enabled', False)
    controller._handle_voice_command_transcript('', b'')
    assert not controller.media.handles
    assert controller.microphone._pending_followup is None


def test_slow_tts_does_not_open_attention_early(subject):
    controller, clock = subject
    entered, release = threading.Event(), threading.Event()

    def synthesize(text):
        entered.set()
        assert release.wait(2)
        return b"wav"

    controller.llm.synthesize = synthesize
    thread = threading.Thread(target=controller._speak_text, args=("Thinking",))
    thread.start()
    try:
        assert entered.wait(1)
        clock[0] += 40
        assert controller.microphone._pending_followup is None
        assert controller.microphone._armed_until == 0
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    controller.media.finish()
    tick(subject)
    assert controller.microphone._armed_until == pytest.approx(150.15)


@pytest.mark.parametrize("priority, owner", [(AUTONOMY, "proactive:question"), (APP, "app:reply"),
                                            (AUTONOMY, "background:result")])
def test_non_voice_speech_reopens_attention_without_turn_count(subject, priority, owner):
    controller, _ = subject
    with intent_scope(Intent(owner, priority)):
        controller._speak_text("Would you like to know more?")
    controller.media.finish()
    tick(subject)
    assert controller.microphone._mode == "armed"
    assert controller._conversation_turns == 0


def test_completion_never_submits_a_waiter_to_voice_executor(subject):
    controller, _ = subject
    controller._voice_executor = SimpleNamespace(submit=lambda *args: pytest.fail("No polling task"))
    controller._speak_text("Hello")
    controller.media.finish()
    tick(subject)
    assert controller.microphone._mode == "armed"


@pytest.mark.parametrize("code", [-1, 1, None])
def test_failed_cancelled_or_unknown_audio_does_not_reopen_attention(subject, code):
    controller, _ = subject
    controller._speak_text("Hello")
    controller.media.finish(code)
    assert controller.microphone._pending_followup is None


def test_infinite_turn_setting_allows_later_turns(subject):
    controller, _ = subject
    controller._conversation_turns = 100
    controller._speak_text("We can keep talking")
    controller.media.finish()
    tick(subject)
    assert controller.microphone._mode == "armed"


def test_explicit_turn_limit_still_ends_attention(subject, monkeypatch):
    controller, _ = subject
    monkeypatch.setattr(settings, "conversation_max_turns", 3)
    controller._conversation_turns = 3
    controller._speak_text("Last configured turn")
    controller.media.finish()
    assert controller.microphone._pending_followup is None


@pytest.mark.parametrize("when", ["before_completion", "pending"])
def test_stop_invalidates_late_or_pending_followup(subject, when):
    controller, _ = subject
    controller._speak_text("Almost done")
    if when == "pending":
        controller.media.finish()
    controller.cancel_actions()
    controller.media.finish()
    assert controller.microphone._pending_followup is None
    assert controller.microphone._mode == "wake"


def test_stop_during_tts_preparation_prevents_late_rearm(subject):
    controller, _ = subject

    def synthesize(text):
        controller.cancel_actions()
        return b"wav"

    controller.llm.synthesize = synthesize
    assert controller._speak_text("Old response") is None
    assert not controller.media.handles
    assert controller.microphone._pending_followup is None


def test_wake_off_then_on_invalidates_prepared_reply(subject):
    controller, _ = subject
    prepared = controller._prepare_tts_audio("Ready")
    controller.microphone.set_wake_enabled(False)
    controller.microphone.set_wake_enabled(True)
    controller._play_prepared_tts(prepared)
    controller.media.finish()
    assert controller.microphone._pending_followup is None


@pytest.mark.parametrize("mode", ["capture", "processing_command", "processing_wake"])
def test_attention_request_preserves_recording_or_transcription(subject, mode):
    controller, clock = subject
    mic = controller.microphone
    capture = {"sentinel": "unfinished utterance"}
    if mode == "capture":
        mic._capture = capture
    else:
        mic._mode = mode
    controller._speak_text("Just finished")
    controller.media.finish()
    assert not mic._apply_pending_followup(clock[0])
    assert mic._capture is capture if mode == "capture" else mic._mode == mode
    assert mic._pending_followup is not None
    mic._capture, mic._mode = None, "wake"
    assert mic._apply_pending_followup(clock[0])


def test_followup_waits_for_audio_and_foreground_voice_work_without_consuming_window(subject):
    controller, clock = subject
    controller._speak_text("One moment")
    controller.media.finish()
    controller._voice_processing = True
    clock[0] += 2
    tick(subject)
    assert controller.microphone._armed_until == 0
    controller._voice_processing = False
    controller.media.playing = True
    tick(subject)
    assert controller.microphone._armed_until == 0
    controller.media.playing = False
    tick(subject)
    assert controller.microphone._armed_until == pytest.approx(112.15)


def test_duplicate_completions_are_coalesced_and_echo_preroll_cleared(subject):
    controller, clock = subject
    controller._speak_text("First")
    controller.media.finish()
    first = controller.microphone._pending_followup
    clock[0] += .5
    controller._speak_text("Second")
    controller.media.finish()
    assert controller.microphone._pending_followup is not first
    controller.microphone._recent_chunks.extend([b"robot echo"])
    tick(subject)
    assert not controller.microphone._recent_chunks
    assert controller.microphone._pending_followup is None
    assert controller.microphone._armed_until == pytest.approx(110.65)


def test_new_utterance_invalidates_older_reply_notification(subject):
    controller, clock = subject
    controller._speak_text("Older reply")
    controller.microphone._begin_capture("command", clock[0])
    capture = controller.microphone._capture
    controller.media.finish()
    assert controller.microphone._pending_followup is None
    assert controller.microphone._capture is capture


def test_goodbye_acknowledgement_and_older_reply_do_not_rearm(subject):
    controller, _ = subject
    controller._speak_text("Older reply")
    old = controller.media.handles[-1]
    queued = []

    class Deferred:
        def submit(self, callback, *args):
            queued.append((callback, args))
            return Future()

    controller.execute_action = lambda action: controller._submit_guarded(Deferred(), controller._speak_text, action["speak"])
    controller._handle_voice_command_transcript("bye", b"wav")
    assert not controller._voice_processing
    callback, args = queued.pop()
    callback(*args)
    old.set_result(0)
    controller.media.finish()
    assert controller.microphone._pending_followup is None
    assert controller.microphone._mode == "wake"


@pytest.mark.parametrize("phrase", ["stop", "CANCEL!", "stopp", "Abbrechen."])
def test_exact_spoken_stop_bypasses_voice_queue_and_stops_each_component(subject, phrase):
    controller, _ = subject
    controller._voice_executor = SimpleNamespace(submit=lambda *args: pytest.fail("Stop cannot wait for voice worker"))
    stopped = []

    def failed_motion():
        stopped.append("motion")
        raise RuntimeError("missing optional motor")

    controller.motion = SimpleNamespace(stop=failed_motion)
    controller.slam = SimpleNamespace(stop=lambda: stopped.append("mapping"))
    controller.media.stop_audio = lambda: stopped.append("audio")
    initial = controller._action_token()
    controller._action_local.token = initial
    controller._handle_voice_command_transcript(phrase, b"wav")
    assert initial[1].is_set()
    assert stopped == ["motion", "mapping", "audio"]
    assert len(controller.media.handles) == 1
    controller.media.finish()
    assert controller.microphone._pending_followup is None
    assert controller.microphone._mode == "wake"


def test_goodbye_only_ends_attention_without_stopping_other_work(subject):
    controller, _ = subject
    controller.motion = SimpleNamespace(stop=lambda: pytest.fail("Goodbye does not stop motors"))
    controller.slam = SimpleNamespace(stop=lambda: pytest.fail("Goodbye does not stop mapping"))
    controller.media.stop_audio = lambda: pytest.fail("Goodbye does not stop independent audio")
    initial = controller._action_token()
    controller._handle_voice_command_transcript("bye", b"wav")
    controller.media.finish()
    assert not initial[1].is_set()
    assert controller.microphone._pending_followup is None


def test_disabled_or_stopped_microphone_rejects_attention(subject):
    controller, _ = subject
    mic = controller.microphone
    mic._running = False
    assert not mic.request_followup(10)
    mic._running = True
    mic.set_wake_enabled(False)
    assert not mic.request_followup(10)


def test_sdk_speech_uses_same_completion_hook(subject):
    controller, _ = subject
    original = controller.media.speak_wav_bytes

    def completed_audio(wav, **options):
        handle = original(wav, **options)
        controller.media.finish()
        return handle

    controller.media.speak_wav_bytes = completed_audio
    runtime = SimpleNamespace(controller=lambda: controller)
    port = RuntimeRobotPort(runtime)

    class Cancel(threading.Event):
        def __call__(self):
            return self.is_set()

    result = port.speak("An app answer", controller._action_token(), Cancel())
    assert result["ok"]
    tick(subject)
    assert controller.microphone._mode == "armed"


def test_playback_callbacks_are_thread_safe_idempotent_and_isolated():
    handle = media.PlaybackHandle()
    calls = []
    handle.add_done_callback(lambda result: calls.append(result.returncode))
    handle.add_done_callback(lambda result: (_ for _ in ()).throw(RuntimeError("bad callback")))
    handle.add_done_callback(lambda result: calls.append("after bad callback"))
    handle.set_result(0)
    handle.set_result(-1)
    handle.add_done_callback(lambda result: calls.append("late registration"))
    assert handle.wait(0) == 0
    assert calls == [0, "after bad callback", "late registration"]


def test_audio_completion_runs_after_output_cleanup_and_lease_release(tmp_path, monkeypatch):
    coordinator = Orchestrator()
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 100)
    entered, finish, notified = threading.Event(), threading.Event(), threading.Event()

    def playback(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return 0

    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", playback)
    player = media.AudioPlayer(coordinator)
    path = tmp_path / "speech.wav"
    path.write_bytes(b"fixture")
    observed = []
    try:
        with intent_scope(Intent("spoken:reply", APP)):
            handle = player.play_file(path, delete_after=True)
        assert entered.wait(1)

        def complete(result):
            observed.append((result.returncode, path.exists(), player.is_playing(),
                             coordinator.snapshot()["resources"].get("audio")))
            notified.set()

        handle.add_done_callback(complete)
        finish.set()
        assert notified.wait(1)
        assert observed == [(0, False, False, None)]
    finally:
        finish.set()
        player.shutdown()

def test_pending_window_survives_long_queued_audio(subject):
    controller, clock = subject
    controller._speak_text("Here is the first part")
    controller.media.finish()
    controller.media.playing = True
    clock[0] += 45
    tick(subject)
    assert controller.microphone._pending_followup is not None
    assert not controller.microphone.followup_listening()
    controller.media.playing = False
    tick(subject)
    assert controller.microphone._armed_until == pytest.approx(155.15)


def test_later_audio_suspends_open_window_and_reopens_full_duration(subject):
    controller, clock = subject
    controller._speak_text("First part")
    controller.media.finish()
    tick(subject)
    clock[0] += 2
    tick(subject)
    assert controller.microphone.followup_listening()
    # Other output can start after attention was armed, without a speech hook.
    controller.media.playing = True
    tick(subject)
    assert not controller.microphone.followup_listening()
    clock[0] += 45
    tick(subject)
    assert controller.microphone._pending_followup is not None
    controller.media.playing = False
    tick(subject)
    assert controller.microphone._armed_until == pytest.approx(157.15)
    clock[0] += .2
    tick(subject)
    assert controller.microphone.followup_listening()


def test_stop_during_suspended_window_never_reopens(subject):
    controller, clock = subject
    controller._speak_text("Reply")
    controller.media.finish()
    tick(subject)
    clock[0] += 1
    controller.media.playing = True
    tick(subject)
    controller.cancel_actions()
    controller.media.playing = False
    clock[0] += 30
    tick(subject)
    assert controller.microphone._pending_followup is None
    assert not controller.microphone.followup_listening()


def test_cue_notifications_match_actual_window_and_capture(subject):
    controller, clock = subject
    cues = []
    controller.microphone._followup_state_callback = cues.append
    controller._speak_text("Long reply")
    clock[0] += 45
    tick(subject)
    assert cues == []
    controller.media.finish()
    tick(subject)
    assert cues == []  # Echo guard is not an invitation to speak.
    clock[0] += .2
    tick(subject)
    tick(subject)
    assert cues == [True]
    clock[0] += 11
    tick(subject)
    tick(subject)
    assert cues == [True, False]
    assert controller.microphone._armed_followup is None


def test_face_cue_skips_stale_jobs_and_preserves_held_images(subject):
    from pathlib import Path
    controller, clock = subject
    queued, shown = [], []

    class Deferred:
        def submit(self, callback, *args):
            queued.append((callback, args))
            return Future()

    controller._overlay_executor = Deferred()
    controller._presentation_state = {}
    controller._set_presentation = lambda **kw: controller._presentation_state.update(kw)
    controller.assets = SimpleNamespace(get=lambda category, name, **kw: Path(name + '.png'))
    controller.behavior = SimpleNamespace(set_current_face=lambda name: None)
    controller.media.show_face = lambda path, **kw: shown.append(path.name)
    controller.microphone._followup_state_callback = controller._handle_followup_listening_state

    def drain():
        while queued:
            callback, args = queued.pop(0)
            callback(*args)

    controller._speak_text("Ready")
    controller.media.finish()
    tick(subject)
    clock[0] += .2
    tick(subject)
    drain()
    assert shown == ['thinking.png']
    assert controller._presentation_state['thinking']
    clock[0] += 11
    tick(subject)
    drain()
    assert shown == ['thinking.png', 'happy.png']
    assert not controller._presentation_state['thinking']
    controller._speak_text("Look at this image")
    controller.media.finish()
    tick(subject)
    clock[0] += .2
    tick(subject)
    controller._app_display_until = float('inf')
    drain()
    assert len(shown) == 2
    controller._app_display_until = 0
    controller._handle_followup_listening_state(True)
    controller.microphone.cancel_followup()
    tick(subject)
    drain()
    assert len(shown) == 2
