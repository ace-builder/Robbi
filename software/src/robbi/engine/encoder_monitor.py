"""Progress-based motion watchdog, independent of GPIO and wall-clock time."""

from dataclasses import dataclass


@dataclass
class EncoderProgressWatch:
    target: int
    last_progress_at: float
    progress_counts: int = 0
    min_progress_counts: int = 4

    def stalled(self, count: int, reached: bool, now: float, timeout_s: float) -> bool:
        if self.target == 0 or reached:
            return False
        progress = count if self.target > 0 else -count
        # A single working A/B channel can oscillate the decoded count forever.
        # Only a new high-water mark of one full x4 cycle renews the deadline.
        if progress >= self.progress_counts + self.min_progress_counts:
            self.progress_counts = progress
            self.last_progress_at = now
        return now - self.last_progress_at >= timeout_s
