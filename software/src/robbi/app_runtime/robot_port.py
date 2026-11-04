"""Only adapter that connects the app SDK to the existing robot composition."""
from __future__ import annotations

from contextlib import contextmanager
import time
from pathlib import Path

from robbi.errors import UnavailableError
from robbi.orchestration import current_intent
from .schema import bounded_json

from .sdk import AppCancelled, health_only_status


class RuntimeRobotPort:
    def __init__(self, runtime):
        self.runtime = runtime

    def capture_token(self):
        controller = self.runtime.controller()
        token = getattr(controller, "_action_token", None)
        return token() if token else None

    def status(self):
        snapshot = self.runtime.snapshot()
        coordinator = getattr(self.runtime.controller(), "orchestrator", None)
        return health_only_status({"health": self.runtime.health_snapshot(),
                "outputs": coordinator.snapshot() if coordinator is not None else {},
                "components": {key: snapshot.get(key, {}) for key in ("camera", "motion", "tof", "imu", "media", "microphone")}})

    @contextmanager
    def _scope(self, token, cancelled):
        if cancelled():
            raise AppCancelled("Robot output cancelled")
        controller = self.runtime.controller()
        if token and not controller._action_current(token[0]):
            raise AppCancelled("Robot output cancelled by Stop")
        local = getattr(controller, "_action_local", None)
        previous = getattr(local, "token", None) if local else None
        if local and token:
            local.token = (token[0], cancelled)
        try:
            yield controller
        finally:
            if local:
                local.token = previous

    def action(self, action, token, cancelled):
        with self._scope(token, cancelled) as controller:
            return controller.execute_action(action)

    def show_image(self, path, token, cancelled, *, hold_s=60):
        with self._scope(token, cancelled) as controller:
            display = controller.media.health().get("display", {})
            if display.get("enabled") is False or not display.get("available"):
                return {"displayed": False, "reason": "Display is unavailable; the generated preview is still saved"}
            if cancelled():
                raise AppCancelled("Image display cancelled")
            options = {"cancel_event": cancelled}
            if getattr(controller, "orchestrator", None) is not None:
                options["hold_s"] = hold_s
            show = getattr(controller.media.display, "show_app_image", None)
            if show is not None:
                options["hold_s"] = hold_s
                options["presentation_signal"] = getattr(cancelled, "presentation_signal", cancelled)
            else:
                show = controller.media.display.show_image
            shown = bool(show(path, **options))
            if shown and not cancelled():
                controller._app_display_until = time.monotonic() + hold_s
                controller._set_presentation(face=path.name, thinking=False)
            return {"displayed": shown and not cancelled(), "display_hold_s": hold_s if shown else 0, "reason": "" if shown else "Display rejected the image; check display diagnostics"}

    def capture_image(self):
        result = self.camera_snapshot()
        return result.get("data") if result.get("ok") else None

    def camera_snapshot(self, *, max_width=640, quality=75, max_age_s=2):
        camera = self.runtime.controller().camera
        read = getattr(camera, "get_image_snapshot", None)
        if read is None:
            raise UnavailableError("Camera does not support image snapshots")
        result = read(max_width=max_width, quality=quality, max_age_s=max_age_s)
        if not result or not result.get("ok"):
            raise UnavailableError("Camera has no fresh image; check the camera connection")
        if len(result["data"]) > 2 * 1024 * 1024:
            raise UnavailableError("Camera image exceeds the 2 MiB app limit")
        recorder = getattr(self.runtime, "recording", None)
        if recorder is not None:
            recorder.emit("camera.photo", {"source": "app_snapshot", **{key: value for key, value in result.items() if key != "data"}},
                          media=[(result["data"], result.get("media_type", "image/jpeg"))])
        return result

    def listen_claps(self, duration_s, token, cancelled):
        from robbi.app_services.claps import detect_claps
        with self._scope(token, cancelled) as controller:
            microphone = getattr(controller, "microphone", None)
            capture = getattr(microphone, "record_clap_audio", None)
            if capture is None:
                raise UnavailableError("Clap listening is unavailable; use tap input")
            try:
                data = capture(duration_s, cancelled)
            except RuntimeError as error:
                raise UnavailableError(str(error)) from error
            if cancelled():
                raise AppCancelled("Clap listening cancelled")
            return detect_claps(data)

    @staticmethod
    def _sensor_state(data):
        fresh = bool(data.get("enabled", True) and data.get("available") and data.get("fresh"))
        state = "ready" if fresh else "disabled" if data.get("enabled") is False else "stale" if data.get("available") else "unavailable"
        return {**data, "ok": fresh, "fresh": fresh, "state": state,
                "reason": "" if fresh else str(data.get("last_error") or data.get("error") or "No fresh sensor sample")}

    def tof(self, sensor):
        snapshot = self.runtime.controller().tof.status()
        data = snapshot.get("sensors", {}).get(sensor, snapshot if sensor == "h1" else {})
        result = self._sensor_state(dict(data))
        if not result["fresh"]:
            result["distance_cm"] = None
        return bounded_json({**result, "sensor": sensor, "units": {"distance_cm": "cm"},
                             "max_age_s": snapshot.get("max_age_s"), "poll_hz": snapshot.get("poll_hz")})

    def imu(self):
        result = self._sensor_state(bounded_json(self.runtime.controller().imu.status()))
        if not result["fresh"]:
            for key in ("acceleration_m_s2", "gyro_deg_s", "orientation_deg", "quaternion", "temperature_c"):
                result[key] = None
        return {**result, "heading_reference": "relative_gyro_no_magnetometer",
                "units": {"acceleration_m_s2": "m/s²", "gyro_deg_s": "degrees/s", "orientation_deg": "degrees", "temperature_c": "C"}}

    def assets(self, kind):
        return {"kind": kind, "names": self.runtime.controller().assets.names(kind)[:256]}

    @staticmethod
    def _asset(controller, kind, name, extensions):
        path = controller.assets.get(kind, name, extensions=extensions)
        if path is None:
            raise UnavailableError(f"Unknown {kind} asset: {name}")
        return path

    @staticmethod
    def _wait_audio(handle, cancelled):
        if handle is None:
            return {"ok": False, "state": "unavailable", "reason": "Audio did not start"}
        while True:
            if cancelled():
                raise AppCancelled("Audio cancelled")
            code = handle.wait(timeout=.05)
            if code is not None:
                return {"ok": code == 0, "state": "completed" if code == 0 else "interrupted",
                        "reason": "" if code == 0 else "Audio failed or was interrupted", "returncode": code}

    def play_sound(self, name, token, cancelled):
        with self._scope(token, cancelled) as controller:
            if not controller.media.health().get("audio", {}).get("available"):
                return {"ok": False, "state": "unavailable", "reason": "Audio output is unavailable"}
            path = name if isinstance(name, Path) else self._asset(controller, "sounds", name, (".wav",))
            return self._wait_audio(controller.media.play_sound(path, wait=False, cancel_event=cancelled), cancelled)

    def speak(self, text, token, cancelled):
        with self._scope(token, cancelled) as controller:
            if not controller.media.health().get("audio", {}).get("available"):
                return {"ok": False, "state": "unavailable", "reason": "Audio output is unavailable"}
            prepared = controller._prepare_tts_audio(text)
            if cancelled():
                raise AppCancelled("Speech preparation cancelled")
            if not prepared or not prepared.get("wav"):
                return {"ok": False, "state": "unavailable", "reason": "Speech provider did not produce audio"}
            handle = controller.media.speak_wav_bytes(prepared["wav"], wait=False, cancel_event=cancelled)
            track = getattr(controller, "_track_spoken_playback", None)
            if track is not None:
                track(handle, context=prepared.get("_spoken_followup"))
            return self._wait_audio(handle, cancelled)

    def show_face(self, name, token, cancelled, *, hold_s):
        path = self._asset(self.runtime.controller(), "faces", name, (".png", ".jpg", ".jpeg", ".webp"))
        return self.show_image(path, token, cancelled, hold_s=hold_s)

    def play_video(self, name, token, cancelled):
        with self._scope(token, cancelled) as controller:
            media = controller.media.health()
            if not media.get("display", {}).get("available"):
                return {"ok": False, "state": "unavailable", "reason": "Display is unavailable"}
            path = self._asset(controller, "videos", name, (".mp4", ".avi", ".mkv"))
            audio = controller.assets.get("sounds", path.stem, extensions=(".wav",))
            result = controller.media.play_video(path, audio, cancel_event=cancelled)
            if cancelled():
                raise AppCancelled("Video cancelled")
            return result if isinstance(result, dict) else {"ok": False, "state": "unavailable", "reason": "Video did not report completion"}

    def move(self, spec, token, cancelled):
        with self._scope(token, cancelled) as controller:
            if not controller.motion.status().get("available"):
                return {"ok": False, "state": "unavailable", "reason": "Motors are disabled or unavailable"}
            future = controller.motion.drive(spec, wait=False, cancel_event=cancelled)
            if future is None:
                return {"ok": False, "state": "unavailable", "reason": "Motion did not start"}
            while not future.done():
                if cancelled():
                    controller.motion.stop(owner=current_intent())
                    raise AppCancelled("Motion cancelled")
                cancelled.wait(.02)
            result = future.result() or controller.motion.status().get('last_result', {})
            if cancelled():
                raise AppCancelled("Motion cancelled")
            result = result if isinstance(result, dict) else {"ok": False, "reason": "No completion result"}
            return {**result, "state": "completed" if result.get("ok") else "interrupted"}

    @contextmanager
    def session(self, resources, token, cancelled, duration_s):
        with self._scope(token, cancelled) as controller:
            with controller.orchestrator.acquire(resources, cancel_event=cancelled, ttl_s=duration_s) as lease:
                yield
                if lease.is_set():
                    raise AppCancelled("Output session cancelled or expired")

    def gesture(self, name, token, cancelled, *, with_motion, with_audio):
        with self._scope(token, cancelled) as controller:
            return controller.dynamics.play(name, with_motion=with_motion, with_audio=with_audio, cancel_event=cancelled)

    def face_animation(self, name, token, cancelled, *, fps, loops):
        with self._scope(token, cancelled) as controller:
            return controller.dynamics.faces.play(name, fps=fps, loops=loops, cancel_event=cancelled)

    def timer(self, operation, arguments, token, cancelled):
        with self._scope(token, cancelled) as controller:
            manager = controller.reminders
            prefix = '[Kitchen] '
            items = [item for item in manager.list_items(include_completed=False, limit=500) if item['message'].startswith(prefix)]
            if operation == 'start':
                if sum(item['status'] == 'scheduled' for item in items) >= 12:
                    return {'ok': False, 'text': 'Twelve kitchen timers are already active.'}
                item = manager.add(prefix + arguments['label'] + ' timer is done.', arguments['seconds'])
                return {'ok': True, 'text': arguments['label'] + ' timer started.', 'data': {'id': item.id, 'due_at': item.due_at}}
            if operation == 'cancel':
                active = [item for item in items if item['id'] == arguments['id'] and item['status'] == 'scheduled']
                if not active:
                    return {'ok': False, 'text': 'That active kitchen timer was not found.'}
                manager.cancel(arguments['id'])
                return {'ok': True, 'text': 'Kitchen timer cancelled.'}
            now = time.time()
            active = [{**item, 'remaining_s': max(0, round(item['due_at']-now))} for item in items if item['status'] == 'scheduled']
            return {'ok': True, 'text': f'{len(active)} active kitchen timers.', 'data': {'timers': active}}
