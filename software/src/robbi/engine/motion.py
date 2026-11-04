from __future__ import annotations

import logging
from collections import deque
import math
import threading
import time
from concurrent.futures import Future
from typing import Callable

from robbi.errors import BusyError
from robbi.orchestration import current_intent

from robbi.configuration.settings import settings
from .curve_path import CurveFollower, CurvePath
from .encoder_monitor import EncoderProgressWatch
from .control_tick import ControlTick
from .turn_control import run_turn
from .heading import HeadingError, HeadingFeedback
from .pcb import MOTOR_MAX_DUTY

try:
    from . import motor_gpio as motor

    MOTOR_AVAILABLE = True
except ImportError:
    motor = None
    MOTOR_AVAILABLE = False


DistanceGetter = Callable[[], float | None]


class _MotionCancellation:
    """Event-shaped cancellation combining Stop, the caller and its lease."""

    def __init__(self, internal, external=None, lease=None):
        self.internal, self.external, self.lease = internal, external, lease

    def is_set(self):
        return self.internal.is_set() or any(
            source.is_set() if hasattr(source, "is_set") else source()
            for source in (self.external, self.lease) if source is not None)

    def wait(self, timeout=None):
        until = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if until is None else until - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            self.internal.wait(.02 if remaining is None else min(.02, remaining))
        return self.is_set()


