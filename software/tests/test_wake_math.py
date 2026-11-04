"""Exact matcher semantics, circular audio history and bounded reference input."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from robbi.audio.wake_math import AudioWindow, dtw_normalized_rows, normalize_rows
from robot import local_wake


def original_distance(a, b):
    a, b = normalize_rows(a), normalize_rows(b)
    cost = 1.0 - np.clip(a @ b.T, -1.0, 1.0)
    rows, cols = cost.shape
    dp = np.full((rows + 1, cols + 1), np.inf, dtype=np.float32)
    dp[0, 0] = 0
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            dp[row, col] = cost[row - 1, col - 1] + min(
                dp[row - 1, col], dp[row, col - 1], dp[row - 1, col - 1])
    return float(dp[rows, cols] / max(1, rows + cols))


@pytest.mark.parametrize("rows,cols", [(1, 1), (1, 11), (17, 1), (2, 7), (7, 2),
                                      (21, 21), (180, 120), (120, 400)])
def test_optimized_dtw_exactly_matches_original_recurrence(rows, cols):
    rng = np.random.default_rng(rows * 1000 + cols)
    a = rng.normal(size=(rows, 13)).astype(np.float32)
    b = rng.normal(size=(cols, 13)).astype(np.float32)
    assert dtw_normalized_rows(normalize_rows(a), normalize_rows(b)) == original_distance(a, b)


@pytest.mark.parametrize("value", [np.zeros((4, 13), dtype=np.float32),
                                   np.ones((4, 13), dtype=np.float32)])
def test_zero_and_identical_features_preserve_original_distance(value):
    assert dtw_normalized_rows(normalize_rows(value), normalize_rows(value)) == original_distance(value, value)


@pytest.mark.parametrize("value", [np.empty((0, 13)), np.zeros((2, 0)), np.zeros(13),
                                   np.full((2, 13), np.nan), np.full((2, 13), np.inf)])
def test_invalid_features_rejected_before_matching(value):
    with pytest.raises(ValueError):
        normalize_rows(value)


def test_ring_window_matches_stream_and_reuses_allocated_view():
    window = AudioWindow(7)
    expected = np.zeros(7, dtype=np.float32)
    view = window.view()
    for length in (1, 3, 2, 5, 0, 7, 18, 4):
        values = np.arange(length, dtype=np.float32) + length
        window.append(values)
        expected = np.concatenate((expected, values))[-7:]
        assert window.view() is view
        np.testing.assert_array_equal(window.view(), expected)
    window.clear()
    np.testing.assert_array_equal(window.view(), np.zeros(7))


def wav_bytes(seconds=0.1, sample_rate=16000, width=2, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\0" * int(seconds * sample_rate) * width * channels)
    return output.getvalue()


@pytest.fixture
def detector(monkeypatch, tmp_path):
    monkeypatch.setattr(local_wake, "WAKE_REFERENCES_DIR", tmp_path)
    monkeypatch.setattr(local_wake, "settings", SimpleNamespace(
        mic_sample_rate=16000, voice_local_wake_buffer_s=1.8, voice_local_wake_slide_s=0.35,
        voice_local_wake_max_references=8, voice_local_wake_max_reference_s=4.0,
        voice_local_wake_max_reference_bytes=2097152, voice_local_wake_max_total_feature_frames=1600,
        voice_local_wake_min_active_rms=400, voice_local_wake_threshold=0.22,
        voice_local_wake_method="mfcc", voice_wake_phrase="wake"))
    monkeypatch.setattr(local_wake.LocalWakeDetector, "_extract_mfcc_features",
                        lambda self, audio: np.ones((max(1, len(audio) // 160), 13), dtype=np.float32))
    return local_wake.LocalWakeDetector()


@pytest.mark.parametrize("payload,message", [
    (b"bad WAV", "Invalid wake"), (wav_bytes(seconds=4.01), "max_reference_s"),
    (wav_bytes(width=1), "16-bit"), (wav_bytes(channels=3), "mono or stereo"),
    (wav_bytes(sample_rate=4000), "sample rate"), (wav_bytes(seconds=0), "no audio"),
    (wav_bytes()[:-100], "truncated"), (b"x" * 2097153, "max_reference_bytes"),
], ids=["invalid-header", "too-long", "8-bit", "3-channels", "bad-rate", "empty", "truncated", "too-large"])
def test_invalid_wav_upload_rejected_without_creating_file(detector, payload, message, tmp_path):
    with pytest.raises(ValueError, match=message):
        detector.save_reference(payload)
    assert list(tmp_path.iterdir()) == []


def test_declared_huge_wav_rejected_before_reading_frames(detector):
    class HeaderOnly:
        getframerate = lambda self: 16000
        getsampwidth = lambda self: 2
        getnchannels = lambda self: 1
        getnframes = lambda self: 1_000_000_000
        def readframes(self, count):
            pytest.fail("unbounded read")
    with pytest.raises(ValueError, match="max_reference_s"):
        detector._decode_wav(HeaderOnly())


def test_corrupt_reference_does_not_disable_valid_reference(detector, tmp_path):
    (tmp_path / "a-corrupt.wav").write_bytes(b"corrupt")
    (tmp_path / "b-valid.wav").write_bytes(wav_bytes())
    detector.reload()
    status = detector.health()
    assert status["available"] and detector.available()
    assert status["degraded"]
    assert status["references"] == ["b-valid.wav"]
    assert status["reference_issues"][0]["name"] == "a-corrupt.wav"
    assert not detector._references[0].features.flags.writeable


def test_reference_count_limit_is_visible_and_upload_does_not_overwrite(detector, tmp_path, monkeypatch):
    monkeypatch.setattr(local_wake.settings, "voice_local_wake_max_references", 2)
    for index in range(3):
        (tmp_path / f"{index}.wav").write_bytes(wav_bytes())
    detector.reload()
    assert detector.health()["reference_count"] == 2
    assert "max_references" in detector.health()["reference_issues"][0]["error"]
    with pytest.raises(ValueError, match="limit reached"):
        detector.save_reference(wav_bytes())
    assert len(list(tmp_path.iterdir())) == 3


def test_total_frame_budget_rejects_before_feature_extraction(detector, tmp_path, monkeypatch):
    monkeypatch.setattr(local_wake.settings, "voice_local_wake_max_total_feature_frames", 15)
    (tmp_path / "a.wav").write_bytes(wav_bytes())
    (tmp_path / "b.wav").write_bytes(wav_bytes())
    detector.reload()
    assert detector.health()["reference_count"] == 1
    assert "max_total_feature_frames" in detector.health()["reference_issues"][0]["error"]
    with pytest.raises(ValueError, match="max_total_feature_frames"):
        detector.save_reference(wav_bytes())
    assert len(list(tmp_path.iterdir())) == 2


def test_directory_scan_stops_at_visible_limit_without_materializing_all_entries(detector, monkeypatch):
    visited = []
    class Entry:
        name = "non-audio.txt"
    class Directory:
        def __enter__(self):
            def entries():
                for index in range(10000):
                    visited.append(index)
                    yield Entry()
            return entries()
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(local_wake.os, "scandir", lambda path: Directory())
    detector.reload()
    assert len(visited) == detector.health()["directory_scan_limit"] + 1
    assert "scan budget" in detector.health()["reference_issues"][0]["error"]


def test_new_names_do_not_overwrite_gaps_and_writes_are_complete(detector, tmp_path):
    original = wav_bytes(seconds=0.2)
    (tmp_path / "wake-02.wav").write_bytes(original)
    detector.reload()
    result = detector.save_reference(wav_bytes(), "wake")
    assert result["name"] == "wake-01.wav"
    assert (tmp_path / "wake-02.wav").read_bytes() == original
    assert detector.health()["reference_count"] == 2
    assert not list(tmp_path.glob(".*"))


@pytest.mark.parametrize("threshold_offset,detect", [(-1e-7, False), (0, False), (1e-7, True)])
def test_detection_threshold_preserves_strict_comparison(detector, monkeypatch, tmp_path, threshold_offset, detect):
    (tmp_path / "reference.wav").write_bytes(wav_bytes())
    detector.reload()
    rng = np.random.default_rng(18)
    candidate = rng.normal(size=(5, 13)).astype(np.float32)
    reference = rng.normal(size=(3, 13)).astype(np.float32)
    detector._references[0].features = normalize_rows(reference)
    monkeypatch.setattr(detector, "_extract_mfcc_features", lambda audio: candidate)
    distance = original_distance(candidate, reference)
    monkeypatch.setattr(local_wake.settings, "voice_local_wake_threshold", distance + threshold_offset)
    chunk = b"\0\0" * detector._slide_size_samples
    assert (detector.process_chunk(chunk, rms=32767) is not None) is detect


def test_stream_reset_keeps_references_but_clears_audio_and_pending(detector, tmp_path):
    (tmp_path / "reference.wav").write_bytes(wav_bytes())
    detector.reload()
    references = detector._references
    detector.process_chunk(b"\xff\x1f" * 500, rms=1000)
    assert detector._pending_samples == 500
    assert detector._audio_buffer.view().any()
    detector.reset()
    assert detector._references is references
    assert detector._pending_samples == 0
    assert not detector._audio_buffer.view().any()


def test_chunk_processing_normalizes_candidate_once_for_multiple_references(detector, tmp_path, monkeypatch):
    for name in ("first.wav", "second.wav"):
        (tmp_path / name).write_bytes(wav_bytes())
    detector.reload()
    calls = []
    original = local_wake.normalize_rows
    def counting(values):
        calls.append(values.shape)
        return original(values)
    monkeypatch.setattr(local_wake, "normalize_rows", counting)
    detector.process_chunk(b"\0\0" * detector._slide_size_samples, rms=1000)
    assert len(calls) == 1


def test_malformed_pcm_does_not_mutate_history_and_oversized_chunk_keeps_latest(detector, tmp_path):
    (tmp_path / "reference.wav").write_bytes(wav_bytes())
    detector.reload()
    assert detector.process_chunk(b"\0", rms=1000) is None
    assert not detector._audio_buffer.view().any()
    assert "complete 16-bit" in detector.health()["last_error"]
    assert detector.available()
    detector.process_chunk(b"\1\0" * (detector._buffer_size_samples * 2), rms=0)
    np.testing.assert_array_equal(detector._audio_buffer.view(),
                                  np.full(detector._buffer_size_samples, 1 / 32768, dtype=np.float32))
    detector.process_chunk(b"\1\0" * detector._slide_size_samples, rms=1000)
    assert detector.health()["last_error"] == ""


def test_math_module_can_import_without_robot_configuration_or_devices():
    source = """
import sys
class RejectRobot:
    def find_spec(self, fullname, *args):
        if fullname == 'robot' or fullname.startswith('robot.'):
            raise AssertionError('audio math imported robot dependency')
sys.meta_path.insert(0, RejectRobot())
from robbi.audio.wake_math import AudioWindow, normalize_rows, dtw_normalized_rows
assert 'robot' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", source], check=True, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
