"""Thread-safe, cached health records; readers never probe hardware or network."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class ComponentHealth:
    name: str
    state: str = "starting"
    message: str = "Starting"
    hint: str = ""
    retries: int = 0
    changed_at: float = 0.0


class HealthRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, ComponentHealth] = {}

    def set(self, name: str, state: str, message: str = "", hint: str = "", retries: int = 0):
        if state not in {"disabled", "starting", "ready", "degraded", "failed", "retrying", "stopped"}:
            raise ValueError(f"Unknown health state: {state}")
        with self._lock:
            old = self._entries.get(name)
            changed_at = old.changed_at if old and (old.state, old.message) == (state, message) else time.time()
            self._entries[name] = ComponentHealth(name, state, str(message)[:1000], str(hint)[:500], retries, changed_at)

    def snapshot(self):
        with self._lock:
            return [asdict(entry) for entry in self._entries.values()]

