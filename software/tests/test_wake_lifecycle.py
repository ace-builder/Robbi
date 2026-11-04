"""Wake models are demand loaded without touching ALSA or network providers."""
import threading
import time
from unittest.mock import Mock

import pytest

from robot import microphone
from robot.config import settings


def detector(name, available=True):
    value = Mock()
    value.available.return_value = available
    value.health.return_value = {"engine": name, "available": available, "last_error": ""}
    value.process_chunk.return_value = None
    return value


@pytest.fixture
def engines(monkeypatch):
    local = Mock(side_effect=lambda: detector("local_wake"))
    nano = Mock(side_effect=lambda: detector("nanowakeword"))
    monkeypatch.setattr(microphone, "LocalWakeDetector", local)
    monkeypatch.setattr(microphone, "NanoWakeDetector", nano)
    monkeypatch.setattr(settings, "voice_enabled", True)
    monkeypatch.setattr(settings, "voice_wake_engine", "local_wake")
    monkeypatch.setattr(microphone.MicrophoneService, "_capture_loop", lambda self: None)
    return local, nano


def test_disabled_constructor_start_and_diagnostics_never_load_models(monkeypatch, engines):
    local, nano = engines
    monkeypatch.setattr(settings, "voice_enabled", False)
    mic = microphone.MicrophoneService()
    mic.start()
    for _ in range(3):
        status = mic.health()
        assert status["wake_engine_active"] == "disabled"
        assert status["local_wake"]["state"] == "not_loaded"
        assert status["nano_wake"]["state"] == "not_loaded"
        assert not mic.local_wake_status()["loaded"]
    local.assert_not_called()
    nano.assert_not_called()


@pytest.mark.parametrize("configured,local_calls,nano_calls", [("local_wake", 1, 0),
                                                               ("nanowakeword", 0, 1), ("stt", 0, 0)])
def test_start_loads_only_selected_engine(monkeypatch, engines, configured, local_calls, nano_calls):
    local, nano = engines
    monkeypatch.setattr(settings, "voice_wake_engine", configured)
    mic = microphone.MicrophoneService()
    assert mic.health()["wake_engine_active"] == "not_loaded"
    assert local.call_count == nano.call_count == 0
    mic.start()
    try:
        assert mic.health()["wake_engine_active"] == configured
        for _ in range(4):
            mic.health()
            mic._handle_idle_chunk(b"\0\0", time.monotonic(), 0, False)
        assert local.call_count == local_calls
        assert nano.call_count == nano_calls
    finally:
        mic.stop()


@pytest.mark.parametrize("local_available,expected", [(True, "local_wake"), (False, "stt")])
def test_missing_nano_retains_existing_fallback(monkeypatch, engines, local_available, expected):
    local, nano = engines
    nano.side_effect = lambda: detector("nanowakeword", available=False)
    local.side_effect = lambda: detector("local_wake", available=local_available)
    monkeypatch.setattr(settings, "voice_wake_engine", "nanowakeword")
    mic = microphone.MicrophoneService()
    mic.start()
    try:
        assert mic.health()["wake_engine_active"] == expected
        assert nano.call_count == local.call_count == 1
    finally:
        mic.stop()


def test_optional_detector_constructor_failure_is_reported_once(monkeypatch, engines):
    local, nano = engines
    nano.side_effect = ImportError("optional model runtime missing")
    monkeypatch.setattr(settings, "voice_wake_engine", "nanowakeword")
    mic = microphone.MicrophoneService()
    mic.start()
    try:
        for _ in range(5):
            status = mic.health()
            assert status["wake_engine_active"] == "local_wake"
            assert status["nano_wake"]["state"] == "unavailable"
            assert "runtime missing" in status["nano_wake"]["last_error"]
            mic._handle_idle_chunk(b"\0\0", time.monotonic(), 0, False)
        assert nano.call_count == local.call_count == 1
    finally:
        mic.stop()


