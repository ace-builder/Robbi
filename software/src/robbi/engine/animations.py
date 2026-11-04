from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from .assets import AssetRegistry


@dataclass(slots=True)
class AnimationEvent:
    at_ms: int
    kind: str
    payload: dict | str


class AnimationPlayer:
    def __init__(self, assets: AssetRegistry, execute_callback) -> None:
        self._log = logging.getLogger(__name__)
        self._assets = assets
        self._execute_callback = execute_callback

    def play(self, name: str, cancel_event=None, request_id=None) -> None:
        path = self._assets.get("animations", name, extensions=(".yaml",))
        if path is None:
            self._log.warning("Animation not found: %s", name)
            return
        self.play_path(path, cancel_event=cancel_event, request_id=request_id)

    def play_filming_sequence(self, name: str, cancel_event=None, request_id=None) -> None:
        path = self._assets.get("filming_sequences", name, extensions=(".yaml",))
        if path is None:
            self._log.warning("Filming sequence not found: %s", name)
            return
        self.play_path(path, cancel_event=cancel_event, request_id=request_id)

    def play_path(self, path: Path, cancel_event=None, request_id=None) -> None:
        cancel_event = cancel_event or threading.Event()
        events = self._load_events(path)
        start = time.perf_counter()
        for event in events:
            if cancel_event.is_set():
                return
            target = start + (event.at_ms / 1000.0)
            now = time.perf_counter()
            if target > now:
                if cancel_event.wait(target - now):
                    return
            if event.kind == "action":
                payload = event.payload
                assert isinstance(payload, dict)
                if request_id is None:
                    self._execute_callback(payload)
                else:
                    self._execute_callback(payload, request_id=request_id)

    def _load_events(self, path: Path) -> list[AnimationEvent]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list) or len(raw) > 256:
            raise ValueError('Timeline must contain at most 256 steps')
        events: list[AnimationEvent] = []
        cursor_ms = 0
        for step in raw:
            if not isinstance(step, dict):
                raise ValueError('Timeline steps must be objects')
            action: dict = {}
            for key in ("face", "sound", "video", "speak", "drive"):
                if key in step:
                    action[key] = step[key]
            if action:
                events.append(AnimationEvent(at_ms=cursor_ms, kind="action", payload=action))
            wait = int(step.get("ms", 0))
            if not 0 <= wait <= 300000 or cursor_ms + wait > 1800000:
                raise ValueError('Timeline exceeds delay or total duration limit')
            cursor_ms += wait
        return events
