"""Bounded, settled encoder-position control for in-place turns (no GPIO imports)."""

import math
import time

from .encoder_monitor import EncoderProgressWatch
from .heading import HeadingError
from .control_tick import ControlTick
from .pcb import MOTOR_MAX_DUTY


def run_turn(motor, stop_event, target, *, cap, start_duty, tolerance,
             interval=0.02, timeout=8.0, stall_timeout=0.75, sync_gain=0.125,
             clock=None, feedback=None, interlock=None, on_sample=None, rate_counts_per_s=None,
             best_effort=False, pulsed=False, continuous=False, rolling_duty=30.):
    clock = clock or time.monotonic
    started = clock()
    read_counts = motor.counts
    interval = max(0.01, min(0.02, interval))
    cap = max(0.0, min(MOTOR_MAX_DUTY, cap))
    start_duty = max(0.0, min(cap, start_duty))
    full_start=not continuous and cap==MOTOR_MAX_DUTY and start_duty>=cap
    if continuous:pulsed=False
    if full_start:
        # A full-power startup profile cannot regulate speed by reducing PWM:
        # these motors stall below their breakaway duty. Keep energized wheels
        # at full power and control position with braking / finite pulses.
        rate_counts_per_s=None
    signs = tuple(math.copysign(1, t) for t in target)
    previous = (0, 0)
    rates, peaks = [0.0, 0.0], [0.0, 0.0]
    small_turn = max(abs(t) for t in target) <= 200
    # Lower-power profiles retain their acceleration phase. Full-startup-duty
    # short turns can opt into finite pulses to avoid accelerating past the goal.
    latched = [False, False]
    watches = [EncoderProgressWatch(t, started) for t in target]
    cadence = ControlTick(interval, clock)
    warnings = set()
    corrections = 0
    speed_trim = 0.
    velocity_integrals=[0.,0.]
    rolling=max(0.,min(cap,float(rolling_duty)))
    motor.reset_counts()

    def positions():
        raw = read_counts()
        timestamp = clock()
        fused = feedback.counts(raw, timestamp) if feedback else raw
        if on_sample is not None:
            on_sample(raw, fused, timestamp)
        return raw, fused

    def result(ok, reason=""):
        precision_reached = ok
        if best_effort and reason in {'turn_accuracy_not_reached','turn_not_settled','turn_excessive_error'}:
            warnings.add(reason)
            ok, reason = True, ''
        counts = read_counts()
        return {"ok": ok, "reason": reason, "control": "settled_turn",
                'precision_reached':precision_reached, 'warnings':sorted(warnings),
                **({'heading': feedback.status(target)} if feedback else {}),
                "target": dict(zip(("left", "right"), target, strict=True)),
                "final": dict(zip(("left", "right"), counts, strict=True)),
                "error_counts": dict(zip(("left", "right"), (t - c for t, c in zip(target, counts, strict=True)), strict=True)),
                "tolerance_counts": tolerance, "correction_rounds": corrections,
                "elapsed_s": round(clock() - started, 3), "pwm_cap": cap,
                "startup_control": "finite_pulses" if pulsed else "continuous",
                "rate_control": "continuous_velocity" if continuous else "full_pwm" if full_start else "velocity" if rate_counts_per_s is not None else "position",
                **cadence.status()}

    def guard():
        if stop_event.is_set():
            return "stopped"
        if interlock and interlock():
            return "picked_up"
        if clock() - started > timeout:
            return "timeout"
        return ""

    def settle():
        if reason := guard():
            return reason
        motor.set_wheel_pwm_with_brake(0, 0, left_brake=True, right_brake=True)
        began = stable_since = clock()
        _, baseline = positions()
        while True:
            if reason := guard():
                return reason
            now = clock()
            _, counts = positions()
            if any(abs(c - b) > 2 for c, b in zip(counts, baseline, strict=True)):
                baseline, stable_since = counts, now
            if now - began >= 0.06 and now - stable_since >= 0.04:
                return ""
            if now - began >= 0.6:
                return "turn_not_settled"
            stop_event.wait(interval)

    try:
        if cap == 0 or target == (0, 0):
            return result(True, "zero_speed" if cap == 0 else "zero_distance")
        while not pulsed and not all(latched):
            if reason := guard():
                return result(False, reason)
            tick = clock()
            dt = cadence.tick(tick)
            if dt > 0.25:
                if not best_effort:return result(False, "control_loop_late")
                warnings.add('control_loop_late')
            raw, counts = positions()
            for i in (0, 1):
                if raw[i] * signs[i] < -max(20, tolerance):
                    return result(False, "encoder_direction_mismatch")
                rate = max(0.0, (counts[i] - previous[i]) * signs[i] / dt)
                alpha = dt / (.02 + dt)
                rates[i] += alpha * (rate-rates[i])
                peaks[i] = max(peaks[i], rates[i])
            previous = counts
            errors = [abs(target[i]) - counts[i] * signs[i] for i in (0, 1)]
            # Stop slightly early based on measured rate; final position is
            # verified after braking, never accepted merely for passing a mark.
            for i in (0, 1):
                lead = max(tolerance / 2, min(abs(target[i]) * 0.15, rates[i] * 0.045))
                latched[i] = latched[i] or errors[i] <= lead
                if watches[i].stalled(raw[i], latched[i], tick, stall_timeout):
                    return result(False, "encoder_stall")
            phase_error = raw[0] * signs[0] - raw[1] * signs[1]
            correction = max(-12.0, min(12.0, phase_error * sync_gain))
            if rate_counts_per_s is not None and not continuous:
                # PI on measured wheel speed; PWM remains bounded by the owner.
                rate_error = rate_counts_per_s-sum(rates)/2
                speed_trim = max(-start_duty,min(cap-start_duty,speed_trim+rate_error*min(dt,.05)*.025))
            ramp = min(1.0, (tick - started) / 0.45)
            acceleration_cap = start_duty + (cap - start_duty) * ramp
            duties = []
            if continuous:
                maximum=rate_counts_per_s or max(100.,max(abs(t) for t in target)*2)
                acceleration=maximum*2
                desired_rate=min(maximum,acceleration*max(interval,tick-started),
                                 math.sqrt(2*acceleration*max(0.,sum(errors)/2-tolerance/2)))
            # Slow down near the measured stopping region, not throughout the
            # last 55% of every turn. Longer turns can retain cruise speed;
            # predictive braking and measured settling still verify the end.
            slowdown = max(4*tolerance, min(max(abs(t) for t in target)*.25, max(rates)*.25))
            for i in (0, 1):
                if continuous:
                    # One uninterrupted, velocity-regulated turn, with a
                    # starting torque floor and full breakaway assist only if
                    # needed. Braking is reserved for the measured endpoint.
                    desired=max(0.,desired_rate+(-phase_error if i==0 else phase_error)*2)
                    error=desired-rates[i]
                    integral=max(-rolling,min(cap-rolling,velocity_integrals[i]+error*min(dt,.05)*.04))
                    demand=rolling+error*.018+integral
                    if 0<=demand<=cap or demand>cap and error<0 or demand<0 and error>0:
                        velocity_integrals[i]=integral
                    if tick-started<.06 and raw[i]*signs[i]<max(4,tolerance/2):
                        demand=max(demand,min(start_duty,rolling+cap*.2))
                    if not latched[i] and tick-watches[i].last_progress_at>=.12:
                        # Recover breakaway torque without turning the bridge
                        # off, and retain the learned effort after movement.
                        demand=cap
                        velocity_integrals[i]=max(velocity_integrals[i],cap-rolling-max(0.,error)*.018)
                    demand=max(rolling*.8,min(cap,demand))
                else:
                    fraction = max(0.0, min(1.0, errors[i] / max(1, slowdown)))
                    decel = fraction * fraction * (3 - 2 * fraction)
                    cruise = cap if rate_counts_per_s is None else max(0.,min(cap,start_duty+speed_trim+rate_error*.008))
                    demand = min(acceleration_cap, min(start_duty,cruise) + (cruise-min(start_duty,cruise)) * decel)
                    demand += -correction if i == 0 else correction
                    if full_start:demand=cap
                duties.append(0 if latched[i] else signs[i] * max(0.0, min(acceleration_cap, demand)))
            motor.set_wheel_pwm_with_brake(*duties, left_brake=latched[0], right_brake=latched[1])
            if not all(latched):
                cadence.wait(stop_event)

        # Short, bounded correction pulses allow both under- and overshoot to
        # converge without pretending that encoder position proves floor heading.
        max_corrections = 20 if small_turn or pulsed else 10
        pulse_rates = [0.0, 0.0]
        prior_counts, prior_lengths = None, None
        prior_raw, pulse_signs = None, None
        pulse_progress_at = [started, started]
        for attempt in range(max_corrections + 1):
            if reason := settle():
                return result(False, reason)
            raw, counts = positions()
            if pulsed and prior_raw is not None:
                for i in (0,1):
                    # Corrections deliberately reverse after overshoot. Require
                    # a full encoder cycle in the direction actually commanded;
                    # a high-water mark in the original direction would mistake
                    # valid reverse corrections for a stalled wheel.
                    if prior_lengths[i] and (raw[i]-prior_raw[i])*pulse_signs[i]>=4:
                        pulse_progress_at[i]=clock()
            if prior_counts is not None:
                for i in (0, 1):
                    if prior_lengths[i] and abs(counts[i] - prior_counts[i]) > 2:
                        pulse_rates[i] = abs(counts[i] - prior_counts[i]) / prior_lengths[i]
            errors = [t - c for t, c in zip(target, counts, strict=True)]
            if small_turn or pulsed:
                for i in (0, 1):
                    if raw[i] * signs[i] < -max(20, tolerance):
                        return result(False, "encoder_direction_mismatch")
                    stalled = (clock()-pulse_progress_at[i]>=stall_timeout if pulsed else
                               watches[i].stalled(raw[i], abs(errors[i]) <= tolerance, clock(), stall_timeout))
                    if abs(errors[i])>tolerance and stalled:
                        return result(False, "encoder_stall")
            if all(abs(e) <= tolerance for e in errors):
                if feedback and any(abs(c) < min(20, abs(t)*.1) for c, t in zip(raw, target, strict=True)):
                    return result(False, 'encoder_stall')
                return result(True)
            if attempt == max_corrections:
                return result(False, "turn_accuracy_not_reached")
            if any(abs(e) > max(80, abs(t) * 0.4) and e * t < 0 for e, t in zip(errors, target, strict=True)):
                return result(False, "turn_excessive_error")
            corrections += 1
            pulse_start = clock()
            lengths = [0 if abs(errors[i]) <= tolerance else
                       (min(0.025, 0.008 * (attempt + 1)) if (small_turn or pulsed) and not pulse_rates[i] else
                        max(0.001 if pulsed else 0.005, min(0.025, 0.7 * (abs(errors[i]) - tolerance / 2) /
                                       max(80, pulse_rates[i] or peaks[i] * start_duty / max(1, cap))))) for i in (0, 1)]
            prior_counts, prior_lengths = counts, lengths
            prior_raw, pulse_signs = raw, [math.copysign(1,error) for error in errors]
            finite_pulse = getattr(motor, "pulse_wheel_pwm", None)
            if callable(finite_pulse):
                finite_pulse(math.copysign(start_duty, errors[0]), math.copysign(start_duty, errors[1]), *lengths)
                while clock() - pulse_start + 1e-9 < max(lengths):
                    if reason := guard():
                        return result(False, reason)
                    stop_event.wait(0.005)
                continue
            while True:
                if reason := guard():
                    return result(False, reason)
                elapsed = clock() - pulse_start
                active = [elapsed + 1e-9 < length for length in lengths]
                motor.set_wheel_pwm_with_brake(
                    math.copysign(start_duty, errors[0]) if active[0] else 0,
                    math.copysign(start_duty, errors[1]) if active[1] else 0,
                    left_brake=not active[0], right_brake=not active[1],
                )
                if not any(active):
                    break
                stop_event.wait(min(0.005, min(lengths[i] - elapsed for i in (0, 1) if active[i])))
        return result(False, "turn_accuracy_not_reached")
    except HeadingError as error:
        return result(False, str(error))
    finally:
        motor.stop()
