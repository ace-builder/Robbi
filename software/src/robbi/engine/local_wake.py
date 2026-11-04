from __future__ import annotations

import io
import logging
import math
import os
import time
import wave
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

try:
    import numpy as np
except (ImportError, OSError):
    np = None

if np is not None:
    from robbi.audio.wake_math import AudioWindow, dtw_normalized_rows, normalize_rows

from robbi.storage import atomic_write
from robbi.configuration.settings import WAKE_REFERENCES_DIR, settings


@dataclass(slots=True)
class WakeReference:
    name: str
    path: Path
    features: np.ndarray  # Normalized once at load time; immutable during matching.
    duration_s: float


class LocalWakeDetector:
    """Adapted from local-wake's MFCC+DTW approach for Robbi's existing mic loop."""

    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._sample_rate = settings.mic_sample_rate
        self._buffer_size_samples = max(1, int(settings.voice_local_wake_buffer_s * self._sample_rate))
        self._slide_size_samples = max(1, int(settings.voice_local_wake_slide_s * self._sample_rate))
        self._audio_buffer = AudioWindow(self._buffer_size_samples) if np is not None else None
        self._pending_samples = 0
        self._references: list[WakeReference] = []
        self._last_distance = 1.0
        self._last_match = ""
        self._last_detected_at = 0.0
        self._last_error = ""
        self._reference_issues: list[dict] = []
        self.reload()

    def _limits(self) -> dict:
        return {
            "max_references": int(getattr(settings, "voice_local_wake_max_references", 8)),
            "max_reference_s": float(getattr(settings, "voice_local_wake_max_reference_s", 4.0)),
            "max_reference_bytes": int(getattr(settings, "voice_local_wake_max_reference_bytes", 2 * 1024 * 1024)),
            "max_total_feature_frames": int(getattr(settings, "voice_local_wake_max_total_feature_frames", 1600)),
        }

    def _reference_paths(self) -> tuple[list[Path], bool]:
        maximum = self._limits()["max_references"]
        # The directory itself can be untrusted/oversized. Never collect or sort
        # an unlimited list merely to discover it exceeds the configured budget.
        paths = []
        scan_limit = max(64, maximum * 8)
        try:
            with os.scandir(WAKE_REFERENCES_DIR) as entries:
                for scanned, entry in enumerate(islice(entries, scan_limit + 1), start=1):
                    if scanned > scan_limit:
                        return sorted(paths), True
                    if entry.name.lower().endswith(".wav") and entry.is_file():
                        if len(paths) == maximum:
                            return sorted(paths), True
                        paths.append(Path(entry.path))
        except FileNotFoundError:
            pass
        return sorted(paths), False

    def reload(self) -> None:
        references = []
        issues = []
        self._last_error = ""
        if np is None:
            self._references = []
            self._last_error = "Local wake detection requires optional NumPy"
            return
        try:
            paths, overflow = self._reference_paths()
            if overflow:
                issues.append({"name": "*", "error": "Reference directory exceeds max_references or scan budget; "
                               "only the listed references are loaded. Remove files or raise the limit."})
            frame_count = 0
            for path in paths:
                try:
                    audio, sample_rate = self._read_wav_file(path)
                    audio = self._resample_if_needed(audio, sample_rate)
                    trimmed = self._trim_silence(audio)
                    self._check_feature_budget(trimmed, frame_count)
                    features = normalize_rows(self._extract_mfcc_features(trimmed))
                    if frame_count + len(features) > self._limits()["max_total_feature_frames"]:
                        raise ValueError("Reference dataset exceeds max_total_feature_frames")
                    features.flags.writeable = False
                    references.append(WakeReference(
                        name=path.name, path=path, features=features,
                        duration_s=round(len(audio) / float(self._sample_rate), 2),
                    ))
                    frame_count += len(features)
                except Exception as exc:
                    issues.append({"name": path.name, "error": str(exc)[:300]})
                    self._log.warning("Local wake reference %s not loaded: %s", path.name, exc)
        except Exception as exc:
            self._last_error = str(exc)
            self._log.warning("Local wake reload failed: %s", exc)
        self._references = references
        self._reference_issues = issues
        if not references and issues:
            self._last_error = issues[0]["error"]
        if self._audio_buffer is not None:
            self._audio_buffer.clear()
        self._pending_samples = 0

    def _check_feature_budget(self, audio: np.ndarray, used: int) -> None:
        frame_size = int(round(self._sample_rate * 0.025))
        step_size = max(1, int(round(self._sample_rate * 0.01)))
        frame_count = 1 + max(0, math.ceil((len(audio) - frame_size) / step_size))
        if used + frame_count > self._limits()["max_total_feature_frames"]:
            raise ValueError("Reference dataset exceeds max_total_feature_frames; remove references or raise the limit")

    def health(self) -> dict:
        return {
            "engine": "local_wake",
            "available": bool(self._references),
            "reference_count": len(self._references),
            "references": [ref.name for ref in self._references],
            "feature_frames": sum(len(ref.features) for ref in self._references),
            "limits": self._limits(),
            "directory_scan_limit": max(64, self._limits()["max_references"] * 8),
            "reference_issues": list(self._reference_issues),
            "degraded": bool(self._reference_issues or self._last_error),
            "threshold": settings.voice_local_wake_threshold,
            "method": settings.voice_local_wake_method,
            "last_distance": round(float(self._last_distance), 4),
            "last_match": self._last_match,
            "last_detected_at": self._last_detected_at,
            "last_error": self._last_error,
        }

    def available(self) -> bool:
        # A malformed transient chunk must not permanently select another engine;
        # the next valid candidate can recover while health keeps the error visible.
        return bool(self._references)

    def reset(self) -> None:
        """Forget discontinuous audio without reloading models or references."""
        if self._audio_buffer is not None:
            self._audio_buffer.clear()
        self._pending_samples = 0

    def list_references(self) -> list[dict]:
        return [{"name": ref.name, "duration_s": ref.duration_s} for ref in self._references]

    def process_chunk(self, chunk: bytes, rms: int) -> dict | None:
        if not self._references:
            return None
        if len(chunk) % 2:
            self._last_error = "Local wake input must contain complete 16-bit PCM samples"
            return None
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0:
            return None
        self._audio_buffer.append(samples)
        self._pending_samples += samples.size
        if self._pending_samples < self._slide_size_samples:
            return None
        self._pending_samples = 0
        if int(rms) < settings.voice_local_wake_min_active_rms:
            return None
        try:
            features = normalize_rows(self._extract_mfcc_features(self._audio_buffer.view()))
            self._last_error = ""
            best_ref = None
            best_distance = math.inf
            for ref in self._references:
                distance = dtw_normalized_rows(features, ref.features)
                if distance < best_distance:
                    best_distance = distance
                    best_ref = ref
            if best_ref is None:
                return None
            self._last_distance = float(best_distance)
            self._last_match = best_ref.name
            if best_distance < settings.voice_local_wake_threshold:
                now = time.time()
                self._last_detected_at = now
                self._audio_buffer.clear()
                return {
                    "timestamp": int(now * 1000),
                    "wakeword": best_ref.name,
                    "distance": float(best_distance),
                }
        except Exception as exc:
            self._last_error = str(exc)
            self._log.warning("Local wake processing failed: %s", exc)
        return None

    def save_reference(self, wav_bytes: bytes, sample_name: str | None = None) -> dict:
        if np is None:
            raise ValueError("Local wake detection requires optional NumPy")
        paths, overflow = self._reference_paths()
        if overflow or len(paths) >= self._limits()["max_references"]:
            raise ValueError("Wake reference limit reached; remove references or raise max_references")
        audio, sample_rate = self._read_wav_bytes(wav_bytes)
        audio = self._resample_if_needed(audio, sample_rate)
        if audio.size == 0:
            raise ValueError("recorded wake sample contained no speech")
        trimmed = self._trim_silence(audio)
        self._check_feature_budget(trimmed, sum(len(ref.features) for ref in self._references))
        # Reject unsupported/invalid feature extraction before committing a file.
        normalize_rows(self._extract_mfcc_features(trimmed))
        target = WAKE_REFERENCES_DIR / self._next_reference_name(sample_name)
        self._write_wav_file(target, audio)
        self.reload()
        return {
            "name": target.name,
            "duration_s": round(len(audio) / float(self._sample_rate), 2),
            "reference_count": len(self._references),
        }

    def clear_references(self) -> int:
        removed = 0
        for path in WAKE_REFERENCES_DIR.glob("*.wav"):
            path.unlink(missing_ok=True)
            removed += 1
        self.reload()
        return removed

    def reference_file(self, name: str) -> Path | None:
        target = (WAKE_REFERENCES_DIR / name).resolve()
        try:
            target.relative_to(WAKE_REFERENCES_DIR.resolve())
        except ValueError:
            return None
        return target if target.exists() and target.suffix.lower() == ".wav" else None

    def _next_reference_name(self, sample_name: str | None) -> str:
        base = (sample_name or settings.voice_wake_phrase or "wake").strip().lower().replace(" ", "-")
        base = "".join(ch for ch in base if ch.isalnum() or ch in {"-", "_"}).strip("-_") or "wake"
        base = base[:64]
        for index in range(1, self._limits()["max_references"] + 2):
            name = f"{base}-{index:02d}.wav"
            if not (WAKE_REFERENCES_DIR / name).exists():
                return name
        raise ValueError("Wake reference name limit reached")

    def _extract_mfcc_features(self, audio: np.ndarray) -> np.ndarray:
        try:
            from python_speech_features import mfcc
        except Exception as exc:
            raise RuntimeError(f"python_speech_features import failed: {exc}") from exc
        features = mfcc(
            audio,
            samplerate=self._sample_rate,
            numcep=13,
            nfilt=26,
            nfft=512,
            winlen=0.025,
            winstep=0.01,
            appendEnergy=True,
        )
        return np.asarray(features, dtype=np.float32)

    def _dtw_cosine_normalized_distance(self, features1: np.ndarray, features2: np.ndarray) -> float:
        return dtw_normalized_rows(normalize_rows(features1), normalize_rows(features2))

    def _normalize_rows(self, values: np.ndarray) -> np.ndarray:
        return normalize_rows(values)

    def _trim_silence(self, audio: np.ndarray) -> np.ndarray:
        frame_samples = max(1, int(self._sample_rate * 0.02))
        pad_samples = int(self._sample_rate * 0.12)
        threshold = max(0.004, settings.voice_local_wake_min_active_rms / 32768.0)
        frame_levels = []
        for start in range(0, len(audio), frame_samples):
            frame = audio[start : start + frame_samples]
            if frame.size == 0:
                continue
            frame_levels.append(float(np.sqrt(np.mean(frame * frame))))
        active = [index for index, value in enumerate(frame_levels) if value >= threshold]
        if not active:
            return audio
        start_sample = max(0, active[0] * frame_samples - pad_samples)
        end_sample = min(len(audio), (active[-1] + 1) * frame_samples + pad_samples)
        return audio[start_sample:end_sample]

    def _read_wav_file(self, path: Path) -> tuple[np.ndarray, int]:
        if path.stat().st_size > self._limits()["max_reference_bytes"]:
            raise ValueError("Wake reference exceeds max_reference_bytes")
        with wave.open(str(path), "rb") as wav_file:
            return self._decode_wav(wav_file)

    def _read_wav_bytes(self, wav_bytes: bytes) -> tuple[np.ndarray, int]:
        if len(wav_bytes) > self._limits()["max_reference_bytes"]:
            raise ValueError("Wake reference exceeds max_reference_bytes")
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
                return self._decode_wav(wav_file)
        except (wave.Error, EOFError) as exc:
            raise ValueError(f"Invalid wake reference WAV: {exc}") from exc

    def _decode_wav(self, wav_file) -> tuple[np.ndarray, int]:
        sample_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        frame_count = wav_file.getnframes()
        if sample_width != 2 or channels not in (1, 2):
            raise ValueError("Wake references must be 16-bit mono or stereo PCM WAV")
        if not 8000 <= sample_rate <= 96000:
            raise ValueError("Wake reference sample rate must be between 8000 and 96000 Hz")
        if frame_count <= 0:
            raise ValueError("Wake reference contains no audio frames")
        if frame_count / sample_rate > self._limits()["max_reference_s"]:
            raise ValueError("Wake reference exceeds max_reference_s")
        expected_bytes = frame_count * channels * sample_width
        if expected_bytes > self._limits()["max_reference_bytes"]:
            raise ValueError("Wake reference PCM data exceeds max_reference_bytes")
        frames = wav_file.readframes(frame_count)
        if len(frames) != expected_bytes:
            raise ValueError("Wake reference WAV is truncated")
        return self._pcm_to_float32(frames, sample_width, channels), sample_rate

    def _pcm_to_float32(self, frames: bytes, sample_width: int, channels: int) -> np.ndarray:
        if sample_width != 2:
            raise ValueError(f"unsupported sample width: {sample_width}")
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32)
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio / 32768.0

    def _write_wav_file(self, path: Path, audio: np.ndarray) -> None:
        pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._sample_rate)
            wav_file.writeframes(pcm.tobytes())
        atomic_write(path, output.getvalue())

    def _resample_if_needed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate == self._sample_rate:
            return audio.astype(np.float32, copy=False)
        duration_s = len(audio) / float(sample_rate)
        target_length = max(1, int(round(duration_s * self._sample_rate)))
        positions = np.linspace(0, len(audio) - 1, num=target_length)
        return np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
