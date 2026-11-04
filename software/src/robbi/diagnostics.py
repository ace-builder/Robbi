"""Independent, rate-limited diagnostic producers; no devices or global config.

Only diagnostics use this cache. Control and safety decisions must read their
own live state. Producers must return detached JSON-compatible values and must
not probe hardware or make network requests.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class DiagnosticSection:
    read: Callable[[], object]
    interval_s: float


class DiagnosticSampler:
    def __init__(self, sections: dict[str, DiagnosticSection], *, clock=time.monotonic):
        for name, section in sections.items():
            if not name or not callable(section.read) or not math.isfinite(section.interval_s) or section.interval_s < 0:
                raise ValueError("Diagnostics require named producers and finite nonnegative intervals")
        self._sections = dict(sections)
        self._clock = clock
        self._values = {}
        self._metrics = {}
        self._versions = {name: 0 for name in sections}
        self._sampled_versions = {}
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def invalidate(self, *names):
        """Mark sections due on the next sample; never perform work on the caller."""
        with self._lock:
            for name in names or self._sections:
                if name not in self._sections:
                    raise KeyError(name)
                self._versions[name] += 1

    def sample(self, *, force=False):
        # Concurrent readers reuse the last snapshot instead of repeating I/O
        # or waiting for another producer. Startup can initially return {}.
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            for name, section in self._sections.items():
                now = self._clock()
                with self._lock:
                    previous = self._metrics.get(name)
                    version = self._versions[name]
                    due = (force or previous is None or version != self._sampled_versions.get(name)
                           or now - previous["sampled_at"] >= section.interval_s)
                if not due:
                    continue
                started = time.perf_counter()
                error = ""
                try:
                    value = copy.deepcopy(section.read())
                except Exception as exc:
                    error = str(exc)[:1000]
                    value = {"available": False, "error": error}
                elapsed_ms = (time.perf_counter() - started) * 1000
                with self._lock:
                    self._values[name] = value
                    self._sampled_versions[name] = version
                    self._metrics[name] = {
                        "sampled_at": self._clock(), "last_sample_ms": elapsed_ms,
                        "samples": (previous or {}).get("samples", 0) + 1,
                        "error": error,
                    }
        finally:
            self._refresh_lock.release()
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._values)

    def metrics(self):
        """Explain refresh cost and staleness only when diagnostics are requested."""
        now = self._clock()
        with self._lock:
            return {
                name: {
                    "interval_s": section.interval_s,
                    "age_s": round(max(0, now - self._metrics[name]["sampled_at"]), 3) if name in self._metrics else None,
                    "last_sample_ms": round(self._metrics.get(name, {}).get("last_sample_ms", 0), 3),
                    "samples": self._metrics.get(name, {}).get("samples", 0),
                    "error": self._metrics.get(name, {}).get("error", ""),
                }
                for name, section in self._sections.items()
            }
