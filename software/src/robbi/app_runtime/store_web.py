"""Owner dashboard access to the external store and bounded lifecycle jobs."""
from __future__ import annotations

import io
import threading
import time

from flask import Blueprint, current_app, g, jsonify, request, send_file

from robbi.errors import BusyError, ValidationError
from .manifest import confined
from .owner import require_owner


def blueprint(runtime):
    routes = Blueprint("app_store_client", __name__)
    manager, host = runtime.app_manager, runtime.app_host
    reads = threading.BoundedSemaphore(2)

    def payload(allowed):
        value = request.get_json()
        if not isinstance(value, dict) or set(value) - set(allowed):
            raise ValidationError("Invalid Robbi App Hub request fields")
        return value

    def remote(callback):
        # Share admission with provider/legacy HTTP work, preserving HTTP worker
        # capacity for Stop and diagnostics when a store is slow or offline.
        slots = current_app.config.get("ROBBI_REMOTE_SLOTS", reads)
        if not slots.acquire(blocking=False):
            raise BusyError("The Robbi App Hub is handling another request; try again shortly")
        try:
            return callback()
        finally:
            slots.release()

    @routes.get("/api/app-store")
    def status():
        require_owner()
        return jsonify(**manager.status(), hub_url=runtime.config.values.get("app_hub", {}).get("url", "https://ai-tier.com/apphub"))

    @routes.post("/api/app-store/probe")
    def probe():
        require_owner()
        values = payload({"url", "allow_http"})
        return jsonify(remote(lambda: manager.store.probe(values.get("url"), allow_http=values.get("allow_http", False), deadline=time.monotonic()+15)))

    @routes.post("/api/app-store/connect")
    def connect():
        require_owner()
        values = payload({"url", "public_key", "allow_http"})
        return jsonify(remote(lambda: manager.connect_store(values.get("url"), values.get("public_key"), allow_http=values.get("allow_http", False), deadline=time.monotonic()+15)))

    @routes.delete("/api/app-store")
    def disconnect():
        require_owner()
        return jsonify(manager.disconnect_store())

    @routes.get("/api/app-store/catalog")
    def catalog():
        require_owner()
        query = request.args.get("q", "")
        if len(query) > 200:
            raise ValidationError("Search is limited to 200 characters")
        return jsonify(remote(lambda: manager.browse(query=query, refresh=request.args.get("refresh") == "1", deadline=time.monotonic()+15)))

    @routes.get("/api/app-store/apps/<app_id>")
    def detail(app_id):
        require_owner()
        return jsonify(app=remote(lambda: manager.store.app(app_id, version=request.args.get("version"), deadline=time.monotonic()+15)))

    @routes.get("/api/app-store/apps/<app_id>/images/<kind>")
    def store_image(app_id, kind):
        require_owner()
        data = remote(lambda: manager.store.image(app_id, kind, deadline=time.monotonic()+15))
        response = send_file(io.BytesIO(data), mimetype="image/png", max_age=300)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response

    @routes.get("/api/apps/<app_id>/images/<kind>")
    def installed_image(app_id, kind):
        project = host.project(app_id)
        if kind not in {"screen", "icon"} or kind not in project.manifest.get("images", {}):
            raise ValidationError("App image is unavailable")
        path = confined(project.root, project.manifest["images"][kind])
        response = send_file(path, max_age=300)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response

    @routes.post("/api/app-management")
    def manage():
        require_owner()
        spec = payload({"operation", "app_id", "query", "version"})
        source = getattr(g, "robbi_tool_source", "ui_text")
        generation = runtime._generation
        deadline = time.monotonic()+90
        signal = lambda: runtime._stop.is_set() or runtime._generation != generation
        job = runtime.app_management_jobs.submit("app_manager", lambda: manager.execute(spec, source=source, cancelled=signal, deadline=deadline))
        return jsonify(job=job), 202

    @routes.get("/api/app-management/<job_id>")
    def job(job_id):
        require_owner()
        return jsonify(job=runtime.app_management_jobs.get(job_id))

    @routes.post("/api/apps/preview")
    def preview():
        require_owner()
        uploaded = request.files.get("file") or request.files.get("archive")
        if uploaded is None:
            raise ValidationError("Choose an app project ZIP")
        return jsonify(host.packages.preview(uploaded.stream.read(host.packages.MAX_BYTES+1)))

    @routes.post("/api/apps/<app_id>/restore")
    def restore(app_id):
        require_owner()
        if payload(set()) != {}:
            raise ValidationError("Restore does not accept replacement settings")
        return jsonify(host.restore(app_id, enable=True))

    return routes
