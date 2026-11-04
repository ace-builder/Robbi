"""Follow-up admission is isolated from wake-word commands and stays quiet on doubt."""
import json
import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_spoken_followup import Controller

from robbi.configuration.settings import settings
from robbi.engine import microphone
from robbi.engine.domains.voice import VoiceDomain
from robbi.engine.llm import LLMClient
from robbi.services.spoken_followup import accepted, candidate_reason, evidence


@pytest.fixture
def mic(monkeypatch):
    monkeypatch.setattr(settings, 'voice_enabled', True)
    monkeypatch.setattr(settings, 'conversation_followup_enabled', True)
    device = microphone.MicrophoneService()
    device._running = True
    device._command_callback = Mock()
    device._followup_check_callback = Mock(return_value=True)
    return device


def context(mic, reply='Would you like to hear tomorrow’s forecast?'):
    return {'reply': reply, 'token': mic.followup_token()}


@pytest.mark.parametrize('text,reply', [
    ('', 'Hello'), ('[noise]', 'Hello'), ('um uh hmm', 'Hello'),
    ('Thank you for watching.', 'Hello'), ('123', 'Hello'),
    ('FF FF FF FF FF', 'Would you like to continue?'),
    ('Tomorrow will be rainy.', 'Tomorrow will be rainy.'),
    ('will be rainy tomorrow', 'It will be rainy tomorrow in Berlin.'),
    ('And tomorrow?', ''), ('a'*801, 'Hello'),
])
def test_local_rejects_non_words_artifacts_echo_and_missing_context(text, reply):
    assert candidate_reason(text, reply)


@pytest.mark.parametrize('text,reply', [
    ('Yes.', 'Would you like the forecast?'), ('Nein danke', 'Soll ich weitermachen?'),
    ('And tomorrow?', 'It will rain today.'), ('Why?', 'The battery is low.'),
    ('Robbi, tell me a joke.', 'Hello.'),
])
def test_real_short_responses_reach_semantic_check(text, reply):
    assert candidate_reason(text, reply) == ''
    assert json.loads(evidence(text, reply))['candidate_transcript'] == text


@pytest.mark.parametrize('verdict', [None, True, {}, {'directed': 'true', 'confidence': 1},
    {'directed': True, 'confidence': True}, {'directed': True, 'confidence': .89},
    {'directed': False, 'confidence': 1}, {'directed': True, 'confidence': float('nan')},
    {'directed': True, 'confidence': float('inf')}, {'directed': True, 'confidence': 2},
    {'directed': True, 'confidence': 1, 'drive': 'forward'}, {'speak': 'Yes'}])
def test_strict_verdict_never_executes_an_action_or_uncertain_decision(verdict):
    assert not accepted(verdict)


def test_valid_confident_verdict():
    assert accepted({'directed': True, 'confidence': .95})


def test_rejected_background_speech_never_reaches_commands_or_last_transcript(mic):
    mic._transcribe = Mock(return_value='Welcome back to the evening news.')
    mic._followup_check_callback.return_value = False
    mic._mode = 'processing_command'
    mic._last_command_transcript = 'previous accepted command'
    mic._process_command_capture(b'wav', followup=context(mic))
    mic._transcribe.assert_called_once_with('followup', b'wav')
    mic._command_callback.assert_not_called()
    assert mic._last_command_transcript == 'previous accepted command'
    assert mic._mode == 'wake'
    assert mic._pending_followup is None


def test_empty_followup_has_no_stt_retry_or_spoken_try_again(mic):
    mic._transcribe = Mock(return_value='')
    mic._process_command_capture(b'wav', followup=context(mic))
    assert mic._transcribe.call_count == 1
    mic._followup_check_callback.assert_not_called()
    mic._command_callback.assert_not_called()


def test_accepted_contextual_response_is_dispatched_once(mic):
    mic._transcribe = Mock(return_value='And tomorrow?')
    mic._process_command_capture(b'wav', followup=context(mic))
    mic._command_callback.assert_called_once_with('And tomorrow?', b'wav')
    assert mic._last_command_transcript == 'And tomorrow?'


def test_missing_gate_and_provider_failure_fail_quietly(mic):
    mic._transcribe = Mock(return_value='Yes')
    for gate in (None, Mock(side_effect=TimeoutError('private transcript'))):
        mic._followup_check_callback = gate
        mic._process_command_capture(b'wav', followup=context(mic))
    mic._command_callback.assert_not_called()


def test_explicit_wake_command_does_not_need_addressedness_or_context(mic):
    mic._transcribe = Mock(side_effect=['', 'Turn left'])
    mic._followup_check_callback = Mock(side_effect=AssertionError('not for explicit input'))
    mic._process_command_capture(b'wav')
    assert [call.args[0] for call in mic._transcribe.call_args_list] == ['command', 'command_retry']
    mic._command_callback.assert_called_once_with('Turn left', b'wav')


