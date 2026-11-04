"""Exercise the optional voice packages together using a generated WAV."""
import io
import wave

import numpy as np

from robot import local_wake


def test_real_wav_mfcc_and_matcher_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(local_wake, "WAKE_REFERENCES_DIR", tmp_path)
    for name, value in {
        "mic_sample_rate": 16000, "voice_local_wake_buffer_s": 0.6,
        "voice_local_wake_slide_s": 0.1, "voice_local_wake_threshold": 0.22,
        "voice_local_wake_min_active_rms": 400,
    }.items():
        monkeypatch.setattr(local_wake.settings, name, value)
    time_axis = np.arange(9600, dtype=np.float64) / 16000
    signal = (12000 * np.sin(2 * np.pi * (250 * time_axis + 200 * time_axis ** 2))).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(signal.tobytes())
    detector = local_wake.LocalWakeDetector()
    saved = detector.save_reference(output.getvalue(), "synthetic")
    assert detector.available(), detector.health()
    assert detector.health()["feature_frames"] > 20
    with wave.open(str(tmp_path / saved["name"]), "rb") as wav_file:
        replay = wav_file.readframes(wav_file.getnframes())
    matched = detector.process_chunk(replay, rms=8000)
    assert matched is not None, detector.health()
    assert matched["wakeword"] == saved["name"]
    assert matched["distance"] < 1e-5
    assert detector.health()["last_error"] == ""
