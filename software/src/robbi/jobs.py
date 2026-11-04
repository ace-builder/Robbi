"""Bounded HTTP job history; worker admission lives in robbi.execution."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict

from .errors import BusyError
# Preserve the established import path for apps and integrations.
from .execution import BoundedExecutor as BoundedExecutor


class JobRegistry:
    def __init__(self, max_workers=1, max_pending=2, history_limit=64, lease_s=10.0):
        self.executor = BoundedExecutor(max_workers, max_pending, "commands")
        self._jobs = OrderedDict()
        self._lock = threading.RLock()
        self._history_limit = history_limit
        self._lease_s = lease_s

    def submit(self, operation, callback):
        job_id = uuid.uuid4().hex
        now = time.monotonic()
        record = {"id": job_id, "operation": operation, "state": "queued", "result": None, "error": None}
        with self._lock:
            while len(self._jobs) >= self._history_limit:
                terminal = next((key for key, value in self._jobs.items() if value["state"] in {"done", "failed", "cancelled"}), None)
                if terminal is None:
                    raise BusyError("Job capacity reached")
                del self._jobs[terminal]
            self._jobs[job_id] = record

        def run():
            with self._lock:
                if time.monotonic() - now > self._lease_s:
                    record.update(state="cancelled", error="Request expired before execution; submit again")
                    return
                record["state"] = "running"
            try:
                result = callback()
                with self._lock:
                    if isinstance(result, dict) and result.get("cancelled") is True:
                        record.update(state="cancelled", result=result, error="Request was cancelled")
                    else:
                        record.update(state="done", result=result)
            except Exception as error:
                with self._lock:
                    record.update(state="failed", error=str(error)[:1000])

        try:
            future = self.executor.submit(run)
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise

        def finished(done):
            with self._lock:
                if done.cancelled():
                    record.update(state="cancelled", error="Runtime stopped")
                elif done.exception() is not None:
                    # Abnormal worker exits are captured by the executor too.
                    record.update(state="failed", result=None,
                                  error=f"Worker failed ({type(done.exception()).__name__})")
        future.add_done_callback(finished)
        return self.get(job_id)

    def get(self, job_id):
        with self._lock:
            return dict(self._jobs[job_id])

    def stop(self):
        self.executor.shutdown(wait=False, cancel_futures=True)

