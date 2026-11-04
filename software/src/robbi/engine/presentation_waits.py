"""Bounded presentation waits that honor both Stop and activity replacement."""
from __future__ import annotations

import time

from robbi.orchestration import current_intent


class PresentationCancellation:
    def __init__(self, signal, owner):
        self.signal, self.owner = signal, owner

    def is_set(self):
        return self.signal.is_set() or bool(self.owner is not None and self.owner.is_set())

    def wait(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.signal.wait(min(.025, remaining))
        return True


def wait_for_playback(handle, signal, timeout_s=120):
    owner = current_intent()
    cancelled = PresentationCancellation(signal, owner)
    deadline = time.monotonic() + timeout_s
    while not cancelled.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = handle.wait(timeout=min(.1, remaining))
        # A native completion can race Stop or arrive after its deadline.
        if cancelled.is_set():
            return None
        if time.monotonic() >= deadline:
            break
        if result is not None:
            return result
    else:
        return None
    if owner is not None:
        owner.cancel('Playback deadline exceeded')
    raise TimeoutError('Playback deadline exceeded')
