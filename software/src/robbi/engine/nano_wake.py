from __future__ import annotations

import logging
import time
from pathlib import Path

try:
    import numpy as np
except (ImportError, OSError):
    np = None

from robbi.configuration.settings import WAKE_MODELS_DIR, settings


class NanoWakeDetector:
    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._interpreter = None
        self._model_path: Path | None = None
        self._model_name = ""
        self._last_score = 0.0
        self._last_raw_score = 0.0
        self._last_detected_at = 0.0
        self._last_error = ""
        self._streak = 0
        self._stream_resets = 0
        self._pending_audio = np.empty(0, dtype=np.int16) if np is not None else None
        self.reload()

    def reload(self) -> None:
        self._interpreter = None
        self._model_path = None
        self._model_name = ""
        self._last_error = ""
        self._streak = 0
        if np is None:
            self._last_error = "NanoWake requires optional NumPy"
            return
        self._pending_audio = np.empty(0, dtype=np.int16)
        path = self._resolve_model_path()
        if path is None:
            self._last_error = "no .onnx model uploaded"
            return
        try:
            from nanowakeword import NanoInterpreter

            self._interpreter = NanoInterpreter.load_model(
                str(path),
                vad_threshold=settings.voice_nanowake_vad_threshold,
            )
            self._model_path = path
            self._model_name = path.stem
        except Exception as exc:
            self._last_error = str(exc)
            self._log.warning("NanoWakeWord init failed: %s", exc)

    def available(self) -> bool:
        return self._interpreter is not None and self._model_path is not None

    def reset(self) -> None:
        """Discard discontinuous audio without allocating new ONNX sessions.

        Audio gaps are normal under load. Reloading here repeatedly allocates
        native model arenas (some retained by reference cycles) and can exhaust
        a Pi Zero's RAM even though the audio queue itself is bounded.
        """
        self._pending_audio = np.empty(0, dtype=np.int16) if np is not None else None
        self._streak = 0
        self._last_score = self._last_raw_score = 0.0
        if self._interpreter is not None:
            self._interpreter.reset()
            # NanoInterpreter.reset() does not reset its optional Silero VAD.
            vad = getattr(self._interpreter, "vad", None)
            if vad is not None:
                vad.reset_states()
                vad.prediction_buffer.clear()
        self._stream_resets += 1

    def health(self) -> dict:
        return {
            "engine": "nanowakeword",
            "available": self.available(),
            "model": self._model_path.name if self._model_path is not None else "",
            "model_name": self._model_name,
            "threshold": settings.voice_nanowake_threshold,
            "patience": settings.voice_nanowake_patience,
            "vad_threshold": settings.voice_nanowake_vad_threshold,
            "last_score": round(float(self._last_score), 4),
            "last_raw_score": round(float(self._last_raw_score), 4),
            "streak": int(self._streak),
            "stream_resets": self._stream_resets,
            "last_detected_at": self._last_detected_at,
            "last_error": self._last_error,
        }

    def process_chunk(self, chunk: bytes) -> dict | None:
        if self._interpreter is None or self._model_path is None or not chunk:
            return None
        try:
            audio = np.frombuffer(chunk, dtype=np.int16)
            if audio.size == 0:
                return None
            self._pending_audio = np.concatenate((self._pending_audio, audio))
            detection = None
            while self._pending_audio.size >= 1280:
                frame = self._pending_audio[:1280]
                self._pending_audio = self._pending_audio[1280:]
                self._interpreter.predict(
                    frame,
                    threshold={self._model_name: 0.0},
                    patience={self._model_name: 1},
                )
                raw_scores = getattr(self._interpreter, "raw_scores", {}) or {}
                raw_score = float(raw_scores.get(self._model_name, 0.0))
                self._last_raw_score = raw_score
                threshold = settings.voice_nanowake_threshold
                patience = max(1, int(settings.voice_nanowake_patience))
                if raw_score >= threshold:
                    self._streak += 1
                else:
                    self._streak = 0
                detected = self._streak >= patience
                self._last_score = raw_score if detected else 0.0
                if detected and self._streak == patience:
                    self._last_detected_at = time.time()
                    detection = {
                        "timestamp": int(self._last_detected_at * 1000),
                        "wakeword": self._model_name,
                        "score": raw_score,
                    }
            if detection is not None:
                return detection
        except Exception as exc:
            self._last_error = str(exc)
            self._log.warning("NanoWakeWord inference failed: %s", exc)
        return None

    def _resolve_model_path(self) -> Path | None:
        configured = settings.voice_nanowake_model.strip()
        if configured:
            target = (WAKE_MODELS_DIR / configured).resolve()
            if target.exists() and target.suffix.lower() == ".onnx":
                return target
        models = sorted(WAKE_MODELS_DIR.glob("*.onnx"))
        return models[0] if models else None
