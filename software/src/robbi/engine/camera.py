from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path

try:
    import cv2
    import numpy as np
except (ImportError, OSError):
    cv2 = np = None

from robbi.configuration.settings import PHOTOS_DIR, settings

try:
    from picamera2 import Picamera2

    PICAMERA2_AVAILABLE = True
except (ImportError, OSError, RuntimeError):
    Picamera2 = None
    PICAMERA2_AVAILABLE = False


class CameraService:
    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._camera: Picamera2 | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._camera_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._last_frame_ts = 0.0
        self._edge_tint_mask_cache: dict[tuple[int, int], np.ndarray] = {}
        self._last_error = ""
        self._jpeg_cache = {}
        self._encode_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sensor_fps = None
        self._rate_limit_warning = ""
        self._video_lock = threading.RLock()
        self._video = None
        self._preview_stream = 'main'
        self._video_size = None
        self._video_error = ''

    def video_ready(self):
        if not self.health()['available'] or self._camera is None:
            raise RuntimeError('Camera is unavailable; reconnect it or disable Record camera for this shot')
        if self._video is not None:
            raise RuntimeError('Camera is already recording')
        if self._video_size is None or self._video_size[0]<1920 or self._video_size[1]<1080:
            raise RuntimeError('1080p recording is unavailable: '+(self._video_error or 'HD stream was not configured'))
        # Verify dependencies without opening hardware or creating a file.
        from picamera2.outputs import PyavOutput  # noqa: F401 - probe required recording support
        from picamera2.encoders import H264Encoder  # noqa: F401 - probe required recording support
        import av  # noqa: F401 - probe required recording support

    def start_video(self, path):
        from .camera_video import VideoRecording
        with self._video_lock:
            self.video_ready()
            self._configure_capture_rate(30)
            video = None
            try:
                video = VideoRecording(self._camera, path, self._sensor_fps or 30.)
                self._video = video
                video.start()
            except Exception:
                try:
                    if video: video.stop()
                finally:
                    self._video = None
                    self._configure_capture_rate()
                raise
            return video

    def stop_video(self, video):
        with self._video_lock:
            if self._video is video:
                try: video.stop()
                finally:
                    self._video = None
                    self._configure_capture_rate()

    @staticmethod
    def _frame_to_bgr(frame):
        # Picamera2 RGB888 capture arrives in RGB order on this setup.
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def start(self) -> bool:
        if not getattr(settings, "camera_enabled", True):
            self._last_error = ""
            return False
        if not PICAMERA2_AVAILABLE or cv2 is None or np is None:
            self._last_error = "Camera dependencies unavailable (Picamera2, OpenCV or NumPy)"
            self._log.warning(self._last_error)
            return False
        if self._running:
            return True
        if self._thread is not None and self._thread.is_alive():
            self._last_error = "Previous camera worker has not stopped; refusing duplicate ownership"
            return False
        try:
            self._stop_event.clear()
            # Camera work must not create an OpenCV worker pool on the Pi Zero.
            cv2.setNumThreads(1)
            tuning_file = str(settings.camera_tuning_file or "").strip()
            if tuning_file:
                self._camera = Picamera2(tuning=tuning_file)
                self._log.info("Camera tuning file: %s", tuning_file)
            else:
                self._camera = Picamera2()
            self._configure_streams()
            self._configure_capture_rate()
            self._camera.start()
            time.sleep(settings.camera_warmup_s)
            self._running = True
            self._last_error = ""
            self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="camera")
            self._thread.start()
            self._log.info("Camera started")
            return True
        except Exception as exc:
            self._last_error = str(exc)
            self._log.exception("Failed to start camera: %s", exc)
            self.stop()
            return False

    def _configure_streams(self):
        self._video_size = None
        self._video_error = ''
        self._preview_stream = 'main'
        try:
            # YUV keeps all HD frames in ISP/encoder buffers. Only the small
            # lores stream crosses into Python; normal preview remains 3–5 fps.
            width = min(640, max(64, int(settings.camera_width))) // 32 * 32
            height = round(width * 9 / 16) // 2 * 2
            config = self._camera.create_video_configuration(
                main={'size': (1920,1080), 'format': 'YUV420'},
                lores={'size': (width,height), 'format': 'YUV420'}, raw=None, buffer_count=4)
            self._camera.configure(config)
            size = tuple(self._camera.camera_configuration()['main']['size'])
            if size[0]<1920 or size[1]<1080:
                raise RuntimeError('Camera adjusted the main stream below 1080p')
            self._video_size = size
            self._preview_stream = 'lores'
        except Exception as error:
            self._video_error = str(error)
            self._log.warning('HD recording unavailable; keeping ordinary preview: %s', error)
            config = self._camera.create_preview_configuration(
                main={'size': (settings.camera_width,settings.camera_height), 'format': 'RGB888'}, buffer_count=3)
            self._camera.configure(config)

    def _configure_capture_rate(self, fps=None):
        # Sleeping between capture_array calls only throttles Python; the
        # sensor/ISP would otherwise continue producing its default frame rate.
        # Query limits after configuration because the selected mode changes
        # the supported frame durations (Picamera2 manual, camera controls).
        self._sensor_fps = None
        self._rate_limit_warning = ""
        try:
            limits = self._camera.camera_controls.get("FrameDurationLimits")
            if limits is None:
                self._rate_limit_warning = "Camera has no sensor frame-rate control; software capture is throttled"
                return
            requested = float(settings.camera_fps) if fps is None else float(fps)
            duration_us = round(1_000_000 / max(0.1, requested))
            minimum_us, maximum_us = int(limits[0]), int(limits[1])
            duration_us = max(minimum_us, min(maximum_us, duration_us))
            self._camera.set_controls({"FrameDurationLimits": (duration_us, duration_us)})
            self._sensor_fps = round(1_000_000 / duration_us, 3)
            if abs(self._sensor_fps - requested) > 0.05:
                self._rate_limit_warning = "Requested camera rate was clamped to sensor limits"
        except Exception as exc:
            # An unsupported optional control must not disable an otherwise
            # usable camera. Keep the software rate cap and expose the fallback.
            self._rate_limit_warning = f"Sensor frame-rate control unavailable: {exc}"
            self._log.warning(self._rate_limit_warning)

    def stop(self) -> None:
        if self._video is not None:
            try: self.stop_video(self._video)
            except Exception as exc: self._log.warning('Video shutdown issue: %s', exc)
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                self._last_error = "Camera capture is unresponsive; worker still owns device"
                self._clear_frame()
                return
        if self._camera is not None:
            try:
                self._camera.stop()
                self._camera.close()
            except Exception as exc:
                self._log.warning("Camera shutdown issue: %s", exc)
        self._camera = None
        self._clear_frame()

    def _clear_frame(self):
        with self._frame_lock:
            self._latest_frame = None
            self._last_frame_ts = 0.0
            self._jpeg_cache.clear()

    def get_latest_frame(self):
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def get_frame(self, fresh: bool = False, max_age_s: float = 2.0):
        # Request paths never restart hardware or reuse a stale image. The
        # supervisor owns reconnects; explicit fresh captures have a short wait.
        if not self._running:
            return None
        if fresh:
            previous = self._last_frame_ts
            deadline = time.monotonic() + min(2.0, max(0.1, max_age_s))
            while self._last_frame_ts <= previous and time.monotonic() < deadline:
                if self._stop_event.wait(0.02):
                    return None
            if self._last_frame_ts <= previous:
                return None
        with self._frame_lock:
            age = time.monotonic() - self._last_frame_ts if self._last_frame_ts else None
            if self._latest_frame is None or age is None or age > max_age_s:
                return None
            return self._latest_frame.copy()

    def get_jpeg_frame(self) -> bytes | None:
        return self.get_llm_image_bytes(quality=80)

    def get_llm_image_bytes(self, max_width: int | None = None, quality: int | None = None) -> bytes | None:
        if cv2 is None:
            return None
        with self._encode_lock:
            return self._encode_jpeg(max_width, quality)

    def _encode_jpeg(self, max_width, quality):
        result = self._image_snapshot(int(max_width or 0), max(25, min(95, int(quality or 80))), 2.0, bounded=False)
        return result.get("data") if result["ok"] else None

    def capture_llm_image_bytes(self, max_width=None, quality=None):
        """Interactive capture gets one bounded fresh-frame retry, no UI warmup."""
        data = self.get_llm_image_bytes(max_width=max_width, quality=quality)
        if data is not None or not self._running or cv2 is None:
            return data
        if self.get_frame(fresh=True, max_age_s=1.5) is None:
            return None
        return self.get_llm_image_bytes(max_width=max_width, quality=quality)

    def get_image_snapshot(self, max_width=640, quality=75, max_age_s=2):
        """Encode a bounded copy of an already captured frame; never touch hardware.

        Metadata refers to the encoded copy, even if capture publishes a newer
        frame during encoding. Returned bytes are immutable and metadata copied.
        """
        if type(max_width) is not int or not 64 <= max_width <= 640:
            raise ValueError("max_width must be an integer between 64 and 640")
        if type(quality) is not int or not 25 <= quality <= 85:
            raise ValueError("quality must be an integer between 25 and 85")
        if isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float)) or not math.isfinite(max_age_s) or not .05 <= max_age_s <= 2:
            raise ValueError("max_age_s must be between 0.05 and 2 seconds")
        if not getattr(settings, "camera_enabled", True):
            return {"ok": False, "state": "disabled", "reason": "Camera is disabled"}
        if cv2 is None:
            return {"ok": False, "state": "unavailable", "reason": "OpenCV is unavailable"}
        with self._encode_lock:
            return self._image_snapshot(max_width, quality, max_age_s, bounded=True)

    def _image_snapshot(self, max_width, quality, max_age_s, *, bounded):
        with self._frame_lock:
            stamp = self._last_frame_ts
            age = max(0., time.monotonic() - stamp) if stamp else None
            if not self._running or self._latest_frame is None or age is None:
                return {"ok": False, "state": "unavailable", "reason": "No camera frame is available"}
            if age > max_age_s:
                return {"ok": False, "state": "stale", "reason": "Latest camera frame is stale", "age_s": age}
            key = (stamp, max_width, quality, bounded)
            cached = self._jpeg_cache.get(key)
            if cached is not None:
                return {**cached, "age_s": age}
            height, width = self._latest_frame.shape[:2]
            if width <= 0 or height <= 0 or (bounded and width * height > 16_000_000):
                return {"ok": False, "state": "invalid", "reason": "Camera frame dimensions exceed the decode limit"}
            # Capture timestamp and copy under the SAME lock. Sampling the
            # timestamp separately can label a different frame as fresh.
            frame = self._latest_frame.copy()
        try:
            target_width = max(64, max_width) if max_width else width
            scale = min(1., target_width / width, 640 / height if bounded else 1.)
            if scale < 1.:
                width, height = max(1, int(width * scale)), max(1, int(height * scale))
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if not ok:
                return {"ok": False, "state": "failed", "reason": "Camera JPEG encoding failed"}
            encoded_size = getattr(encoded, "nbytes", None)
            if bounded and isinstance(encoded_size, int) and encoded_size > 2 * 1024 * 1024:
                return {"ok": False, "state": "invalid", "reason": "Camera JPEG exceeds the 2 MiB limit"}
            data = encoded.tobytes()
            if not data or (bounded and len(data) > 2 * 1024 * 1024):
                return {"ok": False, "state": "invalid", "reason": "Camera JPEG is empty or exceeds the 2 MiB limit"}
        except Exception as exc:
            return {"ok": False, "state": "failed", "reason": f"Camera JPEG encoding failed: {exc}"}
        with self._frame_lock:
            age = max(0., time.monotonic() - stamp)
            if age > max_age_s:
                return {"ok": False, "state": "stale", "reason": "Camera frame expired during encoding", "age_s": age}
            if not self._running or self._latest_frame is None:
                return {"ok": False, "state": "unavailable", "reason": "Camera stopped during encoding"}
            result = {"ok": True, "data": data, "media_type": "image/jpeg", "width": width, "height": height,
                      "age_s": age, "captured_monotonic": stamp}
            if len(self._jpeg_cache) >= 4:
                self._jpeg_cache.clear()
            if self._last_frame_ts == stamp and len(data) <= 2 * 1024 * 1024:
                self._jpeg_cache[key] = dict(result)
        return result

    def save_photo(self, fresh: bool = False) -> Path | None:
        frame = self.get_frame(fresh=fresh)
        if frame is None:
            return None
        out_path = PHOTOS_DIR / f"{int(time.time() * 1000)}.jpg"
        if cv2.imwrite(str(out_path), frame):
            recorder = getattr(self, "recording", None)
            if recorder is not None:
                recorder.emit("camera.photo", {"source": "explicit_capture", "filename": out_path.name},
                              media=[(out_path, "image/jpeg")])
            return out_path
        return None

    def restart(self) -> bool:
        with self._restart_lock:
            was_running = self._running
            self.stop()
            if not was_running and not PICAMERA2_AVAILABLE:
                return False
            return self.start()

    def health(self) -> dict:
        age = self._frame_age_s()
        return {
            "enabled": bool(getattr(settings, "camera_enabled", True)),
            "available": bool(self._running and age is not None and age <= 2.0),
            "fresh": bool(self._running and age is not None and age <= 2.0),
            "last_error": self._last_error,
            "hint": "Check camera ribbon and Picamera2 installation; reconnect then retry Camera.",
            "running": self._running,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "has_frame": self._latest_frame is not None,
            "frame_age_s": round(age, 2) if age is not None else None,
            "capture_fps": float(settings.camera_fps),
            "sensor_fps": self._sensor_fps,
            "recording_size": self._video_size,
            "recording_error": self._video_error,
            "recording_active": self._video is not None,
            "rate_limit_warning": self._rate_limit_warning,
        }

    def _capture_loop(self) -> None:
        interval = max(0.05, 1.0 / settings.camera_fps)
        while self._running and self._camera is not None:
            started_at = time.monotonic()
            try:
                frame = self._capture_array()
                self._store_frame(frame)
            except Exception as exc:
                self._last_error = str(exc)
                self._log.warning("Camera capture failed: %s", exc)
                self._clear_frame()
                self._stop_event.wait(1.0)
                continue
            # capture_array can itself wait for the next sensor frame; don't
            # add a second full frame interval after that wait.
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started_at)))

    def _capture_array(self):
        camera = self._camera
        if camera is None:
            raise RuntimeError("camera unavailable")
        with self._camera_lock:
            frame = camera.capture_array(self._preview_stream)
            if self._preview_stream == 'lores':
                frame = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
        return self._apply_frame_processing(frame)

    def _store_frame(self, frame) -> None:
        if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] != 3:
            raise ValueError("Camera returned an invalid RGB frame")
        with self._frame_lock:
            self._latest_frame = frame.copy()
            self._last_frame_ts = time.monotonic()
            self._last_error = ""
            self._jpeg_cache.clear()

    def _frame_age_s(self) -> float | None:
        if not self._last_frame_ts:
            return None
        return max(0.0, time.monotonic() - self._last_frame_ts)

    def _wait_for_recent_frame(self, max_age_s: float, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.1, timeout_s)
        while time.monotonic() < deadline:
            age = self._frame_age_s()
            if age is not None and age <= max_age_s:
                return True
            time.sleep(0.05)
        return False

    def _apply_frame_processing(self, frame):
        processed = frame
        if settings.camera_edge_tint_fix_enabled:
            processed = self._correct_edge_tint(processed)
        return processed

    def _correct_edge_tint(self, frame):
        if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
            return frame
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            return frame
        mask = self._edge_tint_mask(height, width)
        green_boost = max(0.0, float(settings.camera_edge_tint_fix_green_boost))
        fallback_reduction = max(0.0, min(0.95, float(settings.camera_edge_tint_fix_rb_reduction)))
        red_reduction = float(settings.camera_edge_tint_fix_red_reduction)
        blue_reduction = float(settings.camera_edge_tint_fix_blue_reduction)
        if red_reduction < 0.0:
            red_reduction = fallback_reduction
        if blue_reduction < 0.0:
            blue_reduction = fallback_reduction
        red_reduction = max(0.0, min(0.95, red_reduction))
        blue_reduction = max(0.0, min(0.95, blue_reduction))
        corrected = frame.astype(np.float32, copy=True)
        corrected[..., 1] *= (1.0 + green_boost * mask)
        corrected[..., 0] *= (1.0 - red_reduction * mask)
        corrected[..., 2] *= (1.0 - blue_reduction * mask)
        np.clip(corrected, 0, 255, out=corrected)
        return corrected.astype(frame.dtype)

    def _edge_tint_mask(self, height: int, width: int) -> np.ndarray:
        key = (height, width)
        cached = self._edge_tint_mask_cache.get(key)
        if cached is not None:
            return cached
        start = max(0.0, min(0.98, float(settings.camera_edge_tint_fix_start)))
        exponent = max(0.1, float(settings.camera_edge_tint_fix_exponent))
        yy = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
        xx = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
        if height:
            xx = xx * (width / float(height))
        radius = np.sqrt((xx * xx) + (yy * yy))
        radius_max = float(radius.max()) or 1.0
        if start >= radius_max:
            mask = np.zeros((height, width), dtype=np.float32)
        else:
            mask = np.clip((radius - start) / (radius_max - start), 0.0, 1.0)
            if exponent != 1.0:
                mask = np.power(mask, exponent).astype(np.float32, copy=False)
        mask = mask.astype(np.float32, copy=False)
        self._edge_tint_mask_cache.clear()
        self._edge_tint_mask_cache[key] = mask
        return mask
