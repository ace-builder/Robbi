"""Thin HTTP boundary with immediate diagnostics and preserved feature routes."""
from __future__ import annotations

import hmac
import io
import logging
import math
import os
import re
import threading
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .config import merge, parse_yaml, read_defaults, validate
from .errors import BusyError, UnavailableError, ValidationError
from .orchestration import ResourceBusy

LOG = logging.getLogger(__name__)
PAGE_PATHS = {"", "app", "onboard", "live", "mic", "wake", "settings", "memory", "today", "map", "imu"}


def install_error_handlers(app):
    @app.errorhandler(Exception)
    def error_response(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, ResourceBusy):
            return jsonify(error=str(error), **error.details), 429
        if isinstance(error, BusyError):
            return jsonify(error=str(error)), 429
        if isinstance(error, (ValidationError, ValueError, TypeError, KeyError)):
            return jsonify(error=str(error)[:1000]), 400
        if isinstance(error, (UnavailableError, RuntimeError, ImportError, OSError)):
            return jsonify(error=str(error)[:1000]), 503
        LOG.exception("Request failed")
        return jsonify(error="This operation failed; see Diagnostics for component status"), 500


def create_app(runtime):
    from .services.host_management import HostManagement
    host_controls = HostManagement(runtime)
    web_root = runtime.config.root / "web"
    app = Flask(__name__, static_folder=str(web_root), static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024 * 1024, JSON_SORT_KEYS=False)
    app.config["ROBBI_RUNTIME_CONFIG"] = runtime.config
    config_write_lock = threading.RLock()
    app.config["ROBBI_CONFIG_LOCK"] = config_write_lock
    # Compatibility endpoints can perform cloud/recording/file work. At most
    # two may occupy HTTP workers; diagnostics and Stop bypass these slots.
    http_workers = int(runtime.config.values.get("runtime", {}).get("worker_threads", 4))
    feature_slots = threading.BoundedSemaphore(max(1, min(2, http_workers - 1)))
    app.config["ROBBI_REMOTE_SLOTS"] = feature_slots
    install_error_handlers(app)
    from .media_library import blueprint as media_library_blueprint
    app.register_blueprint(media_library_blueprint(runtime))
    from .connections import blueprint as connections_blueprint
    app.register_blueprint(connections_blueprint(runtime))
    from .live_awareness.web import blueprint as live_awareness_blueprint
    app.register_blueprint(live_awareness_blueprint(runtime))
    from .conversation_lab.web import blueprint as conversation_lab_blueprint
    app.register_blueprint(conversation_lab_blueprint(runtime))
    from .audio.wake_training_web import blueprint as wake_training_blueprint
    app.register_blueprint(wake_training_blueprint(runtime))
    from .app_runtime.web import blueprint as apps_blueprint
    app.register_blueprint(apps_blueprint(runtime))
    from .app_runtime.store_web import blueprint as store_blueprint
    app.register_blueprint(store_blueprint(runtime))
    from .audio_studio.web import blueprint as audio_studio_blueprint
    app.register_blueprint(audio_studio_blueprint(runtime.audio_studio))
    from .motion_studio_web import blueprint as motion_studio_blueprint
    app.register_blueprint(motion_studio_blueprint(runtime.config.root, runtime.config.data_dir, runtime))
    from .imu_view import blueprint as imu_view_blueprint
    app.register_blueprint(imu_view_blueprint(runtime))
    from .manual_motion import blueprint as manual_motion_blueprint
    app.register_blueprint(manual_motion_blueprint(runtime))
    from .video_capture import blueprint as video_capture_blueprint
    app.register_blueprint(video_capture_blueprint(runtime))
    from .navigation.web import blueprint as navigation_blueprint
    app.register_blueprint(navigation_blueprint(runtime))

    @app.before_request
    def boundary():
        from .dashboard_security import allowed_host
        if not allowed_host(request.host, runtime.config.values.get('server', {}).get('allowed_hosts', [])):
            return jsonify(error='Unrecognised dashboard hostname; use the Pi IP address or configure server.allowed_hosts'), 403
        token = os.getenv("ROBBI_WEB_TOKEN", "")
        if runtime.config.values.get("server", {}).get("auth_enabled", False):
            if not token:
                return jsonify(error="Dashboard authentication is enabled but its password is missing; configure ROBBI_WEB_TOKEN and restart Robbi"), 503
            auth = request.authorization
            supplied = auth.password if auth and auth.type == "basic" else request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
                return Response("Robbi login required", 401, {"WWW-Authenticate": 'Basic realm="Robbi", charset="UTF-8"'})
            g.robbi_owner_authenticated = True
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            origin = request.headers.get("Origin")
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and urlsplit(origin).netloc != request.host):
                return jsonify(error="Cross-origin control requests are not allowed"), 403
            if request.is_json:
                payload = request.get_json()
                if not isinstance(payload, dict):
                    raise ValidationError("Request body must be a JSON object")
                def inspect(value, depth=0):
                    if depth > 12:
                        raise ValidationError("Request nesting exceeds limit")
                    if isinstance(value, dict):
                        for child in value.values():
                            inspect(child, depth + 1)
                    elif isinstance(value, list):
                        if len(value) > 1500:
                            raise ValidationError("Request list exceeds limit")
                        for child in value:
                            inspect(child, depth + 1)
                    elif isinstance(value, float) and not math.isfinite(value):
                        raise ValidationError("Numbers must be finite")
                inspect(payload)
            elif request.content_length and not (request.content_type or "").startswith("multipart/form-data"):
                raise ValidationError("Use application/json or a file upload")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'")
        if request.path in ("/api/media-library/thumbnail", "/api/media-library/file"):
            response.headers["Cache-Control"] = "private, max-age=60"
        if not request.path.startswith(("/static/", "/api/media-library/thumbnail", "/api/media-library/file")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def conversation_scope():
        from .services.productivity_undo import change_scope
        from .services.tool_followups import scope_source
        identifier = request.headers.get("X-Robbi-Conversation", "")
        if identifier and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ValidationError("Invalid conversation identifier")
        g.robbi_conversation_id = identifier
        g.robbi_tool_source = scope_source("ui_text", identifier)
        g.robbi_change_scope = change_scope(g.robbi_tool_source)
        g.robbi_change_scope.__enter__()

    @app.teardown_request
    def close_conversation_scope(error):
        context = getattr(g, "robbi_change_scope", None)
        if context is not None:
            g.robbi_change_scope = None
            context.__exit__(None, None, None)

    @app.route("/api/morning-briefing", methods=["GET", "PATCH"])
    def morning_briefing():
        from .app_runtime.owner import require_owner
        require_owner()
        if request.method == "PATCH":
            return jsonify(runtime.morning_briefing.configure(request.get_json()))
        return jsonify(runtime.morning_briefing.snapshot())

    @app.post("/api/morning-briefing/run")
    def run_morning_briefing():
        from .app_runtime.owner import require_owner
        require_owner()
        payload = request.get_json() or {}
        if set(payload) - {"preview"}:
            raise ValidationError("Only preview is accepted")
        return jsonify(job=runtime.submit_briefing(preview=payload.get("preview", False))), 202

    @app.get("/api/morning-briefing/jobs/<job_id>")
    def morning_briefing_job(job_id):
        from .app_runtime.owner import require_owner
        require_owner()
        try:
            return jsonify(job=runtime.briefing_jobs.get(job_id))
        except KeyError:
            return jsonify(error="Briefing job is unknown or expired"), 404

    @app.get("/api/tool-history")
    def tool_history():
        from .app_runtime.owner import require_owner
        require_owner()
        entries = [{**item, "kind": "app"} for item in runtime.app_host.history(source=g.robbi_tool_source)]
        if runtime._ready.is_set():
            service = getattr(runtime.controller(), "productivity_service", None)
            if service is not None:
                entries += service.history(g.robbi_tool_source)
        return jsonify(entries=sorted(entries, key=lambda item: item["created_at"], reverse=True)[:64])

    @app.post("/api/tool-history/<change_id>/undo")
    def undo_saved_change(change_id):
        from .app_runtime.owner import require_owner
        require_owner()
        controller = runtime.controller()
        if request.get_json() != {"kind": "productivity"}:
            raise ValidationError("Select a saved productivity change")
        source = g.robbi_tool_source
        generation = runtime._generation
        def undo():
            if generation != runtime._generation or runtime._stop.is_set():
                return {"ok": False, "cancelled": True, "text": "Undo was cancelled."}
            return controller.productivity_service.undo(change_id, source=source)
        return jsonify(job=runtime.jobs.submit("undo_productivity", undo)), 202

    @app.get("/api/health")
    def health():
        return jsonify(runtime.health_snapshot())

    @app.get("/api/extensions")
    def extensions():
        return jsonify(apps=runtime.plugins.catalog())

    @app.get("/healthz")
    def healthz():
        return jsonify(alive=True)

    @app.get("/api/diagnostics")
    def diagnostics():
        return jsonify(runtime.diagnostics_metrics())

    @app.get('/api/runtime-extension')
    def runtime_extension():
        sink = runtime.recording
        ui = getattr(sink, 'public_ui', None)
        try:
            return jsonify(ui() if callable(ui) else {})
        except Exception:
            LOG.warning('Optional dashboard extension unavailable')
            return jsonify({})

    register_extension = getattr(runtime.recording, 'register_web', None)
    if callable(register_extension):
        try:
            register_extension(app, runtime, feature_slots)
        except Exception:
            LOG.warning('Optional extension routes unavailable')

    @app.get("/status")
    def status():
        if str(request.args.get("refresh_assets") or "").strip().lower() in {"1", "true", "yes", "on"}:
            if not feature_slots.acquire(blocking=False):
                raise BusyError("Feature request capacity is full; Stop and Diagnostics remain available")
            try:
                return jsonify(runtime.refresh_assets())
            finally:
                feature_slots.release()
        return jsonify(runtime.snapshot())

    @app.post("/api/stop")
    def stop():
        return jsonify(runtime.emergency_stop())

    @app.post("/api/jobs")
    def submit():
        payload = request.get_json()
        arguments = payload.get("payload", {})
        if payload.get("operation") == "text" and isinstance(arguments, dict) and g.robbi_conversation_id:
            arguments = {**arguments, "conversation_id": g.robbi_conversation_id}
        return jsonify(runtime.submit(payload.get("operation"), arguments)), 202

    @app.get("/api/jobs/<job_id>")
    def job(job_id):
        try:
            return jsonify(runtime.jobs.get(job_id))
        except KeyError:
            return jsonify(error="Job not found or history expired"), 404

    @app.get("/api/conversation")
    def conversation_feed():
        controller = runtime.controller()
        background = getattr(controller, "background_work", None)
        return jsonify(controller.conversation_feed.snapshot(
            g.robbi_conversation_id, read_task=getattr(background, "get", None)))

    @app.post("/api/components/<name>/retry")
    def retry(name):
        return jsonify(ok=runtime.controller().retry_component(name))

    @app.get("/api/background-work")
    def background_work():
        return jsonify(runtime.controller().background_work_snapshot())

    @app.get("/api/background-work/<task_id>")
    def background_task(task_id):
        try:
            return jsonify(runtime.controller().background_work.get(task_id))
        except KeyError:
            return jsonify(error="Task not found or history expired"), 404

    @app.post("/api/background-work/<task_id>/cancel")
    def cancel_background_task(task_id):
        try:
            return jsonify(runtime.controller().cancel_background_work(task_id))
        except KeyError:
            return jsonify(error="Task not found or history expired"), 404

    @app.post("/api/background-work/<task_id>/show")
    def show_background_result(task_id):
        payload = request.get_json() or {}
        if set(payload) - {"artifact_id"}:
            raise ValidationError("Only artifact_id is accepted")
        artifact_id = payload.get("artifact_id")
        if artifact_id is not None and (not isinstance(artifact_id, str) or len(artifact_id) > 120):
            raise ValidationError("artifact_id must be a saved artifact identifier")
        try:
            return jsonify(runtime.controller().show_background_result(task_id, artifact_id))
        except KeyError:
            return jsonify(error="Task not found or history expired"), 404

    @app.get("/api/autonomy")
    def autonomy_status():
        from .app_runtime.owner import owner_access
        allowed, hint = owner_access()
        return jsonify(**runtime.controller().autonomy_status(), owner_can_resume=allowed, owner_hint=hint)

    @app.post("/api/autonomy/pause")
    def autonomy_pause():
        return jsonify(runtime.controller().autonomy_pause())

    @app.post("/api/autonomy/resume")
    def autonomy_resume():
        from .app_runtime.owner import require_owner
        require_owner()
        return jsonify(runtime.controller().autonomy_resume())

    @app.route("/api/config", methods=["GET", "POST"])
    def config():
        if request.method == "POST":
            # The complete profile includes the app-permission bypass switch.
            from .app_runtime.owner import require_owner
            require_owner()
            text = request.get_json().get("yaml")
            if not isinstance(text, str):
                raise ValidationError("yaml must be text")
            defaults, _ = read_defaults(runtime.config.root)
            values = validate(merge(defaults, parse_yaml(text)))
            if values.get("server", {}).get("auth_enabled", False) and not os.getenv("ROBBI_WEB_TOKEN"):
                raise ValidationError("Set ROBBI_WEB_TOKEN in .env.secrets and restart Robbi before enabling dashboard sign-in")
            with config_write_lock:
                runtime.config.save(text)
            return jsonify(ok=True, restart_required=True)
        source = runtime.config.source
        return jsonify(yaml=source.read_text(encoding="utf-8") if source.exists() else "# Overrides merged over config/desktop.yaml\n",
                       issues=runtime.config.issues, path=str(source))

    @app.route("/api/dashboard-access", methods=["GET", "POST"])
    def dashboard_access():
        from .app_runtime.owner import require_owner
        require_owner()
        if request.method == "POST":
            payload = request.get_json()
            if set(payload) - {"auth_enabled", "password"} or not isinstance(payload.get("auth_enabled"), bool):
                raise ValidationError("Provide auth_enabled and an optional password")
            enabled = payload["auth_enabled"]
            password = payload.get('password', '')
            if not isinstance(password, str):
                raise ValidationError('Password must be text')
            if password:
                from .dashboard_security import password_value
                password_value(password)
            if enabled and not (password or os.getenv("ROBBI_WEB_TOKEN")):
                raise ValidationError("Enter a dashboard password to enable sign-in")
            # Preserve unrelated saved overrides. Publish the live switch only
            # after the atomic write succeeds, so a failed save cannot lock out
            # the owner or disagree with the next boot.
            import yaml
            with config_write_lock:
                if password:
                    from .setup.configuration import Configuration
                    Configuration(runtime.config.root).save_key('ROBBI_WEB_TOKEN', password)
                    os.environ['ROBBI_WEB_TOKEN'] = password
                source = runtime.config.source
                saved = parse_yaml(source.read_text(encoding="utf-8")) if source.exists() else {}
                overlay = {"server": {"auth_enabled": enabled}}
                runtime.config.save(yaml.safe_dump(merge(saved, overlay), sort_keys=False))
                runtime.config.values = merge(runtime.config.values, overlay)
        return jsonify(auth_enabled=runtime.config.values.get("server", {}).get("auth_enabled", False),
                       password_configured=bool(os.getenv("ROBBI_WEB_TOKEN")), restart_required=False)

    @app.post("/settings_data")
    def settings_update():
        payload = request.get_json()
        if "apps" in payload:
            raise ValidationError("Change app permission enforcement in the Full configuration file and restart Robbi")
        if "server" in payload:
            raise ValidationError("Use Dashboard access for sign-in or the Full configuration file for other server settings")
        with config_write_lock:
            validate(merge(runtime.config.values, payload))
            result = runtime.controller().apply_settings(payload)
            runtime.config.values = merge(runtime.config.values, payload)
        return jsonify(result)

    @app.get("/api/routes")
    def routes():
        apps = [app] + ([runtime._legacy_app] if runtime._legacy_app else [])
        seen = set()
        result = []
        for source in apps:
            for rule in source.url_map.iter_rules():
                for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                    key = (rule.rule, method)
                    if key not in seen:
                        result.append({"path": rule.rule, "method": method})
                        seen.add(key)
        return jsonify(routes=result)

    @app.post("/restart_service")
    @app.post("/shutdown")
    def host_management():
        if not runtime.config.values.get("runtime", {}).get("allow_host_management"):
            return jsonify(error="Enable power controls in Settings → Service & power first."), 403
        return jsonify(host_controls.request('restart' if request.path == '/restart_service' else 'shutdown')), 202

    @app.route('/api/host-management', methods=['GET', 'PATCH'])
    def host_management_settings():
        if request.method == 'PATCH':
            enabled = request.get_json().get('enabled')
            if type(enabled) is not bool:
                raise ValidationError('enabled must be true or false')
            import yaml
            with config_write_lock:
                source = runtime.config.source
                saved = parse_yaml(source.read_text(encoding='utf-8')) if source.exists() else {}
                overlay = {'runtime': {'allow_host_management': enabled}}
                runtime.config.save(yaml.safe_dump(merge(saved, overlay), sort_keys=False))
                runtime.config.values = merge(runtime.config.values, overlay)
        return jsonify(host_controls.status())

    @app.route("/", defaults={"path": ""}, methods=["GET"])
    @app.route("/<path:path>", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
    def feature(path):
        if request.method == "GET" and path in PAGE_PATHS:
            return send_from_directory(web_root, "index.html")
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            if path == "slam/stop" or (path == "drive" and str(payload.get("mode") or payload.get("direction") or "").lower() == "stop"):
                return jsonify(runtime.emergency_stop())
        if runtime._legacy_app is None:
            raise UnavailableError("Features are starting; diagnostics remain available")
        runtime.controller()  # A constructed controller may still be starting.
        if not feature_slots.acquire(blocking=False):
            raise BusyError("Feature request capacity is full; Stop and Diagnostics remain available")
        # The outer validation boundary may already have parsed JSON. WSGI input
        # is a one-shot stream: replay the bounded cached body for the domain app.
        try:
            body = request.get_data(cache=True)
            environ = dict(request.environ)
            environ["wsgi.input"] = io.BytesIO(body)
            environ["CONTENT_LENGTH"] = str(len(body))
            response = Response.from_app(runtime._legacy_app.wsgi_app, environ)
        except BaseException:
            feature_slots.release()
            raise
        released, release_lock = False, threading.Lock()
        def release_slot():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    feature_slots.release()
        # Keep a slot for the full response lifetime, including MJPEG streams.
        response.call_on_close(release_slot)
        return response

    return app

