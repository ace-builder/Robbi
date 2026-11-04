"""Bounded, rotating diagnostic traces; never block response delivery on disk."""
from __future__ import annotations

import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import queue


class DroppingQueueHandler(QueueHandler):
    dropped = 0

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


class BoundedQueueListener(QueueListener):
    def stop(self):
        # Producers are detached by the caller. Make room for the sentinel and
        # bound shutdown even when the filesystem itself has stopped responding.
        while True:
            try:
                self.queue.put_nowait(self._sentinel)
                break
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    continue
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


def start_trace_logging(log_dir):
    events = queue.Queue(maxsize=200)
    handler = DroppingQueueHandler(events)
    output = RotatingFileHandler(log_dir / "trace.jsonl", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    output.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("robot.trace")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    listener = BoundedQueueListener(events, output)
    listener.start()
    return listener

