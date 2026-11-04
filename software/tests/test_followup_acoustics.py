"""Offline acoustic regressions: no cloud, private clips, keys or hardware."""
import importlib.util
import io
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from robbi.audio.followup_gate import rejection
from robbi.configuration.settings import settings
from robbi.engine import microphone
from robbi.engine.llm import LLMClient

_path = Path(__file__).resolve().parents[1] / 'tools/qa/followup_audio.py'
_spec = importlib.util.spec_from_file_location('followup_audio_qa', _path)
qa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qa)


def pcm(audio):
    return np.clip(audio * 32767, -32768, 32767).astype('<i2').tobytes()


def speech_like(seconds=1.8):
    t = np.arange(int(16000 * seconds)) / 16000
    envelope = (.05 + np.sin(np.pi * t * 3.2)**4) * np.minimum(1, t/.1)
    return .35 * envelope * (np.sin(t*2*np.pi*220) + .3*np.sin(t*2*np.pi*440))


@pytest.mark.parametrize('seed', [1, 42, 113])
@pytest.mark.parametrize('name', ['silence', 'fan', 'hum', 'clicks', 'dc_fault'])
def test_obvious_non_speech_is_rejected_without_any_provider(seed, name):
    assert rejection(pcm(qa.negatives(seed)[name]), 16000)


@pytest.mark.parametrize('profile', qa.PROFILES)
def test_modulated_voiced_signal_survives_acoustic_effects(profile):
    assert not rejection(pcm(qa.simulate(speech_like(), profile)), 16000)


@pytest.mark.parametrize('name', ['silence', 'fan', 'hum', 'clicks', 'dc_fault'])
def test_noise_replay_never_submits_transcription(name):
    result = qa.replay(qa.negatives()[name])
    assert not result['captures']
    assert result['mode'] == 'wake'
    assert not result['pending']


@pytest.mark.parametrize('data,rate', [(b'',16000), (b'x',16000), (b'\0\0',0),
                                      (b'\0'*960002,16000)],
                         ids=['empty', 'truncated', 'invalid-rate', 'oversized'])
def test_invalid_or_oversized_capture_is_bounded(data, rate):
    assert rejection(data, rate)


def test_short_reply_has_acoustic_energy_without_requiring_a_long_sentence():
    assert not rejection(pcm(qa.simulate(speech_like(.3), 'clean')), 16000)


def test_two_clear_frames_of_a_short_yes_can_reach_the_acoustic_gate():
    mic = microphone.MicrophoneService()
    capture = {'mode':'command', 'followup':{'reply':'Want more?'},
               'speech_chunks':2, 'chunks':[b'\0' * mic._chunk_bytes] * 10}
    assert mic._capture_has_enough_speech(capture)
    # A brief click still cannot pass merely because the coarse frame count did.
    assert rejection(pcm(qa.negatives()['clicks']), 16000)


def test_current_frame_is_not_duplicated_in_preroll():
    mic = microphone.MicrophoneService()
    previous, current = b'\x01\0'*1600, b'\x02\0'*1600
    mic._recent_chunks.extend([previous, current])
    mic._mode = 'armed'
    mic._begin_capture('command', 100.)
    mic._handle_capture_chunk(current, 100., 1000, True)
    assert mic._capture['chunks'] == [previous, current]


def test_stale_followup_result_cannot_reset_newer_command_state(monkeypatch):
    mic = microphone.MicrophoneService()
    mic._running = True
    old = {'reply':'Want more?', 'token':mic.followup_token()}
    mic._followup_epoch += 1
    mic._mode = 'processing_command'
    mic._transcribe = Mock()
    mic._process_command_capture(b'wav', followup=old, deadline=0.)
    assert mic._mode == 'processing_command'
    mic._transcribe.assert_not_called()


def test_cancel_ends_only_the_followup_it_owns():
    mic = microphone.MicrophoneService()
    mic._mode = 'processing_command'
    mic._followup_processing_token = mic.followup_token()
    mic.cancel_followup()
    assert mic._mode == 'wake'
    mic._mode = 'processing_command'
    mic._followup_processing_token = None  # New explicit wake-word command.
    mic.cancel_followup()
    assert mic._mode == 'processing_command'


def test_busy_output_cannot_reopen_a_reply_window_minutes_later(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(microphone, 'time', SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setattr(settings, 'voice_enabled', True)
    monkeypatch.setattr(settings, 'conversation_followup_enabled', True)
    mic = microphone.MicrophoneService()
    mic._running = True
    mic.request_followup(5., ready=lambda:False, reply='Want more?')
    clock[0] += 11
    assert not mic._apply_pending_followup(clock[0])
    assert mic._pending_followup is None
    assert mic._mode == 'wake'


def test_disabling_followup_discards_queued_reply_window(monkeypatch):
    monkeypatch.setattr(settings, 'voice_enabled', True)
    mic = microphone.MicrophoneService()
    mic._running = True
    mic.request_followup(5., reply='Want more?')
    monkeypatch.setattr(settings, 'conversation_followup_enabled', False)
    assert not mic._apply_pending_followup(microphone.time.monotonic())
    assert mic._pending_followup is None


def test_stt_upload_stays_in_memory_and_closes_the_buffer():
    provider = LLMClient(None, None)
    captured = []
    audio = qa.wav_bytes(speech_like())
    def upload(**options):
        captured.append(options['file'])
        assert isinstance(options['file'], io.BytesIO)
        assert options['file'].name == 'robbi-command.wav'
        with wave.open(options['file']) as source:
            assert source.getframerate() == 16000
        return SimpleNamespace(text='And tomorrow?')
    provider._ensure_stt_client = lambda _: SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=upload)))
    try:
        assert provider.transcribe(audio, provider='openai', model='whisper-1') == 'And tomorrow?'
        assert captured[0].closed
    finally:
        provider.shutdown()