@pytest.mark.parametrize('cancel', ['stop', 'followup', 'wake_off', 'disabled'])
def test_late_semantic_result_cannot_revive_cancelled_input(mic, monkeypatch, cancel):
    entered, release = threading.Event(), threading.Event()
    mic._transcribe = Mock(return_value='Yes')
    def gate(*args):
        entered.set()
        assert release.wait(2)
        return True
    mic._followup_check_callback = gate
    thread = threading.Thread(target=mic._process_command_capture,
                              args=(b'wav',), kwargs={'followup': context(mic)})
    thread.start()
    try:
        assert entered.wait(1)
        if cancel == 'stop':
            mic.cancel_pending_input()
        elif cancel == 'followup':
            mic.cancel_followup()
        elif cancel == 'wake_off':
            mic.set_wake_enabled(False)
        else:
            monkeypatch.setattr(settings, 'conversation_followup_enabled', False)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    mic._command_callback.assert_not_called()


def test_deadline_expired_after_semantic_check_is_silent(mic, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(microphone, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    mic._transcribe = Mock(return_value='Yes')
    def gate(*args):
        clock[0] = 125.
        return True
    mic._followup_check_callback = gate
    mic._process_command_capture(b'wav', followup=context(mic), deadline=120.)
    mic._command_callback.assert_not_called()
    assert mic._mode == 'wake'


def test_followup_start_requires_sustained_stronger_energy(mic, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(microphone, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    assert mic.request_followup(5., reply='Hello')
    assert mic._apply_pending_followup(clock[0])
    threshold = mic._current_speech_threshold()
    def chunk(level):
        clock[0] += mic._chunk_ms/1000
        mic._handle_chunk(struct.pack('<h', int(level)) * (mic._chunk_bytes//2))
    clock[0] += .2
    for _ in range(5):
        chunk(threshold * 1.2)
    assert mic._capture is None
    chunk(threshold * 2)
    if mic._chunk_ms < 160:
        assert mic._capture is None
    for _ in range(5):
        chunk(threshold * 2)
    assert mic._capture is not None
    assert mic._capture['followup']['reply'] == 'Hello'
    assert mic._capture['followup']['token'] == mic.followup_token()


def test_click_and_followup_timeout_need_no_stt(mic, monkeypatch):
    mic._transcribe = Mock()
    mic.request_followup(5., reply='Hello')
    now = microphone.time.monotonic()
    mic._apply_pending_followup(now)
    mic._handle_idle_chunk(b'', now+6, 0, False)
    assert mic._mode == 'wake'
    mic._transcribe.assert_not_called()
    capture = {'mode': 'command', 'followup': context(mic), 'speech_chunks': 1,
               'chunks': [b'click'] * 30}
    assert not mic._capture_has_enough_speech(capture)


def test_other_audio_aborts_followup_capture(mic):
    mic._mode = 'armed'
    mic._armed_followup = context(mic)
    mic._begin_capture('command', microphone.time.monotonic())
    mic._can_listen_callback = lambda: False
    mic._handle_chunk(b'\0' * mic._chunk_bytes)
    assert mic._capture is None
    assert mic._mode == 'wake'


def test_fresh_wake_restores_explicit_command_path(mic):
    mic._armed_followup = context(mic)
    mic._process_local_wake_detection({})
    assert mic._armed_followup is None
    mic._begin_capture('command', microphone.time.monotonic())
    assert mic._capture['followup'] is None


def test_followup_transcription_not_logged_or_published_before_admission():
    controller = SimpleNamespace(llm=SimpleNamespace(transcribe=Mock(return_value='private background')),
                                 _log=Mock(), _publish_event=Mock())
    assert VoiceDomain._transcribe_microphone_audio(controller, 'followup', b'wav') == 'private background'
    controller._publish_event.assert_not_called()
    controller._log.info.assert_not_called()


def test_gate_reuses_speech_target_and_does_not_accept_provider_errors(monkeypatch):
    monkeypatch.setattr(settings, 'conversation_followup_enabled', True)
    controller = Controller()
    controller.llm.classify_followup = Mock(return_value={'directed': True, 'confidence': .99})
    assert controller._accept_spoken_followup('And tomorrow?', 'It is rainy today.')
    assert controller.llm.classify_followup.call_args.kwargs == {
        'provider': settings.speech_input_provider, 'model': settings.speech_input_model}
    controller.llm.classify_followup.return_value = {'error': 'provider_unavailable'}
    assert not controller._accept_spoken_followup('And tomorrow?', 'It is rainy today.')
    controller.llm.classify_followup.reset_mock()
    assert not controller._accept_spoken_followup('uh', 'Hello')
    controller.llm.classify_followup.assert_not_called()
    controller._filming_mode = True
    assert not controller._accept_spoken_followup('And tomorrow?', 'It is rainy today.')


@pytest.mark.parametrize('provider,model', [('openai','gpt-6-luna'), ('local','local-model')])
def test_provider_gate_is_small_tool_free_and_does_not_trace_or_record(provider, model, monkeypatch):
    import robbi.engine.llm as module
    trace = Mock(side_effect=AssertionError('No transcript trace'))
    monkeypatch.setattr(module, 'trace_event', trace)
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"directed":true,"confidence":0.95}'))]))
    client = object.__new__(LLMClient)
    client.recording = Mock()
    client._ensure_chat_client = lambda provider: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    verdict = client.classify_followup('And tomorrow?', 'Today is rainy.', provider=provider, model=model)
    assert accepted(verdict)
    options = create.call_args.kwargs
    assert options['model'] == model
    assert options['timeout'].read == 3.
    assert len(options['messages']) == 2
    assert not {'tools', 'tool_choice'} & options.keys()
    assert 'Drive' not in options['messages'][0]['content']
    assert not client.recording.mock_calls
    assert not trace.mock_calls
    create.side_effect = TimeoutError('private transcript')
    assert client.classify_followup('And tomorrow?', 'Today is rainy.', provider=provider, model=model) == {}


def test_gemini_uses_same_admission_policy(monkeypatch):
    monkeypatch.setattr(settings, 'gemini_api_key', 'offline-test-key')
    client = object.__new__(LLMClient)
    client._gemini_request = Mock(return_value={'candidates': [{'content': {'parts': [
        {'text': '{"directed":true,"confidence":0.95}'}]}}]})
    assert accepted(client.classify_followup('Yes', 'Want the forecast?', provider='gemini', model='test-model'))
    assert client._gemini_request.call_args.kwargs == {'timeout_s': 3.}
    payload = client._gemini_request.call_args.args[1]
    assert payload['generationConfig']['maxOutputTokens'] == 128
    assert 'tools' not in payload


def test_completed_playback_supplies_exact_reply_not_future_or_failed_speech(monkeypatch):
    monkeypatch.setattr(settings, 'conversation_followup_enabled', True)
    monkeypatch.setattr(settings, 'voice_enabled', True)
    controller = Controller()
    controller._speak_text('Would you like to hear more?')
    assert controller.microphone._pending_followup is None
    controller.media.finish()
    assert controller.microphone._pending_followup['reply'] == 'Would you like to hear more?'


def test_new_defaults_are_brief_and_enabled():
    from robbi.configuration.schema import Settings
    defaults = Settings()
    assert defaults.conversation_followup_enabled is True
    assert defaults.conversation_followup_timeout_s == 5.


def test_explicit_capture_speech_accounting_is_unchanged(mic):
    mic._speech_streak = 8
    mic._mode = 'armed'
    mic._armed_followup = None
    mic._begin_capture('command', microphone.time.monotonic())
    assert mic._capture['speech_chunks'] == 0


@pytest.mark.parametrize('directed', [True, False])
def test_complete_followup_path_only_dispatches_accepted_words(monkeypatch, directed):
    monkeypatch.setattr(settings, 'conversation_followup_enabled', True)
    monkeypatch.setattr(settings, 'voice_enabled', True)
    controller = Controller()
    mic = controller.microphone
    controller.think_and_respond = Mock()
    controller.llm.transcribe = Mock(return_value='And tomorrow?')
    controller.llm.classify_followup = Mock(return_value={'directed': directed, 'confidence': .99})
    controller._handle_voice_listening_finished = Mock()
    controller._configure_microphone()
    mic._processing_local.context = controller._action_token()
    controller._speak_text('Today will be rainy.')
    controller.media.finish()
    assert mic._apply_pending_followup(microphone.time.monotonic())
    mic._speech_streak = 4
    mic._begin_capture('command', microphone.time.monotonic())
    capture = mic._capture
    from test_followup_acoustics import pcm, speech_like
    audio = pcm(speech_like(2.))
    capture['chunks'] = [audio[i:i+mic._chunk_bytes] for i in range(0, len(audio), mic._chunk_bytes)]
    capture['speech_chunks'] = 10
    # Run the existing bounded worker callback inline, preserving its arguments.
    mic._submit_transcription = lambda callback, wav, name, **options: callback(wav, **options)
    mic._finalize_capture(capture)
    controller._handle_voice_listening_finished.assert_not_called()
    if directed:
        controller.think_and_respond.assert_called_once_with('And tomorrow?', source='speech_input')
    else:
        controller.think_and_respond.assert_not_called()
    assert mic._mode == 'wake'
    assert mic._pending_followup is None
