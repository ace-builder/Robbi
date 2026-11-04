"""An audio dropout must preserve native wake models and clear temporal state."""
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from robot.nano_wake import NanoWakeDetector
from robot.microphone import _LazyWakeEngine


@pytest.mark.parametrize("with_vad", [False, True])
def test_repeated_gaps_reset_same_interpreter_without_reload(monkeypatch, with_vad):
    monkeypatch.setattr(NanoWakeDetector, "reload", lambda self: None)
    detector = NanoWakeDetector()
    interpreter = SimpleNamespace(reset=Mock())
    if with_vad:
        interpreter.vad = SimpleNamespace(reset_states=Mock(), prediction_buffer=deque([1.0]))
    detector._interpreter = interpreter
    detector._model_path = Path("wake.onnx")
    detector.reload = Mock(side_effect=AssertionError("Model reloaded after a gap"))
    engine = _LazyWakeEngine("nano", Mock(side_effect=AssertionError("Model reconstructed")))
    engine._instance = detector
    engine._attempted = True
    for _ in range(100):
        detector._pending_audio = np.ones(640, dtype=np.int16)
        detector._streak = 3
        detector._last_raw_score = detector._last_score = 1.0
        engine.reset_stream()
        assert detector._interpreter is interpreter
        assert detector.available()
        assert detector._pending_audio.size == detector._streak == 0
        assert detector._last_raw_score == detector._last_score == 0
    assert interpreter.reset.call_count == 100
    assert detector.health()["stream_resets"] == 100
    if with_vad:
        assert interpreter.vad.reset_states.call_count == 100
        assert not interpreter.vad.prediction_buffer


def test_unknown_engine_cannot_allocate_models_in_audio_gap_recovery():
    factory = Mock(side_effect=AssertionError("Model reconstructed"))
    engine = _LazyWakeEngine("unknown", factory)
    engine._instance = object()
    engine._attempted = True
    with pytest.raises(RuntimeError, match="does not support audio stream reset"):
        engine.reset_stream()
    factory.assert_not_called()
