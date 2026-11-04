"""Durable bounded long work, independent of robot hardware and configuration.

Only task descriptions/results are stored. Futures and execution callbacks stay
in memory; interrupted attempts are never automatically replayed after restart.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import closing
from contextvars import Context
from dataclasses import dataclass
import copy
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from robbi.errors import BusyError, ValidationError
from robbi.activity import bind_turn, current_turn_id


@dataclass(frozen=True, slots=True)
class BackgroundWorkOptions:
    enabled: bool = True
    foreground_budget_s: float = 2.0
    timeout_s: float = 120.0
    provider: str = ""
    model: str = ""
    max_pending: int = 2
    max_history: int = 32
    notify_on_complete: bool = False
    acknowledgement: str = "I will work on that in the background. You can keep talking to me."


class TaskCancelled(Exception):
    pass


class BackgroundAcknowledgement(dict):
    """Trusted runtime receipt; provider JSON cannot manufacture this type."""


class TaskContext:
    def __init__(self, task_id, deadline, signals):
        self.id, self.deadline = task_id, deadline
        self.event = threading.Event()
        self._signals = signals

    def checkpoint(self):
        if self.event.is_set() or any(signal is not None and signal.is_set() for signal in self._signals):
            raise TaskCancelled("Task cancelled")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Background task deadline exceeded")

    def is_set(self):
        try:
            self.checkpoint()
            return False
        except (TaskCancelled, TimeoutError):
            return True

    def wait(self, timeout):
        until = time.monotonic() + max(0, timeout)
        while not self.is_set() and time.monotonic() < until:
            self.event.wait(min(.05, max(0, until - time.monotonic())))
        return self.is_set()

    def wait_future(self, future: Future, deadline=None):
        effective_deadline = min(self.deadline, self.deadline if deadline is None else deadline)
        try:
            while True:
                self.checkpoint()
                remaining = effective_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Provider deadline exceeded")
                try:
                    result = future.result(timeout=min(.05, remaining))
                except FutureTimeout:
                    if not future.done():
                        continue
                    result = future.result()
                # Keep our checks outside the FutureTimeout handler: Python's
                # TimeoutError is the same class, so that handler must not
                # swallow an expired task and return the late result anyway.
                self.checkpoint()
                if time.monotonic() >= effective_deadline:
                    raise TimeoutError("Provider response arrived after its deadline")
                return result
        except (TaskCancelled, TimeoutError):
            future.cancel()
            raise


class TaskRepository:
    MAX_RECORD_BYTES = 80000

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, updated REAL NOT NULL, record TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=.25)

    def load(self, limit):
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT id, record FROM tasks ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        records = []
        for task_id, encoded in reversed(rows):
            if len(encoded.encode("utf-8")) > self.MAX_RECORD_BYTES:
                raise ValidationError("Stored background task exceeds its size limit")
            record = json.loads(encoded)
            if not isinstance(record, dict) or record.get("id") != task_id:
                raise ValidationError("Invalid stored background task")
            self._validate_record(record)
            records.append(record)
        return records

    @staticmethod
    def _validate_record(record):
        lengths = {"id": (32, 32), "text": (1, 12000), "source": (0, 80),
                   "provider": (0, 120), "model": (0, 200), "error": (0, 200)}
        for key, (minimum, maximum) in lengths.items():
            value = record.get(key)
            if not isinstance(value, str) or not minimum <= len(value) <= maximum:
                raise ValidationError("Invalid stored background task field: " + key)
        if any(character not in "0123456789abcdef" for character in record["id"]):
            raise ValidationError("Invalid stored background task ID")
        enums = {"status": {"queued", "running", "completed", "failed", "cancelled", "interrupted"},
                 "phase": {"queued", "working", "finished", "stopping", "interrupted"},
                 "delivery": {"pending", "announced", "retained"}}
        for key, values in enums.items():
            if not isinstance(record.get(key), str) or record[key] not in values:
                raise ValidationError("Invalid stored background task field: " + key)
        for key in ("created_at", "updated_at"):
            value = record.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValidationError("Invalid stored background task timestamp")
        if "result" not in record or (record["result"] is not None and not isinstance(record["result"], dict)):
            raise ValidationError("Invalid stored background task result")
        if len(json.dumps(record["result"], ensure_ascii=False, allow_nan=False).encode("utf-8")) > 60000:
            raise ValidationError("Stored background result exceeds 60 KB")

    def save(self, record, retained):
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > self.MAX_RECORD_BYTES:
            raise ValidationError("Background task result is too large")
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)", (record["id"], record["updated_at"], encoded))
            placeholders = ",".join("?" for _ in retained)
            connection.execute(f"DELETE FROM tasks WHERE id NOT IN ({placeholders})", tuple(retained))


class BackgroundTaskService:
    def __init__(self, path, *, scheduler, options=BackgroundWorkOptions, publish_event=lambda *_: None):
        self._scheduler, self.options, self._publish = scheduler, options, publish_event
        self._lock = threading.RLock()
        self._epoch_lock = threading.Lock()
        self._epoch = threading.Event()
        self._closed = False
        self._active = {}
        self._delivery_epochs = {}
        self._delivery_guards = {}
        self._store = TaskRepository(path)
        limit = self._limits()[1]
        self._records = OrderedDict((row["id"], row) for row in self._store.load(limit))
        for record in self._records.values():
            if record.get("status") in {"queued", "running"}:
                record.update(status="interrupted", phase="interrupted", error="Runtime restarted; this attempt was not replayed.", updated_at=time.time(), delivery="retained")
                self._store.save(record, self._records)
            elif record.get("delivery") == "pending":
                # Keep results in history, but never replay old speech on boot.
                record.update(delivery="retained", updated_at=time.time())
                self._store.save(record, self._records)

    def _limits(self):
        selected = self.options()
        return max(0, min(2, int(selected.max_pending))), max(4, min(32, int(selected.max_history)))

    def _emit(self, kind, data):
        try:
            self._publish(kind, data)
        except Exception:
            pass

    def _save(self, record, retained=None):
        record["updated_at"] = time.time()
        self._store.save(record, self._records if retained is None else retained)

    def submit(self, *, text, source, provider, model, work, cancellation=None, reply_requested=False):
        selected = self.options()
        if type(reply_requested) is not bool:
            raise ValidationError("reply_requested must be true or false")
        if not selected.enabled or self._closed:
            raise BusyError("Background work is disabled or stopped")
        if not isinstance(text, str) or not 1 <= len(text) <= 12000:
            raise ValidationError("Background task requires 1 to 12000 characters")
        with self._epoch_lock:
            # Admission and shutdown must agree on the same cancellation epoch.
            if self._closed:
                raise BusyError("Background work is disabled or stopped")
            epoch = self._epoch
        task_id = uuid.uuid4().hex
        context = TaskContext(task_id, time.monotonic() + max(1, min(300, float(selected.timeout_s))), (epoch, cancellation))
        context.checkpoint()
        with self._lock:
            pending, history = self._limits()
            if len(self._active) >= 1 + pending:
                raise BusyError("Background work queue is full; let a task finish first")
            # Stage retention without changing live history. The database
            # transaction owns admission; a full disk must not evict old results.
            retained = OrderedDict(self._records)
            evicted = []
            while len(retained) >= history:
                old = next((key for key in retained if key not in self._active), None)
                if old is None:
                    raise BusyError("Background task history is full")
                del retained[old]
                evicted.append(old)
            record = {"id": task_id, "text": text, "source": str(source)[:80], "provider": str(provider)[:120],
                      "model": str(model)[:200], "status": "queued", "phase": "queued", "result": None,
                      "error": "", "created_at": time.time(), "updated_at": time.time(), "delivery": "pending",
                      "recording_turn_id": current_turn_id(), "reply_requested": reply_requested}
            retained[task_id] = record
            self._save(record, retained)
            self._records = retained
            for old in evicted:
                self._delivery_epochs.pop(old, None)
                self._delivery_guards.pop(old, None)
            self._delivery_epochs[task_id] = epoch
            self._delivery_guards[task_id] = cancellation
            self._active[task_id] = (context, None)

        def run():
            try:
                context.checkpoint()
                with self._lock:
                    record.update(status="running", phase="working")
                    self._save(record)
                result = work(context)
                context.checkpoint()
                if not isinstance(result, dict):
                    raise ValidationError("Background work must return a result object")
                # Encode before success: oversized/non-JSON results never become
                # completed tasks or trigger a success announcement.
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 60000:
                    raise ValidationError("Background result exceeds 60 KB")
                # Own the JSON we validated and persist, not a provider's mutable
                # dictionary. Later changes cannot alter delivery or history.
                result = json.loads(encoded)
                with self._lock:
                    context.checkpoint()
                    record.update(status="failed" if result.get("error") else "completed", phase="finished", result=result,
                                  error=str(result.get("error") or "")[:200],
                                  delivery="pending" if self.options().notify_on_complete or reply_requested else "retained")
                    self._save(record)
                self._emit("background.completed" if record["status"] == "completed" else "background.failed", {"task_id": task_id})
            except Exception as error:
                cancelled = isinstance(error, TaskCancelled)
                with self._lock:
                    partial = getattr(context, 'partial_result', None)
                    try:
                        encoded_partial = json.dumps(partial, ensure_ascii=False, allow_nan=False)
                        partial = json.loads(encoded_partial) if len(encoded_partial.encode('utf8')) <= 60000 else None
                    except (ValueError, TypeError, UnicodeError, RecursionError):
                        partial = None
                    record.update(status="cancelled" if cancelled else "failed", phase="finished", result=partial if cancelled else None,
                                  error="Task cancelled" if cancelled else ("Task timed out" if isinstance(error, TimeoutError) else type(error).__name__),
                                  delivery="retained" if cancelled or not (self.options().notify_on_complete or reply_requested) else "pending")
                    try:
                        self._save(record)
                    except Exception:
                        record["error"] = "Task result could not be saved"
                self._emit("background.cancelled" if cancelled else "background.failed", {"task_id": task_id, "error": record["error"]})

        try:
            # Never inherit an interactive output owner from the initiating turn.
            future = self._scheduler.submit(Context().run, bind_turn(run))
            with self._lock:
                self._active[task_id] = (context, future)
        except Exception:
            with self._lock:
                self._active.pop(task_id, None)
                record.update(status="failed", phase="finished", error="Task admission failed", delivery="retained")
                self._save(record)
            raise

        def finished(done):
            with self._lock:
                self._active.pop(task_id, None)
                if done.cancelled():
                    record.update(status="cancelled", phase="finished", error="Task cancelled", delivery="retained")
                elif done.exception() is not None:
                    # The executor also catches BaseException from worker code.
                    # Such a failure must not leave a phantom running task.
                    record.update(status="failed", phase="finished", result=None,
                                  error=f"Worker failed ({type(done.exception()).__name__})",
                                  delivery="retained")
                else:
                    return
                try:
                    self._save(record)
                except Exception:
                    record["error"] = "Task result could not be saved"
        future.add_done_callback(finished)
        self._emit("background.queued", {"task_id": task_id})
        return self.get(task_id)

    def get(self, task_id):
        with self._lock:
            if task_id not in self._records:
                raise KeyError(task_id)
            return self._copy_record(self._records[task_id], True)

    def _copy_record(self, stored, include_results):
        record = copy.deepcopy(stored if include_results else {key: value for key, value in stored.items() if key != "result"})
        if not include_results:
            result = stored.get("result") or {}
            metadata = result.get("app_result")
            app_result = metadata.get("result") if isinstance(metadata, dict) else None
            artifacts = app_result.get("artifacts") if isinstance(app_result, dict) else None
            record.update(text=stored["text"][:240], has_result=bool(result),
                          result_summary=str(result.get("speak") or "")[:240], has_artifacts=bool(artifacts))
        task_id = record["id"]
        current = self._active.get(task_id)
        if current and current[0].is_set() and record["status"] in {"queued", "running"}:
            record.update(status="cancelled", phase="stopping", error="Task cancelled or expired", delivery="retained")
        return record

    def snapshot(self, include_results=True):
        with self._lock:
            tasks = [self._copy_record(self._records[key], include_results) for key in reversed(self._records)]
            return {"enabled": bool(self.options().enabled), "active": sum(row["status"] in {"queued", "running"} for row in tasks), "tasks": tasks}

    def cancel(self, task_id):
        with self._lock:
            if task_id not in self._records:
                raise KeyError(task_id)
            active = self._active.get(task_id)
            if active:
                active[0].event.set()
                if active[1] is not None:
                    active[1].cancel()
            record = self._records[task_id]
            if record["status"] in {"queued", "running"}:
                record.update(status="cancelled", phase="stopping", error="Task cancelled", delivery="retained")
                self._save(record)
            elif record["delivery"] != "retained":
                record["delivery"] = "retained"
                self._save(record)
            return self.get(task_id)

    def cancel_all(self):
        # Stop does not take storage/worker locks or wait on filesystem writes.
        with self._epoch_lock:
            self._epoch.set()
            self._epoch = threading.Event()

    def pending_delivery(self, include_results=True):
        with self._lock:
            return [self._copy_record(row, include_results) for row in self._records.values() if row["status"] in {"completed", "failed"} and row["delivery"] == "pending"
                    and (self.options().notify_on_complete or (row.get("reply_requested") is True and time.time() - row["created_at"] < 300))
                    and self._delivery_valid(row['id'])]

    def _delivery_valid(self, task_id):
        # Cancellation still matters after computation has completed, including
        # while synthesis is in flight. Keep the result available in history.
        try:
            return not any(signal is not None and signal.is_set() for signal in (
                self._delivery_epochs.get(task_id), self._delivery_guards.get(task_id)))
        except Exception:
            return False

    def mark_delivered(self, task_id, delivery="announced"):
        with self._lock:
            record = self._records[task_id]
            if record["status"] in {"completed", "failed"}:
                record["delivery"] = delivery if delivery in {"announced", "retained"} else "retained"
                self._save(record)

    def set_progress(self, task_id, text):
        """Transient model-authored update; never replaces a terminal result."""
        with self._lock:
            record = self._records.get(task_id)
            if record and record['status'] in {'queued', 'running'} and self._delivery_valid(task_id):
                record['progress'] = str(text).strip()[:500]
                return True
            return False

    def delivery_current(self, task_id):
        with self._lock:
            record = self._records.get(task_id)
            # Successful speech can open a microphone follow-up under the same
            # owner. Marking the announcement complete must not cancel it.
            return bool(record and record["delivery"] in {"pending", "announced"} and self._delivery_valid(task_id))

    def shutdown(self):
        # Never wait for the storage lock. Mark closed and cancel the epoch
        # atomically; shutdown must not create a fresh admissible generation.
        with self._epoch_lock:
            self._closed = True
            self._epoch.set()
