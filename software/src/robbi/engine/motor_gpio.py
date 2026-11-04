from __future__ import annotations

import atexit
import logging
import math
import threading
import time

import lgpio

from robbi.configuration.settings import settings
from .pcb import (
    LEFT_ENCODER, LEFT_MOTOR, MOTOR_MAX_DUTY, MOTOR_WAKE_S,
    RIGHT_ENCODER, RIGHT_MOTOR, ROBOT_EN,
)

MOTOR1_PIN1, MOTOR1_PIN2 = LEFT_MOTOR
MOTOR2_PIN1, MOTOR2_PIN2 = RIGHT_MOTOR
ENCODER1_A, ENCODER1_B = LEFT_ENCODER
ENCODER2_A, ENCODER2_B = RIGHT_ENCODER
PWM_HZ = 1000

_log = logging.getLogger(__name__)
_lock = threading.RLock()
_encoder_lock = threading.Lock()
_initialized = False
_closed = False
_chip: int | None = None
_callbacks: list = []
_claimed: set[int] = set()
_driver_enabled = False
_inhibited = False
_fault = ""
_encoder_alerts_enabled = False
_left_count = _right_count = 0
_counts_reset_ns = 0
_left_state = _right_state = 0
_invalid_transitions = {"left": 0, "right": 0}
_encoder_edges = {
    pin: {"rising": 0, "falling": 0, "duplicates": 0, "last_tick_ns": None}
    for pin in (*LEFT_ENCODER, *RIGHT_ENCODER)
}
_wheel_outputs: dict[tuple[int, int], tuple[float, bool, int]] = {}

_TRANSITIONS = {
    (0b00, 0b01): 1, (0b01, 0b11): 1,
    (0b11, 0b10): 1, (0b10, 0b00): 1,
    (0b00, 0b10): -1, (0b10, 0b11): -1,
    (0b11, 0b01): -1, (0b01, 0b00): -1,
}


