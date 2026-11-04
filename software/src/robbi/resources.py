"""Explicit feature concurrency policy for a small robot computer.

Admission never waits: a saturated feature reports BusyError. Stop, HTTP health,
device sampling and recovery do not consume the feature admission budget. Lanes
that wait for child work keep distinct workers, preventing same-pool deadlocks.
This limits queued/running tasks, not all process memory or native driver work.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import BusyError
from .execution import BoundedExecutor


@dataclass(frozen=True)
class Lane:
    workers: int
    pending: int
    reserved: bool = False


FEATURE_LANES = {
    "voice": Lane(1, 1),
    "maintenance": Lane(1, 2, reserved=True),
    "speech": Lane(1, 2),
    "overlay": Lane(1, 1),
    "effects": Lane(2, 2),
    "presentation": Lane(1, 1),
    "tts-prefetch": Lane(1, 2),
    "memory-extract": Lane(1, 1),
    "research": Lane(1, 1),
    # A slow request may be adopted by background tracking while its native
    # provider call finishes. Keep one additional bounded slot for conversation.
    "llm-live": Lane(2, 0),
    "llm-background": Lane(1, 0),
    "background-work": Lane(1, 2),
    "apps": Lane(2, 2),
}


class AdmissionBudget:
    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("Task capacity must be a positive integer")
        self.capacity = capacity
        self._used = 0
        self._rejected = 0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            if self._used >= self.capacity:
                self._rejected += 1
                raise BusyError("Feature task budget is full; let current work finish")
            self._used += 1

    def release(self):
        with self._lock:
            self._used -= 1

    def snapshot(self):
        with self._lock:
            return {"capacity": self.capacity, "outstanding": self._used, "rejected": self._rejected}


class BudgetedExecutor:
    def __init__(self, name, lane, budget):
        self._executor = BoundedExecutor(lane.workers, lane.pending, name)
        self._budget = None if lane.reserved else budget
        self._lane = lane
        self._lock = threading.Lock()
        self._outstanding = self._running = self._rejected = 0

    def submit(self, fn, /, *args, **kwargs):
        if self._budget is not None:
            self._budget.acquire()
        with self._lock:
            self._outstanding += 1
        def invoke():
            with self._lock:
                self._running += 1
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    self._running -= 1
        def release(_=None):
            with self._lock:
                self._outstanding -= 1
            if self._budget is not None:
                self._budget.release()
        try:
            future = self._executor.submit(invoke)
        except BaseException:
            release()
            with self._lock:
                self._rejected += 1
            raise
        future.add_done_callback(release)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def snapshot(self):
        with self._lock:
            return {"workers": self._lane.workers, "pending_limit": self._lane.pending,
                    "reserved": self._lane.reserved, "outstanding": self._outstanding,
                    "running": self._running, "rejected": self._rejected}


class ExecutionResources:
    def __init__(self, task_limit=16):
        self._budget = AdmissionBudget(task_limit)
        self._lanes = {name: BudgetedExecutor(name, lane, self._budget) for name, lane in FEATURE_LANES.items()}

    def lane(self, name):
        return self._lanes[name]

    def snapshot(self):
        return {"feature_tasks": self._budget.snapshot(),
                "lanes": {name: executor.snapshot() for name, executor in self._lanes.items()}}

    def shutdown(self):
        for executor in self._lanes.values():
            executor.shutdown(wait=False, cancel_futures=True)
