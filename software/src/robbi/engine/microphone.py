from __future__ import annotations

from robbi.audio import pcm
import io
import logging
import math
import re
import subprocess
import threading
import time
import wave
import os
import select
from collections import deque
from contextlib import contextmanager
from robbi.audio.inbox import AudioInbox

from robbi.configuration.settings import settings
from .local_wake import LocalWakeDetector
from .nano_wake import NanoWakeDetector
from .wake_dataset import WakeDatasetManager


class _LazyWakeEngine:
    """Load an optional detector on demand; diagnostics never initialize it."""

    def __init__(self, name, factory):
        self.name = name
        self._factory = factory
        self._instance = None
        self._attempted = False
        self._loading = False
        self._error = ""
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            if not self._attempted:
                self._loading = True
                self._attempted = True
                try:
                    self._instance = self._factory()
                    self._error = ""
                except Exception as exc:
                    self._error = str(exc)
                finally:
                    self._loading = False
            return self._instance

    def unload(self):
        with self._lock:
            self._instance = None
            self._attempted = False
            self._error = ""

    def reload_if_loaded(self):
        with self._lock:
            attempted = self._attempted
            if attempted:
                # Replace rather than mutating a detector currently processing
                # a chunk. Its caller retains the old instance until it returns.
                self.unload()
                self.load()

    def available(self):
        instance = self._instance
        return bool(instance is not None and instance.available())

    def health(self):
        instance = self._instance
        if instance is not None:
            return dict(instance.health(), loaded=True, loading=False)
        state = "loading" if self._loading else "unavailable" if self._attempted else "not_loaded"
        return {"engine": self.name, "available": False, "loaded": False,
                "loading": self._loading, "state": state, "last_error": self._error}

    def process_chunk(self, *args):
        instance = self._instance
        return instance.process_chunk(*args) if instance is not None else None

    def reset_stream(self):
        """Reset temporal state without constructing models in the audio loop."""
        instance = self._instance
        if instance is not None:
            reset = getattr(instance, "reset", None)
            if callable(reset):
                reset()
            else:
                raise RuntimeError(f"{self.name} does not support audio stream reset")


