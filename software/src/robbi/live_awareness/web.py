"""Owner-only controls; credentials use the existing Connections screen."""

import json

from flask import Blueprint, jsonify, request

from robbi.app_runtime.owner import require_owner
from robbi.errors import ValidationError

from .openai_vision import DEFAULT_MODEL


def reaction_status(controller, state):
    if not state.get("running"):
        return {"state": "off", "reason": "Session is not running"}
    if not state.get("options", {}).get("react"):
        return {"state": "observing", "reason": "Observation only — enable reactions to hear or see a response"}
    attention = getattr(controller, "attention", None)
    evidence = attention.live_evidence() if attention is not None else None
    heartbeat = controller.heartbeat_status()
    if evidence is None:
        acted = heartbeat.get("last_decision") == "action_requested"
        return {"state": "responded" if acted else "waiting",
                "reason": "Reaction sent" if acted else "Waiting for a directed greeting, question or meaningful change"}
    reason = controller._heartbeat_skip_reason()
    if reason:
        return {"state": "waiting", "reason": "Reaction waiting: " + reason.replace("_", " ")}
    reason = controller.autonomy_policy.eligible("react", uses_model=True)
    if reason and reason != "activity_running":
        return {"state": "waiting", "reason": "Reaction waiting: " + reason.replace("_", " ")}
    return {"state": "thinking", "reason": "Preparing a response to this interaction"}


def blueprint(runtime):
    bp = Blueprint("live_awareness", __name__)

    def service():
        require_owner()
        return runtime.controller().live_awareness

    @bp.get("/api/live-awareness")
    def status():
        live = service()
        controller = runtime.controller()
        state = live.status()
        return jsonify(
            **state,
            default_model=DEFAULT_MODEL,
            default_provider="openai",
            autonomy=controller.autonomy_status(),
            reaction=reaction_status(controller, state),
        )

    @bp.post("/api/live-awareness/start")
    def start():
        live = service()
        if not request.is_json:
            raise ValidationError("Send live session options as JSON")
        # The dashboard boundary already parses JSON. Reuse Flask's cached
        # body; the underlying request stream has been consumed.
        if request.content_length is not None and request.content_length > 2048:
            raise ValidationError("Live session options exceed 2 KiB")
        raw = request.get_data(cache=True)
        if len(raw) > 2048:
            raise ValidationError("Live session options exceed 2 KiB")
        return jsonify(live.start(json.loads(raw)))

    @bp.post("/api/live-awareness/stop")
    def stop():
        live = service()
        live.stop()
        return jsonify(live.status())

    return bp
