"""Bounded audio handoff: capture never waits for inference or old audio."""
from collections import deque
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class AudioPacket:
    data: bytes
    captured_at: float
    epoch: int
    context: object = None


class AudioInbox:
    def __init__(self, capacity=5, max_age_s=0.5, *, clock=time.monotonic):
        if not isinstance(capacity, int) or capacity < 1 or not 0 < max_age_s <= 2:
            raise ValueError("Invalid audio queue limits")
        self._capacity, self._max_age_s, self._clock = capacity, max_age_s, clock
        self._queue = deque()
        self._condition = threading.Condition()
        self._closed = False
        self.epoch = 0
        self._dropped = self._overruns = 0
        self._reason = ""
        self._last_processing_ms = 0.0

    def _discard(self, reason, *, overrun=False):
        self.epoch += 1
        self._dropped += len(self._queue)
        self._queue.clear()
        self._reason = reason
        self._overruns += int(overrun)

    def push(self, data, *, context=None, expected_epoch=None):
        with self._condition:
            if self._closed:
                return False
            if expected_epoch is not None and expected_epoch != self.epoch:
                # The blocking device read crossed Stop/reconfiguration. It
                # must not seed VAD or wake history under the new stream epoch.
                self._dropped += 1
                self._reason = "Audio captured across a stream reset discarded"
                return False
            if len(self._queue) >= self._capacity:
                self._discard("Audio processing fell behind; buffered utterance discarded", overrun=True)
            self._queue.append(AudioPacket(data, self._clock(), self.epoch, context))
            self._condition.notify()
            return True

    def receive(self):
        with self._condition:
            while not self._closed:
                if self._queue:
                    if self._clock() - self._queue[0].captured_at > self._max_age_s:
                        self._discard("Queued audio expired; buffered utterance discarded", overrun=True)
                        continue
                    return self._queue.popleft()
                self._condition.wait()
            return None

    def current(self, packet):
        with self._condition:
            return (not self._closed and packet.epoch == self.epoch
                    and self._clock() - packet.captured_at <= self._max_age_s)

    def reset(self, reason="Audio stream restarted"):
        with self._condition:
            self._discard(reason)
            self._condition.notify_all()
            return self.epoch

    def close(self):
        with self._condition:
            self._closed = True
            self._discard("Audio stream stopped")
            self._condition.notify_all()

    def processing_time(self, milliseconds):
        with self._condition:
            self._last_processing_ms = round(milliseconds, 2)

    def snapshot(self):
        with self._condition:
            return {"queued_chunks": len(self._queue), "capacity": self._capacity,
                    "max_age_s": self._max_age_s, "dropped_chunks": self._dropped,
                    "overruns": self._overruns, "last_reset": self._reason,
                    "last_processing_ms": self._last_processing_ms}
