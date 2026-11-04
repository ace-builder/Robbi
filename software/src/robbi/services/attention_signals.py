"""Small working intentions and text novelty, without inference or I/O."""

from __future__ import annotations

import re
import threading
import time


def words(value):
    return re.findall(r"\w+", str(value or "").casefold())[:160]


def speech_key(value):
    return " ".join(words(value))


def scene_key(value):
    # Ignore only grammatical padding. Preserve order, numbers, direction,
    # negation and colour: "no person" must not equal "a person".
    padding = {"a", "an", "the", "there", "is", "are", "currently"}
    return " ".join(word for word in words(value) if word not in padding)


class WorkingIntention:
    """One short intention; only completed sensing can admit a continuation.

    Never resumes on process restart. The persistent attention episode journal
    supplies history, while execution authority remains with current intents.
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._generation = 0
        self._state = {"focus": "", "phase": "idle", "steps": 0, "reason": ""}
        self._deadline = 0
        self._ready = False

    def _expire(self):
        if (
            self._state["phase"] in {"waiting", "thinking"}
            and self.clock() >= self._deadline
        ):
            self._state.update(phase="expired", reason="Fresh evidence window ended")
            self._ready = False
            self._generation += 1

    def ready(self):
        with self._lock:
            self._expire()
            return self._ready

    def begin(self, *, continuation=False, source="autonomy"):
        with self._lock:
            self._expire()
            if continuation:
                if not self._ready:
                    return None
                self._state["steps"] += 1
            else:
                self._state = {
                    "focus": "",
                    "phase": "idle",
                    "steps": 0,
                    "reason": "",
                    "source": "live" if source == "live" else "autonomy",
                }
            self._ready = False
            self._generation += 1
            self._state["phase"] = "thinking"
            self._deadline = self.clock() + 120
            return self._generation

    def finish(self, token, *, focus, outcome, reason, followup=False):
        with self._lock:
            self._expire()
            if token != self._generation:
                return  # A late result cannot undo Stop or a newer decision.
            if not self._state["focus"]:
                self._state["focus"] = str(focus or reason)[:160]
            self._state["reason"] = str(reason)[:240]
            self._ready = bool(
                followup
                and outcome in {"observed", "explored"}
                and self._state["steps"] < 2
            )
            if self._ready:
                self._state["phase"] = "waiting"
                self._deadline = self.clock() + 45
            elif outcome in {
                "failed",
                "cancelled",
                "unavailable",
                "budget_or_cooldown",
                "skipped",
            }:
                self._state["phase"] = "interrupted"
            elif outcome == "action_requested":
                self._state["phase"] = "requested"
            else:
                self._state["phase"] = "complete"

    def current(self, token):
        with self._lock:
            self._expire()
            return token == self._generation and self._state["phase"] == "thinking"

    def interrupt(self, reason="Owner interrupted autonomy", *, source=None):
        with self._lock:
            if source is not None and self._state.get("source") != source:
                return
            self._generation += 1
            self._ready = False
            if self._state["phase"] in {"thinking", "waiting"}:
                self._state.update(phase="interrupted", reason=str(reason)[:240])

    def snapshot(self):
        with self._lock:
            self._expire()
            return {
                **self._state,
                "continuation_ready": self._ready,
                "remaining_s": max(0, round(self._deadline - self.clock()))
                if self._ready
                else 0,
            }
