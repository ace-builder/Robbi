"""Owner-only ephemeral feature preparation and explicit calibration activation."""
import json
import math
from flask import Blueprint, current_app, jsonify, request
from robbi.app_runtime.owner import require_owner
from robbi.config import merge
from robbi.errors import ValidationError
from robbi.storage import atomic_write
from .wake_training import WakeTraining, dataset_stamp


def blueprint(runtime):
    bp = Blueprint("wake_training", __name__)
    training = WakeTraining()

    @bp.post("/api/wake-training/prepare")
    def start():
        require_owner()
        from robbi.configuration.settings import settings
        from robbi.engine.wake_dataset import WakeDatasetManager
        runtime.controller()
        return jsonify(training.start(WakeDatasetManager(), settings)), 202

    @bp.get("/api/wake-training/<identifier>")
    def status(identifier):
        require_owner()
        return jsonify(training.get(identifier))

    @bp.get("/api/wake-training/<identifier>/features")
    def features(identifier):
        require_owner()
        return jsonify(training.get(identifier, features=True))

    @bp.post("/api/wake-training/<identifier>/cancel")
    def cancel(identifier):
        require_owner()
        with training.lock:
            training.get(identifier)
            training.cancelled.set()
            training.result = None
        return jsonify(ok=True)

    @bp.post("/api/wake-training/<identifier>/apply")
    def apply(identifier):
        require_owner()
        from robbi.configuration.settings import settings
        from robbi.engine.local_wake import LocalWakeDetector
        from robbi.engine.wake_dataset import WakeDatasetManager
        payload = request.get_json()
        if not isinstance(payload, dict) or set(payload) != {"threshold", "fingerprint"}:
            raise ValidationError("Provide the calibrated threshold and dataset fingerprint")
        value = payload["threshold"]
        if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= .5:
            raise ValidationError("Wake threshold must be finite and between 0.01 and 0.5")
        with current_app.config["ROBBI_CONFIG_LOCK"], training.lock:
            result = training.get(identifier, features=True)
            stamp = dataset_stamp(WakeDatasetManager(), settings)[0]
            if payload["fingerprint"] != stamp or result["fingerprint"] != stamp:
                raise ValidationError("Recordings or wake settings changed; train again before applying")
            detector = LocalWakeDetector()
            if [ref.name for ref in detector._references] != [ref["name"] for ref in result["references"]]:
                raise ValidationError("Active wake references changed; train again")
            patch = {"voice": {"wake_engine": "local_wake", "local_wake_threshold": value}}
            previous = {"voice": {"wake_engine": settings.voice_wake_engine, "local_wake_threshold": settings.voice_local_wake_threshold}}
            try:
                runtime.controller().apply_settings(patch)
            except Exception:
                runtime.controller().apply_settings(previous)
                raise
            runtime.config.values = merge(runtime.config.values, patch)
            atomic_write(runtime.config.data_dir / "state/wake-calibration.json",
                         json.dumps({"version": 1, "threshold": value, "fingerprint": stamp,
                                     "method": "browser-personal-DTW", "prepare_seconds": result["prepare_seconds"]}).encode())
        return jsonify(ok=True, message="Personal wake calibration applied. Test it with fresh speech and background noise.", threshold=value)

    return bp
