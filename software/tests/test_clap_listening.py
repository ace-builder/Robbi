from array import array
from concurrent.futures import ThreadPoolExecutor
import io
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from robbi.app_services.claps import detect_claps
from robbi.app_runtime.sdk import AppContext, Artifacts
from robbi.app_runtime.robot_port import RuntimeRobotPort
from robbi.errors import ValidationError
from robot.microphone import MicrophoneService


def audio(beats=(), *, seconds=2, constant=0):
    samples = array('h', [constant] * int(seconds * 16000))
    for beat in beats:
        start = int(beat * 16000)
        for offset in range(160):
            if start + offset < len(samples):
                samples[start + offset] = 16000 if offset % 2 else -16000
    target = io.BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(samples.tobytes())
    return target.getvalue()


def test_clap_timings_and_constant_noise_do_not_become_fake_beats():
    assert detect_claps(audio([.2, .7, 1.3]))['clap_times_s'] == [.2, .7, 1.3]
    assert detect_claps(audio())['clap_times_s'] == []
    assert detect_claps(audio(constant=3000))['clap_times_s'] == []
    with pytest.raises(ValidationError):
        detect_claps(audio(seconds=9))
    with pytest.raises(ValidationError):
        detect_claps(b'not audio')


def test_sdk_requires_explicit_mic_permission_and_honours_capture_cancellation(tmp_path):
    stop = threading.Event()
    calls = []
    microphone = SimpleNamespace(record_clap_audio=lambda duration, cancelled: (calls.append(duration), audio([.5]))[1])
    controller = SimpleNamespace(microphone=microphone, _action_token=lambda: (0, stop), _action_current=lambda generation: not stop.is_set())
    port = RuntimeRobotPort(SimpleNamespace(controller=lambda: controller))
    def context(permissions):
        return AppContext(app_id='test', config={}, permissions=permissions, directory=tmp_path,
            artifacts=Artifacts(tmp_path/'images','test'), cancelled=stop.is_set,
            deadline=time.monotonic()+20, robot=port, robot_token=port.capture_token(), services=lambda name: None)
    with pytest.raises(ValidationError):
        context([]).robot.listen_claps()
    assert not calls
    result = context(['robot.microphone']).robot.listen_claps(duration_s=2)
    assert result['clap_times_s'] == [.5]
    assert 'audio' not in result and calls == [2]
    with pytest.raises(ValidationError):
        context(['robot.microphone']).robot.listen_claps(duration_s=9)


def test_live_stream_collector_is_removed_promptly_on_stop():
    mic = MicrophoneService()
    mic._running, mic._stream_proc = True, object()
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mic.record_clap_audio, 8, cancel.is_set)
        limit = time.monotonic() + 1
        while not mic._manual_collectors and time.monotonic() < limit:
            time.sleep(.005)
        assert len(mic._manual_collectors) == 1
        cancel.set()
        assert future.result(timeout=.5) == b''
    assert mic._manual_collectors == []
    mic._running = False
    with pytest.raises(RuntimeError, match='microphone'):
        mic.record_clap_audio(2, lambda: False)
