from __future__ import annotations

import logging
import math
import queue
import subprocess
import tempfile
import threading
import time
import wave
from robbi.audio import pcm
import json
import os
import shutil
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path

try:
    import cv2
except (ImportError, OSError):
    cv2 = None
try:
    from PIL import Image, ImageOps, ImageDraw
except (ImportError, OSError):
    Image = ImageOps = ImageDraw = None

from robbi.configuration.settings import STATE_DIR, settings


MEDIA_STATE_FILE = STATE_DIR / "media.json"
DEFAULT_ALSA_DEVICE = "plughw:CARD=MAX98357A,DEV=0"
MAX_PCM_BYTES = 16 * 1024 * 1024
PCM_CHUNK_BYTES = 64 * 1024


class _MediaCancellation:
    """Event-compatible cancellation without another polling thread."""
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = .05 if deadline is None else min(.05, deadline - time.monotonic())
            if remaining <= 0:
                return False
            time.sleep(remaining)
        return True

try:
    import st7789

    LCD_AVAILABLE = True
except (ImportError, OSError, RuntimeError):
    st7789 = None
    LCD_AVAILABLE = False


class Display:
    def __init__(self, orchestrator=None) -> None:
        self._log = logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._disp = None
        self._initializing = None
        self._closed = False
        self._pending_image = None
        self._last_error = ""
        self._last_asset_error = ""
        self._faces = OrderedDict()
        self._app_frame = None
        self._app_listening = False
        self.orchestrator = orchestrator

    def bind_orchestrator(self, orchestrator):
        self.orchestrator = orchestrator

    def start(self):
        """Initialize optional SPI only in the supervised background lifecycle."""
        with self._lock:
            if self._closed or not settings.enable_lcd:
                return False
            if self._disp is not None:
                return True
            if self._initializing is not None:
                return False
            if not LCD_AVAILABLE:
                self._last_error = "ST7789 driver is unavailable"
                return False
            token = self._initializing = object()
        driver = None
        published = False
        try:
            # The constructor and reset both issue SPI commands. Keep this
            # driver private until the entire sequence has completed; holding
            # the public frame lock here would block boot on a stuck driver.
            driver = st7789.ST7789(
                    port=0,
                    cs=0,
                    dc=25,
                    rst=24,
                    width=280,
                    height=240,
                    rotation=0,
                    invert=True,
                    offset_left=20,
                    offset_top=0,
                    spi_speed_hz=80_000_000,
            )
            driver.reset()
            driver._init()
            with self._lock:
                if self._closed or self._initializing is not token:
                    return False
                self._disp = driver
                published = True
                self._last_error = ""
                pending, self._pending_image = self._pending_image, None
                if pending is not None:
                    self._show_pending_image(pending)
            return True
        except Exception as exc:
            with self._lock:
                if not self._closed:
                    self._last_error = str(exc)
            self._log.warning("LCD init failed: %s", exc)
            return False
        finally:
            with self._lock:
                if self._initializing is token:
                    self._initializing = None
            if driver is not None and not published:
                self._close_driver(driver)

    def _show_pending_image(self, pending):
        image, intent, cancelled, hold_s = pending
        if cancelled.is_set():
            return
        lease = None
        shown = False
        try:
            if self.orchestrator:
                lease = self.orchestrator.acquire(("display",), intent=intent,
                                                  cancel_event=cancelled, wait_s=0)
            if (lease or cancelled).is_set():
                return
            self._app_frame = None
            self._app_listening = False
            self._disp.display(image)
            shown = True
        except Exception as exc:
            from robbi.errors import BusyError, UnavailableError
            if not isinstance(exc, (BusyError, UnavailableError)):
                self._last_error = str(exc)
                self._log.warning("Startup display write failed: %s", exc)
        finally:
            if lease is not None:
                lease.release(hold_s=hold_s if shown and not lease.is_set() else 0)

    def show_image(self, path: Path, *, cancel_event=None, hold_s=0, defer_until_ready=False, _app=False, _presentation_signal=None) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return False
        lease = self.orchestrator.acquire(("display",), cancel_event=cancel_event) if self.orchestrator else None
        cancelled = lease or cancel_event
        shown = False
        try:
            intent = lease.intent if lease is not None else None
            deferred = (intent, _MediaCancellation(intent, cancel_event), hold_s) if defer_until_ready else None
            shown = self._show_image(path, cancel_event=cancelled, deferred=deferred,
                                     app=(intent, _presentation_signal if _presentation_signal is not None else cancel_event, hold_s) if _app else None)
            return shown
        finally:
            if lease is not None:
                # Never transfer ownership while the native write still runs.
                lease.release(hold_s=hold_s if shown and not lease.is_set() else 0)

    def _show_image(self, path: Path, *, cancel_event=None, deferred=None, app=None) -> bool:
        with self._lock:
            # A frame can wait behind a native display write. Recheck after
            # acquiring the lock so Stop cannot be followed by a stale frame.
            if self._closed or (cancel_event is not None and cancel_event.is_set()):
                return False
            try:
                if Image is None:
                    raise RuntimeError("Pillow is required to decode face images")
                path = Path(path)
                stat = path.stat()
                if stat.st_size > 16 * 1024 * 1024:
                    raise ValueError("Face image exceeds the 16 MiB file limit")
                key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
                image = self._faces.get(key)
                if image is None:
                    with Image.open(path) as source:
                        if source.width * source.height > 16_000_000:
                            raise ValueError("Face image exceeds the 16 megapixel decode limit")
                        image = ImageOps.pad(ImageOps.exif_transpose(source).convert("RGB"),
                                             (280, 240), color="black", method=Image.Resampling.LANCZOS)
                    self._faces[key] = image
                    while len(self._faces) > 12:
                        self._faces.popitem(last=False)
                self._faces.move_to_end(key)
                self._last_asset_error = ""
            except Exception as exc:
                self._last_asset_error = str(exc)
                self._log.warning("Face could not be displayed: %s", exc)
                return False
            if cancel_event is not None and cancel_event.is_set():
                return False
            if self._disp is None:
                if deferred is not None and settings.enable_lcd:
                    # Startup only: coalesce restarting/ready faces into one
                    # latest frame, retaining their original cancellation.
                    self._pending_image = (image, *deferred)
                return False
            try:
                self._app_frame = None
                self._app_listening = False
                self._disp.display(image)
                if app is not None and (cancel_event is None or not cancel_event.is_set()):
                    intent, signal, hold_s = app
                    self._app_frame = (image, intent, signal, time.monotonic() + hold_s)
                self._last_error = ""
                return cancel_event is None or not cancel_event.is_set()
            except Exception as exc:
                self._last_error = str(exc)
                self._log.warning("Display write failed: %s", exc)
                return False

    def show_app_image(self, path: Path, *, cancel_event=None, hold_s=60, presentation_signal=None) -> bool:
        """Publish an app base frame atomically with its normal display lease."""
        return self.show_image(path, cancel_event=cancel_event, hold_s=hold_s, _app=True,
                               _presentation_signal=presentation_signal)

    def set_app_listening(self, active: bool) -> bool:
        """Decorate only the current, uncancelled app frame without changing owner."""
        from robbi.errors import BusyError, UnavailableError
        active = bool(active)
        with self._lock:
            state = self._app_frame
            if self._closed or self._disp is None or state is None:
                return False
            image, intent, signal, deadline = state
            cancelled = _MediaCancellation(intent, signal)
            if cancelled.is_set() or time.monotonic() >= deadline:
                self._app_frame = None
                self._app_listening = False
                return False
            if self._app_listening == active:
                return True
            lease = None
            try:
                if self.orchestrator is not None:
                    lease = self.orchestrator.acquire(("display",), intent=intent,
                                                       cancel_event=cancelled, wait_s=0)
                if (lease or cancelled).is_set() or time.monotonic() >= deadline:
                    return False
                frame = image
                if active:
                    frame = image.copy()
                    draw = ImageDraw.Draw(frame)
                    # A compact microphone badge leaves the game board visible.
                    draw.rounded_rectangle((246, 4, 275, 37), radius=8,
                                           fill=(15, 35, 48), outline=(95, 225, 255), width=2)
                    draw.rounded_rectangle((257, 10, 264, 23), radius=3, fill=(95, 225, 255))
                    draw.arc((253, 16, 268, 28), 0, 180, fill=(255, 255, 255), width=2)
                    draw.line((261, 28, 261, 32), fill="white", width=2)
                    draw.line((257, 32, 265, 32), fill="white", width=2)
                self._disp.display(frame)
                self._app_listening = active
                self._last_error = ""
                return not (lease or cancelled).is_set()
            except (BusyError, UnavailableError):
                return False
            except Exception as exc:
                self._last_error = str(exc)
                return False
            finally:
                if lease is not None:
                    lease.release(hold_s=max(0, deadline - time.monotonic()))

    def health(self):
        return {"enabled": bool(settings.enable_lcd), "available": self._disp is not None and not self._last_error,
                "app_listening": self._app_listening,
                "last_error": self._last_error or self._last_asset_error,
                "last_asset_error": self._last_asset_error, "cached_faces": len(self._faces),
                "hint": "Check SPI/display wiring or replace the invalid face asset."}

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._initializing = None
            self._pending_image = None
            self._faces.clear()
            self._app_frame = None
            self._app_listening = False
            display, self._disp = self._disp, None
        if display is not None:
            self._close_driver(display)

    def _close_driver(self, display):
        try:
            close = getattr(display, "close", None)
            if callable(close):
                close()
                return
            # st7789 1.0.1 has no public close method.
            spi = getattr(display, "_spi", None)
            if spi is not None:
                spi.close()
            released = set()
            for name in ("_dc", "_rst", "_bl"):
                pin = getattr(display, name, None)
                if isinstance(pin, tuple) and id(pin[0]) not in released:
                    pin[0].release()
                    released.add(id(pin[0]))
        except Exception as exc:
            self._last_error = str(exc)

    def show_pil_frame(self, frame, *, cancel_event=None) -> bool:
        """Write an already decoded screen frame, with the usual output ownership."""
        if Image is None or not isinstance(frame, Image.Image) or frame.size != (280, 240) or frame.mode != 'RGB':
            raise ValueError('LCD frames must be 280x240 RGB images')
        if cancel_event is not None and cancel_event.is_set():
            return False
        lease = self.orchestrator.acquire(('display',), cancel_event=cancel_event) if self.orchestrator else None
        signal = lease or cancel_event
        try:
            with self._lock:
                if self._closed or self._disp is None or (signal is not None and signal.is_set()):
                    return False
                try:
                    self._app_frame = None
                    self._app_listening = False
                    self._disp.display(frame)
                    self._last_error = ''
                    return signal is None or not signal.is_set()
                except Exception as exc:
                    self._last_error = str(exc)
                    return False
        finally:
            if lease is not None:
                lease.release()

    def show_frame(self, frame_rgb, *, cancel_event=None) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return False
        lease = self.orchestrator.acquire(("display",), cancel_event=cancel_event) if self.orchestrator else None
        try:
            return self._show_frame(frame_rgb, cancel_event=lease or cancel_event)
        finally:
            if lease is not None:
                lease.release()

    def _show_frame(self, frame_rgb, *, cancel_event=None) -> bool:
        with self._lock:
            if self._disp is None or (cancel_event is not None and cancel_event.is_set()):
                return False
            if cv2 is None or Image is None:
                self._last_error = "OpenCV and Pillow are required for video playback"
                return False
            if frame_rgb is None or getattr(frame_rgb, "ndim", 0) != 3 or not all(frame_rgb.shape[:2]):
                self._last_asset_error = "Invalid video frame"
                return False
            height, width = frame_rgb.shape[:2]
            scale = min(280 / width, 240 / height)
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            resized = cv2.resize(frame_rgb, new_size, interpolation=cv2.INTER_AREA)
            canvas = Image.new("RGB", (280, 240), (0, 0, 0))
            frame_image = Image.fromarray(resized)
            canvas.paste(frame_image, ((280 - new_size[0]) // 2, (240 - new_size[1]) // 2))
            if cancel_event is not None and cancel_event.is_set():
                return False
            try:
                self._app_frame = None
                self._app_listening = False
                self._disp.display(canvas)
                self._last_error = ""
                return cancel_event is None or not cancel_event.is_set()
            except Exception as exc:
                self._last_error = str(exc)
                return False


class PlaybackHandle:
    def __init__(self) -> None:
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = []
        self.returncode: int | None = None
        self.started_at: float | None = None

    def set_result(self, returncode: int | None) -> None:
        with self._lock:
            if self._done.is_set():
                return
            self.returncode = returncode
            callbacks, self._callbacks = self._callbacks, []
            self._done.set()
        for callback in callbacks:
            self._notify(callback)

    def add_done_callback(self, callback):
        """Run a tiny completion notification; no waiter thread is created."""
        if not callable(callback):
            raise TypeError("Playback completion callback must be callable")
        with self._lock:
            if not self._done.is_set():
                if len(self._callbacks) >= 16:
                    raise ValueError("Too many playback completion callbacks")
                self._callbacks.append(callback)
                return
        self._notify(callback)

    def _notify(self, callback):
        try:
            callback(self)
        except Exception:
            logging.getLogger(__name__).exception("Audio completion notification failed")

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout=timeout)
        return self.returncode


class AudioPlayer:
    def __init__(self, orchestrator=None) -> None:
        self._log = logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._volume_percent = self._load_volume()
        self._active_playback = 0
        self._current_proc: subprocess.Popen | None = None
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=8)
        self._closed = threading.Event()
        self._last_error = "" if shutil.which("aplay") else "ALSA aplay is unavailable"
        self._generation = 0
        self.orchestrator = orchestrator
        self._jobs = {}
        self._worker = threading.Thread(target=self._playback_loop, daemon=True, name="audio-player")
        self._worker.start()

    def bind_orchestrator(self, orchestrator):
        self.orchestrator = orchestrator

    def stop_owner(self, intent):
        """Cancel this owner's sounds without stopping another activity."""
        if intent is None:
            return
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.get("intent") is intent]
        for job in jobs:
            job["owner_cancel"].set()

    def _release_job(self, job):
        with self._lock:
            self._jobs.pop(id(job["handle"]), None)
        lease = job.get("lease")
        if lease is not None:
            lease.release()

    def set_volume(self, volume_percent: int) -> int:
        volume = max(0, min(100, int(volume_percent)))
        with self._lock:
            self._volume_percent = volume
        self._save_volume(volume)
        return volume

    def volume(self) -> int:
        with self._lock:
            return self._volume_percent

    def is_playing(self) -> bool:
        with self._lock:
            return self._active_playback > 0

    def stop_all(self) -> None:
        with self._lock:
            self._generation += 1
            current_proc = self._current_proc
        if current_proc is not None:
            try:
                current_proc.terminate()
            except Exception:
                pass
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job is None:
                continue
            handle: PlaybackHandle = job["handle"]
            cleanup_path: Path | None = job["cleanup_path"]
            self._decrement_active_playback()
            try:
                if cleanup_path is not None:
                    cleanup_path.unlink(missing_ok=True)
            finally:
                self._release_job(job)
                handle.set_result(-1)

    def health(self):
        with self._lock:
            return {"available": not self._closed.is_set() and not self._last_error,
                    "last_error": self._last_error, "playing": self._active_playback > 0,
                    "queue_size": self._queue.qsize(), "queue_capacity": self._queue.maxsize,
                    "hint": "Check ALSA output device and speaker connection; install alsa-utils on Pi."}

    def shutdown(self):
        self._closed.set()
        self.stop_all()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
            if self._worker.is_alive():
                with self._lock:
                    process = self._current_proc
                if process is not None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                self._worker.join(timeout=0.5)

    def play_file(self, path: Path, wait: bool = False, allow_slow_volume_scale: bool = True,
                  *, delete_after: bool = False, cancel_event=None) -> PlaybackHandle | None:
        source_path = path
        cleanup_path: Path | None = Path(path) if delete_after else None
        handle = PlaybackHandle()
        if cancel_event is not None and cancel_event.is_set():
            if cleanup_path is not None:
                cleanup_path.unlink(missing_ok=True)
            handle.set_result(-1)
            return handle
        if self._closed.is_set() or not Path(path).is_file():
            self._last_error = "Audio player is stopped or the sound file is missing"
            if cleanup_path is not None:
                cleanup_path.unlink(missing_ok=True)
            handle.set_result(-1)
            return handle
        lease = None
        admitted = False
        try:
            lease = self.orchestrator.acquire(("audio",), cancel_event=cancel_event) if self.orchestrator else None
            owner_cancel = threading.Event()
            cancelled = _MediaCancellation(lease, owner_cancel) if lease is not None else cancel_event
            job = {"path": source_path, "cleanup_path": cleanup_path, "handle": handle,
                   "generation": self._generation, "queued_at": time.monotonic(),
                   "cancel_event": cancelled, "lease": lease,
                   "intent": lease.intent if lease is not None else None,
                   "owner_cancel": owner_cancel, "allow_slow_volume_scale": allow_slow_volume_scale}
            with self._lock:
                if self._closed.is_set() or (cancelled is not None and cancelled.is_set()):
                    handle.set_result(-1)
                    return handle
                job["generation"] = self._generation
                self._active_playback += 1
                self._jobs[id(handle)] = job
                try:
                    self._queue.put_nowait(job)
                    admitted = True
                except queue.Full:
                    self._last_error = "Audio queue is full; request was rejected"
                    self._jobs.pop(id(handle), None)
                    self._active_playback = max(0, self._active_playback - 1)
                    handle.set_result(-1)
        finally:
            if not admitted:
                if lease is not None:
                    lease.release()
                if cleanup_path is not None:
                    cleanup_path.unlink(missing_ok=True)
        if wait:
            handle.wait(timeout=120)
        return handle

    def duration_s(self, path: Path) -> float | None:
        try:
            with Path(path).open("rb") as source, wave.open(source, "rb") as handle:
                # Streaming providers can leave a placeholder data length in
                # the header. Bound it by bytes actually present, without
                # reading the audio just to determine a playback deadline.
                available = max(0, os.fstat(source.fileno()).st_size - source.tell())
                frame_bytes = handle.getnchannels() * handle.getsampwidth()
                frames = min(handle.getnframes(), available // frame_bytes)
                return frames / float(handle.getframerate())
        except Exception:
            return None

    def _scale_wav(self, path: Path, volume_percent: int, allow_slow_fallback: bool = True) -> Path | None:
        factor = max(0.0, min(1.0, volume_percent / 100.0))
        python_scaled = self._scale_wav_python(path, factor)
        if python_scaled is not None:
            return python_scaled
        if not allow_slow_fallback:
            return None
        return self._scale_wav_ffmpeg(path, factor)

    def _scale_wav_ffmpeg(self, path: Path, factor: float) -> Path | None:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                temp_path = Path(handle.name)
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(path),
                    "-filter:a",
                    f"volume={factor}",
                    "-c:a",
                    "pcm_s16le",
                    str(temp_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0:
                return temp_path
            temp_path.unlink(missing_ok=True)
        except Exception as exc:
            self._log.warning("FFmpeg volume scaling failed for %s: %s", path, exc)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return None

    def _scale_wav_python(self, path: Path, factor: float) -> Path | None:
        temp_path = None
        try:
            with wave.open(str(path), "rb") as src:
                params = src.getparams()
                if params.sampwidth not in (1, 2, 3, 4):
                    raise ValueError("Unsupported PCM sample width")
                frame_bytes = params.nchannels * params.sampwidth
                chunk_frames = max(1, PCM_CHUNK_BYTES // frame_bytes)
                total = 0
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    temp_path = Path(handle.name)
                with wave.open(str(temp_path), "wb") as dst:
                    # Do not copy an unknown-length provider header. wave
                    # patches the output length after writing the actual PCM.
                    dst.setparams(params._replace(nframes=0))
                    while True:
                        # Read at most one frame beyond the limit so an
                        # oversized input is rejected without a large buffer.
                        read_frames = min(chunk_frames, (MAX_PCM_BYTES - total) // frame_bytes + 1)
                        frames = src.readframes(read_frames)
                        if not frames:
                            break
                        total += len(frames)
                        if total > MAX_PCM_BYTES or len(frames) % frame_bytes:
                            raise ValueError("Speech PCM exceeds 16 MiB or contains an incomplete frame")
                        # WAV's 8-bit PCM is unsigned; gain uses signed PCM.
                        if params.sampwidth == 1:
                            frames = pcm.bias(frames, 1, -128)
                        scaled = pcm.mul(frames, params.sampwidth, factor)
                        if params.sampwidth == 1:
                            scaled = pcm.bias(scaled, 1, 128)
                        dst.writeframesraw(scaled)
                    if not total:
                        raise ValueError("Speech WAV contains no PCM frames")
            return temp_path
        except Exception as exc:
            self._log.warning("Volume scaling failed for %s: %s", path, exc)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            return None

    def _playback_loop(self) -> None:
        while not self._closed.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            path = job["path"]
            cleanup_path = job["cleanup_path"]
            source_cleanup = cleanup_path
            handle: PlaybackHandle = job["handle"]
            completion = -1
            try:
                cancelled = job.get("cancel_event")
                if job["generation"] != self._generation or time.monotonic() - job["queued_at"] > 30 or (cancelled is not None and cancelled.is_set()):
                    continue
                volume = self.volume()
                if volume != 100:
                    transformed = self._scale_wav(path, volume, job["allow_slow_volume_scale"])
                    if transformed is None:
                        raise RuntimeError("Cannot apply requested volume; playback rejected")
                    path = cleanup_path = transformed
                # Expose the playback dispatch boundary after optional decoding/
                # volume preparation so filming start-linked cues do not drift.
                handle.started_at = time.monotonic()
                returncode = (self._play_with_fallbacks(path, job["generation"], cancelled) if cancelled is not None
                              else self._play_with_fallbacks(path, job["generation"]))
                if job["generation"] != self._generation or self._closed.is_set() or (cancelled is not None and cancelled.is_set()):
                    continue
                if returncode == 0:
                    self._last_error = ""
                else:
                    self._last_error = "Audio playback failed or was stopped"
                completion = returncode
            except Exception as exc:
                if job["generation"] == self._generation and not self._closed.is_set() and not (job.get("cancel_event") is not None and job["cancel_event"].is_set()):
                    self._last_error = str(exc)
                self._log.warning("Audio playback failed for %s: %s", path, exc)
            finally:
                self._decrement_active_playback()
                try:
                    if cleanup_path is not None:
                        cleanup_path.unlink(missing_ok=True)
                    if source_cleanup is not None and source_cleanup != cleanup_path:
                        source_cleanup.unlink(missing_ok=True)
                finally:
                    # Process reaping and cleanup precede hardware handover.
                    self._release_job(job)
                    # Speech can reopen listening only after actual playback,
                    # cleanup and ownership release have finished.
                    handle.set_result(completion)

    def _play_with_fallbacks(self, path: Path, generation: int | None = None, cancel_event=None) -> int:
        last_returncode = -1
        generation = self._generation if generation is None else generation
        for device in self._playback_devices()[:3]:
            if self._closed.is_set() or generation != self._generation or (cancel_event is not None and cancel_event.is_set()):
                return -1
            returncode = self._play_with_device(path, device, generation, cancel_event)
            last_returncode = returncode
            if returncode == 0:
                return 0
        return last_returncode

    def _play_with_device(self, path: Path, device: str, generation: int | None = None, cancel_event=None) -> int:
        proc = None
        try:
            with self._lock:
                if self._closed.is_set() or (generation is not None and generation != self._generation) or (cancel_event is not None and cancel_event.is_set()):
                    return -1
            # Process creation can enter native code. Stop must still be able
            # to set its generation while this optional operation is blocked.
            proc = subprocess.Popen(
                ["aplay", "-D", device, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._lock:
                self._current_proc = proc
            timeout = min(120, max(3, (self.duration_s(path) or 5) + 2))
            deadline = time.monotonic() + timeout
            cancellation_sent = False
            while True:
                if (self._closed.is_set() or (generation is not None and generation != self._generation)
                    or (cancel_event is not None and cancel_event.is_set())) and not cancellation_sent:
                    cancellation_sent = True
                    proc.terminate()
                    deadline = min(deadline, time.monotonic() + 0.5)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(["aplay", str(path)], timeout)
                try:
                    _, stderr = proc.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired:
            if proc is None:
                return -1
            proc.kill()
            _, stderr = proc.communicate()
            self._log.warning("Audio playback timed out: device=%s path=%s stderr=%s", device, path, (stderr or "").strip())
            return -1
        finally:
            # Unexpected pipe/process errors must not transfer the speaker to
            # a new owner while a previous process can still produce audio.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
            with self._lock:
                if proc is not None and self._current_proc is proc:
                    self._current_proc = None
        if proc.returncode != 0:
            self._log.warning(
                "Audio playback returned %s: device=%s path=%s stderr=%s",
                proc.returncode,
                device,
                path,
                (stderr or "").strip(),
            )
        return int(proc.returncode or 0)

    def _playback_devices(self) -> list[str]:
        configured = str(settings.alsa_device or "").strip()
        candidates = [
            configured,
            DEFAULT_ALSA_DEVICE,
            "default:CARD=MAX98357A",
            "plughw:CARD=MAX98357A,DEV=0",
            "hw:CARD=MAX98357A,DEV=0",
            "sysdefault",
        ]
        return [device for index, device in enumerate(candidates) if device and device not in candidates[:index]]

    def _increment_active_playback(self) -> None:
        with self._lock:
            self._active_playback += 1

    def _decrement_active_playback(self) -> None:
        with self._lock:
            self._active_playback = max(0, self._active_playback - 1)

    def _load_volume(self) -> int:
        try:
            if MEDIA_STATE_FILE.exists():
                data = json.loads(MEDIA_STATE_FILE.read_text(encoding="utf-8"))
                return max(0, min(100, int(data.get("volume_percent", 100))))
        except Exception as exc:
            self._log.warning("Failed to load persisted volume: %s", exc)
        return 100

    def _save_volume(self, volume: int) -> None:
        temp_path = None
        try:
            MEDIA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=MEDIA_STATE_FILE.parent,
                                             prefix=".media-", suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump({"volume_percent": volume}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, MEDIA_STATE_FILE)
        except Exception as exc:
            self._log.warning("Failed to persist volume: %s", exc)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


class MediaManager:
    def __init__(self, orchestrator=None) -> None:
        self._log = logging.getLogger(__name__)
        self.orchestrator = orchestrator
        self.display = Display(orchestrator)
        self.audio = AudioPlayer(orchestrator)
        self._stopping = threading.Event()
        self._presentation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_video_playback = 0

    def bind_orchestrator(self, orchestrator):
        self.orchestrator = orchestrator
        self.display.bind_orchestrator(orchestrator)
        self.audio.bind_orchestrator(orchestrator)

    def start(self):
        return self.display.start()

    def health(self):
        display, audio = self.display.health(), self.audio.health()
        return {"available": display["available"] or audio["available"],
                "last_error": "; ".join(x for x in (display["last_error"], audio["last_error"]) if x),
                "display": display, "audio": audio,
                "hint": "Check individual display and audio diagnostics."}

    def shutdown(self):
        self._stopping.set()
        self.audio.shutdown()
        self.display.shutdown()

    def set_app_listening(self, active: bool) -> bool:
        return self.display.set_app_listening(active)

    def show_face(self, path: Path, *, cancel_event=None, hold_s=0, defer_until_ready=False) -> bool:
        return self.display.show_image(path, cancel_event=cancel_event, hold_s=hold_s,
                                       defer_until_ready=defer_until_ready)

    def play_sound(self, path: Path, wait: bool = False, *, cancel_event=None) -> PlaybackHandle | None:
        return self.audio.play_file(path, wait=wait, cancel_event=cancel_event)

    def duration_s(self, path: Path) -> float | None:
        return self.audio.duration_s(path)

    def set_volume(self, volume_percent: int) -> int:
        return self.audio.set_volume(volume_percent)

    def volume(self) -> int:
        return self.audio.volume()

    def is_audio_playing(self) -> bool:
        return self.audio.is_playing()

    def stop_audio(self, *, owner=None) -> None:
        if owner is None:
            self.audio.stop_all()
        else:
            self.audio.stop_owner(owner)

    def is_video_playing(self) -> bool:
        with self._state_lock:
            return self._active_video_playback > 0

    def speak_wav_bytes(self, wav_bytes: bytes, wait: bool = True, *, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return None
        if not wav_bytes or len(wav_bytes) > 16 * 1024 * 1024:
            raise ValueError("Speech audio must be nonempty and at most 16 MiB")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(wav_bytes)
        handle = self.audio.play_file(temp_path, wait=False, allow_slow_volume_scale=False, delete_after=True, cancel_event=cancel_event)
        if handle is None:
            temp_path.unlink(missing_ok=True)
            return None
        if wait:
            handle.wait(timeout=120)
        return handle

    def play_video(self, video_path: Path, audio_path: Path | None = None, *, cancel_event=None) -> dict:
        if self._stopping.is_set() or (cancel_event is not None and cancel_event.is_set()):
            return {"ok": False, "state": "cancelled", "reason": "Video cancelled", "frames_displayed": 0}
        if cv2 is None:
            return {"ok": False, "state": "unavailable", "reason": "OpenCV is unavailable", "frames_displayed": 0}
        if self.orchestrator is not None and not self.display.health().get("available"):
            return {"ok": False, "state": "unavailable", "reason": "Display is unavailable", "frames_displayed": 0}
        lease = None
        scope = nullcontext()
        if self.orchestrator is not None:
            from robbi.orchestration import intent_scope
            resources = ("display", "audio") if audio_path is not None and audio_path.is_file() else ("display",)
            lease = self.orchestrator.acquire(resources, cancel_event=cancel_event)
            scope = intent_scope(lease.intent)
        try:
            with scope, self._presentation_lock:
                cancelled = lease or cancel_event
                if self._stopping.is_set() or (cancelled is not None and cancelled.is_set()):
                    return {"ok": False, "state": "cancelled", "reason": "Video cancelled", "frames_displayed": 0}
                self._increment_active_video()
                try:
                    return self._play_video_locked(video_path, audio_path, cancelled)
                finally:
                    self._decrement_active_video()
        finally:
            if lease is not None:
                lease.release()

    def _play_video_locked(self, video_path: Path, audio_path: Path | None, cancel_event=None) -> dict:
        video_cancel = threading.Event()
        cancellation = _MediaCancellation(self._stopping, cancel_event, video_cancel)
        cap, audio_proc = None, None
        frames_displayed, frame_index = 0, 0
        audio_state = "not_requested" if audio_path is None else "missing"
        result = {"ok": False, "state": "failed", "reason": "Video contained no displayable frames"}
        try:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return {**result, "reason": "Video could not be opened", "frames_displayed": 0}
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
            if not math.isfinite(fps) or fps <= 0 or fps > 120:
                fps = 25.0
            frame_interval = 1.0 / fps
            start_at = time.perf_counter() + 0.15
            while time.perf_counter() < start_at and not cancellation.is_set():
                cancellation.wait(min(.01, max(0., start_at - time.perf_counter())))
            if audio_path is not None and audio_path.is_file() and not cancellation.is_set():
                audio_proc = self.audio.play_file(audio_path, wait=False, cancel_event=cancellation)
                audio_state = "playing" if audio_proc is not None else "unavailable"
            while not cancellation.is_set():
                target_time = start_at + (frame_index * frame_interval)
                now = time.perf_counter()
                if now < target_time:
                    cancellation.wait(min(0.01, target_time - now))
                    continue
                elif now - target_time > frame_interval:
                    skip_frames = min(10, int((now - target_time) / frame_interval))
                    ended = False
                    for _ in range(skip_frames):
                        if not cap.grab():
                            ended = True
                            break
                        frame_index += 1
                    if ended:
                        break
                    continue
                ok, frame = cap.read()
                if not ok or cancellation.is_set():
                    break
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if not self.display.show_frame(rgb, cancel_event=cancellation):
                    result = {"ok": False, "state": "failed", "reason": "Display rejected a video frame"}
                    video_cancel.set()
                    break
                frames_displayed += 1
                frame_index += 1
            if frames_displayed and not video_cancel.is_set():
                result = {"ok": True, "state": "completed", "reason": ""}
            elif not frames_displayed:
                video_cancel.set()
        except Exception as exc:
            result = {"ok": False, "state": "failed", "reason": f"Video playback failed: {exc}"}
            video_cancel.set()
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception as exc:
                    result = {"ok": False, "state": "failed", "reason": f"Video cleanup failed: {exc}"}
                    video_cancel.set()
            if audio_proc is not None:
                deadline = time.monotonic() + 60
                code = None
                while not cancellation.is_set() and time.monotonic() < deadline:
                    code = audio_proc.wait(timeout=0.1)
                    if code is not None:
                        break
                audio_state = "completed" if code == 0 else "failed" if code is not None else "interrupted"
                if code is None:
                    video_cancel.set()
        if self._stopping.is_set() or (cancel_event is not None and cancel_event.is_set()):
            result = {"ok": False, "state": "cancelled", "reason": "Video cancelled"}
        elif result["ok"] and audio_state not in {"not_requested", "completed"}:
            result = {"ok": False, "state": "partial", "reason": f"Video displayed, but audio was {audio_state}"}
        return {**result, "frames_displayed": frames_displayed, "audio_state": audio_state}

    def _increment_active_video(self) -> None:
        with self._state_lock:
            self._active_video_playback += 1

    def _decrement_active_video(self) -> None:
        with self._state_lock:
            self._active_video_playback = max(0, self._active_video_playback - 1)
