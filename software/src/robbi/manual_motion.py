"""Small live dashboard boundary; measured commands share the motion controller.

No hardware polling, job queue, or extra worker here. The current Future is
bounded to one record and reflects cleanup completion, not just admission.
"""
import math
import logging
import threading
import uuid

from flask import Blueprint, jsonify, request

from .errors import BusyError, UnavailableError, ValidationError
from .orchestration import Intent, MANUAL, intent_scope


def validate_command(payload):
    if not isinstance(payload, dict):
        raise ValidationError("Movement must be an object")
    mode = payload.get("mode")
    parameters = {
        "distance": {"distance_cm": (-100, 100, 20)},
        "turn": {"angle_deg": (-180, 180, 90)},
        "arc": {"angle_deg": (-180, 180, 90), "radius_cm": (6, 100, 15)},
        "sine": {"distance_cm": (.5, 100, 20), "cycles": (1, 6, 1),
                 "radius_cm": (6, 100, 15), "angle_deg": (5, 60, 30)},
        "wiggle": {"angle_deg": (5, 60, 15), "cycles": (1, 6, 1)},
        "wiggle_forward": {"distance_cm": (.5, 100, 20),
                           "angle_deg": (5, 60, 15), "cycles": (1, 6, 1)},
        "avoid_obstacle": {"distance_cm": (.5, 50, 10), "angle_deg": (1, 180, 90)},
    }
    if not isinstance(mode, str) or mode not in parameters:
        raise ValidationError("Unknown manual movement")
    limits = {"speed": (1, 100, 100), **parameters[mode]}
    if mode in {'sine','wiggle_forward'} and 'amplitude_cm' in payload:
        limits['amplitude_cm'] = (0, 30, 0)
    if mode == 'wiggle_forward' and 'radius_cm' in payload:
        limits['radius_cm'] = (6, 100, 15)
    if set(payload) - {"mode", *limits}:
        raise ValidationError("Unexpected movement parameter")
    result = {"mode": mode, "require_encoders": True}
    for key, (low, high, default) in limits.items():
        value = payload.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValidationError(f"{key} must be between {low:g} and {high:g}")
        if key in {"cycles", "speed"} and int(value) != value:
            raise ValidationError(f"{key} must be a whole number")
        result[key] = value
    if mode in {"distance", "turn", "arc"} and result.get("distance_cm", result.get("angle_deg")) == 0:
        raise ValidationError("Distance or angle must be nonzero")
    return result


def validate_raw(payload):
    if not isinstance(payload, dict) or set(payload)-{'left','right','duration'}:
        raise ValidationError('Supply left/right wheel duty and duration')
    result = {'mode':'raw'}
    for name, low, high, default in [('left',-100,100,0),('right',-100,100,0),('duration',.05,2,.2)]:
        value = payload.get(name,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
            raise ValidationError(f'{name} must be between {low:g} and {high:g}')
        result[name] = value
    return result


def needs_forward_range(spec):
    if spec['mode'] in {'turn','wiggle','avoid_obstacle'}:
        return False
    if spec['mode']=='distance':
        return spec['distance_cm']>0
    if spec['mode']=='raw':
        left,right=spec['left'],spec['right']
        return (left>0 or right>0) and left*right>=0
    return True


class ManualMotion:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.Lock()
        self.last = None
        self.warnings = []

    def snapshot(self):
        controller = self.runtime.controller()
        motion, tof = controller.motion.status(), controller.tof.status()
        imu = controller.imu.status()
        with self.lock:
            command = self._command_status()
        return {
            "motion": {key: motion.get(key) for key in (
                "enabled", "available", "encoder_enabled", "running", "last_error",
                "heading", "last_result", "obstacle")},
            "tof": tof,
            "imu": {key: imu.get(key) for key in ("enabled", "available", "fresh", "calibration", "last_error")},
            "command": command,
            "handling": getattr(getattr(controller, 'dynamics', None), 'status', lambda: {})(),
        }

    def _command_status(self):
        if self.last is None:
            return None
        ident, spec, future = self.last
        value = {"id": ident, "spec": spec, "state": "running", "warnings":list(self.warnings)}
        if future.done():
            try:
                result = future.result()
                if not isinstance(result, dict):
                    result = {"ok": False, "reason": "No completion result received"}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            value.update(state="completed" if result.get("ok") else "stopped", result=result)
        return value

    def start(self, payload, *, raw=False):
        spec = validate_raw(payload) if raw else validate_command(payload)
        controller = self.runtime.controller()
        # Stop's token is captured BEFORE admission. Stop racing this request
        # must cancel it rather than allowing a delayed command to start.
        cancel = controller._action_token()[1]
        if not self.lock.acquire(blocking=False):
            raise BusyError("A manual command is already being started")
        try:
            if self.last and not self.last[2].done():
                raise BusyError("Wait for this movement to finish or press Stop")
            motion = controller.motion.status()
            if not motion.get("enabled") or not motion.get("available"):
                raise UnavailableError("Motor hardware is unavailable")
            if motion.get("running"):
                raise BusyError("Robbi is already moving. Stop it before a new manual command.")
            if not raw and not motion.get("encoder_enabled"):
                raise UnavailableError("Measured movements require wheel encoders")
            if not raw and getattr(controller.motion, 'handling_interlock', lambda: False)():
                raise UnavailableError("Robbi detects a tilted or held pose. Put it upright; if already upright, use Set upright pose in the IMU tab.")
            tof = controller.tof.status()
            warnings = []
            if needs_forward_range(spec) and (not tof.get("fresh") or not tof.get("available") or not tof.get("enabled")):
                warnings.append('front_range_unavailable')
            with intent_scope(Intent("manual:dashboard", MANUAL, cancelled=cancel)):
                future = (controller.motion.raw_wheel_drive(spec['left'],spec['right'],spec['duration'],cancel_event=cancel)
                          if raw else controller.motion.drive(spec, cancel_event=cancel))
            if future is None:
                raise UnavailableError("Motion could not be started")
            self.last = (uuid.uuid4().hex, spec, future)
            self.warnings = warnings
            # Register odometry exactly as ordinary /drive does. A tracking
            # failure must not turn an accepted movement into a retryable error.
            try:
                if not raw:controller.slam.track_drive_action(spec, future)
            except Exception:
                logging.getLogger(__name__).exception("Could not register manual-motion odometry")
            return self._command_status()
        finally:
            self.lock.release()


def blueprint(runtime):
    bp = Blueprint("manual_motion", __name__)
    service = ManualMotion(runtime)

    @bp.get("/api/manual-motion")
    def status():
        return jsonify(service.snapshot())

    @bp.post("/api/manual-motion")
    def drive():
        return jsonify(ok=True, command=service.start(request.get_json()))

    @bp.post("/api/manual-motion/raw")
    def raw():
        return jsonify(ok=True, command=service.start(request.get_json(),raw=True))

    return bp
