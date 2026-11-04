import pytest

from robot.encoder_monitor import EncoderProgressWatch


@pytest.mark.parametrize("target", [1400, -1400])
def test_one_channel_bouncing_cannot_keep_watchdog_alive(target):
    watch = EncoderProgressWatch(target, 0)
    for tick in range(1, 15):
        assert not watch.stalled(tick % 2 - 1, False, tick * 0.05, 0.75)
    assert watch.stalled(-1, False, 0.75, 0.75)


@pytest.mark.parametrize("target,sign", [(1400, 1), (-1400, -1)])
def test_watchdog_needs_real_forward_progress(target, sign):
    watch = EncoderProgressWatch(target, 0)
    for tick in range(1, 101):
        assert not watch.stalled(sign * tick * 4, False, tick * 0.1, 0.75)
    assert watch.stalled(sign * 400, False, 10.75, 0.75)


def test_returning_to_an_old_position_does_not_count_as_progress():
    watch = EncoderProgressWatch(1400, 0)
    assert not watch.stalled(100, False, 0.1, 0.75)
    assert not watch.stalled(90, False, 0.3, 0.75)
    assert not watch.stalled(100, False, 0.6, 0.75)
    assert watch.stalled(100, False, 0.86, 0.75)


def test_reached_or_stationary_wheel_does_not_stall():
    assert not EncoderProgressWatch(0, 0).stalled(0, False, 100, 0.75)
    assert not EncoderProgressWatch(100, 0).stalled(100, True, 100, 0.75)