class MotionManager:
    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._enabled = MOTOR_AVAILABLE and settings.enable_motors
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._stop_generation = 0
        self._stop_event = threading.Event()
        self._motion_thread: threading.Thread | None = None
        self._last_result: dict = {"mode": "idle"}
        self._distance_getter: DistanceGetter | None = None
        self._navigation_distance_getter: DistanceGetter | None = None
        self._orchestrator = None
        self._active_intent = None
        self._active_cancel = None
        self._heading_getter = None
        self._navigation_feedback = None
        self._feedback_lock = threading.Lock()
        self._navigation_history = deque(maxlen=256)
        self.motion_observer = None

    def navigation_feedback(self):
        """One immutable cached control sample; no GPIO reads or extra sampler."""
        return self._navigation_feedback

    def navigation_samples(self, after=0):
        with self._feedback_lock:
            return [dict(s) for s in self._navigation_history if s['timestamp']>after]

    def _publish_navigation_feedback(self, raw, fused, timestamp):
        intent = current_intent()
        self._navigation_feedback = {
            'intent_id': intent.id if intent else None, 'timestamp': timestamp,
            'distance_cm': sum(raw) * self.cm_per_count() / 2,
            'heading_deg': math.degrees((fused[1]-fused[0]) * self.cm_per_count() / settings.motion_wheel_base_cm),
        }
        if hasattr(self,'_feedback_lock'):
            with self._feedback_lock:
                self._navigation_history.append(self._navigation_feedback)

    def _notify_motion_started(self, intent):
        self._navigation_feedback = None
        if hasattr(self,'_feedback_lock'):
            with self._feedback_lock:self._navigation_history.clear()
        observer = getattr(self, 'motion_observer', None)
        if observer is not None:
            observer(intent)

    def bind_orchestrator(self, orchestrator):
        if self.is_running():
            raise BusyError("Cannot replace orchestration while motion is running")
        self._orchestrator = orchestrator

    def _cancellation(self):
        return getattr(self, "_active_cancel", None) or self._stop_event

    def start(self):
        """GPIO setup belongs to the background lifecycle, never construction."""
        if self._enabled:
            try:
                motor.initialize()
            except Exception as exc:
                self._enabled = False
                self._last_result = {"mode": "unavailable", "error": str(exc)}
                self._log.warning("Motor initialization failed: %s", exc)
        return bool(self._enabled)

    def set_obstacle_distance_getter(self, getter: DistanceGetter) -> None:
        self._distance_getter = getter

    def set_navigation_distance_getter(self, getter: DistanceGetter) -> None:
        self._navigation_distance_getter = getter

    def set_navigation_unknown_getter(self, getter) -> None:
        self._navigation_unknown_getter = getter

    def set_heading_getter(self, getter):
        self._heading_getter = getter

    @staticmethod
    def _manual_command():
        intent = current_intent()
        return bool(intent and (intent.owner in {'manual:dashboard', 'motion-studio', 'conversation-lab:tool'}
                    or (intent.priority >= 60 and intent.owner in {'conversation', 'robot-action'})))

    def _heading_feedback(self, allow_encoder_fallback=False):
        if not settings.motion_imu_heading_enabled:
            return None, 'disabled'
        if getattr(self, '_heading_getter', None) is None:
            return None, 'imu_not_connected'
        try:
            return HeadingFeedback(self._heading_getter, self.cm_per_count(), settings.motion_wheel_base_cm,
                                   settings.motion_imu_gyro_weight, now=time.monotonic(),
                                   allow_encoder_fallback=allow_encoder_fallback,
                                   recover=self._manual_command() or bool(current_intent() and current_intent().owner=='navigation')), ''
        except HeadingError as error:
            return None, str(error)

    def heading_status(self):
        try:
            sample = self._heading_getter() if self._heading_getter else None
            ready = bool(sample and sample.get('ready') and
                         0 <= time.monotonic()-sample['sample_time'] <= .15)
        except Exception:
            ready = False
        return {'enabled': settings.motion_imu_heading_enabled, 'available': ready,
                'source': 'imu_encoder' if ready and settings.motion_imu_heading_enabled else 'encoders'}

    def reload_runtime_settings(self) -> None:
        requested = MOTOR_AVAILABLE and settings.enable_motors
        if not requested:
            self.stop()
            self._enabled = False
        elif not self._enabled:
            try:
                motor.initialize()
                self._enabled = True
            except Exception as exc:
                self._last_result = {"mode": "unavailable", "error": str(exc)}

    def is_running(self) -> bool:
        """Cheap activity check for observers; never samples GPIO."""
        with self._lock:
            return self._motion_thread is not None and self._motion_thread.is_alive()

    def status(self) -> dict:
        counts = {"left": 0, "right": 0}
        quadrature_counts = {"left": 0, "right": 0}
        physical_counts = {}
        encoder_levels = {}
        if self._enabled:
            try:
                left, right = motor.cached_counts()
                counts = {"left": left, "right": right}
                q_left, q_right = left, right
                quadrature_counts = {"left": q_left, "right": q_right}
                physical_counts = motor.physical_counts()
                encoder_levels = motor.cached_levels()
            except Exception as exc:
                self._last_result = {"mode": "error", "error": str(exc)}
        with self._lock:
            running = self._motion_thread is not None and self._motion_thread.is_alive()
            last_result = dict(self._last_result)
        distance_cm = self._obstacle_distance_cm()
        return {
            "available": bool(self._enabled),
            "enabled": bool(settings.enable_motors),
            "last_error": str(last_result.get("error", "")),
            "hint": "Enable calibrated motors in Settings. Manual driving can continue with encoder feedback when auxiliary sensors are unavailable.",
            "encoder_enabled": bool(settings.motion_encoder_enabled),
            "heading": self.heading_status(),
            "running": running,
            "counts": counts,
            "quadrature_counts": quadrature_counts,
            "physical_counts": physical_counts,
            "driver": motor.driver_status() if self._enabled else {},
            "encoder_levels": encoder_levels,
            "last_result": last_result,
            "obstacle": {
                "enabled": settings.motion_obstacle_stop_enabled,
                "distance_cm": distance_cm,
                "threshold_cm": settings.motion_obstacle_stop_cm,
                "blocked": self._obstacle_blocked(distance_cm),
            },
            "config": {
                "default_step_cm": settings.motion_default_step_cm,
                "default_turn_deg": settings.motion_default_turn_deg,
                "arc_radius_cm": settings.motion_arc_radius_cm,
                "sine_radius_cm": settings.motion_sine_radius_cm,
                "sine_angle_deg": settings.motion_sine_angle_deg,
                "wiggle_angle_deg": settings.motion_wiggle_angle_deg,
                "wheel_diameter_cm": settings.motion_wheel_diameter_cm,
                "wheel_base_cm": settings.motion_wheel_base_cm,
                "encoder_counts_per_rev": settings.motion_encoder_counts_per_rev,
                "pwm_max_duty": settings.motion_pwm_max_duty,
                "pwm_min_duty": settings.motion_pwm_min_duty,
                "turn_max_duty": min(MOTOR_MAX_DUTY, settings.motion_turn_max_duty),
                "turn_start_duty": settings.motion_turn_start_duty,
                "turn_tolerance_counts": settings.motion_turn_tolerance_counts,
                "imu_heading_enabled": settings.motion_imu_heading_enabled,
                "imu_gyro_weight": settings.motion_imu_gyro_weight,
                "imu_turn_tolerance_deg": settings.motion_imu_turn_tolerance_deg,
                "swap_motor_outputs": settings.motion_swap_motor_outputs,
                "swap_encoder_inputs": settings.motion_swap_encoder_inputs,
                "pid": {
                    "kp": settings.motion_pid_kp,
                    "ki": settings.motion_pid_ki,
                    "kd": settings.motion_pid_kd,
                },
            },
        }

    def move(
        self,
        direction: str = "forward",
        speed: int = 100,
        duration: float = 1.0,
        wait: bool = False,
        distance_cm: float | None = None,
        angle_deg: float | None = None,
        mode: str | None = None,
        radius_cm: float | None = None,
        amplitude_cm: float | None = None,
        cycles: int | float | None = None,
        ignore_obstacles: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> Future | None:
        if not self._enabled:
            self._log.warning("Motion requested while motors are unavailable")
            return None

        spec = {
            "mode": mode,
            "direction": direction,
            "speed": speed,
            "duration": duration,
            "distance_cm": distance_cm,
            "angle_deg": angle_deg,
            "radius_cm": radius_cm,
            "amplitude_cm": amplitude_cm,
            "cycles": cycles,
            "ignore_obstacles": ignore_obstacles,
        }
        return self.drive(spec, wait=wait, cancel_event=cancel_event)

    def drive(self, spec: dict, wait: bool = False, *, cancel_event: threading.Event | None = None,
              allow_encoder_fallback: bool = False) -> Future | None:
        interlock = getattr(self, 'handling_interlock', None)
        if interlock is not None and interlock():
            self.stop()
            result = {'ok': False, 'mode': 'idle', 'reason': 'picked_up'}
            self._set_last_result(result)
            blocked = Future()
            blocked.set_result(result)
            return blocked
        if not self._enabled:
            self._log.warning("Motion requested while motors are unavailable")
            return None
        spec = dict(spec or {})
        for key in ("speed", "duration", "distance_cm", "angle_deg", "radius_cm", "amplitude_cm", "cycles"):
            if spec.get(key) is not None and not math.isfinite(float(spec[key])):
                raise ValueError(f"{key} must be a finite number")
        if abs(float(spec.get("distance_cm") or 0)) > 500 or abs(float(spec.get("angle_deg") or 0)) > 720:
            raise ValueError("One motion is limited to 500 cm or 720 degrees")
        label = self._motion_label(spec)
        turn_rate = spec.get('turn_rate_deg_s')
        if turn_rate is not None:
            if label not in {'turn','turn_left','turn_right'} or type(turn_rate) not in (int,float) or not math.isfinite(turn_rate) or not 5 <= turn_rate <= 180:
                raise ValueError('Turn rate must be 5–180 degrees/s for an in-place turn')
        future: Future = Future()
        if label == "stop":
            self._request_command_stop()
            future.set_result(None)
            return future

        speed = max(0, min(100, int(float(spec.get("speed", 100)))))
        if speed == 0:
            self._request_command_stop()
            self._set_last_result({"mode": "idle", "ok": True, "reason": "zero_speed"})
            future.set_result(None)
            return future
        duration = max(0.0, min(10.0, float(spec.get("duration", 1.0))))
        curve = self._curve_for_spec(spec)
        targets = [curve.targets(self.cm_per_count())] if curve else self._targets_for_spec(spec)
        use_encoder = bool(targets) and settings.motion_encoder_enabled

        if (curve or spec.get('require_encoders')) and not use_encoder:
            self._request_command_stop()
            self._set_last_result({"mode": "curve", "ok": False, "reason": "encoders_required"})
            future.set_result(None)
            return future

        if use_encoder:
            # Filming gestures also enter here, outside HardwarePort's direct
            # move call. Apply the same nonfatal IMU policy to every shot move.
            intent = current_intent()
            allow_encoder_fallback = allow_encoder_fallback or bool(intent and intent.owner == 'filming-shot')
            runner = self._run_target_sequence
            args = (label, speed, duration, targets, bool(spec.get("ignore_obstacles") or spec.get("obstacle_override")), future, curve, allow_encoder_fallback)
            if turn_rate is not None:args += (turn_rate,)
        else:
            runner = self._run_timed_move
            args = (label, speed, duration, bool(spec.get("ignore_obstacles") or spec.get("obstacle_override")), future)
        self._launch(runner, args, future, cancel_event)
        if wait:
            future.result()
        return future

    def _launch(self, runner, args, future, cancel_event=None) -> None:
        if getattr(self, "_orchestrator", None) is not None:
            return self._launch_orchestrated(runner, args, future, cancel_event)
        # Serialize new commands, but STOP never waits for this lock or a join.
        with self._start_lock:
            self._notify_motion_started(current_intent())
            with self._lock:
                ticket = self._stop_generation
            self._request_stop()
            with self._lock:
                if self._stop_generation != ticket + 1 or (cancel_event is not None and cancel_event.is_set()):
                    self._last_result = {"ok": False, "reason": "stopped", "mode": "idle"}
                    future.set_result(None)
                    return
                self._stop_event.clear()
                self._active_cancel = _MotionCancellation(self._stop_event, cancel_event)
                motor.arm()
                # Preserve the same owner-derived feedback policy even in the
                # standalone driver, where there is no orchestrator lease.
                from robbi.orchestration import intent_scope
                intent = current_intent()
                def invoke():
                    with intent_scope(intent):runner(*args)
                thread = threading.Thread(target=invoke, daemon=True, name="motion")
                self._motion_thread = thread
                thread.start()

    def _launch_orchestrated(self, runner, args, future, cancel_event):
        from robbi.orchestration import intent_scope

        with self._lock:
            ticket = self._stop_generation
        lease = self._orchestrator.acquire(("motion",), cancel_event=cancel_event,
                                            ttl_s=120, wait_s=.25)
        launched = prepared = False
        try:
            with self._start_lock:
                with self._lock:
                    if self._motion_thread is not None and self._motion_thread.is_alive():
                        raise BusyError("Motion is still running for this owner")
                    cancelled = _MotionCancellation(self._stop_event, cancel_event, lease)
                    if self._stop_generation != ticket or lease.is_set() or (
                            cancel_event is not None and (
                                cancel_event.is_set() if hasattr(cancel_event, "is_set") else cancel_event())):
                        self._last_result = {"ok": False, "reason": "stopped", "mode": "idle"}
                        future.set_result(None)
                        return
                    self._stop_event.clear()
                    self._active_cancel, self._active_intent = cancelled, lease.intent
                    prepared = True
                # No native driver work under the manager lock needed by Stop.
                self._notify_motion_started(lease.intent)
                motor.arm()
                worker_future = Future()
                worker_args = tuple(worker_future if arg is future else arg for arg in args)

                def invoke():
                    error = None
                    try:
                        with intent_scope(lease.intent):
                            if not cancelled.is_set():
                                runner(*worker_args)
                            else:
                                motor.inhibit()
                                self._set_last_result({"mode": "idle", "ok": False, "reason": "stopped"})
                    except BaseException as exc:
                        error = exc
                    finally:
                        # Runners resolve their internal future before braking.
                        # Keep the lease until all their cleanup actually returns.
                        with self._lock:
                            completed = dict(self._last_result)
                            if self._motion_thread is threading.current_thread():
                                self._motion_thread = None
                            self._active_intent = None
                            self._active_cancel = None
                        lease.release()
                        if not future.done():
                            if error is not None:
                                future.set_exception(error)
                            elif worker_future.done():
                                try:
                                    result = worker_future.result()
                                except BaseException as exc:
                                    future.set_exception(exc)
                                else:
                                    future.set_result(completed if result is None else result)
                            else:
                                future.set_result(completed)

                with self._lock:
                    thread = threading.Thread(target=invoke, daemon=True, name="motion")
                    self._motion_thread = thread
                    thread.start()
                    launched = True
        finally:
            if not launched:
                with self._lock:
                    if prepared and self._active_intent is lease.intent:
                        self._active_intent = self._active_cancel = None
                lease.release()

    def filming_timed_drive(self, spec, *, cancel_event=None, started=None):
        """Explicit, bounded filming cues using the normal owner and interlocks."""
        from robbi.filming.timed_motion import validate
        spec = validate(spec)
        interlock = getattr(self, 'handling_interlock', None)
        if interlock is not None and interlock():
            self.stop()
            result = {'ok': False, 'mode': spec['mode'], 'reason': 'picked_up'}
            self._set_last_result(result)
            future = Future()
            future.set_result(result)
            return future
        if not self._enabled:
            return None
        future = Future()
        self._launch(self._run_filming_timed, (spec, started, future), future, cancel_event)
        return future

    def _run_filming_timed(self, spec, started, future):
        from robbi.filming.timed_motion import execute
        began = time.monotonic()

        def guard(left, right):
            interlock = getattr(self, 'handling_interlock', None)
            if interlock is not None and interlock():
                return {'reason': 'picked_up', 'mode': spec['mode']}
            if self._should_stop_for_obstacle(left, right, False):
                return self._obstacle_result(spec['mode'], began)
            return None

        try:
            result = execute(spec, motor, self._cancellation(), guard, started=started)
            self._set_last_result(result)
            future.set_result(result)
        except Exception as error:
            self._set_last_result({'ok': False, 'mode': spec['mode'], 'reason': 'error', 'error': str(error)})
            future.set_exception(error)
        finally:
            self._clear_thread()

    def raw_wheel_drive(self, left: float, right: float, duration: float = 0.35, wait: bool = False, *, cancel_event=None) -> Future | None:
        if not self._enabled:
            self._log.warning("Raw motion requested while motors are unavailable")
            return None
        future: Future = Future()
        if not all(math.isfinite(float(value)) for value in (left, right, duration)):
            raise ValueError("Wheel duty and duration must be finite numbers")
        left = max(-100.0, min(100.0, float(left)))
        right = max(-100.0, min(100.0, float(right)))
        duration = max(0.0, min(2.0, float(duration)))

        self._launch(self._run_raw_wheel_drive, (left, right, duration, future), future, cancel_event)
        if wait:
            future.result()
        return future

    def _run_raw_wheel_drive(self, left: float, right: float, duration: float, future: Future) -> None:
        started = time.monotonic()
        try:
            if duration <= 0 or (abs(left) <= 0 and abs(right) <= 0):
                motor.stop()
                self._set_last_result({"mode": "raw", "ok": True, "left": 0.0, "right": 0.0, "duration_s": 0.0})
                future.set_result(None)
                return
            while not self._cancellation().is_set() and time.monotonic() - started < duration:
                if self._should_stop_for_obstacle(left, right, False):
                    self._set_last_result(self._obstacle_result("raw", started))
                    future.set_result(None)
                    return
                motor.set_wheel_pwm_raw(left, right)
                self._cancellation().wait(timeout=0.02)
            self._set_last_result(
                {
                    "mode": "raw",
                    "ok": not self._cancellation().is_set(),
                    "reason": "stopped" if self._cancellation().is_set() else "completed",
                    "left": round(left, 2),
                    "right": round(right, 2),
                    "duration_s": round(duration, 3),
                    "elapsed_s": round(time.monotonic() - started, 3),
                }
            )
            future.set_result(None)
        except Exception as exc:
            self._set_last_result({"mode": "error", "direction": "raw", "error": str(exc)})
            future.set_exception(exc)
        finally:
            motor.stop()
            self._clear_thread()

    def stop(self, *, owner=None) -> bool:
        if owner is not None and getattr(self, "_orchestrator", None) is not None:
            # Prevent a scoped cleanup from passing its owner check and then
            # inhibiting a replacement launched while the native call waits.
            with self._start_lock:
                with self._lock:
                    active = self._active_intent
                    if active is None or active.id != owner.id:
                        return False
                    self._stop_generation += 1
                    self._stop_event.set()
                if self._enabled:
                    motor.inhibit()
                return True
        with self._lock:
            self._stop_generation += 1
            self._stop_event.set()
        if self._enabled:
            motor.inhibit()
        return True

    def _request_command_stop(self):
        if getattr(self, "_orchestrator", None) is None:
            return self._request_stop()
        from robbi.orchestration import current_intent
        owner = current_intent()
        if owner is not None:
            self.stop(owner=owner)

    def cm_per_count(self) -> float:
        return math.pi * max(0.1, settings.motion_wheel_diameter_cm) / max(1, settings.motion_encoder_counts_per_rev)

    def turn_angle_deg_from_wheel_cm(self, left_cm: float, right_cm: float) -> float:
        return math.degrees((float(right_cm) - float(left_cm)) / max(0.1, settings.motion_wheel_base_cm))

    def wheel_delta_cm_from_result(self, result: dict) -> tuple[float, float] | None:
        if not isinstance(result, dict):
            return None
        segments = result.get("segments")
        if isinstance(segments, list) and segments:
            left = 0.0
            right = 0.0
            used = False
            for segment in segments:
                final = segment.get("final") if isinstance(segment, dict) else None
                if not isinstance(final, dict):
                    continue
                left += float(final.get("left", 0)) * self.cm_per_count()
                right += float(final.get("right", 0)) * self.cm_per_count()
                used = True
            return (left, right) if used else None
        final = result.get("final")
        if isinstance(final, dict):
            return float(final.get("left", 0)) * self.cm_per_count(), float(final.get("right", 0)) * self.cm_per_count()
        return None

    def wheel_delta_cm_from_spec(self, spec: dict) -> tuple[float, float] | None:
        targets = self._targets_for_spec(spec)
        if not targets:
            return None
        left_ticks = sum(target[0] for target in targets)
        right_ticks = sum(target[1] for target in targets)
        return left_ticks * self.cm_per_count(), right_ticks * self.cm_per_count()

    def estimate_wheel_delta_cm(self, spec: dict, result: dict | None = None) -> tuple[float, float] | None:
        return self.wheel_delta_cm_from_result(result or {}) or self.wheel_delta_cm_from_spec(spec or {})

    def _brake_then_stop(self) -> None:
        if not self._enabled:
            return
        try:
            if settings.motion_brake_s > 0:
                motor.brake(settings.motion_brake_s)
            else:
                motor.stop()
        except Exception as exc:
            self._log.warning("Motor brake failed: %s", exc)
            motor.stop()

    def shutdown(self) -> None:
        self._request_stop()
        if self._enabled:
            try:
                motor.cleanup()
            except Exception as exc:
                self._log.warning("Motor cleanup failed: %s", exc)

    def _request_stop(self) -> None:
        self._stop_event.set()
        self.stop()
        with self._lock:
            thread = self._motion_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.2)
            if thread.is_alive():
                raise RuntimeError("Previous motion is still stopping; retry after it finishes")

    def _motion_label(self, spec: dict) -> str:
        mode = str(spec.get("mode") or "").strip().lower()
        direction = str(spec.get("direction") or "").strip().lower()
        return mode or direction or "forward"

    def _targets_for_spec(self, spec: dict) -> list[tuple[int, int]]:
        label = self._motion_label(spec)
        if label in {"sine", "sine_forward", "wave"}:
            return self._sine_targets(spec)
        if label in {"wiggle", "sine_turn", "turn_wave"}:
            return self._wiggle_targets(spec)
        if label == "wiggle_forward":
            return self._forward_wiggle_targets(spec)
        if label in {"avoid", "avoid_obstacle"}:
            back_cm = abs(float(spec.get("distance_cm") or settings.motion_avoid_backoff_cm))
            turn_deg = abs(float(spec.get("angle_deg") or settings.motion_avoid_turn_deg))
            return [(-self._distance_to_ticks(back_cm), -self._distance_to_ticks(back_cm)), self._turn_ticks(-turn_deg)]
        target = self._target_for_spec(spec)
        return [target] if target is not None else []

    def _target_for_spec(self, spec: dict) -> tuple[int, int] | None:
        label = self._motion_label(spec)
        def value(key: str, default: float) -> float:
            return float(default if spec.get(key) is None else spec[key])

        if label in {"forward", "distance"}:
            cm = value("distance_cm", settings.motion_default_step_cm)
            return self._distance_to_ticks(cm), self._distance_to_ticks(cm)
        if label == "backward":
            cm = value("distance_cm", settings.motion_default_step_cm)
            ticks = self._distance_to_ticks(cm)
            return -ticks, -ticks
        if label in {"left", "turn_left", "turn"}:
            deg = abs(value("angle_deg", settings.motion_default_turn_deg))
            if label == "turn" and value("angle_deg", deg) < 0:
                return self._turn_ticks(-deg)
            return self._turn_ticks(deg)
        if label in {"right", "turn_right"}:
            deg = abs(value("angle_deg", settings.motion_default_turn_deg))
            return self._turn_ticks(-deg)
        if label in {"arc", "arc_left", "arc_right"}:
            raw_angle = value("angle_deg", 90.0)
            if label == "arc_left":
                raw_angle = abs(raw_angle)
            elif label == "arc_right":
                raw_angle = -abs(raw_angle)
            return self._arc_ticks(
                radius_cm=float(spec.get("radius_cm") or settings.motion_arc_radius_cm),
                angle_deg=raw_angle,
            )
        return None

    def _sine_targets(self, spec: dict) -> list[tuple[int, int]]:
        return [self._curve_for_spec({**spec, "mode": "sine"}).targets(self.cm_per_count())]

    def _curve_for_spec(self, spec: dict) -> CurvePath | None:
        label = self._motion_label(spec)
        def value(key, default):
            return float(default if spec.get(key) is None else spec[key])
        if label == 'studio_path':
            return CurvePath.authored(spec.get('path'), spec.get('path_direction', 1), settings.motion_wheel_base_cm)
        if label in {"arc", "arc_left", "arc_right"}:
            angle = value("angle_deg", 90)
            if label == "arc_left":
                angle = abs(angle)
            elif label == "arc_right":
                angle = -abs(angle)
            return CurvePath.arc(value("radius_cm", settings.motion_arc_radius_cm), angle,
                                 settings.motion_wheel_base_cm)
        if label == 'wiggle_forward':
            spec = {'distance_cm':20, 'angle_deg':15, 'cycles':1, **spec}
        if label in {"sine", "sine_forward", "wave", "wiggle_forward"}:
            return CurvePath.sine(
                value("radius_cm", settings.motion_sine_radius_cm),
                value("angle_deg", settings.motion_sine_angle_deg),
                int(value("cycles", 2)), settings.motion_wheel_base_cm,
                None if spec.get("distance_cm") is None else value("distance_cm", 0),
                None if spec.get("amplitude_cm") is None else value("amplitude_cm", 0),
            )
        return None

    def _wiggle_targets(self, spec: dict) -> list[tuple[int, int]]:
        cycles = max(1, min(8, int(float(spec.get("cycles") or 3))))
        angle = max(5.0, min(60.0, float(spec.get("angle_deg") or settings.motion_wiggle_angle_deg)))
        targets: list[tuple[int, int]] = []
        for _ in range(cycles):
            targets.append(self._turn_ticks(angle))
            targets.append(self._turn_ticks(-2.0 * angle))
            targets.append(self._turn_ticks(angle))
        return targets

    def _forward_wiggle_targets(self, spec: dict) -> list[tuple[int, int]]:
        """Same continuous trajectory as drive(), including measured odometry."""
        return [self._curve_for_spec({**spec, 'mode':'wiggle_forward'}).targets(self.cm_per_count())]

    def _turn_ticks(self, angle_deg: float) -> tuple[int, int]:
        wheel_cm = math.pi * settings.motion_wheel_base_cm * (abs(angle_deg) / 360.0)
        ticks = self._distance_to_ticks(wheel_cm)
        return (-ticks, ticks) if angle_deg >= 0 else (ticks, -ticks)

    def _arc_ticks(self, radius_cm: float, angle_deg: float) -> tuple[int, int]:
        radius = max(settings.motion_wheel_base_cm / 2.0 + 0.5, abs(radius_cm))
        half_track = settings.motion_wheel_base_cm / 2.0
        theta = math.radians(abs(angle_deg))
        inner = self._distance_to_ticks(theta * max(0.1, radius - half_track))
        outer = self._distance_to_ticks(theta * (radius + half_track))
        return (inner, outer) if angle_deg >= 0 else (outer, inner)

    def _distance_to_ticks(self, distance_cm: float) -> int:
        circumference = math.pi * max(0.1, settings.motion_wheel_diameter_cm)
        revolutions = float(distance_cm) / circumference
        return int(round(revolutions * max(1, settings.motion_encoder_counts_per_rev)))

    def _run_timed_move(self, direction: str, speed: int, duration: float, ignore_obstacles: bool, future: Future) -> None:
        if direction in {"left", "right"}:
            speed = max(0, min(speed, settings.motion_turn_max_duty, MOTOR_MAX_DUTY))
        self._log.info("Timed motion: direction=%s speed=%s duration=%.2f", direction, speed, duration)
        started = time.monotonic()
        try:
            while not self._cancellation().is_set() and time.monotonic() - started < duration:
                forward = direction not in {"backward", "left", "right"}
                if forward and self._should_stop_for_obstacle(speed, speed, ignore_obstacles):
                    result = self._obstacle_result(direction, started)
                    self._set_last_result(result)
                    future.set_result(None)
                    return
                if direction == "forward":
                    motor.set_wheel_pwm(speed, speed)
                elif direction == "backward":
                    motor.set_wheel_pwm(-speed, -speed)
                elif direction == "left":
                    motor.set_wheel_pwm(-speed, speed)
                elif direction == "right":
                    motor.set_wheel_pwm(speed, -speed)
                elif direction in {"forward_slight_left", "slight_left"}:
                    motor.set_wheel_pwm(speed * 0.5, speed)
                elif direction in {"forward_slight_right", "slight_right"}:
                    motor.set_wheel_pwm(speed, speed * 0.5)
                elif direction in {"left_motor_forward", "motor_left_forward"}:
                    motor.set_wheel_pwm(speed, 0)
                elif direction in {"right_motor_forward", "motor_right_forward"}:
                    motor.set_wheel_pwm(0, speed)
                else:
                    self._log.warning("Unknown timed motion direction: %s", direction)
                    future.set_result(None)
                    return
                self._cancellation().wait(timeout=max(0.02, float(settings.motion_control_interval_s)))
            self._set_last_result({"mode": "timed", "direction": direction, "duration_s": duration,
                                   "ok": not self._cancellation().is_set(),
                                   "reason": "stopped" if self._cancellation().is_set() else "completed"})
            future.set_result(None)
        except Exception as exc:
            self._set_last_result({"mode": "error", "direction": direction, "error": str(exc)})
            future.set_exception(exc)
        finally:
            self._brake_then_stop()
            self._clear_thread()

    def _run_target_sequence(
        self,
        label: str,
        speed: int,
        duration: float,
        targets: list[tuple[int, int]],
        ignore_obstacles: bool,
        future: Future,
        curve: CurvePath | None = None,
        allow_encoder_fallback: bool = False,
        turn_rate_deg_s: float | None = None,
    ) -> None:
        started = time.monotonic()
        results: list[dict] = []
        try:
            for index, target in enumerate(targets):
                if self._cancellation().is_set():
                    self._set_last_result({"mode": "sequence", "direction": label, "ok": False,
                                           "reason": "stopped", "segments": results})
                    future.set_result(None)
                    return
                result = self._run_target_blocking(label, speed, duration, target, ignore_obstacles,
                                                  index=index, curve=curve, allow_encoder_fallback=allow_encoder_fallback,
                                                  turn_rate_deg_s=turn_rate_deg_s)
                results.append(result)
                if not result.get("ok"):
                    self._set_last_result({**result, "mode": "sequence", "direction": label, "ok": False, "segments": results})
                    future.set_result(None)
                    return
            final = {
                "mode": "sequence" if len(targets) > 1 else "encoder",
                "direction": label,
                "ok": True,
                "elapsed_s": round(time.monotonic() - started, 3),
                "segments": results,
                "control": "curve_tracking" if curve else "position_pid",
            }
            if results:
                final["target"] = results[-1].get("target")
                final["final"] = results[-1].get("final")
                if 'odometry' in results[-1]:
                    final['odometry'] = results[-1]['odometry']
                if 'path_tracking' in results[-1]:
                    final['path_tracking'] = results[-1]['path_tracking']
                    final['control'] = 'curve_tracking'
                if 'heading' in results[-1]:
                    final['heading'] = results[-1]['heading']
                for key in ('control_hz', 'target_control_hz', 'max_control_gap_ms', 'control_jitter_rms_ms', 'missed_control_ticks', 'precision_reached'):
                    if key in results[-1]:
                        final[key] = results[-1][key]
                if results[-1].get("control") == "settled_turn":
                    final.update({key: results[-1][key] for key in (
                        "control", "error_counts", "tolerance_counts", "control_hz",
                        "max_control_gap_ms", "correction_rounds", "pwm_cap", "startup_control", "rate_control")
                        if key in results[-1]})
            final['warnings'] = sorted({w for row in results for w in
                [*row.get('warnings', []), *row.get('heading', {}).get('warnings', [])]})
            if any(row.get('precision_reached') is False for row in results):final['precision_reached'] = False
            self._set_last_result(final)
            future.set_result(None)
        except Exception as exc:
            self._set_last_result({"mode": "error", "direction": label, "error": str(exc)})
            future.set_exception(exc)
        finally:
            self._brake_then_stop()
            self._clear_thread()

    def _run_target_blocking(
        self,
        label: str,
        speed: int,
        duration: float,
        target: tuple[int, int],
        ignore_obstacles: bool,
        index: int = 0,
        curve: CurvePath | None = None,
        allow_encoder_fallback: bool = False,
        turn_rate_deg_s: float | None = None,
    ) -> dict:
        target_left, target_right = target
        command_duty, min_duty = self._duty_limits(speed)
        heading, heading_fallback = self._heading_feedback(allow_encoder_fallback)
        intent = current_intent()
        if intent is not None and intent.owner == 'navigation' and heading is None and not allow_encoder_fallback:
            motor.stop()
            return {'mode':'encoder', 'ok':False, 'reason':heading_fallback or 'imu_heading_unavailable'}
        def heading_result():
            return heading.status(target) if heading else {'source': 'encoders', 'reason': heading_fallback}
        if curve is None and target_left * target_right < 0:
            cap = min(command_duty, settings.motion_turn_max_duty, MOTOR_MAX_DUTY)
            continuous_turn=bool(intent and intent.owner=='motion-studio')
            tolerance = (settings.motion_imu_turn_tolerance_deg / (2*heading.degrees_per_count)
                         if heading else max(1, settings.motion_turn_tolerance_counts))
            rate_counts = (turn_rate_deg_s/(2*math.degrees(self.cm_per_count()/settings.motion_wheel_base_cm))
                           if turn_rate_deg_s is not None else None)
            turn_timeout = max(settings.motion_encoder_timeout_s,
                               abs(target_left)/max(1,settings.motion_encoder_counts_per_rev)*8, duration+2)
            if rate_counts is not None:
                # Slow sensor-limited scans need time to reach their requested
                # angle. Stall detection remains independent of this deadline.
                turn_timeout = min(90.,max(turn_timeout,max(abs(target_left),abs(target_right))/rate_counts*1.5+3))
            result = run_turn(
                motor, self._cancellation(), target, cap=cap,
                start_duty=min(cap, settings.motion_turn_start_duty),
                tolerance=tolerance, feedback=heading,
                interlock=getattr(self, 'handling_interlock', None),
                interval=settings.motion_control_interval_s,
                timeout=turn_timeout,
                stall_timeout=settings.motion_stall_timeout_s,
                sync_gain=max(0.03, min(0.15, settings.motion_pid_kp)),
                pulsed=(not continuous_turn and cap==MOTOR_MAX_DUTY and settings.motion_turn_start_duty>=cap and abs(self.turn_angle_deg_from_wheel_cm(
                    target_left*self.cm_per_count(),target_right*self.cm_per_count()))<=45),
                on_sample=self._publish_navigation_feedback,
                best_effort=self._manual_command() or bool(intent and intent.owner=='navigation'),
                rate_counts_per_s=rate_counts,
                continuous=continuous_turn,
                rolling_duty=min(cap,settings.motion_pwm_min_duty),
            )
            result.update(mode="encoder", direction=label, segment=index, heading=heading_result())
            self._log.info("Settled turn: %s", result)
            return result
        if curve is None and target_left == target_right and target_left:
            curve = CurvePath.straight(target_left*self.cm_per_count(), settings.motion_wheel_base_cm)
        if command_duty <= 0 or target == (0, 0):
            motor.stop()
            return {"mode": "encoder", "direction": label, "segment": index,
                    "ok": True, "reason": "zero_speed" if command_duty <= 0 else "zero_distance",
                    "target": {"left": target_left, "right": target_right},
                    "final": {"left": 0, "right": 0}}
        read_counts = motor.counts
        started = time.monotonic()
        max_s = max(
            float(settings.motion_encoder_timeout_s),
            abs(target_left) / max(1, settings.motion_encoder_counts_per_rev) * 4.0,
            abs(target_right) / max(1, settings.motion_encoder_counts_per_rev) * 4.0,
            duration + 1.0,
        )
        # The initial estimate assumes fairly quick wheels. Curves can move
        # much slower near the PWM/friction threshold, especially while easing
        # into their endpoint. Permit measured progress to extend that estimate,
        # never indefinitely; per-wheel stall watches still act independently.
        hard_deadline_s = max(max_s, min(90., max_s * 3))
        tolerance = max(1, int(settings.motion_encoder_tolerance_counts))
        stop_margin = self._stop_margin(target_left, target_right, tolerance, command_duty)
        # Preserve equal fractional completion on tight arcs: a fixed 40-count
        # margin can be a large fraction of the short inside-wheel target.
        left_margin, right_margin = (stop_margin, stop_margin)
        if curve:
            longest = max(1, abs(target_left), abs(target_right))
            left_margin, right_margin = (max(1, round(stop_margin * abs(t) / longest)) for t in target)
        self._log.info("PID motion: label=%s segment=%s speed=%s target=%s/%s duty<=%.1f control=%s", label, index, speed, target_left, target_right, command_duty, "curve_tracking" if curve else "position_pid")
        motor.reset_counts()
        follower = CurveFollower(curve, self.cm_per_count()) if curve else None
        # Integrate measured increments throughout a curve. Its endpoint cannot
        # be reconstructed as a single constant-radius arc from wheel totals.
        odometry, previous_counts = [0.0, 0.0, 0.0], [0, 0]
        def measure_pose(left, right):
            if curve is None:
                return
            dl, dr = ((count-old)*self.cm_per_count() for count, old in zip((left, right), previous_counts, strict=True))
            previous_counts[:] = (left, right)
            distance, angle, h = (dl+dr)/2, (dr-dl)/curve.track_cm, odometry[2]
            if abs(angle) < 1e-9:
                odometry[0] += distance*math.cos(h)
                odometry[1] += distance*math.sin(h)
            else:
                odometry[0] += distance/angle*(math.sin(h+angle)-math.sin(h))
                odometry[1] -= distance/angle*(math.cos(h+angle)-math.cos(h))
            odometry[2] += angle
        left_watch = EncoderProgressWatch(target_left, time.monotonic())
        right_watch = EncoderProgressWatch(target_right, time.monotonic())
        integral_left = integral_right = 0.0
        prev_err_left = float(target_left)
        prev_err_right = float(target_right)
        cadence = ControlTick(settings.motion_control_interval_s)
        warnings = set()
        best_effort = self._manual_command() or bool(intent and intent.owner=='navigation')
        while not self._cancellation().is_set():
            now = time.monotonic()
            dt = cadence.tick(now)
            if dt > .25:
                if not best_effort:
                    motor.stop()
                    return {'ok':False,'reason':'control_loop_late',**cadence.status()}
                warnings.add('control_loop_late')
            left, right = read_counts()
            raw_left, raw_right = left, right
            if heading:
                try:
                    left, right = heading.counts((left, right), now)
                except HeadingError as error:
                    motor.stop()
                    return {'mode': 'encoder', 'ok': False, 'reason': str(error), 'heading': heading_result()}
            measure_pose(left, right)
            self._publish_navigation_feedback((raw_left, raw_right), (left, right), now)
            err_left = target_left - left
            err_right = target_right - right
            if ((target_left and raw_left * math.copysign(1, target_left) < -max(20, tolerance)) or
                    (target_right and raw_right * math.copysign(1, target_right) < -max(20, tolerance))):
                motor.stop()
                return {"mode": "encoder", "direction": label, "ok": False,
                        "reason": "encoder_direction_mismatch", "segment": index,
                        "target": {"left": target_left, "right": target_right},
                        "final": {"left": left, "right": right}}
            left_reached = self._wheel_reached(left, target_left, left_margin)
            right_reached = self._wheel_reached(right, target_right, right_margin)
            if curve:
                # One continuous path has one stopping point. Braking one wheel
                # at its endpoint lets the other wheel add the missing heading
                # as a pivot, destroying the requested arc/sine trajectory.
                remaining_counts = (curve.length_cm / self.cm_per_count()
                                    - curve.direction * (left + right) / 2)
                left_reached = right_reached = remaining_counts <= (left_margin + right_margin) / 2
            if left_reached and right_reached:
                self._brake_then_stop()
                left, right = read_counts()
                if heading:
                    try:
                        measured = heading.counts((left, right), time.monotonic())
                    except HeadingError as error:
                        return {'mode': 'encoder', 'ok': False, 'reason': str(error), 'heading': heading_result()}
                    measure_pose(*measured)
                else:
                    measure_pose(left, right)
                self._publish_navigation_feedback((left, right), measured if heading else (left, right), time.monotonic())
                result = {
                    "mode": "encoder",
                    "direction": label,
                    "segment": index,
                    "target": {"left": target_left, "right": target_right},
                    "final": {"left": left, "right": right},
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "ok": True,
                    "heading": heading_result(),
                    **cadence.status(), 'warnings':sorted(warnings),
                }
                if curve:
                    result['odometry'] = dict(zip(('x_cm', 'y_cm', 'heading_rad'), odometry, strict=True))
                    result['path_tracking'] = {
                        'control':'wheel_phase_and_velocity', 'stop':'coupled',
                        'max_heading_error_deg':round(follower.max_heading_error_deg,3),
                        'end_heading_error_deg':round(math.degrees(curve.points[-1].heading-odometry[2]),3),
                        'wheel_balance_trim_pwm':round(follower.trim,3),
                        'breakaway_assists':list(follower.breakaway_assists),
                        'max_cross_track_error_cm':round(follower.max_cross_track_cm,3),
                        'checkpoints':follower.checkpoints,
                    }
                return result
            if self._should_stop_for_obstacle(target_left, target_right, ignore_obstacles):
                self._brake_then_stop()
                left, right = read_counts()
                result = self._obstacle_result(label, started)
                result.update({"segment": index, "target": {"left": target_left, "right": target_right}, "final": {"left": left, "right": right}})
                return result
            if follower and remaining_counts > 0:
                rate = sum(follower.velocity) / 2
                progress = curve.length_cm / self.cm_per_count() - remaining_counts
                if rate > 5 and progress > curve.length_cm / self.cm_per_count() * .05:
                    estimate = now - started + 1.5 * remaining_counts / rate + .5
                    max_s = max(max_s, min(hard_deadline_s, estimate))
            if now - started > max_s:
                motor.stop()
                return {
                    "mode": "encoder",
                    "direction": label,
                    "segment": index,
                    "target": {"left": target_left, "right": target_right},
                    "final": {"left": left, "right": right},
                    "elapsed_s": round(now - started, 3),
                    "ok": False,
                    "reason": "timeout",
                }

            if follower:
                # A shared endpoint slowdown plus differential feedforward and
                # measured path-phase PID avoids two saturated endpoint PIDs.
                remaining = max(0, remaining_counts)
                cruise = min(command_duty, max(min_duty, float(settings.motion_pid_kp) * remaining))
                duty_left, duty_right = follower.command(
                    (left, right), cruise, min_duty, command_duty,
                    settings.motion_pid_kp, settings.motion_pid_ki, settings.motion_pid_kd,
                    settings.motion_pid_integral_limit, dt, settings.motion_curve_pwm_slew_per_s,
                    raw_counts=(raw_left, raw_right),
                    trim_gain=settings.motion_curve_trim_gain,
                    start_assist=settings.motion_curve_start_assist,
                    pose=odometry, cross_track_gain=settings.motion_curve_cross_track_gain,
                )
            else:
                integral_left = self._clamp_integral(integral_left + err_left * dt)
                integral_right = self._clamp_integral(integral_right + err_right * dt)
                derivative_left = (err_left - prev_err_left) / dt
                derivative_right = (err_right - prev_err_right) / dt
                prev_err_left = err_left
                prev_err_right = err_right
                duty_left = self._pid_duty(err_left, integral_left, derivative_left, target_left, left, stop_margin, command_duty, min_duty)
                duty_right = self._pid_duty(err_right, integral_right, derivative_right, target_right, right, stop_margin, command_duty, min_duty)
                correction = self._sync_correction(left, right, target_left, target_right, command_duty)
                duty_left -= math.copysign(1, target_left or 1) * correction
                duty_right += math.copysign(1, target_right or 1) * correction
            # Synchronization may slow a leading wheel, never exceed the user's
            # duty cap, reverse an unfinished wheel, or restart a finished one.
            duty_left = self._bounded_wheel_duty(duty_left, target_left, left_reached, command_duty)
            duty_right = self._bounded_wheel_duty(duty_right, target_right, right_reached, command_duty)
            motor.set_wheel_pwm_with_brake(
                duty_left,
                duty_right,
                left_brake=left_reached and not right_reached,
                right_brake=right_reached and not left_reached,
            )
            stalled_wheels = [name for name, watch, count, reached in (
                ("left", left_watch, raw_left, left_reached),
                ("right", right_watch, raw_right, right_reached),
            ) if watch.stalled(count, reached, now, settings.motion_stall_timeout_s)]
            if stalled_wheels:
                motor.stop()
                return {
                    "mode": "encoder",
                    "direction": label,
                    "segment": index,
                    "target": {"left": target_left, "right": target_right},
                    "final": {"left": left, "right": right},
                    "ok": False,
                    "reason": "encoder_stall",
                    "stalled_wheels": stalled_wheels,
                    "elapsed_s": round(now - started, 3),
                }
            cadence.wait(self._cancellation())
        left, right = read_counts()
        return {"mode": "encoder", "direction": label, "segment": index, "ok": False, "reason": "stopped",
                "target": {"left": target_left, "right": target_right}, "final": {"left": left, "right": right}}

    def _duty_limits(self, speed: float) -> tuple[float, float]:
        # Same absolute PWM-percent semantics as raw/timed movement. The safety
        # maximum is a ceiling, not a second multiplier on the requested speed.
        command = max(0.0, min(MOTOR_MAX_DUTY, float(settings.motion_pwm_max_duty), float(speed)))
        minimum = max(0.0, min(command, float(settings.motion_pwm_min_duty)))
        return command, minimum

    @staticmethod
    def _bounded_wheel_duty(duty: float, target: int, reached: bool, cap: float) -> float:
        if reached or target == 0:
            return 0.0
        sign = math.copysign(1.0, target)
        return sign * max(0.0, min(cap, duty * sign))

    def _wheel_reached(self, current: int, target: int, tolerance: int) -> bool:
        if target == 0:
            return True
        if target > 0:
            return current >= target - tolerance
        return current <= target + tolerance

    def _stop_margin(self, target_left: int, target_right: int, tolerance: int, command_duty: float) -> int:
        target_abs = max(1, min(abs(target_left) or abs(target_right), abs(target_right) or abs(target_left)))
        speed_margin = int(round(command_duty * float(settings.motion_stop_margin_per_duty)))
        target_margin = int(round(target_abs * float(settings.motion_stop_margin_ratio)))
        return max(tolerance, min(speed_margin, target_margin))

    def _pid_duty(
        self,
        error: int,
        integral: float,
        derivative: float,
        target: int,
        current: int,
        tolerance: int,
        command_duty: float,
        min_duty: float,
    ) -> float:
        if self._wheel_reached(current, target, tolerance):
            return 0.0
        output = (
            float(settings.motion_pid_kp) * error
            + float(settings.motion_pid_ki) * integral
            + float(settings.motion_pid_kd) * derivative
        )
        if abs(output) < min_duty:
            output = math.copysign(min_duty, output if output else error)
        return max(-command_duty, min(command_duty, output))

    def _clamp_integral(self, value: float) -> float:
        limit = max(0.0, float(settings.motion_pid_integral_limit))
        if limit <= 0:
            return 0.0
        return max(-limit, min(limit, value))

    def _sync_correction(self, left: int, right: int, target_left: int, target_right: int, command_duty: float) -> float:
        if not target_left or not target_right:
            return 0.0
        tolerance = max(1, int(settings.motion_encoder_tolerance_counts))
        if self._wheel_reached(left, target_left, tolerance) or self._wheel_reached(right, target_right, tolerance):
            return 0.0
        left_progress = left / target_left
        right_progress = right / target_right
        error = left_progress - right_progress
        max_correction = command_duty * 0.35
        correction = error * float(settings.motion_sync_kp) * command_duty
        return max(-max_correction, min(max_correction, correction))

    def _obstacle_distance_cm(self) -> float | None:
        getter=self._distance_getter
        intent=current_intent()
        if intent is not None and intent.owner=='navigation':
            getter=getattr(self,'_navigation_distance_getter',None) or getter
        if getter is None:
            return None
        try:
            distance = getter()
            if distance is None or not math.isfinite(float(distance)) or float(distance) < 0:
                return None
            return float(distance)
        except Exception:
            return None

    def _obstacle_blocked(self, distance_cm: float | None) -> bool:
        return (
            settings.motion_obstacle_stop_enabled
            and (distance_cm is None or not math.isfinite(distance_cm) or distance_cm < float(settings.motion_obstacle_stop_cm))
        )

    def _should_stop_for_obstacle(self, target_left: float, target_right: float, ignore_obstacles: bool) -> bool:
        # Owner-directed filming deliberately runs without range gating. This
        # follows the trusted activity through every worker/gesture, not JSON
        # flags or a temporary global setting that could leak into autonomy.
        intent = current_intent()
        if intent is not None and intent.owner == 'filming-shot':
            return False
        # Individual normal/app actions cannot bypass the configured protection.
        forward_component = target_left > 0 or target_right > 0
        turning_in_place = target_left * target_right < 0
        if not forward_component or turning_in_place:
            return False
        distance = self._obstacle_distance_cm()
        if distance is None and intent is not None and intent.owner=='navigation':
            unknown=getattr(self,'_navigation_unknown_getter',None)
            try:
                if unknown and unknown():return False
            except Exception:pass
        if distance is None and self._manual_command():
            return False
        return self._obstacle_blocked(distance)

    def _obstacle_result(self, label: str, started: float) -> dict:
        distance = self._obstacle_distance_cm()
        return {
            "mode": "safety_stop",
            "direction": label,
            "ok": False,
            "reason": "range_unavailable" if distance is None else "obstacle",
            "distance_cm": distance,
            "threshold_cm": settings.motion_obstacle_stop_cm,
            "elapsed_s": round(time.monotonic() - started, 3),
        }

    def _set_last_result(self, result: dict) -> None:
        with self._lock:
            self._last_result = result
        if result.get("reason"):
            self._log.info("Motion stopped: direction=%s reason=%s distance_cm=%s", result.get("direction"), result["reason"], result.get("distance_cm"))

    def _clear_thread(self) -> None:
        if getattr(self, "_orchestrator", None) is not None:
            return  # The wrapper releases ownership after runner cleanup.
        with self._lock:
            if self._motion_thread is threading.current_thread():
                self._motion_thread = None