def initialize() -> None:
    global _initialized, _chip, _left_state, _right_state, _encoder_alerts_enabled
    with _lock:
        if _initialized:
            return
        if _closed:
            raise RuntimeError("Motor GPIO has shut down; restart service to rearm")
        try:
            _chip = lgpio.gpiochip_open(0)
            # Keep nSLEEP low while claiming every bridge input LOW.
            for pin in (ROBOT_EN, *LEFT_MOTOR, *RIGHT_MOTOR):
                lgpio.gpio_claim_output(_chip, pin, 0)
                _claimed.add(pin)
            # Keep counting when timed motion is selected, too.
            for pin in (*LEFT_ENCODER, *RIGHT_ENCODER):
                lgpio.gpio_claim_alert(_chip, pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
                _claimed.add(pin)
            _left_state = _read_state(*LEFT_ENCODER)
            _right_state = _read_state(*RIGHT_ENCODER)
            for pin, callback in (
                (ENCODER1_A, _left_encoder_callback), (ENCODER1_B, _left_encoder_callback),
                (ENCODER2_A, _right_encoder_callback), (ENCODER2_B, _right_encoder_callback),
            ):
                _callbacks.append(lgpio.callback(_chip, pin, lgpio.BOTH_EDGES, callback))
            _encoder_alerts_enabled = True
            _initialized = True
            _log.info(
                "DRV8833 ready, disabled at idle; BCM EN=%s L=%s R=%s; x4 encoders L=%s R=%s",
                ROBOT_EN, LEFT_MOTOR, RIGHT_MOTOR, LEFT_ENCODER, RIGHT_ENCODER,
            )
        except Exception:
            cleanup()
            raise


def _require_chip() -> int:
    initialize()
    if _chip is None:
        raise RuntimeError("GPIO chip is not open")
    return _chip


def _read_state(pin_a: int, pin_b: int) -> int:
    return (int(bool(lgpio.gpio_read(_chip, pin_a))) << 1) | int(bool(lgpio.gpio_read(_chip, pin_b)))


def _left_encoder_callback(_chip_id: int, gpio: int, level: int, _tick: int) -> None:
    global _left_count, _left_state
    if level not in (0, 1):
        return
    with _encoder_lock:
        state = _state_with_edge(_left_state, gpio, level, *LEFT_ENCODER)
        _record_edge(gpio, level, _tick, state != _left_state)
        delta = _transition_delta(_left_state, state)
        if state != _left_state and not delta:
            _invalid_transitions["left"] += 1
        if _tick >= _counts_reset_ns:
            _left_count += delta
        _left_state = state


def _right_encoder_callback(_chip_id: int, gpio: int, level: int, _tick: int) -> None:
    global _right_count, _right_state
    if level not in (0, 1):
        return
    with _encoder_lock:
        state = _state_with_edge(_right_state, gpio, level, *RIGHT_ENCODER)
        _record_edge(gpio, level, _tick, state != _right_state)
        delta = _transition_delta(_right_state, state)
        if state != _right_state and not delta:
            _invalid_transitions["right"] += 1
        if _tick >= _counts_reset_ns:
            _right_count += delta
        _right_state = state


def _transition_delta(previous_state: int, new_state: int) -> int:
    return _TRANSITIONS.get((previous_state, new_state), 0)


def _record_edge(gpio: int, level: int, tick: int, changed: bool) -> None:
    """Per-pin evidence, cumulative until service restart, not signed position."""
    stats = _encoder_edges.get(gpio)
    if stats is None:
        return
    stats["last_tick_ns"] = tick
    stats[("rising" if level else "falling") if changed else "duplicates"] += 1


def _state_with_edge(previous_state: int, gpio: int, level: int, pin_a: int, pin_b: int) -> int:
    if gpio == pin_a:
        return (level << 1) | (previous_state & 1)
    if gpio == pin_b:
        return (previous_state & 2) | level
    return previous_state


def _limited_duty(duty: float, *, raw=False) -> float:
    if not math.isfinite(float(duty)) or not math.isfinite(float(settings.motion_pwm_max_duty)):
        raise ValueError("Motor duty must be finite")
    limit = MOTOR_MAX_DUTY if raw else min(MOTOR_MAX_DUTY, float(settings.motion_pwm_max_duty))
    return max(0.0, min(limit, abs(float(duty))))


def _enable_driver(chip: int) -> None:
    global _driver_enabled
    if _fault:
        raise RuntimeError(f"Motor GPIO fault; restart service to rearm: {_fault}")
    if not _driver_enabled:
        lgpio.gpio_write(chip, ROBOT_EN, 1)
        time.sleep(MOTOR_WAKE_S)
        _driver_enabled = True


def _zero_pin(chip: int, pin: int) -> None:
    # 0% duty still schedules LOW writes and can undo a subsequent brake HIGH.
    # Cancel PWM entirely before handing the pin back to static GPIO writes.
    if lgpio.tx_busy(chip, pin, lgpio.TX_PWM):
        lgpio.tx_pwm(chip, pin, 0, 0)
    lgpio.gpio_write(chip, pin, 0)


def _set_one(chip: int, pin1: int, pin2: int, signed_duty: float, brake: bool = False, cycles: int = 0, *, raw=False) -> None:
    duty = math.copysign(_limited_duty(signed_duty, raw=raw), signed_duty)
    key = (pin1, pin2)
    desired = (duty, brake, cycles)
    previous = _wheel_outputs.get(key)
    if previous == desired and not cycles:
        return
    # Break before make on direction/brake changes. Updating an active PWM duty
    # must not introduce a stop/start on every PID tick.
    same_direction = previous and not previous[1] and not previous[2] and not cycles and not brake and previous[0] * duty > 0
    if not same_direction:
        _zero_pin(chip, pin1)
        _zero_pin(chip, pin2)
    if brake:
        # DRV8833 11 shorts the winding; 00 coasts.
        lgpio.gpio_write(chip, pin1, 1)
        lgpio.gpio_write(chip, pin2, 1)
    elif duty:
        if cycles:
            lgpio.tx_pwm(chip, pin1 if duty > 0 else pin2, PWM_HZ, abs(duty), 0, cycles)
        else:
            lgpio.tx_pwm(chip, pin1 if duty > 0 else pin2, PWM_HZ, abs(duty))
    _wheel_outputs[key] = desired


def set_wheel_pwm(left: float, right: float) -> None:
    set_wheel_pwm_with_brake(left, right)


def set_wheel_pwm_raw(left: float, right: float) -> None:
    """Explicit wheel test duty; normal motion retains its configured ceiling."""
    if any(not math.isfinite(float(v)) or abs(float(v)) > MOTOR_MAX_DUTY for v in (left, right)):
        raise ValueError("Raw wheel duty must be between -100 and 100 percent")
    set_wheel_pwm_with_brake(left, right, _raw=True)


def set_wheel_pwm_with_brake(left: float, right: float, left_brake: bool = False, right_brake: bool = False, *, _pulse_s=None, _raw=False) -> None:
    global _fault
    with _lock:
        if _inhibited:
            return
        chip = _require_chip()
        try:
            # Scale a wheel pair together. Independent clipping turns e.g.
            # (70,100) into (50,50), erasing a timed arc/wave's steering.
            if not all(math.isfinite(float(v)) for v in (left,right)):
                raise ValueError('Motor duty must be finite')
            largest = max(abs(left),abs(right))
            scale = _limited_duty(largest,raw=_raw)/largest if largest else 1.
            left,right = left*scale,right*scale
            left = math.copysign(_limited_duty(left, raw=_raw), float(left))
            right = math.copysign(_limited_duty(right, raw=_raw), float(right))
            if not (left or right or left_brake or right_brake):
                stop()
                return
            _enable_driver(chip)
            if settings.motion_left_motor_invert:
                left = -left
            if settings.motion_right_motor_invert:
                right = -right
            outputs = (RIGHT_MOTOR, LEFT_MOTOR) if settings.motion_swap_motor_outputs else (LEFT_MOTOR, RIGHT_MOTOR)
            cycles = (0, 0) if _pulse_s is None else tuple(max(1, round(s * PWM_HZ)) for s in _pulse_s)
            _set_one(chip, *outputs[0], left, left_brake, cycles[0], raw=_raw)
            _set_one(chip, *outputs[1], right, right_brake, cycles[1], raw=_raw)
        except Exception as exc:
            _fault = str(exc)
            stop()
            raise


def pulse_wheel_pwm(left: float, right: float, left_s: float, right_s: float) -> None:
    """Finite lgpio pulse trains: Python scheduling delays cannot extend the drive pulse."""
    if any(not math.isfinite(v) or not 0 <= v <= 0.025 for v in (left_s, right_s)):
        raise ValueError("Correction pulses must be between 0 and 25 ms")
    set_wheel_pwm_with_brake(left if left_s else 0, right if right_s else 0,
                             not left_s, not right_s, _pulse_s=(left_s, right_s))


def inhibit() -> None:
    """Latch a user stop so late worker writes cannot re-enable the bridges."""
    global _inhibited
    with _lock:
        _inhibited = True
        stop()


def arm() -> None:
    """Called only when MotionManager has joined the previous motion worker."""
    global _inhibited
    with _lock:
        if _closed or _fault:
            raise RuntimeError("Motor driver unavailable; restart service to rearm")
        _inhibited = False


def counts() -> tuple[int, int]:
    # reset_counts/command admission already initialized the driver. Avoid the
    # output lock and settings/setup path on every 100 Hz feedback read.
    if not _initialized:initialize()
    return cached_counts()


def cached_counts() -> tuple[int, int]:
    with _encoder_lock:
        return _logical_encoder_pair(_left_count, _right_count)


def quadrature_counts() -> tuple[int, int]:
    return counts()


def physical_counts() -> dict:
    with _encoder_lock:
        return {"encoder1_quad": _left_count, "encoder2_quad": _right_count,
                "decode": "quadrature_x4", "invalid_transitions": dict(_invalid_transitions),
                "channels": {
                    name: {"gpio": pin, "edges": stats["rising"] + stats["falling"], **stats}
                    for name, pin in zip(("left_a", "left_b", "right_a", "right_b"),
                                         (*LEFT_ENCODER, *RIGHT_ENCODER), strict=True)
                    for stats in (_encoder_edges[pin],)
                }}


def _logical_encoder_pair(left: int, right: int) -> tuple[int, int]:
    if settings.motion_swap_encoder_inputs:
        left, right = right, left
    return (-left if settings.motion_encoder_left_invert else left,
            -right if settings.motion_encoder_right_invert else right)


def cached_levels() -> dict:
    """Last quadrature edge state; diagnostics do not initiate GPIO reads."""
    with _encoder_lock:
        levels = [(_left_state >> 1) & 1, _left_state & 1, (_right_state >> 1) & 1, _right_state & 1]
        logical = levels[2:] + levels[:2] if settings.motion_swap_encoder_inputs else levels
        return dict(zip(("left_a", "left_b", "right_a", "right_b"), logical, strict=True),
                    **dict(zip(("encoder1_a", "encoder1_b", "encoder2_a", "encoder2_b"), levels, strict=True)))


def raw_levels() -> dict:
    with _lock:
        chip = _require_chip()
        levels = [int(lgpio.gpio_read(chip, pin)) for pin in (*LEFT_ENCODER, *RIGHT_ENCODER)]
        logical = levels[2:] + levels[:2] if settings.motion_swap_encoder_inputs else levels
        return dict(zip(("left_a", "left_b", "right_a", "right_b"), logical, strict=True),
                    **dict(zip(("encoder1_a", "encoder1_b", "encoder2_a", "encoder2_b"), levels, strict=True)))


def driver_status() -> dict:
    with _lock:
        return {"driver": "DRV8833", "numbering": "BCM", "enable_pin": ROBOT_EN,
                "enabled": _driver_enabled, "inhibited": _inhibited, "fault": _fault, "encoder_decode": "quadrature_x4",
                "max_duty": _limited_duty(MOTOR_MAX_DUTY)}


def reset_counts() -> None:
    global _left_count, _right_count, _counts_reset_ns
    initialize()
    with _encoder_lock:
        _counts_reset_ns = time.monotonic_ns()
        _left_count = _right_count = 0


def stop() -> None:
    global _driver_enabled
    with _lock:
        if _chip is None:
            return
        # Disable the bridges first, even if stopping a PWM channel fails.
        for pin in (ROBOT_EN, *LEFT_MOTOR, *RIGHT_MOTOR):
            if pin not in _claimed:
                continue
            try:
                if pin != ROBOT_EN:
                    _zero_pin(_chip, pin)
                    continue
                lgpio.gpio_write(_chip, pin, 0)
            except Exception as exc:
                _log.error("Failed to stop BCM GPIO%s: %s", pin, exc)
        _driver_enabled = False
        _wheel_outputs.clear()


def brake(duration_s: float = 0.08) -> None:
    with _lock:
        if not _driver_enabled:
            return
    try:
        set_wheel_pwm_with_brake(0, 0, True, True)
        time.sleep(max(0.0, min(0.2, float(duration_s))))
    finally:
        stop()


def motor1_forward(speed: int = 100) -> None:
    set_wheel_pwm(speed, 0)


def motor1_backward(speed: int = 100) -> None:
    set_wheel_pwm(-speed, 0)


def motor2_forward(speed: int = 100) -> None:
    set_wheel_pwm(0, speed)


def motor2_backward(speed: int = 100) -> None:
    set_wheel_pwm(0, -speed)


def motor1_stop() -> None:
    with _lock:
        if _chip is not None:
            _set_one(_chip, *LEFT_MOTOR, 0)


def motor2_stop() -> None:
    with _lock:
        if _chip is not None:
            _set_one(_chip, *RIGHT_MOTOR, 0)


def cleanup() -> None:
    global _initialized, _chip, _encoder_alerts_enabled, _closed
    with _lock:
        _closed = True
        stop()
        for callback in _callbacks:
            try:
                callback.cancel()
            except Exception as exc:
                _log.warning('Encoder callback cleanup failed: %s', exc)
        _callbacks.clear()
        if _chip is not None:
            try:
                lgpio.gpiochip_close(_chip)
            except Exception as exc:
                _log.warning('Motor GPIO cleanup failed: %s', exc)
        _claimed.clear()
        _chip = None
        _initialized = _encoder_alerts_enabled = False


atexit.register(cleanup)
