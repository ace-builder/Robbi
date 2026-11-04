"""Monotonic motor cadence with bounded statistics, no busy wait or catch-up burst."""
import math
import time


class ControlTick:
    def __init__(self, interval, clock=None):
        self.clock = clock or time.monotonic
        self.interval = max(.01, min(.05, float(interval)))
        self.deadline = self.clock() + self.interval
        self.previous = None
        self.periods = self.missed = 0
        self.span = self.max_gap = self.jitter_squared = 0.

    def tick(self, now=None):
        now = self.clock() if now is None else now
        dt = self.interval if self.previous is None else max(.001, now-self.previous)
        if self.previous is not None:
            self.periods += 1
            self.span += dt
            self.max_gap = max(self.max_gap, dt)
            self.jitter_squared += (dt-self.interval)**2
        self.previous = now
        return dt

    def wait(self, cancel):
        now = self.clock()
        if now > self.deadline + 1e-9:
            skipped = math.floor((now-self.deadline)/self.interval) + 1
            self.missed += skipped
            self.deadline += skipped*self.interval
        cancel.wait(max(0., self.deadline-now))
        self.deadline += self.interval

    def status(self):
        return {'control_hz':round(self.periods/self.span,1) if self.span else None,
                'target_control_hz':round(1/self.interval,1),
                'max_control_gap_ms':round(self.max_gap*1000,2),
                'control_jitter_rms_ms':round(math.sqrt(self.jitter_squared/max(1,self.periods))*1000,2),
                'missed_control_ticks':self.missed}