def test_switching_engine_and_disabling_voice_release_unused_models(monkeypatch, engines):
    local, nano = engines
    mic = microphone.MicrophoneService()
    mic.start()
    try:
        assert mic.local_wake_status()["loaded"]
        monkeypatch.setattr(settings, "voice_wake_engine", "nanowakeword")
        mic.reload_runtime_settings()
        assert mic.health()["wake_engine_active"] == "nanowakeword"
        assert not mic.local_wake_status()["loaded"]
        assert nano.call_count == local.call_count == 1
        monkeypatch.setattr(settings, "voice_enabled", False)
        mic.reload_runtime_settings()
        assert mic.health()["wake_engine_active"] == "disabled"
        assert mic._local_wake._instance is None
        assert mic._nano_wake._instance is None
        assert nano.call_count == local.call_count == 1
    finally:
        mic.stop()


def test_inactive_dataset_and_model_edits_do_not_initialize_detectors(monkeypatch, engines):
    local, nano = engines
    monkeypatch.setattr(settings, "voice_enabled", False)
    mic = microphone.MicrophoneService()
    mic._wake_dataset = Mock()
    mic.save_wake_sample("positive", b"wav")
    mic.delete_wake_sample("positive", "sample.wav")
    mic.clear_local_wake_references()
    mic.upload_nanowake_model("sample.onnx", b"model")
    mic.delete_nanowake_model("sample.onnx")
    mic.reload_runtime_settings()
    local.assert_not_called()
    nano.assert_not_called()


def test_active_local_samples_replace_detector_without_loading_nano(engines):
    local, nano = engines
    mic = microphone.MicrophoneService()
    mic._wake_dataset = Mock()
    mic.start()
    try:
        original = mic._local_wake._instance
        mic.save_wake_sample("positive", b"wav")
        assert mic._local_wake._instance is not original
        assert local.call_count == 2
        mic.save_wake_sample("negative", b"wav")
        mic.upload_nanowake_model("model.onnx", b"model")
        assert local.call_count == 2
        nano.assert_not_called()
    finally:
        mic.stop()


def test_uploading_available_nano_model_replaces_local_fallback(monkeypatch, engines):
    local, nano = engines
    nano.side_effect = [detector("nanowakeword", False), detector("nanowakeword", True)]
    monkeypatch.setattr(settings, "voice_wake_engine", "nanowakeword")
    mic = microphone.MicrophoneService()
    mic._wake_dataset = Mock()
    mic.start()
    try:
        assert mic.health()["wake_engine_active"] == "local_wake"
        mic.upload_nanowake_model("model.onnx", b"model")
        assert mic.health()["wake_engine_active"] == "nanowakeword"
        assert not mic.local_wake_status()["loaded"]
        assert nano.call_count == 2
        assert local.call_count == 1
    finally:
        mic.stop()


def test_reference_browsing_uses_metadata_and_explicit_recording_can_demand_load(monkeypatch, engines):
    local, nano = engines
    monkeypatch.setattr(settings, "voice_enabled", False)
    mic = microphone.MicrophoneService()
    mic._wake_dataset = Mock()
    mic._wake_dataset.list_samples.return_value = {"positive": [{"name": "a.wav", "duration_s": 1.2}]}
    assert mic.local_wake_references() == [{"name": "a.wav", "duration_s": 1.2}]
    mic.local_wake_reference_file("a.wav")
    local.assert_not_called()
    instance = detector("local_wake")
    instance.save_reference.return_value = {"name": "new.wav"}
    local.side_effect = lambda: instance
    assert mic.save_local_wake_reference(b"wav") == {"name": "new.wav"}
    instance.save_reference.assert_called_once_with(b"wav", sample_name=None)
    assert mic._local_wake._instance is None
    nano.assert_not_called()


def test_blocked_model_initialization_does_not_block_health_or_resurrect_after_stop(engines):
    local, nano = engines
    entered = threading.Event()
    release = threading.Event()

    def blocked():
        entered.set()
        release.wait(3)
        return detector("local_wake")

    local.side_effect = blocked
    mic = microphone.MicrophoneService()
    mic._capture_loop = Mock()
    initializer = threading.Thread(target=mic.start, daemon=True)
    initializer.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        status = mic.health()
        assert time.monotonic() - started < 0.1
        assert status["wake_engine_active"] == "loading"
        assert status["local_wake"]["loading"]
        mic.stop()
    finally:
        release.set()
        initializer.join(2)
    assert not initializer.is_alive()
    assert not mic._running
    assert mic._local_wake._instance is None
    mic._capture_loop.assert_not_called()
    nano.assert_not_called()
