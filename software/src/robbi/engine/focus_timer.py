from __future__ import annotations

import logging
import threading
import time
import json
import math
from pathlib import Path

from robbi.configuration.settings import RUNTIME_DIR, STATE_DIR, settings
from .state_io import atomic_json


class FocusTimerManager:
    def __init__(self, show_face_callback, finished_callback, can_present_callback, state_file=None) -> None:
        self._log = logging.getLogger(__name__)
        self._show_face_callback = show_face_callback
        self._finished_callback = finished_callback
        self._can_present_callback = can_present_callback
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._image_path = RUNTIME_DIR / "focus_timer.png"
        self._state_file = Path(state_file) if state_file else STATE_DIR / 'focus_timer.json'
        self._last_error = ''
        self._expired_while_stopped = False
        self._active = False
        self._started_at = 0.0
        self._ends_at = 0.0
        self._last_rendered_second: int | None = None
        self._last_presented_second: int | None = None
        self._last_presented_text = ""
        self._defer_until = 0.0
        self._restore()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="focus-timer")
        self._thread.start()

    def start(self, duration_s: float) -> dict:
        duration_s = float(duration_s)
        if not math.isfinite(duration_s) or not 1 <= duration_s <= 604800:
            raise ValueError('Focus duration must be 1 second to 7 days')
        now = time.time()
        with self._lock:
            self._active = True
            self._started_at = now
            self._ends_at = now + duration_s
            self._last_rendered_second = None
            self._last_presented_second = None
            self._last_presented_text = ""
            self._defer_until = 0.0
            self._expired_while_stopped = False
            self._save_locked()
        return self.status()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._last_rendered_second = None
            self._last_presented_second = None
            self._last_presented_text = ""
            self._defer_until = 0.0
            self._save_locked()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def defer_restore(self, seconds: float) -> None:
        until = time.time() + max(0.0, float(seconds))
        with self._lock:
            self._defer_until = max(self._defer_until, until)

    def status(self) -> dict:
        with self._lock:
            active = self._active
            started_at = self._started_at
            ends_at = self._ends_at
        remaining_s = max(0.0, ends_at - time.time()) if active else 0.0
        return {
            "active": active,
            "started_at": int(started_at) if started_at else None,
            "ends_at": int(ends_at) if ends_at else None,
            "remaining_s": round(remaining_s, 1),
            "remaining_text": self._format_remaining(remaining_s) if active else "00:00",
            "error": self._last_error,
            "expired_while_stopped": self._expired_while_stopped,
        }

    def _save_locked(self):
        atomic_json(self._state_file, {'active': self._active, 'started_at': self._started_at, 'ends_at': self._ends_at})

    def _restore(self):
        if not self._state_file.exists():
            return
        try:
            state = json.loads(self._state_file.read_text(encoding='utf-8'))
            start, end = float(state.get('started_at', 0)), float(state.get('ends_at', 0))
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError('Invalid focus timer timestamps')
            self._started_at, self._ends_at = start, end
            self._active = state.get('active') is True and end > time.time()
            self._expired_while_stopped = state.get('active') is True and not self._active
        except Exception as exc:
            self._last_error = f'Focus timer state could not be read: {exc}'
            self._log.warning(self._last_error)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                state = self._snapshot_state()
                if not state["active"]:
                    time.sleep(0.2)
                    continue
                remaining_s = max(0.0, state["ends_at"] - time.time())
                remaining_second = max(0, int(remaining_s + 0.999))
                if state["last_rendered_second"] != remaining_second:
                    remaining_text = self._format_remaining(remaining_s)
                    self._render_countdown(remaining_text)
                    with self._lock:
                        self._last_rendered_second = remaining_second
                else:
                    remaining_text = self._format_remaining(remaining_s)
                if remaining_s <= 0:
                    self.stop()
                    try:
                        self._finished_callback()
                    except Exception as exc:
                        self._log.warning("Focus timer finish callback failed: %s", exc)
                    time.sleep(0.2)
                    continue
                if time.time() >= state["defer_until"] and self._can_present_callback():
                    if state["last_presented_second"] != remaining_second or state["last_presented_text"] != remaining_text:
                        try:
                            self._show_face_callback(self._image_path, remaining_text)
                            with self._lock:
                                self._last_presented_second = remaining_second
                                self._last_presented_text = remaining_text
                        except Exception as exc:
                            self._log.warning("Focus timer presentation failed: %s", exc)
                time.sleep(0.2)
            except Exception as exc:
                self._log.warning("Focus timer loop failed: %s", exc)
                time.sleep(0.5)

    def _snapshot_state(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "ends_at": self._ends_at,
                "last_rendered_second": self._last_rendered_second,
                "last_presented_second": self._last_presented_second,
                "last_presented_text": self._last_presented_text,
                "defer_until": self._defer_until,
            }

    def _render_countdown(self, remaining_text: str) -> None:
        if not settings.enable_lcd:
            return
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self._last_error = 'Countdown is running; install Pillow to render the display.'
            return
        self._image_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (280, 240), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        font_large = self._load_font(82)
        font_small = self._load_font(24)
        label = "FOCUS"
        bbox_label = draw.textbbox((0, 0), label, font=font_small)
        bbox_text = draw.textbbox((0, 0), remaining_text, font=font_large)
        label_width = bbox_label[2] - bbox_label[0]
        label_height = bbox_label[3] - bbox_label[1]
        text_width = bbox_text[2] - bbox_text[0]
        text_height = bbox_text[3] - bbox_text[1]
        label_x = (280 - label_width) // 2
        text_x = (280 - text_width) // 2
        total_height = label_height + 16 + text_height
        start_y = (240 - total_height) // 2
        draw.text((label_x, start_y), label, fill=(170, 170, 170), font=font_small)
        draw.text((text_x, start_y + label_height + 16), remaining_text, fill=(255, 255, 255), font=font_large)
        image.save(self._image_path)

    def _load_font(self, size: int):
        from PIL import ImageFont
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _format_remaining(self, remaining_s: float) -> str:
        total_seconds = max(0, int(remaining_s + 0.999))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"
