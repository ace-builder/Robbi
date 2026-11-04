"""Dashboard routes reuse the main app's authentication and origin boundary."""
import re

from flask import Blueprint, abort, jsonify, request, send_file


def blueprint(studio):
    bp = Blueprint("audio_studio", __name__, url_prefix="/api/audio-studio")

    @bp.route("/settings", methods=["GET", "POST"])
    def settings():
        return jsonify(studio.configure(request.get_json() or {}) if request.method == "POST" else studio.settings())

    @bp.get("/voices")
    def voices():
        return jsonify(studio.voices(request.args.get("page_token", "")))

    @bp.route("/clips", methods=["GET", "POST"])
    def clips():
        if request.method == "POST":
            data = request.get_json() or {}
            return jsonify(studio.generate(data.get("text"), data.get("voice_id"))), 202
        return jsonify(studio.history(offset=int(request.args.get("offset", 0)),
                                      limit=int(request.args.get("limit", 20)), status=request.args.get("status", "all")))

    @bp.route("/clips/<clip_id>", methods=["GET", "DELETE"])
    def clip(clip_id):
        if request.method == "DELETE":
            studio.discard(clip_id, delete=True)
            return jsonify(ok=True)
        return jsonify(studio.get(clip_id) or abort(404, "Clip not found"))

    @bp.post("/clips/<clip_id>/keep")
    def keep(clip_id):
        return jsonify(studio.keep(clip_id, (request.get_json() or {}).get("name")))

    @bp.post("/clips/<clip_id>/discard")
    def discard(clip_id):
        studio.discard(clip_id)
        return jsonify(ok=True)

    @bp.post("/clips/<clip_id>/regenerate")
    def regenerate(clip_id):
        return jsonify(studio.regenerate(clip_id)), 202

    @bp.get("/clips/<clip_id>/audio")
    def audio(clip_id):
        opened = studio.audio(clip_id)
        if opened is None:
            abort(404, "Audio is not available for this generation")
        handle, clip = opened
        name = re.sub(r"[^\w .-]", "_", clip["name"] or f"robbi-{clip_id[:8]}")[:100].strip(" .") or "robbi"
        try:
            response = send_file(handle, mimetype="audio/mpeg", download_name=name + ".mp3",
                                 as_attachment=request.args.get("download") == "1", conditional=False)
            response.content_length = clip["bytes"]
            response.make_conditional(request, accept_ranges=True, complete_length=clip["bytes"])
            response.call_on_close(handle.close)
            return response
        except Exception:
            handle.close()
            raise

    return bp
