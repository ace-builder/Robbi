"""Bounded durable activity accounting and observation journal, not user facts."""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import threading
import time

from robbi.app_runtime.schema import bounded_json
from robbi.storage import write_json


class AutonomyPolicy:
    KINDS = frozenset({"observe", "explore", "checkin", "decide", "react", "continue", "express"})

    def __init__(self, path, day_cycle, options=lambda: {}, *, clock=time.time):
        self.path, self.day_cycle, self.options, self.clock = Path(path), day_cycle, options, clock
        self._lock = threading.RLock()
        # Stop must never wait for an SD-card write or the journal lock.
        self._pause_lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_generation, self._pause_reason = 0, ""
        self._error, self._active = "", None
        self._state = {"day": "", "attempts": 0, "model_calls": 0, "explorations": 0,
                       "paused": False, "pause_reason": "", "last_by_kind": {}, "history": [], "observations": []}
        if self.path.exists():
            try:
                if self.path.stat().st_size > 65536:
                    raise ValueError("Autonomy state exceeds 64 KiB")
                value = bounded_json(json.loads(self.path.read_text(encoding="utf-8")))
                if not isinstance(value, dict):
                    raise ValueError("Autonomy state must be an object")
                if not self._state.keys() <= value.keys():
                    raise ValueError("Autonomy state is incomplete")
                if not isinstance(value["day"], str) or type(value["paused"]) is not bool or not isinstance(value["pause_reason"], str):
                    raise ValueError("Invalid autonomy day or pause state")
                if value["day"]:
                    date.fromisoformat(value["day"])
                for key in ("attempts", "model_calls", "explorations"):
                    if type(value.get(key, 0)) is not int or not 0 <= value.get(key, 0) <= 10000:
                        raise ValueError("Invalid autonomy counter")
                if not isinstance(value.get("last_by_kind", {}), dict) or any(
                        type(v) not in (int, float) or v < 0 for v in value.get("last_by_kind", {}).values()):
                    raise ValueError("Invalid activity timestamps")
                if any(not isinstance(value.get(key, []), list) for key in ("history", "observations")):
                    raise ValueError("Invalid observation history")
                if any(not isinstance(entry, dict) for key in ("history", "observations") for entry in value[key]):
                    raise ValueError("Invalid activity or observation record")
                self._state.update({key: value[key] for key in self._state if key in value})
                self._state["history"] = self._state["history"][-32:]
                self._state["observations"] = self._state["observations"][-12:]
                self._state["last_by_kind"] = {k: v for k, v in self._state["last_by_kind"].items() if k in self.KINDS}
                for entry in self._state["history"]:
                    if isinstance(entry, dict) and entry.get("outcome") == "running":
                        entry.update(outcome="interrupted", reason="Service restarted; activity was not replayed")
            except (OSError, ValueError, RecursionError) as error:
                self._error = str(error)
                self._state.update(paused=True, pause_reason="Autonomy state needs attention; resume explicitly after review")
        if self._state["paused"]:
            self.request_pause(self._state["pause_reason"])

    def _day(self):
        return self.day_cycle.snapshot(self.clock())["date"]

    def _rollover(self):
        day = self._day()
        if not self._state["day"] or self._state["day"] < day:
            self._state.update(day=day, attempts=0, model_calls=0, explorations=0)

    def _save(self):
        try:
            write_json(self.path, bounded_json(self._state))
            self._error = ""
        except (OSError, ValueError) as error:
            self._error = str(error)
            self._state.update(paused=True, pause_reason="Could not persist activity budget")
            self.request_pause(self._state["pause_reason"])
            raise

    def request_pause(self, reason="User paused autonomy"):
        """Immediately close admission; this lock is never held across I/O."""
        with self._pause_lock:
            self._pause_generation += 1
            self._pause_reason = str(reason)[:200]
            self._pause_event.set()
            return self._pause_generation

    def persist_pause(self, generation):
        with self._lock:
            with self._pause_lock:
                if generation != self._pause_generation or not self._pause_event.is_set():
                    return
                self._state.update(paused=True, pause_reason=self._pause_reason)
            self._save()

    def pause_persistence_failed(self, error):
        # Admission stays closed even if the maintenance lane rejected its job.
        self._error = "Could not persist autonomy pause: " + str(error)[:200]

    def pause(self, reason="User paused autonomy"):
        self.persist_pause(self.request_pause(reason))
        return self.status()

    def resume(self):
        with self._lock:
            with self._pause_lock:
                generation = self._pause_generation
            self._state.update(paused=False, pause_reason="")
            self._save()
            with self._pause_lock:
                if generation == self._pause_generation:
                    # Invalidate queued persistence of a superseded Stop.
                    self._pause_generation += 1
                    self._pause_reason = ""
                    self._pause_event.clear()
        return self.status()

    def is_paused(self):
        return self._pause_event.is_set()

    def eligible(self, kind, *, uses_model=False):
        if kind not in self.KINDS:
            return "unknown_activity"
        if self._pause_event.is_set():
            return "paused"
        with self._lock:
            self._rollover()
            options = self.options()
            if self._pause_event.is_set():
                return "paused"
            if self._active is not None:
                return "activity_running"
            if self._state["attempts"] >= int(options.get("max_attempts_per_day", 24)):
                return "daily_attempt_budget"
            if uses_model and self._state["model_calls"] >= int(options.get("max_model_calls_per_day", 8)):
                return "daily_model_budget"
            if kind == "explore" and self._state["explorations"] >= int(options.get("max_explorations_per_day", 2)):
                return "daily_exploration_budget"
            interval = float(options.get(f"{kind}_interval_s", {"observe": 900, "explore": 7200, "checkin": 7200, "decide": 60, "react": 8, "continue": 5, "express": 30}[kind]))
            last = self._state["last_by_kind"].get(kind)
            if kind == "decide":
                # Ending a live experiment must not immediately re-admit the
                # same perception through the slower ordinary-decision lane.
                stamps = [self._state["last_by_kind"][key] for key in ("decide", "react", "continue")
                          if key in self._state["last_by_kind"]]
                last = max(stamps) if stamps else None
            if last is not None and self.clock() - float(last) < max({"continue": 5, "react": 8, "express": 30}.get(kind, 60), interval):
                return "activity_cooldown"
            return ""

    def begin(self, kind, *, uses_model=False, reason=""):
        if self._pause_event.is_set():
            return False
        with self._lock:
            if self.eligible(kind, uses_model=uses_model):
                return False
            stamp = self.clock()
            self._state["attempts"] += 1
            self._state["model_calls"] += int(uses_model)
            self._state["explorations"] += int(kind == "explore")
            self._state["last_by_kind"][kind] = stamp
            record = {"kind": kind, "started_at": stamp, "outcome": "running", "reason": str(reason)[:160]}
            self._state["history"] = [*self._state["history"], record][-32:]
            self._active = record
            try:
                self._save()  # Admission is durable before inference or motion.
            except Exception:
                self._active = None
                raise
            if self._pause_event.is_set():
                record.update(outcome="cancelled", reason="Paused during admission", ended_at=self.clock())
                self._active = None
                return False
            return True

    def finish(self, outcome, reason=""):
        with self._lock:
            if self._active is not None:
                self._active.update(outcome=str(outcome)[:32], reason=str(reason)[:200], ended_at=self.clock())
                self._active = None
                self._save()

    def observe(self, observation):
        # Snapshot size is independent of raw camera bytes or map size.
        data = bounded_json(observation, limit=4096)
        with self._lock:
            entry = {"ts": self.clock(), "source": "autonomy_observation", "review_status": "observation",
                     "data": data}
            self._state["observations"] = [*self._state["observations"], entry][-12:]
            self._save()
            return copy.deepcopy(entry)

    def status(self):
        with self._lock:
            self._rollover()  # Routine diagnostics do not write to the SD card.
            result = copy.deepcopy(self._state)
            result.update(paused=self._pause_event.is_set(),
                          pause_reason=self._pause_reason if self._pause_event.is_set() else "")
            result.update(active=copy.deepcopy(self._active), storage_error=self._error,
                          limits={key: self.options().get(key, default) for key, default in
                                  (("max_attempts_per_day", 24), ("max_model_calls_per_day", 8), ("max_explorations_per_day", 2))})
            return result
