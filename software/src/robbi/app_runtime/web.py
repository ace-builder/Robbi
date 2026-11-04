"""App HTTP endpoints: all slow execution goes through bounded asynchronous jobs."""
from __future__ import annotations

import html
import wave
from flask import Blueprint, Response, jsonify, request, send_file, g

from robbi.errors import ValidationError
from .manifest import confined
from .owner import owner_access, require_owner

PAGE_CSP = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; font-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts allow-forms"


def blueprint(runtime):
    routes = Blueprint("standalone_apps", __name__)
    host = runtime.app_host

    @routes.get("/api/apps")
    def catalog():
        return jsonify(host.snapshot())

    @routes.post("/api/apps/scan")
    def scan():
        require_owner()
        return jsonify(host.scan())

    @routes.post("/api/apps/install")
    def install():
        require_owner()
        uploaded = request.files.get("file") or request.files.get("archive")
        if uploaded is None:
            raise ValidationError("Choose an app project ZIP")
        data = uploaded.stream.read(host.packages.MAX_BYTES + 1)
        return jsonify(host.install(data)), 201

    @routes.get("/api/apps/<app_id>")
    def detail(app_id):
        return jsonify(host.detail(app_id))

    @routes.patch("/api/apps/<app_id>/config")
    def configure(app_id):
        return jsonify(config=host.configure(app_id, request.get_json().get("config")))

    def access_response(app_id):
        allowed, hint = owner_access()
        return jsonify(access=host.permission_snapshot(app_id), owner_can_decide=allowed, owner_hint=hint)

    @routes.get("/api/apps/<app_id>/permissions")
    def permissions(app_id):
        return access_response(app_id)

    @routes.patch("/api/apps/<app_id>/permissions")
    def decide_permissions(app_id):
        require_owner()
        host.set_permissions(app_id, request.get_json().get("decisions"))
        return access_response(app_id)

    @routes.post("/api/apps/<app_id>/enabled")
    def enabled(app_id):
        require_owner()
        return jsonify(host.set_enabled(app_id, request.get_json().get("enabled")))

    @routes.delete("/api/apps/<app_id>")
    def remove(app_id):
        require_owner()
        return jsonify(host.remove(app_id))

    @routes.post("/api/apps/<app_id>/tools/<tool>")
    def call(app_id, tool):
        runtime.controller()  # Refuse calls before startup and after shutdown.
        payload = request.get_json()
        return jsonify(job=host.submit(app_id, tool, payload.get("arguments", {}), source=getattr(g, "robbi_tool_source", "ui_text"))), 202

    @routes.get("/api/apps/<app_id>/history")
    def history(app_id):
        require_owner()
        host.project(app_id)
        return jsonify(entries=host.history(app_id, source=getattr(g, "robbi_tool_source", "ui_text")))

    @routes.get("/api/app-jobs/<job_id>/inputs")
    def inputs(job_id):
        require_owner()
        invocation = host.invocation(job_id, source=getattr(g, "robbi_tool_source", "ui_text"))
        return jsonify(app=invocation["app"], tool=invocation["tool"], arguments=invocation["arguments"])

    @routes.post("/api/app-jobs/<job_id>/undo")
    def undo(job_id):
        require_owner()
        runtime.controller()
        source = getattr(g, "robbi_tool_source", "ui_text")
        host.invocation(job_id, source=source)
        if request.get_json() != {}:
            raise ValidationError("Undo does not accept replacement arguments")
        return jsonify(job=host.submit_undo(job_id, source=source)), 202

    @routes.get("/api/app-jobs/<job_id>")
    def job(job_id):
        host.invocation(job_id, source=getattr(g, "robbi_tool_source", "ui_text"))
        return jsonify(job=host.job(job_id))

    @routes.post("/api/app-jobs/<job_id>/cancel")
    def cancel(job_id):
        host.invocation(job_id, source=getattr(g, "robbi_tool_source", "ui_text"))
        return jsonify(job=host.cancel(job_id))

    @routes.get("/api/apps/<app_id>/artifacts")
    def artifacts(app_id):
        return jsonify(artifacts=host.artifacts(app_id).list())

    @routes.get("/api/apps/<app_id>/artifacts/<artifact_id>")
    def artifact(app_id, artifact_id):
        response = send_file(host.artifacts(app_id).path(artifact_id), mimetype="image/png", max_age=3600)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @routes.get("/api/apps/<app_id>/page")
    def page(app_id):
        project = host.project(app_id)
        if not host.enabled(app_id):
            raise ValidationError("Enable the app before opening its custom page")
        relative = project.manifest["dashboard"].get("page")
        if not relative:
            raise ValidationError("This app uses the generated dashboard")
        path = confined(project.root, relative)
        if path.stat().st_size > 256000:
            raise ValidationError("Custom page exceeds 256 KB")
        content = path.read_text(encoding="utf-8")
        # Subresource URLs retain their project-relative layout.
        directory = relative.rsplit("/", 1)[0] + "/" if "/" in relative else ""
        base = f'<base href="/api/apps/{html.escape(app_id)}/files/{html.escape(directory)}">'
        content = content.replace("<head>", "<head>" + base, 1) if "<head>" in content else base + content
        response = Response(content, mimetype="text/html")
        response.headers["Content-Security-Policy"] = PAGE_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @routes.get("/api/apps/<app_id>/media/<path:name>")
    def app_sound(app_id, name):
        project = host.project(app_id)
        if not host.enabled(app_id):
            raise ValidationError("App is disabled")
        if name not in {"reveal.wav", "flip.wav"}:
            raise ValidationError("Unknown app sound")
        path = confined(project.root, "sounds/" + name)
        if not path.is_file() or path.stat().st_size > 256 * 1024:
            raise ValidationError("App sound is unavailable")
        try:
            with wave.open(str(path), "rb") as sound:
                if sound.getcomptype() != "NONE" or sound.getnchannels() not in {1, 2} or sound.getsampwidth() != 2 or not 8000 <= sound.getframerate() <= 48000 or not 0 < sound.getnframes() / sound.getframerate() <= 3:
                    raise ValidationError("App sound format is unsupported")
        except (wave.Error, EOFError) as error:
            raise ValidationError("App sound is invalid") from error
        response = send_file(path, mimetype="audio/wav", max_age=3600)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response

    @routes.get("/api/apps/<app_id>/files/<path:relative>")
    def file(app_id, relative):
        project = host.project(app_id)
        if not host.enabled(app_id):
            raise ValidationError("App is disabled")
        path = confined(project.root, relative)
        if path.suffix.lower() not in {".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".woff2"} or not path.is_file():
            raise ValidationError("Only app dashboard assets can be served")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValidationError("App dashboard asset exceeds 2 MiB")
        response = send_file(path)
        response.headers["Content-Security-Policy"] = PAGE_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return routes
