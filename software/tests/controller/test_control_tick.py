"""Motor deadline scheduling with deterministic jitter and work duration."""
from robot.control_tick import ControlTick
import pytest


class Clock:
    def __init__(self):self.now=0.;self.extra=0.;self.waits=[]
    def wait(self,seconds):
        self.waits.append(seconds);self.now+=seconds+self.extra


def test_100hz_includes_work_and_keeps_absolute_phase():
    clock=Clock();tick=ControlTick(.01,lambda:clock.now);times=[]
    for i in range(101):
        tick.tick();times.append(clock.now);clock.now+=.006 if i%2 else .002;tick.wait(clock)
    assert times==pytest.approx([i*.01 for i in range(101)])
    assert tick.status()['control_hz']==100
    assert tick.status()['missed_control_ticks']==0


def test_overrun_skips_expired_slots_without_catchup_burst():
    clock=Clock();tick=ControlTick(.01,lambda:clock.now)
    tick.tick();clock.now+=.036;tick.wait(clock)
    assert clock.now==pytest.approx(.04)
    assert tick.tick()==pytest.approx(.04)
    clock.now+=.003;tick.wait(clock)
    assert clock.now==pytest.approx(.05)
    assert tick.status()['missed_control_ticks']==3
    assert tick.status()['max_control_gap_ms']==40


def test_late_wakeup_does_not_accumulate_schedule_drift():
    clock=Clock();tick=ControlTick(.01,lambda:clock.now)
    tick.tick();clock.extra=.003;tick.wait(clock)
    tick.tick();clock.extra=0;clock.now+=.002;tick.wait(clock)
    assert clock.now==pytest.approx(.02)
    assert tick.status()['control_jitter_rms_ms']==3