class MicrophoneService:
    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._state_lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._generation = 0
        self._transcription_slots = threading.BoundedSemaphore(2)
        self._thread: threading.Thread | None = None
        self._processing_thread: threading.Thread | None = None
        self.live_audio_tap = None
        self._audio_input = AudioInbox()
        self._processing_local = threading.local()
        self._stream_proc: subprocess.Popen | None = None
        self._transcribe_callback = None
        self._wake_callback = None
        self._command_callback = None
        self._followup_check_callback = None
        self._armed_followup = None
        self._followup_processing_token = None
        self._last_followup_rejection = ""
        self._followup_state_callback = None
        self._followup_cue_active = False
        self._listening_finished_callback = None
        self._can_listen_callback = None
        self._input_context_callback = None
        self._chunk_ms = max(20, int(settings.voice_chunk_ms))
        # Round whole PCM frames before converting to bytes. Fractional sample
        # counts at nonstandard rates must never create odd 16-bit chunks.
        self._chunk_bytes = max(1, int(round(settings.mic_sample_rate * self._chunk_ms / 1000.0))) * 2
        self._pre_roll_chunks = max(1, int(settings.voice_pre_roll_ms / self._chunk_ms))
        self._recent_chunks: deque[bytes] = deque(maxlen=self._pre_roll_chunks)
        self._manual_collectors: list[dict] = []
        self._capture: dict | None = None
        self._local_wake = _LazyWakeEngine("local_wake", LocalWakeDetector)
        self._nano_wake = _LazyWakeEngine("nanowakeword", NanoWakeDetector)
        self._wake_engine_lock = threading.RLock()
        self._wake_prepared = False
        self._wake_preparing = False
        self._wake_configured = None
        self._wake_dataset = WakeDatasetManager()
        self._mode = "wake"
        self._armed_until = 0.0
        self._followup_epoch = 0
        self._pending_followup = None
        self._ignore_until = 0.0
        self._speech_streak = 0
        self._noise_floor_rms = float(settings.voice_min_rms)
        self._last_level = {
            "ok": False,
            "rms": 0,
            "peak": 0,
            "level_percent": 0,
            "voice_state": "disabled" if not settings.voice_enabled else "idle",
            "noise_floor_rms": int(self._noise_floor_rms),
            "speech_threshold_rms": settings.voice_min_rms,
        }
        self._voice_state = "disabled" if not settings.voice_enabled else "idle"
        self._wake_enabled = bool(settings.voice_enabled)
        self._wake_waiter = None
        self._last_wake_attempt = ""
        self._last_wake_transcript = ""
        self._last_command_transcript = ""
        self._last_error = ""
        self._hardware_probe = {
            "checked_at": 0.0,
            "capture_available": False,
            "usb_mic_detected": False,
        }
        self._stats = {
            "wake_transcribe_requests": 0,
            "wake_transcribe_empty": 0,
            "wake_detected": 0,
            "wake_dropped_local": 0,
        }

    def configure(
        self,
        *,
        transcribe_callback=None,
        wake_callback=None,
        command_callback=None,
        followup_check_callback=None,
        listening_finished_callback=None,
        followup_state_callback=None,
        can_listen_callback=None,
        context_callback=None,
    ) -> None:
        self._transcribe_callback = transcribe_callback
        self._wake_callback = wake_callback
        self._command_callback = command_callback
        self._followup_check_callback = followup_check_callback
        self._followup_state_callback = followup_state_callback
        self._listening_finished_callback = listening_finished_callback
        self._can_listen_callback = can_listen_callback
        self._input_context_callback = context_callback

    def start(self) -> None:
        if not settings.voice_enabled:
            self._set_voice_state("disabled")
            return
        if self._running:
            return
        if any(thread is not None and thread.is_alive() for thread in (self._thread, self._processing_thread)):
            self._set_error("Previous microphone worker is still stopping")
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        self._stop_event.clear()
        # This start method runs in the supervised component initializer, not in
        # an HTTP status request or the real-time audio capture loop.
        self._prepare_wake_engines()
        if not self._running or generation != self._generation:
            self._local_wake.unload()
            self._nano_wake.unload()
            self._wake_prepared = False
            return
        self._mode = "wake"
        self._capture = None
        self._audio_input = AudioInbox()
        self._processing_thread = threading.Thread(target=self._processing_loop, daemon=True, name="audio-processing")
        self._processing_thread.start()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="microphone")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._generation += 1
        self.cancel_followup()
        self._stop_event.set()
        self._audio_input.close()
        self._stop_stream()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._processing_thread and self._processing_thread.is_alive():
            self._processing_thread.join(timeout=1)
            if self._processing_thread.is_alive():
                self._set_error("Audio processing worker has not stopped; refusing duplicate ownership")

    def cancel_pending_input(self):
        """Invalidate captured/processing audio and STT without stopping capture."""
        self.cancel_followup()
        self._audio_input.reset("Pending voice input cancelled by Stop")

    def health(self) -> dict:
        # Status never invokes device enumeration or waits for a subprocess.
        with self._state_lock:
            probe = dict(self._hardware_probe)
            return {
                "enabled": bool(settings.voice_enabled),
                "available": bool(probe["capture_available"]) and not self._last_error,
                "hint": "Check USB microphone and ALSA device name; install alsa-utils on Pi.",
                "capture_available": bool(probe["capture_available"]),
                "device": settings.mic_device,
                "sample_rate": settings.mic_sample_rate,
                "usb_mic_detected": bool(probe["usb_mic_detected"]),
                "voice_enabled": settings.voice_enabled,
                "wake_enabled": self._wake_enabled,
                "wake_engine_configured": settings.voice_wake_engine,
                "wake_engine_active": self._effective_wake_engine(),
                "voice_state": self._voice_state,
                "followup_pending": self._pending_followup is not None,
                "followup_remaining_s": round(max(0., self._armed_until - time.monotonic()), 1) if self._mode == "armed" else 0.,
                "wake_phrase": settings.voice_wake_phrase,
                "last_wake_attempt": self._last_wake_attempt,
                "noise_floor_rms": int(self._noise_floor_rms),
                "speech_threshold_rms": self._current_speech_threshold(),
                "last_wake_transcript": self._last_wake_transcript,
                "last_command_transcript": self._last_command_transcript,
                "last_error": self._last_error,
                "wake_transcribe_requests": self._stats["wake_transcribe_requests"],
                "wake_transcribe_empty": self._stats["wake_transcribe_empty"],
                "wake_detected": self._stats["wake_detected"],
                "wake_dropped_local": self._stats["wake_dropped_local"],
                "local_wake": self._local_wake.health(),
                "nano_wake": self._nano_wake.health(),
                "processing": self._audio_input.snapshot(),
            }

    def _cached_hardware_probe(self) -> dict:
        now = time.monotonic()
        with self._state_lock:
            cached = dict(self._hardware_probe)
        if now - float(cached.get("checked_at", 0.0) or 0.0) < 10.0:
            return cached
        capture_available = False
        detected = False
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            capture_available = result.returncode == 0
            detected = "USB PnP Sound Device" in output or "USB Audio" in output
        except Exception:
            pass
        probe = {
            "checked_at": now,
            "capture_available": capture_available,
            "usb_mic_detected": detected,
        }
        with self._state_lock:
            self._hardware_probe = probe
        return probe

    def level(self, duration_s: float = 1.0) -> dict:
        with self._state_lock:
            return dict(self._last_level)

    def wake_enabled(self) -> bool:
        with self._state_lock:
            return bool(self._wake_enabled)

    def check_wake_wait_ready(self):
        """A filming trigger uses the existing local detector, never cloud STT."""
        health = self.health()
        if (not self._running or not health.get('available') or not settings.voice_enabled
                or health.get('wake_engine_active') not in {'nanowakeword', 'local_wake'}):
            raise RuntimeError('Wake-triggered filming needs the microphone and a ready local wake detector')

    def wake_wait_active(self):
        with self._state_lock:
            waiter = self._wake_waiter
        return bool(waiter and waiter[2].is_set() and not waiter[0].is_set() and not waiter[1].is_set())

    @contextmanager
    def wake_only(self, cancelled):
        """One scoped wake event; no conversation window or persistent setting change."""
        self.check_wake_wait_ready()
        waiter = (threading.Event(), cancelled, threading.Event())
        with self._state_lock:
            if self._wake_waiter is not None:
                raise RuntimeError('Another shot is already waiting for the wake phrase')
            self._wake_waiter = waiter
        try:
            # Discard audio preceding this cue, including an old armed window.
            self.cancel_pending_input()
            waiter[2].set()
            yield waiter[0]
        finally:
            # Keep the waiter installed during reset so a late detection can
            # never escape into normal voice handling as the take ends.
            waiter[2].clear()
            self.cancel_pending_input()
            with self._state_lock:
                if self._wake_waiter is waiter:
                    self._wake_waiter = None

    def _consume_wake_wait(self):
        with self._state_lock:
            waiter = self._wake_waiter
            if waiter is None:
                return False
            if waiter[2].is_set() and not waiter[1].is_set() and self._audio_chunk_current():
                waiter[0].set()
            self._mode = 'wake'
            self._armed_until = 0.
            self._voice_state = 'idle'
        return True

    def voice_state(self) -> str:
        with self._state_lock:
            return str(self._voice_state or "idle")

    def set_wake_enabled(self, enabled: bool) -> bool:
        with self._state_lock:
            self._wake_enabled = bool(enabled) and bool(settings.voice_enabled)
            if not self._wake_enabled:
                self._followup_epoch += 1
                self._pending_followup = None
                self._armed_until = 0.
                self._mode = "wake"
                self._speech_streak = 0
                self._capture = None
                self._ignore_until = max(self._ignore_until, time.monotonic() + 0.2)
                self._voice_state = "idle"
                if self._last_level:
                    self._last_level["voice_state"] = "idle"
            return self._wake_enabled

    def record_wav_bytes(self, duration_s: float = 3.0, prep_delay_s: float = 0.0) -> bytes:
        duration = max(1.0, min(10.0, float(duration_s)))
        prep_delay = max(0.0, min(5.0, float(prep_delay_s)))
        previous_wake_enabled = self.wake_enabled()
        self.set_wake_enabled(False)
        try:
            self.suspend_voice(prep_delay + duration + 0.5)
            if prep_delay:
                time.sleep(prep_delay)
            if not self._running or self._stream_proc is None:
                return self._record_wav_bytes_once(duration)
            target_chunks = max(1, int(round((duration * 1000.0) / self._chunk_ms)))
            collector = {"remaining": target_chunks, "chunks": [], "event": threading.Event()}
            with self._state_lock:
                self._manual_collectors.append(collector)
            if not collector["event"].wait(timeout=max(5.0, duration + 2.0)):
                with self._state_lock:
                    if collector in self._manual_collectors:
                        self._manual_collectors.remove(collector)
                return b""
            if collector.get("error"):
                raise RuntimeError(collector["error"])
            return self._chunks_to_wav_bytes(collector["chunks"])
        finally:
            self.set_wake_enabled(previous_wake_enabled)

    def record_clap_audio(self, duration_s, cancelled):
        """Explicit bounded capture on the existing stream; no STT or saved audio."""
        deadline = time.monotonic() + duration_s + 2
        collector = {"remaining": max(1, round(duration_s * 1000 / self._chunk_ms)),
                     "chunks": [], "event": threading.Event()}
        if cancelled():
            return b""
        with self._state_lock:
            if not self._running or self._stream_proc is None:
                raise RuntimeError("Enable the microphone and wait for its live stream, or use tap input")
            if self._manual_collectors:
                raise RuntimeError("The microphone is already recording another request")
            # Suppress wake/command detection during deliberate clap collection.
            # Do not alter the user's persistent wake-word setting.
            previous_ignore = self._ignore_until
            capture_ignore = max(previous_ignore, time.monotonic() + duration_s + .5)
            self._ignore_until = capture_ignore
            self._manual_collectors.append(collector)
        try:
            while not collector["event"].wait(.05):
                if cancelled() or self._stop_event.is_set():
                    return b""
                if time.monotonic() >= deadline:
                    raise RuntimeError("Microphone capture timed out; use tap input or check the microphone")
            if cancelled() or self._stop_event.is_set():
                return b""
            if collector.get("error"):
                raise RuntimeError(collector["error"])
            return self._chunks_to_wav_bytes(collector["chunks"])
        finally:
            with self._state_lock:
                if collector in self._manual_collectors:
                    self._manual_collectors.remove(collector)
                if self._ignore_until == capture_ignore:
                    self._ignore_until = max(previous_ignore, time.monotonic() + .2)

    def suspend_voice(self, duration_s: float) -> None:
        with self._state_lock:
            self._ignore_until = max(self._ignore_until, time.monotonic() + max(0.0, float(duration_s)))

    def extend_post_wake_block(self, duration_s: float) -> None:
        extra = max(0.0, float(duration_s))
        if extra <= 0:
            return
        with self._state_lock:
            self._ignore_until = max(self._ignore_until, time.monotonic() + extra)
            if self._mode == "armed":
                self._armed_until = max(self._armed_until, self._ignore_until + settings.voice_command_wait_s)

    def followup_token(self):
        with self._state_lock:
            return self._generation, self._followup_epoch

    def cancel_followup(self):
        """End attention and invalidate callbacks already in preparation/playback."""
        with self._state_lock:
            if self._mode == "processing_command" and self._followup_processing_token is not None:
                self._mode = "wake"
                self._voice_state = "idle"
            self._followup_processing_token = None
            self._followup_epoch += 1
            self._pending_followup = None
            self._armed_followup = None
            self._armed_until = 0.
            if self._mode == "armed" and self._capture is None:
                self._mode = "wake"
                self._voice_state = "idle"
                if self._last_level:
                    self._last_level["voice_state"] = "idle"

    def request_followup(self, duration_s, *, token=None, cancelled=None, ready=None, reply="", output_active=None, game=False):
        """Coalesce completed-speech notifications; only the PCM loop arms input.

        The timeout starts after output becomes quiet, not when speech synthesis
        or playback begins. No recording or transcription is reset here.
        """
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) or not math.isfinite(duration_s):
            raise ValueError("Follow-up duration must be a finite number")
        timeout_s = max(1., min(120., float(duration_s)))
        with self._state_lock:
            current = (self._generation, self._followup_epoch)
            if not settings.voice_enabled or not self._running or not self._wake_enabled or (token is not None and token != current):
                return False
            self._pending_followup = {"token": current, "duration_s": timeout_s,
                                      "game": game is True,
                                      "expires_at": time.monotonic() + 10.,
                                      "output_active": output_active,
                                      "cancelled": cancelled, "ready": ready, "reply": str(reply)[-1200:]}
            return True

    def arm_for_followup(self, duration_s: float) -> bool:
        # Compatibility API now requests attention without mutating a capture
        # from the audio-output or request thread.
        return self.request_followup(duration_s)

    def _apply_pending_followup(self, now):
        with self._state_lock:
            pending = self._pending_followup
        if pending is None:
            return False
        try:
            signal = pending["cancelled"]
            cancelled = bool(signal.is_set() if hasattr(signal, "is_set") else signal()) if signal is not None else False
            output_active = pending.get("output_active")
            speaking = bool(output_active()) if output_active is not None else False
            ready = not speaking and (pending["ready"] is None or bool(pending["ready"]()))
            ready = ready and (self._can_listen_callback is None or bool(self._can_listen_callback()))
        except Exception:
            cancelled, ready, speaking = True, False, False
        with self._state_lock:
            if self._pending_followup is not pending:
                return False
            # Only actual speaker activity extends this grace period. Quiet,
            # blocked or abandoned work still expires promptly.
            if speaking:
                pending["expires_at"] = now + 10.
            if (cancelled or pending["expires_at"] <= now or not self._running or not settings.voice_enabled
                    or (not settings.conversation_followup_enabled and not pending.get('game'))
                    or not self._wake_enabled or pending["token"] != (self._generation, self._followup_epoch)):
                self._pending_followup = None
                return False
            if not ready or self._capture is not None or self._mode in {"processing_wake", "processing_command"}:
                return False
            self._pending_followup = None
            self._mode = "armed"
            self._armed_followup = dict(pending)
            self._speech_streak = 0
            # Discard echo/pre-roll from Robbi's own just-finished speech.
            self._recent_chunks.clear()
            self._ignore_until = max(self._ignore_until, now + 0.15)
            self._armed_until = self._ignore_until + pending["duration_s"]
            self._voice_state = "armed"
            if self._last_level:
                self._last_level["voice_state"] = "armed"
            return True

    def local_wake_status(self) -> dict:
        return self._local_wake.health()

    def local_wake_references(self) -> list[dict]:
        samples = self._wake_dataset.list_samples("positive")["positive"]
        return [{"name": sample["name"], "duration_s": sample["duration_s"]} for sample in samples]

    def save_local_wake_reference(self, wav_bytes: bytes, sample_name: str | None = None) -> dict:
        # An explicit reference-recording action needs the detector's WAV
        # normalization. Ordinary dataset/status reads never load either model.
        with self._wake_engine_lock:
            detector = self._local_wake.load()
            if detector is None:
                raise RuntimeError(self._local_wake.health()["last_error"] or "Local wake detector unavailable")
            try:
                return detector.save_reference(wav_bytes, sample_name=sample_name)
            finally:
                if not settings.voice_enabled or settings.voice_wake_engine == "stt" or self._nano_wake.available():
                    self._local_wake.unload()

    def clear_local_wake_references(self) -> int:
        return self.clear_wake_samples("positive")

    def local_wake_reference_file(self, name: str):
        return self._wake_dataset.sample_file("positive", name)

    def wake_dataset_status(self) -> dict:
        return {
            "engine": {
                "configured": settings.voice_wake_engine,
                "active": self._effective_wake_engine(),
            },
            "phrase": settings.voice_wake_phrase,
            "record_seconds": settings.voice_reference_record_s,
            "datasets": self._wake_dataset.list_samples(),
            "local_wake": self._local_wake.health(),
            "nanowakeword": self._nano_wake.health(),
            "models": self._wake_dataset.model_status(),
            "exports": self._wake_dataset.export_status(),
            "paths": {
                "positive": str(self._wake_dataset.CATEGORY_DIRS["positive"]),
                "negative": str(self._wake_dataset.CATEGORY_DIRS["negative"]),
                "noise": str(self._wake_dataset.CATEGORY_DIRS["noise"]),
            },
        }

    def save_wake_sample(
        self,
        category: str,
        wav_bytes: bytes,
        *,
        sample_name: str | None = None,
    ) -> dict:
        saved = self._wake_dataset.save_sample(category, wav_bytes, sample_name=sample_name)
        if category == "positive":
            self._wake_data_changed(self._local_wake)
        return saved

    def delete_wake_sample(self, category: str, name: str) -> dict:
        deleted = self._wake_dataset.delete_sample(category, name)
        if category == "positive":
            self._wake_data_changed(self._local_wake)
        return deleted

    def clear_wake_samples(self, category: str) -> int:
        removed = self._wake_dataset.clear_samples(category)
        if category == "positive":
            self._wake_data_changed(self._local_wake)
        return removed

    def wake_sample_file(self, category: str, name: str):
        return self._wake_dataset.sample_file(category, name)

    def export_wake_dataset_zip(self):
        return self._wake_dataset.export_dataset_zip()

    def upload_nanowake_model(self, filename: str, model_bytes: bytes) -> dict:
        status = self._wake_dataset.save_model(filename, model_bytes)
        self._wake_data_changed(self._nano_wake)
        return status

    def delete_nanowake_model(self, name: str) -> dict:
        status = self._wake_dataset.delete_model(name)
        self._wake_data_changed(self._nano_wake)
        return status

    def reload_runtime_settings(self) -> None:
        self.cancel_followup()
        self._audio_input.reset("Voice settings changed")
        with self._state_lock:
            self._wake_enabled = bool(self._wake_enabled) and bool(settings.voice_enabled)
            self._noise_floor_rms = max(float(settings.voice_min_rms), self._noise_floor_rms)
            if not settings.voice_enabled:
                self._mode = "wake"
                self._capture = None
                self._speech_streak = 0
                self._voice_state = "disabled"
            elif self._voice_state == "disabled":
                self._voice_state = "idle"
        with self._wake_engine_lock:
            self._wake_prepared = False
            self._local_wake.unload()
            self._nano_wake.unload()
            if self._running:
                self._prepare_wake_engines()

    def _wake_data_changed(self, engine):
        self._audio_input.reset("Wake data changed")
        with self._wake_engine_lock:
            self._wake_prepared = False
            self._wake_preparing = True
            try:
                engine.reload_if_loaded()
                if self._running:
                    self._prepare_wake_engines()
            finally:
                self._wake_preparing = False

    def _prepare_wake_engines(self):
        with self._wake_engine_lock:
            self._wake_preparing = True
            self._wake_prepared = False
            try:
                configured = settings.voice_wake_engine
                if not settings.voice_enabled or configured == "stt":
                    self._local_wake.unload()
                    self._nano_wake.unload()
                elif configured == "local_wake":
                    self._nano_wake.unload()
                    self._local_wake.load()
                else:
                    self._nano_wake.load()
                    if self._nano_wake.available():
                        self._local_wake.unload()
                    else:
                        # Preserve the existing Nano -> local -> STT fallback;
                        # a working Nano model never loads the local templates.
                        self._local_wake.load()
                self._wake_configured = configured
                self._wake_prepared = True
            finally:
                self._wake_preparing = False

    def _capture_loop(self) -> None:
        while self._running:
            self._cached_hardware_probe()
            if not self._start_stream():
                self._stop_event.wait(1.5)
                continue
            try:
                if settings.voice_enabled:
                    self._set_voice_state("idle")
                while self._running and self._stream_proc is not None and self._stream_proc.stdout is not None:
                    # Read epoch first: controller Stop publishes cancellation
                    # before resetting the inbox, so any read spanning that
                    # boundary fails the conditional push below.
                    capture_epoch = self._audio_input.epoch
                    context = self._input_context_callback() if self._input_context_callback else None
                    chunk = self._read_chunk(self._stream_proc.stdout, self._chunk_bytes)
                    if not chunk or len(chunk) != self._chunk_bytes:
                        self._set_error("Microphone stopped producing audio")
                        break
                    self._audio_input.push(chunk, context=context, expected_epoch=capture_epoch)
                    tap = self.live_audio_tap
                    if tap is not None:
                        tap.push(chunk)  # Bounded, nonblocking; no encoding or networking here.
            except Exception as exc:
                self._set_error(str(exc))
                self._log.warning("Microphone stream loop failed: %s", exc)
            finally:
                self._stop_stream()
                self._audio_input.reset()
            if self._running:
                self._stop_event.wait(1.0)

    def _processing_loop(self):
        previous_epoch = self._audio_input.epoch
        while self._running:
            packet = self._audio_input.receive()
            if packet is None:
                return
            if packet.epoch != previous_epoch:
                self.cancel_followup()
                self._capture = None
                self._recent_chunks.clear()
                self._speech_streak = 0
                self._mode = "wake"
                for engine in (self._local_wake, self._nano_wake):
                    try:
                        engine.reset_stream()
                    except Exception as exc:
                        # A failing optional model must not kill the sole audio
                        # consumer and leave capture filling its queue forever.
                        engine.unload()
                        self._set_error(f"Wake engine reset failed: {exc}")
                        self._log.exception("Wake engine stream reset failed")
                with self._state_lock:
                    collectors, self._manual_collectors = self._manual_collectors, []
                for collector in collectors:
                    collector["error"] = "Recording interrupted by an audio stream gap; record again"
                    collector["event"].set()
                # Even an in-place native reset can take longer than the audio
                # freshness budget. Coalesce gaps during reset and resume only
                # fresh audio; otherwise stale packets cause a reset loop.
                if not self._audio_input.current(packet):
                    previous_epoch = self._audio_input.reset(
                        "Wake state reset completed; stale buffered audio discarded")
                    continue
                previous_epoch = packet.epoch
            if not self._audio_input.current(packet):
                continue
            self._processing_local.packet = packet
            self._processing_local.context = packet.context
            started = time.perf_counter()
            try:
                self._handle_chunk(packet.data)
            except Exception as exc:
                self._set_error(f"Audio processing failed: {exc}")
                self._log.exception("Audio processing failed")
            finally:
                self._processing_local.packet = None
                self._processing_local.context = None
                self._audio_input.processing_time((time.perf_counter() - started) * 1000)
                if not self._audio_input.current(packet) and packet.epoch == self._audio_input.epoch:
                    self._audio_input.reset("Audio processing deadline exceeded; utterance discarded")

    def _audio_chunk_current(self):
        packet = getattr(self._processing_local, "packet", None)
        return packet is None or self._audio_input.current(packet)

    def _invoke_input_callback(self, callback, *args):
        if self._input_context_callback is None:
            return callback(*args)
        context = getattr(self._processing_local, "context", None)
        return callback(*args, context=context)

    def _start_stream(self) -> bool:
        try:
            self._stream_proc = subprocess.Popen(
                [
                    "arecord",
                    "-D",
                    settings.mic_device,
                    "-f",
                    "S16_LE",
                    "-c",
                    "1",
                    "-r",
                    str(settings.mic_sample_rate),
                    "-t",
                    "raw",
                    "-q",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
            self._set_error("")
            return True
        except Exception as exc:
            self._set_error(str(exc))
            self._log.warning("Microphone stream start failed: %s", exc)
            self._stream_proc = None
            return False

    def _stop_stream(self) -> None:
        proc = self._stream_proc
        self._stream_proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def followup_listening(self) -> bool:
        """Actual open input window, excluding playback, echo guard and STT."""
        with self._state_lock:
            now = time.monotonic()
            followup = ((self._capture or {}).get('followup') or self._armed_followup or {})
            return bool(self._running and self._wake_enabled and settings.voice_enabled
                        and (settings.conversation_followup_enabled or followup.get('game')) and now >= self._ignore_until
                        and ((self._mode == "armed" and self._armed_followup is not None
                              and now < self._armed_until)
                             or (self._capture and self._capture.get("followup") is not None)))

    def _handle_chunk(self, chunk: bytes) -> None:
        try:
            self._handle_chunk_inner(chunk)
        finally:
            # Callback only enqueues a display update; no SPI I/O on the PCM lane.
            active = self.followup_listening()
            if active != self._followup_cue_active:
                self._followup_cue_active = active
                if self._followup_state_callback is not None:
                    try:
                        self._invoke_input_callback(self._followup_state_callback, active)
                    except Exception:
                        self._log.exception("Follow-up display notification failed")

    def _handle_chunk_inner(self, chunk: bytes) -> None:
        now = time.monotonic()
        rms, peak = pcm.levels(chunk, 2)
        level_percent = max(0, min(100, int((rms / 32767.0) * 100)))
        self._feed_manual_collectors(chunk)
        if rms < self._current_speech_threshold() * 0.7:
            self._noise_floor_rms = (self._noise_floor_rms * 0.96) + (rms * 0.04)
        with self._state_lock:
            self._last_level = {
                "ok": True,
                "rms": int(rms),
                "peak": int(peak),
                "level_percent": level_percent,
                "voice_state": self._voice_state,
                "noise_floor_rms": int(self._noise_floor_rms),
                "speech_threshold_rms": self._current_speech_threshold(),
            }
        if not settings.voice_enabled:
            return
        if not self.wake_enabled() and not self.wake_wait_active():
            self._speech_streak = 0
            self._recent_chunks.clear()
            self._set_voice_state("idle")
            return
        self._apply_pending_followup(now)
        with self._state_lock:
            game_window = (self._capture or {}).get('followup') or self._armed_followup
        if game_window and game_window.get('game'):
            try:
                signal = game_window.get('cancelled')
                stale = signal is None or (signal.is_set() if hasattr(signal, 'is_set') else signal())
            except Exception:
                stale = True
            if stale:
                self._capture = None
                self.cancel_followup()
                self._set_voice_state('idle')
                return
        if now < self._ignore_until:
            self._speech_streak = 0
            self._recent_chunks.clear()
            return
        if self._can_listen_callback is not None and not self._can_listen_callback():
            if self._mode == "armed" and self._armed_followup is not None and self._capture is None:
                # A later speech segment or foreground task can start after an
                # earlier segment opened attention. Requeue the FULL window;
                # do not let its deadline expire behind our own speaker output.
                with self._state_lock:
                    if self._pending_followup is None:
                        self._pending_followup = dict(self._armed_followup, expires_at=now + 10.)
                    self._armed_followup = None
                    self._armed_until = 0.
                    self._mode = "wake"
                self._set_voice_state("idle")
            if self._capture is not None and self._capture.get("followup") is not None:
                self._capture = None
                self.cancel_followup()
                self._mode = "wake"
                self._set_voice_state("idle")
            self._speech_streak = 0
            self._recent_chunks.clear()
            return
        self._recent_chunks.append(chunk)
        followup = ((self._mode == "armed" and self._armed_followup is not None)
                    or bool(self._capture and self._capture.get("followup") is not None))
        threshold = self._current_speech_threshold() * (1.5 if followup else 1.)
        is_speech = rms >= threshold
        if self._capture is None:
            self._handle_idle_chunk(chunk, now, rms, is_speech)
        else:
            self._handle_capture_chunk(chunk, now, rms, is_speech)

    def _handle_idle_chunk(self, chunk: bytes, now: float, rms: int, is_speech: bool) -> None:
        if self._mode == "processing_wake":
            self._speech_streak = 0
            self._set_voice_state("processing_wake")
            return
        if self._mode == "processing_command":
            self._speech_streak = 0
            self._set_voice_state("transcribing_command")
            return
        if self._mode == "armed":
            if now >= self._armed_until:
                self._armed_followup = None
                self._mode = "wake"
                self._speech_streak = 0
                self._set_voice_state("idle")
                return
            self._set_voice_state("armed")
            if is_speech:
                self._speech_streak += 1
            else:
                self._speech_streak = 0
            start_chunks = max(1, settings.voice_command_start_chunks)
            if self._armed_followup is not None:
                start_chunks = max(start_chunks, math.ceil(160 / self._chunk_ms))
            if self._speech_streak >= start_chunks:
                self._begin_capture("command", now)
                self._speech_streak = 0
                self._handle_capture_chunk(chunk, now, rms, is_speech)
            return
        self._set_voice_state("idle")
        wake_engine = self._effective_wake_engine()
        if wake_engine in {"disabled", "loading", "not_loaded"}:
            return
        if wake_engine == "nanowakeword":
            detection = self._nano_wake.process_chunk(chunk)
            if detection is not None and self._audio_chunk_current():
                self._process_local_wake_detection(detection)
            return
        if wake_engine == "local_wake":
            detection = self._local_wake.process_chunk(chunk, rms)
            if detection is not None and self._audio_chunk_current():
                self._process_local_wake_detection(detection)
            return
        if is_speech:
            self._speech_streak += 1
        else:
            self._speech_streak = 0
        if self._speech_streak >= max(1, settings.voice_start_chunks):
            self._begin_capture("wake", now)
            self._speech_streak = 0
            self._handle_capture_chunk(chunk, now, rms, is_speech)

    def _begin_capture(self, mode: str, now: float) -> None:
        with self._state_lock:
            # A fresh utterance supersedes any late notification for the prior
            # reply, without discarding the utterance that is about to start.
            self._followup_epoch += 1
            self._pending_followup = None
        self._capture = {
            "mode": mode,
            "followup": (dict(self._armed_followup, token=self.followup_token())
                         if mode == "command" and self._mode == "armed"
                         and self._armed_followup is not None else None),
            # The current frame is appended once by _handle_capture_chunk.
            "chunks": list(self._recent_chunks)[:-1],
            "started_at": now,
            "last_speech_at": now,
            "speech_chunks": (max(0, self._speech_streak - 1) if mode == "command"
                              and self._mode == "armed" and self._armed_followup is not None else 0),
            "peak_rms": 0,
        }
        self._set_voice_state("detecting_wake" if mode == "wake" else "recording_command")

    def _handle_capture_chunk(self, chunk: bytes, now: float, rms: int, is_speech: bool) -> None:
        capture = self._capture
        if capture is None:
            return
        capture["chunks"].append(chunk)
        if is_speech:
            capture["speech_chunks"] += 1
            capture["last_speech_at"] = now
        capture["peak_rms"] = max(capture["peak_rms"], int(rms))
        mode = capture["mode"]
        max_s = settings.voice_wake_phrase_max_s if mode == "wake" else settings.voice_command_max_s
        min_s = settings.voice_min_wake_s if mode == "wake" else settings.voice_min_command_s
        silence_s = (
            settings.voice_silence_ms / 1000.0
            if mode == "wake"
            else settings.voice_command_silence_ms / 1000.0
        )
        if (now - capture["started_at"]) >= max_s:
            self._finalize_capture(capture)
            return
        silence_elapsed = now - capture["last_speech_at"]
        total_elapsed = now - capture["started_at"]
        if silence_elapsed >= silence_s and total_elapsed >= min_s:
            self._finalize_capture(capture)

    def _finalize_capture(self, capture: dict) -> None:
        if not self._audio_chunk_current():
            self._capture = None
            return
        if self._capture is capture:
            self._capture = None
        if not self._capture_has_enough_speech(capture):
            if capture.get("followup") is not None:
                self._last_followup_rejection = "too_little_speech"
            if capture["mode"] == "wake":
                with self._state_lock:
                    self._stats["wake_dropped_local"] += 1
            self._mode = "wake"
            self._ignore_until = time.monotonic() + 0.25
            self._set_voice_state("idle")
            return
        if capture.get("followup") is not None:
            from robbi.audio.followup_gate import rejection
            reason = rejection(b"".join(capture["chunks"]), settings.mic_sample_rate,
                               minimum_rms=settings.voice_min_rms)
            if reason:
                self._last_followup_rejection = reason
                self.cancel_followup()
                self._mode = "wake"
                self._ignore_until = time.monotonic() + .25
                self._set_voice_state("idle")
                return
            self._last_followup_rejection = ""
        wav_bytes = self._chunks_to_wav_bytes(capture["chunks"])
        duration_s = len(wav_bytes) / float(settings.mic_sample_rate * 2) if wav_bytes else 0.0
        if duration_s <= 0:
            self._mode = "wake"
            self._set_voice_state("idle")
            return
        if capture["mode"] == "wake":
            self._mode = "processing_wake"
            self._set_voice_state("transcribing_wake")
            self._submit_transcription(self._process_wake_capture, wav_bytes, "wake-stt")
        else:
            self._mode = "processing_command"
            self._followup_processing_token = (capture.get("followup") or {}).get("token")
            self._set_voice_state("transcribing_command")
            if capture.get("followup") is None and self._listening_finished_callback is not None:
                try:
                    self._invoke_input_callback(self._listening_finished_callback)
                except Exception as exc:
                    self._log.warning("Listening finished callback failed: %s", exc)
            self._submit_transcription(self._process_command_capture, wav_bytes, "command-stt",
                                       followup=capture.get("followup"))

    def _submit_transcription(self, callback, wav_bytes, name, **options):
        if not self._audio_chunk_current():
            return
        if not self._transcription_slots.acquire(blocking=False):
            self._set_error("Voice processing is busy; utterance discarded")
            self._mode = "wake"
            self._set_voice_state("idle")
            return
        generation, deadline = (self._generation, self._audio_input.epoch), time.monotonic() + 20.0
        context = getattr(self._processing_local, "context", None)
        def run():
            self._processing_local.context = context
            try:
                callback(wav_bytes, generation=generation, deadline=deadline, **options)
            finally:
                self._processing_local.context = None
                self._transcription_slots.release()
        threading.Thread(target=run, daemon=True, name=name).start()

    def _transcription_current(self, generation, deadline):
        current = (self._generation, self._audio_input.epoch) if isinstance(generation, tuple) else self._generation
        if not self._running or generation != current:
            return False
        if time.monotonic() > deadline:
            self._mode = "wake"
            self._set_error("Voice request expired and was discarded; ready for another command")
            self._set_voice_state("idle")
            return False
        return True

    def _process_wake_capture(self, wav_bytes: bytes, *, generation=None, deadline=None) -> None:
        generation = self._generation if generation is None else generation
        deadline = time.monotonic() + 20 if deadline is None else deadline
        if not self._transcription_current(generation, deadline):
            return
        with self._state_lock:
            self._stats["wake_transcribe_requests"] += 1
        transcript = self._transcribe("wake", wav_bytes)
        if not self._transcription_current(generation, deadline):
            return
        with self._state_lock:
            self._last_wake_attempt = transcript
        if transcript and self._matches_wake_phrase(transcript):
            with self._state_lock:
                self._last_wake_transcript = transcript
                self._stats["wake_detected"] += 1
            if self._consume_wake_wait():
                return
            self._mode = "armed"
            self._armed_followup = None
            self._ignore_until = time.monotonic() + self._post_wake_block_s()
            self._armed_until = self._ignore_until + settings.voice_command_wait_s
            self._set_voice_state("armed")
            if self._wake_callback is not None:
                try:
                    self._invoke_input_callback(self._wake_callback, transcript)
                except Exception as exc:
                    self._log.warning("Wake callback failed: %s", exc)
        else:
            self._mode = "wake"
            cooldown_s = settings.voice_reject_wake_cooldown_s
            if not transcript:
                cooldown_s = settings.voice_empty_wake_cooldown_s
                with self._state_lock:
                    self._stats["wake_transcribe_empty"] += 1
            self._ignore_until = time.monotonic() + cooldown_s
            self._set_voice_state("idle")

    def _process_command_capture(self, wav_bytes: bytes, *, generation=None, deadline=None, followup=None) -> None:
        generation = self._generation if generation is None else generation
        deadline = time.monotonic() + 20 if deadline is None else deadline

        def current():
            # An old request must not reset a newer command's state, even when
            # its provider response arrives after its deadline.
            if followup is not None:
                if followup.get("token") != self.followup_token():
                    return False
                if (not settings.conversation_followup_enabled and not followup.get('game')) or not self._wake_enabled:
                    self.cancel_followup()
                    return False
                if followup.get('game'):
                    # Recheck the game epoch after STT too: a removed, stopped,
                    # switched or expired game cannot consume a delayed answer.
                    signal = followup.get('cancelled')
                    if signal is None or (signal.is_set() if hasattr(signal, 'is_set') else signal()):
                        self.cancel_followup()
                        return False
            return self._transcription_current(generation, deadline)

        if not current():
            return
        transcript = self._transcribe("followup" if followup is not None else "command", wav_bytes)
        if not current():
            return
        if not transcript and followup is None:
            transcript = self._transcribe("command_retry", wav_bytes)
        if not current():
            return
        if followup is not None:
            accepted = False
            try:
                if transcript and self._followup_check_callback is not None:
                    accepted = self._invoke_input_callback(
                        self._followup_check_callback, transcript, followup["reply"]) is True
            except Exception:
                self._log.warning("Follow-up check unavailable; listening ended quietly")
            if not current():
                return
            if not accepted:
                self.cancel_followup()
                self._mode = "wake"
                self._ignore_until = time.monotonic() + settings.voice_cooldown_s
                self._set_voice_state("idle")
                return
        with self._state_lock:
            self._last_command_transcript = transcript
        self._mode = "wake"
        self._ignore_until = time.monotonic() + settings.voice_cooldown_s
        self._set_voice_state("idle")
        if self._command_callback is not None:
            try:
                self._invoke_input_callback(self._command_callback, transcript, wav_bytes)
            except Exception as exc:
                self._log.warning("Voice command callback failed: %s", exc)

    def _process_local_wake_detection(self, detection: dict) -> None:
        transcript = f"local:{settings.voice_wake_phrase}"
        with self._state_lock:
            self._last_wake_attempt = transcript
            self._last_wake_transcript = transcript
            self._stats["wake_detected"] += 1
        if self._consume_wake_wait():
            return
        self._mode = "armed"
        self._armed_followup = None
        self._ignore_until = time.monotonic() + self._post_wake_block_s()
        self._armed_until = self._ignore_until + settings.voice_command_wait_s
        self._set_voice_state("armed")
        if self._wake_callback is not None:
            try:
                self._invoke_input_callback(self._wake_callback, transcript)
            except Exception as exc:
                self._log.warning("Wake callback failed: %s", exc)

    def _transcribe(self, purpose: str, wav_bytes: bytes) -> str:
        if self._transcribe_callback is None:
            return ""
        try:
            return str(self._transcribe_callback(purpose, wav_bytes) or "").strip()
        except Exception as exc:
            self._set_error(str(exc))
            self._log.warning("Microphone transcription failed: %s", exc)
            return ""

    def _feed_manual_collectors(self, chunk: bytes) -> None:
        finished: list[dict] = []
        with self._state_lock:
            for collector in list(self._manual_collectors):
                collector["chunks"].append(chunk)
                collector["remaining"] -= 1
                if collector["remaining"] <= 0:
                    finished.append(collector)
                    self._manual_collectors.remove(collector)
        for collector in finished:
            collector["event"].set()

    def _record_wav_bytes_once(self, duration_s: float) -> bytes:
        duration = max(1, min(10, int(round(duration_s))))
        try:
            result = subprocess.run(
                [
                    "arecord",
                    "-D",
                    settings.mic_device,
                    "-d",
                    str(duration),
                    "-f",
                    "S16_LE",
                    "-c",
                    "1",
                    "-r",
                    str(settings.mic_sample_rate),
                    "-t",
                    "wav",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(15, int(duration * 4)),
                check=False,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", "replace")
                self._set_error(stderr.strip())
                return b""
            return bytes(result.stdout)
        except Exception as exc:
            self._set_error(str(exc))
            self._log.warning("One-shot microphone record failed: %s", exc)
            return b""

    def _chunks_to_wav_bytes(self, chunks: list[bytes]) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(settings.mic_sample_rate)
            wav_file.writeframes(b"".join(chunks))
        return buffer.getvalue()

    def _read_chunk(self, stream, size: int) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + 2.0
        while len(data) < size and self._running:
            if os.name == "posix":
                ready, _, _ = select.select([stream], [], [], 0.2)
                if not ready:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Microphone stream stalled")
                    continue
            part = stream.read(size - len(data))
            if not part:
                break
            data.extend(part)
        return bytes(data)

    def _is_speech(self, rms: int) -> bool:
        return rms >= self._current_speech_threshold()

    def _capture_has_enough_speech(self, capture: dict) -> bool:
        mode = capture.get("mode")
        speech_chunks = int(capture.get("speech_chunks", 0))
        duration_s = len(capture.get("chunks", [])) * (self._chunk_ms / 1000.0)
        if mode == "wake":
            return (
                duration_s >= settings.voice_min_wake_s
                and speech_chunks >= max(1, settings.voice_min_wake_speech_chunks)
            )
        if capture.get("followup") is not None:
            return (duration_s >= settings.voice_min_command_s
                    and speech_chunks * self._chunk_ms >= 160
                    and speech_chunks >= max(1, settings.voice_min_command_speech_chunks))
        return (
            duration_s >= settings.voice_min_command_s
            and speech_chunks >= max(1, settings.voice_min_command_speech_chunks)
        )

    def _current_speech_threshold(self) -> int:
        return max(settings.voice_min_rms, int(self._noise_floor_rms * settings.voice_noise_multiplier))

    def _post_wake_block_s(self) -> float:
        if str(settings.voice_ready_sound or "").strip():
            return max(0.0, float(settings.voice_ready_tone_block_s))
        return min(max(0.0, float(settings.voice_ready_tone_block_s)), 0.15)

    def _effective_wake_engine(self) -> str:
        if not settings.voice_enabled:
            return "disabled"
        if self._wake_preparing:
            return "loading"
        if not self._wake_prepared or self._wake_configured != settings.voice_wake_engine:
            return "not_loaded"
        configured = settings.voice_wake_engine
        if configured == "nanowakeword":
            if self._nano_wake.available():
                return "nanowakeword"
            if self._local_wake.available():
                return "local_wake"
            return "stt"
        if configured == "local_wake":
            if self._local_wake.available():
                return "local_wake"
            return "stt"
        if configured == "stt":
            return "stt"
        if self._nano_wake.available():
            return "nanowakeword"
        if self._local_wake.available():
            return "local_wake"
        return "stt"

    def _matches_wake_phrase(self, transcript: str) -> bool:
        normalized_transcript = self._normalize_text(transcript)
        normalized_phrase = self._normalize_text(settings.voice_wake_phrase)
        if not normalized_phrase:
            return False
        if normalized_phrase in normalized_transcript:
            return True
        phrase_words = normalized_phrase.split()
        transcript_words = normalized_transcript.split()
        if not phrase_words or len(transcript_words) < len(phrase_words):
            return False
        for index in range(0, len(transcript_words) - len(phrase_words) + 1):
            if transcript_words[index : index + len(phrase_words)] == phrase_words:
                return True
        if self._matches_robbi_alias(normalized_transcript):
            return True
        return False

    def _matches_robbi_alias(self, normalized_transcript: str) -> bool:
        words = normalized_transcript.split()
        if not words:
            return False
        greeting_words = {"hey", "hi", "hello", "yo"}
        robbi_aliases = {
            "robbi",
            "robby",
            "robbie",
            "robi",
            "robie",
            "roby",
            "robbiee",
        }
        has_greeting = any(word in greeting_words for word in words)
        has_name = any(word in robbi_aliases or word.startswith("robb") or word.startswith("robi") for word in words)
        return has_greeting and has_name

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    def _set_voice_state(self, state: str) -> None:
        with self._state_lock:
            self._voice_state = state
            if self._last_level:
                self._last_level["voice_state"] = state

    def _set_error(self, message: str) -> None:
        with self._state_lock:
            self._last_error = (message or "").strip()
