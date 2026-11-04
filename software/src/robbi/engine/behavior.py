from __future__ import annotations
from .state_io import atomic_text

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass

from robbi.configuration.settings import STATE_DIR


STATE_FILE = STATE_DIR / "behavior.json"


@dataclass
class BehaviorSnapshot:
    energy: int = 70
    social: int = 60
    stimulation: int = 50
    mood: str = "calm"
    awake: bool = True
    muted: bool = False
    last_interaction_ts: float = 0.0
    current_face: str = "neutral.png"


class BehaviorEngine:
    def __init__(self, *, day_cycle=None) -> None:
        self._log = logging.getLogger(__name__)
        self._state = self._load()
        from robbi.services.companion import SocialState
        from robbi.configuration.settings import settings
        self.social_state=SocialState(STATE_FILE.with_name('social-mood.json'),options=lambda:{
            'enabled':getattr(settings,'mood_enabled',True),
            'grudge_hours':getattr(settings,'mood_grudge_hours',18),
            'recovery_hours':getattr(settings,'mood_recovery_hours',12)})
        self.day_cycle = day_cycle
        self._last_vitals_tick = time.time()
        self._last_saved = time.time()
        self._lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_expression_at = time.monotonic() + random.uniform(5, 10)
        self._last_expression = ''
        self._last_yawn_at = -120.0
        if self._state.last_interaction_ts == 0:
            self._state.last_interaction_ts = time.time()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="behavior")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.save()

    def snapshot(self) -> BehaviorSnapshot:
        with self._lock:
            state=BehaviorSnapshot(**asdict(self._state))
        mood=self.social_state.snapshot()['mood']
        if mood!='calm':state.mood=mood
        return state

    def note_interaction(self) -> BehaviorSnapshot:
        with self._lock:
            self._state.last_interaction_ts = time.time()
            self._state.social = min(100, self._state.social + 15)
            self._state.stimulation = min(100, self._state.stimulation + 10)
            self._state.awake = True
            self._next_expression_at = time.monotonic() + random.uniform(8, 14)
            self._update_mood_locked()
            self.save()
            return BehaviorSnapshot(**asdict(self._state))

    def set_current_face(self, name: str) -> None:
        with self._lock:
            self._state.current_face = name

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self._state.muted = muted
            self.save()

    def passive_presentation(self, now=None) -> dict | None:
        now = time.monotonic() if now is None else now
        if now < self._next_expression_at:
            return None
        state = self.snapshot()
        if not state.awake:
            choices, interval = ['sleepy-breath'], (35, 55)
        elif state.mood == 'tired':
            choices, interval = ['natural-blink', 'sleepy-breath'], (12, 22)
            if now - self._last_yawn_at > 90:
                choices.append('sleepy-yawn')
        elif state.mood in {'playful','happy','excited'}:
            choices, interval = ['happy-blink', 'happy-blink', 'cheeky-wink', 'mischievous-grin'], (7, 13)
        elif state.mood in {'hurt','grumpy','supportive'}:
            choices, interval = ['natural-blink', 'soft-relief'], (12, 22)
        elif state.mood in {'curious', 'lonely'}:
            choices, interval = ['natural-blink', 'double-blink', 'curious-glance'], (9, 17)
        else:
            choices, interval = ['natural-blink', 'natural-blink', 'double-blink', 'curious-glance'], (7, 14)
        # Blinks can recur naturally; distinctive expressions do not repeat
        # back to back. Idle personality stays visual and never drives wheels.
        filtered = [name for name in choices if name != self._last_expression or 'blink' in name]
        selected = random.choice(filtered or choices)
        self._last_expression = selected
        self._next_expression_at = now + random.uniform(*interval)
        if selected == 'sleepy-yawn':
            self._last_yawn_at = now
        return {'face_animation': selected}

    def llm_context(self) -> dict:
        state = self.snapshot()
        return {
            "mood": state.mood,
            "relationship": self.social_state.snapshot(),
            "awake": state.awake,
            "muted": state.muted,
            "energy": state.energy,
            "social": state.social,
            "stimulation": state.stimulation,
            "day_cycle": self.day_cycle.snapshot() if self.day_cycle else {},
        }

    def save(self) -> None:
        atomic_text(STATE_FILE, json.dumps(asdict(self._state), indent=2))
        self._last_saved = time.time()

    def _load(self) -> BehaviorSnapshot:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                return BehaviorSnapshot(**data)
        except Exception as exc:
            logging.getLogger(__name__).warning("Failed to load behavior state: %s", exc)
        return BehaviorSnapshot()

    def _loop(self) -> None:
        while self._running:
            if self._stop_event.wait(30):
                return
            try:
                self._tick()
            except Exception:
                self._log.exception("Behavior update failed; next cycle will retry")

    def _tick(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            previous_awake = self._state.awake
            if self.day_cycle is not None:
                quiet = self.day_cycle.snapshot(now)["quiet"]
                # Quiet hours suppress initiative, but direct interaction still
                # wakes Robbi briefly. These are personality vitals, not battery.
                recent = now - self._state.last_interaction_ts < 120
                self._state.awake = not quiet or recent
            if now - self._last_vitals_tick >= 300:
                self._last_vitals_tick = now
                self._state.energy = min(100, self._state.energy + 4) if not self._state.awake else max(0, self._state.energy - 1)
                self._state.social = max(0, self._state.social - 1)
                self._state.stimulation = max(0, self._state.stimulation - 1)
                if self.day_cycle is None:
                    if self._state.energy < 20:
                        self._state.awake = False
                    elif self._state.energy > 45:
                        self._state.awake = True
            self._update_mood_locked()
            if self._state.awake != previous_awake or now - self._last_saved >= 300:
                self.save()

    def _update_mood_locked(self) -> None:
        if not self._state.awake or self._state.energy < 25:
            self._state.mood = "tired"
        elif self._state.social < 25:
            self._state.mood = "lonely"
        elif self._state.stimulation < 30:
            self._state.mood = "curious"
        elif self._state.stimulation > 75:
            self._state.mood = "playful"
        else:
            self._state.mood = "calm"
