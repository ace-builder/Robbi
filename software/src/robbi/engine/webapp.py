from __future__ import annotations

import threading
import time

from flask import Flask, Response, jsonify, render_template, request, send_file, g
from robbi.errors import BusyError
from robbi.orchestration import Intent, MANUAL, intent_scope

from robbi.configuration.settings import settings, TEMPLATES_DIR
from .controller import RobotController


def create_app(controller: RobotController | None) -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
    camera_stream_slot = threading.BoundedSemaphore(1)
    app.config["TEMPLATES_AUTO_RELOAD"] = settings.hot_reload

    @app.before_request
    def manual_output_intent():
        if request.method == "POST" and request.path in {"/drive", "/drive_raw", "/face", "/sound", "/video", "/animation"}:
            token = controller._action_token() if controller is not None and hasattr(controller, "_action_token") else None
            g.output_scope = intent_scope(Intent("manual:" + request.path.lstrip('/'), MANUAL,
                                                cancelled=token[1] if token else None))
            g.output_scope.__enter__()

    @app.teardown_request
    def clear_output_intent(error):
        output_scope = getattr(g, "output_scope", None)
        if output_scope is not None:
            output_scope.__exit__(None, None, None)

    def controller_required() -> RobotController:
        if controller is None:
            raise RuntimeError("controller unavailable during reloader bootstrap")
        return controller

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/app")
    def mobile_app():
        return render_template("app.html")

    @app.get("/onboard")
    def onboard_page():
        return render_template("onboard.html")

    @app.get("/live")
    def live():
        return render_template("live.html")

    @app.get("/mic")
    def mic():
        return render_template("mic.html")

    @app.get("/wake")
    def wake():
        return render_template("wake.html")

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html")

    @app.get("/memory")
    def memory_page():
        return render_template("memory.html")

    @app.get("/today")
    def today_page():
        return render_template("today.html")

    @app.get("/map")
    def map_page():
        return render_template("map.html")

    @app.get("/imu")
    def imu_page():
        return render_template("imu.html")

    @app.get("/imu/data")
    def imu_data():
        try:
            data = controller_required().imu.status()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/imu/calibrate")
    def imu_calibrate():
        try:
            return jsonify(controller_required().imu.calibrate())
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.get("/status")
    def status():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        refresh_assets = str(request.args.get("refresh_assets") or "").strip().lower() in {"1", "true", "yes", "on"}
        return jsonify(active_controller.status(refresh_assets=refresh_assets))

    @app.get("/settings_data")
    def settings_data():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.settings_snapshot())

    @app.post("/settings_data")
    def settings_save():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        try:
            result = active_controller.apply_settings(payload)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @app.post("/shutdown")
    def shutdown():
        return jsonify({"error": "System controls require the authenticated v2 runtime."}), 403

    @app.post("/restart_service")
    def restart_service():
        return jsonify({"error": "System controls require the authenticated v2 runtime."}), 403

    @app.get("/apps")
    def apps():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.apps_snapshot())

    @app.post("/apps/install")
    def apps_install():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "missing app zip"}), 400
        try:
            result = active_controller.install_app_zip(upload.filename, upload.read())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @app.delete("/apps/<path:app_id>")
    def apps_remove(app_id: str):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        try:
            result = active_controller.remove_app(app_id)
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, **result})

    @app.post("/apps/run")
    def apps_run():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        app_id = str(payload.get("id") or "").strip()
        if not app_id:
            return jsonify({"error": "missing app id"}), 400
        user_input = str(payload.get("input") or "").strip()
        action = active_controller.run_app(app_id, user_input=user_input)
        return jsonify({"ok": True, "action": action, "status": active_controller.status()})

    @app.get("/memory/search")
    def memory_search():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        query = str(request.args.get("q") or "").strip()
        if not query:
            return jsonify({"error": "missing q"}), 400
        limit = request.args.get("limit", default=10, type=int)
        return jsonify(active_controller.memory_search(query, limit=limit))

    @app.get("/memory_data")
    def memory_data():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        if str(request.args.get("refresh") or "").strip().lower() in {"1", "true", "yes", "on"}:
            active_controller.memory.export_markdown()
        include_pending = str(request.args.get("include_pending") or "").strip().lower() in {"1", "true", "yes", "on"}
        return jsonify(active_controller.memory.grouped_entries(limit=None, include_pending=include_pending))

    @app.get("/today_data")
    def today_data():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.today_state())

    @app.post("/today/brief")
    def today_brief():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        action = active_controller.today_brief(source="ui")
        executable = active_controller._action_for_source_execution(action, "ui_text")
        if executable:
            active_controller.execute_action(executable)
        return jsonify({"ok": True, "action": action, "state": active_controller.today_state()})

    @app.get("/productivity_data")
    def productivity_data():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.productivity_data())

    @app.get("/notes")
    def notes_list():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"notes": active_controller.productivity.list_notes(limit=100)})

    @app.post("/notes")
    def notes_create():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        try:
            note = active_controller.create_note(request.get_json(silent=True) or {})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "note": note})

    @app.patch("/notes/<int:note_id>")
    def notes_update(note_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        note = active_controller.update_note(note_id, request.get_json(silent=True) or {})
        return jsonify({"ok": bool(note), "note": note})

    @app.delete("/notes/<int:note_id>")
    def notes_delete(note_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": active_controller.delete_note(note_id)})

    @app.get("/lists")
    def lists_data():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        list_name = request.args.get("list_name")
        return jsonify({"items": active_controller.productivity.list_items(list_name, include_done=True)})

    @app.post("/lists/items")
    def lists_add_item():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        try:
            item = active_controller.add_list_item(request.get_json(silent=True) or {})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "item": item})

    @app.patch("/lists/items/<int:item_id>")
    def lists_update_item(item_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        item = active_controller.update_list_item(item_id, request.get_json(silent=True) or {})
        return jsonify({"ok": bool(item), "item": item})

    @app.delete("/lists/items/<int:item_id>")
    def lists_delete_item(item_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": active_controller.delete_list_item(item_id)})

    @app.get("/research_notes")
    def research_notes_list():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"research_notes": active_controller.productivity.list_research_notes(limit=100)})

    @app.post("/research_notes")
    def research_notes_create():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        try:
            note = active_controller.create_research_note(request.get_json(silent=True) or {})
        except BusyError:
            raise
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "research_note": note})

    @app.get("/jobs")
    def jobs_list():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"jobs": active_controller.productivity.list_jobs(limit=100)})

    @app.get("/onboarding_data")
    def onboarding_data():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.onboarding_data())

    @app.post("/onboarding_data")
    def onboarding_save():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": True, "draft": active_controller.onboarding_save(request.get_json(silent=True) or {})})

    @app.post("/onboarding_apply")
    def onboarding_apply():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": True, **active_controller.onboarding_apply(request.get_json(silent=True) or {})})

    @app.post("/onboarding_reset")
    def onboarding_reset():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": True, "draft": active_controller.onboarding_reset()})

    @app.get("/kids/status")
    def kids_status():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.kids_status())

    @app.post("/kids/start")
    def kids_start():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.kids_start(request.get_json(silent=True) or {}))

    @app.post("/kids/input")
    def kids_input():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.kids_input(request.get_json(silent=True) or {}))

    @app.post("/kids/stop")
    def kids_stop():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.kids_stop())

    @app.post("/memory/<int:entry_id>/approve")
    def memory_approve(entry_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        entry = active_controller.memory.approve(entry_id)
        return jsonify({"ok": bool(entry), "entry": entry})

    @app.post("/memory/<int:entry_id>/reject")
    def memory_reject(entry_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": active_controller.memory.reject(entry_id)})

    @app.delete("/memory/<int:entry_id>")
    def memory_delete(entry_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"ok": active_controller.memory.delete(entry_id)})

    @app.post("/memory/<int:entry_id>")
    def memory_update(entry_id: int):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        entry = active_controller.memory.update_entry(entry_id, payload)
        return jsonify({"ok": bool(entry), "entry": entry})

    @app.get("/photo")
    def photo():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        path = active_controller.capture_photo()
        if path is None:
            return jsonify({"error": "camera unavailable"}), 503
        response = send_file(str(path), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/video_feed")
    def video_feed():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        if not active_controller.camera.health().get('available'):
            return jsonify({'error': 'Camera is unavailable or its frame is stale'}), 503
        if not camera_stream_slot.acquire(blocking=False):
            return jsonify({'error': 'A camera stream is already open; close it before opening another'}), 429
        release_lock = threading.Lock()
        released = False
        def release():
            nonlocal released
            with release_lock:
                if not released:
                    camera_stream_slot.release()
                    released = True
        def generate():
            missing_since = None
            interval = max(0.1, 1.0 / max(1, float(settings.camera_fps)))
            try:
                while True:
                    frame = active_controller.camera.get_jpeg_frame()
                    if frame is None:
                        missing_since = missing_since or time.monotonic()
                        if time.monotonic() - missing_since >= 5:
                            return
                    else:
                        missing_since = None
                        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                    time.sleep(interval)
            finally:
                release()
        response = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
        response.call_on_close(release)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post("/text")
    def text():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        q = (payload.get("q") or "").strip()
        if not q:
            return jsonify({"error": "empty"}), 400
        include_photo = payload.get("include_photo")
        if include_photo is not None:
            include_photo = bool(include_photo)
        action = active_controller.think_and_respond(q, include_photo=include_photo, source="ui_text")
        return jsonify({"ok": True, "action": action, "status": active_controller.status()})

    @app.post("/mute")
    def mute():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        muted = bool(payload.get("muted", True))
        muted = active_controller.set_muted(muted)
        return jsonify({"ok": True, "muted": muted})

    @app.post("/volume")
    def volume():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        volume_percent = active_controller.set_volume(int(payload.get("volume", 100)))
        return jsonify({"ok": True, "volume_percent": volume_percent})

    @app.post("/filming_mode")
    def filming_mode():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        enabled = active_controller.set_filming_mode(bool(payload.get("enabled", True)))
        return jsonify({"ok": True, "enabled": enabled})

    @app.post("/wake_word")
    def wake_word():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        wake_enabled = active_controller.set_wake_enabled(bool(payload.get("enabled", True)))
        return jsonify({"ok": True, "wake_enabled": wake_enabled, "status": active_controller.status()})

    @app.post("/drive")
    def drive():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        active_controller.execute_action({"drive": payload})
        return jsonify({"ok": True})

    @app.post("/drive_raw")
    def drive_raw():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        try:
            left = float(payload.get("left", 0))
            right = float(payload.get("right", 0))
            duration = float(payload.get("duration", 0.35))
        except (TypeError, ValueError):
            return jsonify({"error": "left, right, and duration must be numeric"}), 400
        active_controller.motion.raw_wheel_drive(left, right, duration)
        return jsonify({"ok": True, "mode": "raw", "left": left, "right": right, "duration": duration})

    @app.post("/slam/start_scan")
    def slam_start_scan():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_start_scan())

    @app.post("/slam/start_explore")
    def slam_start_explore():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Expected an object"}), 400
        max_steps = payload.get("max_steps")
        try:
            if isinstance(max_steps, bool) or (isinstance(max_steps, float) and not max_steps.is_integer()):
                raise ValueError("Invalid step count")
            max_steps = int(max_steps) if max_steps not in (None, "") else None
            if max_steps is not None and not 0 <= max_steps <= 100:
                raise ValueError("Invalid step count")
        except (TypeError, ValueError, OverflowError):
            return jsonify({"ok": False, "error": "max_steps must be 0 (until stopped) or 1-100"}), 400
        return jsonify(active_controller.slam_start_explore(max_steps=max_steps))

    @app.post("/slam/localization")
    def slam_localization():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        return jsonify(active_controller.slam_set_localization(bool(payload.get("enabled", True))))

    @app.post("/slam/stop")
    def slam_stop():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_stop())

    @app.post("/slam/reset")
    def slam_reset():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_reset())

    @app.post("/slam/save")
    def slam_save():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_save())

    @app.get("/slam/status")
    def slam_status():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_status())

    @app.get("/slam/map")
    def slam_map():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_map())

    @app.get("/slam/sessions")
    def slam_sessions():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.slam_sessions())

    @app.post("/face")
    def face():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        active_controller.execute_action({"face": payload.get("name", "")})
        return jsonify({"ok": True})

    @app.post("/sound")
    def sound():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        active_controller.execute_action({"sound": payload.get("name", "")})
        return jsonify({"ok": True})

    @app.post("/animation")
    def animation():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        active_controller.execute_action({"animation": payload.get("name", "")})
        return jsonify({"ok": True})

    @app.post("/video")
    def video():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        active_controller.execute_action({"video": payload.get("name", "")})
        return jsonify({"ok": True})

    @app.post("/filming_sequence")
    def filming_sequence():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        active_controller.execute_action({"filming_sequence": payload.get("name", "")})
        return jsonify({"ok": True})

    @app.get("/mic_level")
    def mic_level():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.microphone.level())

    @app.get("/mic_record.wav")
    def mic_record():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        duration = request.args.get("duration", default=3.0, type=float)
        wav_bytes = active_controller.microphone.record_wav_bytes(duration)
        if not wav_bytes:
            return jsonify({"error": "microphone capture failed"}), 503
        return Response(wav_bytes, mimetype="audio/wav")

    @app.get("/wake_refs")
    def wake_refs():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify(active_controller.wake_reference_status())

    @app.post("/wake_refs/record")
    def wake_refs_record():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        duration = payload.get("duration", settings.voice_reference_record_s)
        sample_name = (payload.get("sample_name") or "").strip() or None
        category = (payload.get("category") or "positive").strip().lower()
        prep_delay_s = payload.get("prep_delay_s", 0.0)
        try:
            result = active_controller.record_wake_reference(
                duration_s=float(duration),
                sample_name=sample_name,
                category=category,
                prep_delay_s=float(prep_delay_s),
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @app.post("/wake_refs/clear")
    def wake_refs_clear():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        category = (payload.get("category") or "positive").strip().lower()
        return jsonify({"ok": True, **active_controller.clear_wake_references(category=category)})

    @app.delete("/wake_refs/<category>/<path:name>")
    def wake_ref_delete(category: str, name: str):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        try:
            return jsonify({"ok": True, **active_controller.delete_wake_reference(category, name)})
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404

    @app.get("/wake_refs/<category>/<path:name>")
    def wake_ref_file(category: str, name: str):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        path = active_controller.wake_reference_file(category, name)
        if path is None:
            return jsonify({"error": "not found"}), 404
        return send_file(str(path), mimetype="audio/wav")

    @app.get("/wake_export.zip")
    def wake_export():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        path = active_controller.export_wake_dataset_zip()
        return send_file(str(path), mimetype="application/zip", as_attachment=True, download_name=path.name)

    @app.post("/wake_model")
    def wake_model_upload():
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "missing .onnx upload"}), 400
        try:
            result = active_controller.upload_wake_model(upload.filename, upload.read())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @app.delete("/wake_model/<path:name>")
    def wake_model_delete(name: str):
        try:
            active_controller = controller_required()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        try:
            result = active_controller.delete_wake_model(name)
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, **result})

    return app
