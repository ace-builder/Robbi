"""Bounded filming wheel cues. No GPIO, provider calls or distance estimates."""
import math
import time

from ..errors import ValidationError


TIMED_MODES = {'wheels', 'wave'}


def _number(value, name, minimum, maximum, integer=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= maximum or (integer and int(value) != value)):
        raise ValidationError(f'{name} must be a finite {"integer" if integer else "number"} between {minimum} and {maximum}')
    return int(value) if integer else value


def validate(spec):
    """Timed motions are deliberate durations, never approximated centimeters."""
    if not isinstance(spec, dict) or spec.get('mode') not in TIMED_MODES:
        raise ValidationError('Timed filming motion requires wheels or wave mode')
    mode = spec['mode']
    allowed = ({'left_speed', 'right_speed'} if mode == 'wheels'
               else {'speed', 'period_ms', 'steering_percent'})
    if set(spec) - {'mode', 'duration_ms'} - allowed:
        raise ValidationError('Unknown timed motion fields; safety overrides are not accepted')
    result = {'mode': mode, 'duration_ms': _number(spec.get('duration_ms'), 'duration_ms', 100, 10000, True)}
    if mode == 'wheels':
        for name in ('left_speed', 'right_speed'):
            result[name] = _number(spec.get(name), name, -100, 100)
    else:
        result['speed'] = _number(spec.get('speed', 100), 'speed', 0, 100, True)
        result['period_ms'] = _number(spec.get('period_ms', 2500), 'period_ms', 200, 10000, True)
        result['steering_percent'] = _number(spec.get('steering_percent', 30), 'steering_percent', 0, 80)
    return result


def wheel_speeds(spec, elapsed_s):
    if spec['mode'] == 'wheels':
        return spec['left_speed'], spec['right_speed']
    # Alternate the inside wheel reduction. Neither wheel reverses or exceeds
    # the requested forward PWM. This is a timed weave, not a measured path.
    steering = math.sin(2 * math.pi * elapsed_s * 1000 / spec['period_ms'])
    reduction = spec['speed'] * spec['steering_percent'] / 100 * abs(steering)
    return ((spec['speed'] - reduction, spec['speed']) if steering >= 0
            else (spec['speed'], spec['speed'] - reduction))


def execute(spec, motor, cancelled, guard, *, started=None, clock=None):
    """Count duration from the first completed PWM write, then stop before return.

    The native driver applies its normal configured duty limit. guard runs on
    every iteration and preserves pickup interlocks. Filming ignores ToF range.
    A late loop never
    issues another powered write after its deadline.
    """
    clock = clock or time.monotonic
    duration_s = spec['duration_ms'] / 1000
    origin = deadline = None
    result = {'ok': False, 'mode': spec['mode'], 'reason': 'stopped'}
    try:
        while not cancelled.is_set():
            now = clock()
            if deadline is not None and now >= deadline:
                result = {'ok': True, 'mode': spec['mode'], 'reason': 'completed'}
                break
            elapsed = 0 if origin is None else now - origin
            left, right = wheel_speeds(spec, elapsed)
            blocked = guard(left, right)
            if blocked:
                result = {**blocked, 'ok': False}
                break
            if cancelled.is_set():
                break
            # A slow sensor/guard call must not extend a timed drive.
            if deadline is not None and clock() >= deadline:
                result = {'ok': True, 'mode': spec['mode'], 'reason': 'completed'}
                break
            motor.set_wheel_pwm(left, right)
            if origin is None:
                origin = clock()
                deadline = origin + duration_s
                if started is not None:
                    started()
            cancelled.wait(max(0, min(.01, deadline - clock())))
    finally:
        motor.stop()
    result['duration_ms'] = spec['duration_ms']
    result['elapsed_ms'] = 0 if origin is None else round((clock() - origin) * 1000, 3)
    return result
