"""Bounded daemon workers: a hung optional operation cannot multiply workers.

Deadlines must also be enforced by the I/O adapter. Python cannot safely kill an
in-flight thread; shutdown cancels queued work and never claims otherwise.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future

from .errors import BusyError
from .activity import bind_turn


class _WorkQueue(queue.Queue):
    def discard(self, item):
        """Unlink a cancelled pending item without disturbing FIFO order.

        Removal and worker get() share Queue's mutex. Whichever removes the
        item owns task_done/slot release; no Future callback runs under it.
        """
        with self.mutex:
            try:
                self.queue.remove(item)
            except ValueError:
                return False
            self.unfinished_tasks -= 1
            if not self.unfinished_tasks:
                self.all_tasks_done.notify_all()
            self.not_full.notify()
            return True


class BoundedExecutor:
    def __init__(self, max_workers=1, max_pending=4, thread_name_prefix="robbi", **_):
        if (type(max_workers) is not int or type(max_pending) is not int
                or max_workers < 1 or max_pending < 0):
            raise ValueError("Invalid worker capacity")
        # Admission is bounded by _slots. Reserve one extra queue entry per
        # worker for shutdown sentinels, so shutdown(wait=False) never waits for
        # a blocked adapter to finish before it can wake the other workers.
        self._queue = _WorkQueue(2 * max_workers + max_pending)
        self._slots = threading.BoundedSemaphore(max_workers + max_pending)
        self._lock = threading.Lock()
        self._closed = False
        self._threads = []
        self._max_workers = max_workers
        self._prefix = thread_name_prefix

    def submit(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("executor stopped")
            if not self._slots.acquire(blocking=False):
                raise BusyError("Robbi is busy; try again when the current task finishes")
            try:
                future = Future()
                item = (future, bind_turn(fn), args, kwargs)

                def discard_cancelled(done):
                    nonlocal item
                    pending, item = item, None
                    if done.cancelled() and self._queue.discard(pending):
                        self._slots.release()
                        # No worker will notify wait()/as_completed() for it.
                        done.set_running_or_notify_cancel()

                future.add_done_callback(discard_cancelled)
                if len(self._threads) < self._max_workers:
                    worker = threading.Thread(
                        target=self._work, name=f"{self._prefix}-{len(self._threads)}",
                        daemon=True,
                    )
                    worker.start()
                    self._threads.append(worker)
                self._queue.put_nowait(item)
            except BaseException:
                # In particular, Thread.start() can fail under memory pressure.
                # Nothing has been queued and the admission belongs to us.
                self._slots.release()
                raise
            return future

    def _work(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            future, fn, args, kwargs = item
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args, **kwargs))
                    except BaseException as error:
                        future.set_exception(error)
            finally:
                # An idle worker can wait here indefinitely. Do not retain the
                # preceding request, media buffers, result or exception frame.
                del item, future, fn, args, kwargs
                self._slots.release()
                self._queue.task_done()

    def shutdown(self, wait=True, *, cancel_futures=False):
        cancelled = []
        with self._lock:
            first_shutdown = not self._closed
            self._closed = True
            sentinels = 0
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        sentinels += 1
                    else:
                        cancelled.append(item[0])
                        self._slots.release()
                    self._queue.task_done()
            if first_shutdown:
                sentinels += len(self._threads)
            for _ in range(sentinels):
                self._queue.put_nowait(None)
        # Future callbacks may call submit/shutdown themselves. Never run them
        # while holding the executor lock.
        for future in cancelled:
            future.cancel()
            future.set_running_or_notify_cancel()
        if wait:
            for worker in self._threads:
                if worker is not threading.current_thread():
                    worker.join()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.shutdown()


