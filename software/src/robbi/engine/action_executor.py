from __future__ import annotations

import time
from .apps import AppPackage
from robbi.actions import validate_action
from robbi.orchestration import INTERACTIVE, current_intent
from robbi.activity import record_tool
from .activity import activity,output_scene
from .presentation_waits import PresentationCancellation, wait_for_playback


class ActionExecutorMixin:
    @record_tool("execute_action")
    @activity("robot-action", INTERACTIVE)
    def execute_action(self, action: dict, request_id: int | None = None, app: AppPackage | None = None) -> dict:
        if not action:
            return {}
        validate_action(action)
        if request_id is None:
            request_id = time.time_ns()
            self._register_request(request_id, self._action_token()[0])
        if not self._request_current(request_id):
            return {"cancelled": True}
        action = self._expand_legacy_timed_action(action)
        self._defer_focus_timer_for_action(action)
        self._publish_event("assistant.response_started", {"request_id": request_id, "keys": sorted(action), "app": app.app_id if app else ""})
        if app_spec := action.get("app"):
            app_id, app_input = self._parse_app_spec(app_spec)
            if app_id:
                self._remember_executed_action("legacy_app", {"app": app_spec}, request_id)
                try:
                    return self.run_app(app_id, user_input=app_input, request_id=request_id)
                except Exception:
                    self._remember_executed_action("legacy_app", {"app": app_spec}, request_id, ok=False)
                    raise
            return {"speak": "I could not determine which app to run."}
        if sequence := action.get("sequence"):
            self._submit_guarded(self._presentation_executor, self._run_sequence, sequence, request_id, app)
        else:
            try:
                self._run_sequence_step(action, request_id=request_id, app=app)
            except Exception:
                self._remember_executed_action("robot_action", action, request_id, ok=False)
                raise
        self._publish_event("assistant.response_finished", {"request_id": request_id, "keys": sorted(action), "speak": self._collect_spoken_text(action)})
        return action

    def _remember_executed_action(self, tool, arguments, request_id, *, ok=True):
        source_for = getattr(self, "_tool_source_for_request", None)
        remember = getattr(self, "_remember_tool_call", None)
        source = source_for(request_id) if callable(source_for) else None
        if source and callable(remember):
            text = ("Action accepted; physical effects and playback cannot be undone." if ok
                    else "Action failed after admission; its effects are uncertain. Give a new full request instead of replaying it.")
            remember(tool, arguments, {"ok": ok, "text": text},
                     source=source, request_id=request_id)

    def _repeat_core_tool(self, call, *, source, request_id=None):
        arguments = call.get("arguments")
        if (call.get("tool") not in {"robot_action", "legacy_app"} or not isinstance(arguments, dict)
                or (call.get("tool") == "legacy_app" and not arguments.get("app"))):
            return {"speak": "Please give the full request to repeat this action."}
        if not self._request_current(request_id):
            return {"cancelled": True}
        result = self.execute_action(arguments, request_id=request_id)
        return {"cancelled": True} if result.get("cancelled") else {"speak": "Repeated the action."}

    def _expand_legacy_timed_action(self, action: dict) -> dict:
        if not isinstance(action, dict) or action.get("sequence"):
            return action
        if not (action.get("speak") and any(action.get(key) for key in ("animation", "filming_sequence", "video"))):
            return action
        intro_step: dict = {}
        for key in ("face", "sound", "speak"):
            value = action.get(key)
            if value:
                intro_step[key] = value
        if intro_step.get("speak"):
            intro_step["wait_for_audio"] = True
        follow_step: dict = {}
        for key in ("animation", "filming_sequence", "video", "drive"):
            value = action.get(key)
            if value:
                follow_step[key] = value
        sequence = []
        if intro_step:
            sequence.append(intro_step)
        if follow_step:
            sequence.append(follow_step)
        if not sequence:
            return action
        normalized = dict(action)
        normalized["sequence"] = sequence
        return normalized

    def _run_sequence(self, sequence: list, request_id: int | None = None, app: AppPackage | None = None) -> None:
        generation, cancelled = self._action_token()
        owner = current_intent()
        cancelled = PresentationCancellation(cancelled, owner)
        # A small lookahead avoids filling the speech queue with a long script.
        prefetched = {}
        try:
            for index, step in enumerate(sequence):
                if cancelled.is_set() or not self._request_current(request_id):
                    return
                if not isinstance(step, dict):
                    continue
                for next_index in range(index, min(index + 2, len(sequence))):
                    speech = str(sequence[next_index].get("speak") or "").strip()
                    if speech and next_index not in prefetched:
                        try:
                            prefetched[next_index] = self._submit_guarded(self._tts_prefetch_executor, self._prepare_tts_audio, speech)
                        except RuntimeError:
                            pass
                prepared = None
                future = prefetched.pop(index, None)
                if future is not None:
                    prepared = self._wait_action_future(future, cancelled, timeout_s=65)
                if cancelled.is_set() or not self._request_current(request_id):
                    return
                self._run_sequence_step(step, request_id=request_id, prepared_tts=prepared, app=app)
                pause = self._sequence_wait_ms(step, sequence[index + 1] if index + 1 < len(sequence) else None)
                if cancelled.wait(pause / 1000.0):
                    return
        except Exception as error:
            self._remember_executed_action("robot_action", {"sequence": sequence}, request_id, ok=False)
            if owner is not None:
                owner.cancel('Sequence failed')
            self._publish_event('presentation.failed', {
                'feature': 'sequence', 'reason': 'timeout' if isinstance(error, TimeoutError) else 'failed',
                'request_id': request_id,
            })
            raise
        finally:
            for future in prefetched.values():
                future.cancel()

    @record_tool("action.step")
    @output_scene
    def _run_sequence_step(self, step: dict, request_id: int | None = None, prepared_tts: dict | None = None, app: AppPackage | None = None) -> None:
        if not self._request_current(request_id):
            return
        validate_action(step)
        preserves = getattr(self, "_app_preserves_display", lambda request_id=None: False)
        preserve_app_display = preserves(request_id) or preserves()
        if preserve_app_display:
            step = {key: value for key, value in step.items()
                    if key not in {"face", "speaking_face_pattern", "animation", "filming_sequence", "video", "gesture", "face_animation"}}
        if step.get("sequence"):
            self._run_sequence(step["sequence"], request_id, app)
            return
        generation, cancelled = self._action_token()
        cancelled = PresentationCancellation(cancelled, current_intent())
        if cancelled.is_set() or not self._request_current(request_id):
            return
        action_keys = {"drive", "slam", "sound", "animation", "filming_sequence", "video", "gesture", "face_animation"}
        if any(step.get(key) for key in action_keys):
            self._remember_executed_action("robot_action", {key: value for key, value in step.items()
                                                          if key in action_keys or key in {"face", "speak", "wait_for_audio", "wait_ms"}}, request_id)
        if face := step.get("face"):
            path = self._resolve_asset("faces", face, app=app, extensions=(".png",))
            if path:
                self.media.show_face(path)
                if cancelled.is_set() or not self._request_current(request_id):
                    return
                self.behavior.set_current_face(path.name)
                self._set_presentation(face=path.name, thinking=False)
        if cancelled.is_set() or not self._request_current(request_id):
            return
        audio_handle = None
        if sound := step.get("sound"):
            path = self._resolve_asset("sounds", sound, app=app, extensions=(".wav",))
            if path:
                audio_handle = self.media.play_sound(path, wait=False, cancel_event=cancelled)
                self._set_presentation(last_sound=path.name)
        if cancelled.is_set() or not self._request_current(request_id):
            return
        motion_future=None
        if drive := step.get("drive"):
            motion_future=self._execute_drive_action(drive)
        if cancelled.is_set() or not self._request_current(request_id):
            return
        if slam := step.get("slam"):
            self._execute_slam_action(slam)
        if cancelled.is_set() or not self._request_current(request_id):
            return
        speech_future = None
        if speak := str(step.get("speak") or "").strip():
            if preserve_app_display or step.get('gesture') or step.get('face_animation'):
                player = self._play_prepared_tts if prepared_tts else self._speak_text
                speech_future = self._submit_optional_presentation(
                    self._audio_executor, "speech", player, prepared_tts or speak,
                    request_id, bool(step.get("wait_for_audio")), cancellation=cancelled,
                )
            elif prepared_tts:
                pattern = step.get("speaking_face_pattern") or [step.get("face") or self.behavior.snapshot().current_face, "speaking"]
                speech_future = self._submit_optional_presentation(self._audio_executor, "speech", self._play_prepared_tts_with_overlay, prepared_tts, pattern, request_id, bool(step.get("wait_for_audio")), cancellation=cancelled)
            else:
                pattern = step.get("speaking_face_pattern") or [step.get("face") or self.behavior.snapshot().current_face, "speaking"]
                speech_future = self._submit_optional_presentation(self._audio_executor, "speech", self._speak_text_with_overlay, speak, pattern, request_id, bool(step.get("wait_for_audio")), cancellation=cancelled)
            if speech_future is not None:
                self._set_presentation(last_speak=speak)
        completions = []
        if step.get('gesture') or step.get('face_animation'):
            dynamic = self.dynamics
            for key, callback in (('gesture', dynamic.play), ('face_animation', dynamic.faces.play)):
                if name := step.get(key):
                    # Speech owns the speaker; its face overlay must not race
                    # the selected sprite animation, nor a dance cue cut it off.
                    options = {'with_audio': not bool(step.get('speak') or step.get('sound'))} if key == 'gesture' else {}
                    future = self._submit_optional_presentation(self._effect_executor, key, callback,
                        name, cancel_event=cancelled, cancellation=cancelled, **options)
                    if future is not None:
                        completions.append(future)
                        self._set_presentation(last_animation=name)
        for key, player in (("animation", self.animations.play), ("filming_sequence", self.animations.play_filming_sequence)):
            if name := step.get(key):
                future = self._submit_optional_presentation(self._effect_executor, key, player, name, cancel_event=cancelled, request_id=request_id, cancellation=cancelled)
                if future is not None:
                    completions.append(future)
                    self._set_presentation(last_animation=name if key == "animation" else f"sequence:{name}")
        if video := step.get("video"):
            path = self._resolve_asset("videos", video, app=app, extensions=(".mp4",))
            if path:
                future = self._submit_optional_presentation(self._effect_executor, "video", self._play_video_action, path, path.with_name(f"{path.stem}_audio.wav"), cancellation=cancelled)
                if future is not None:
                    completions.append(future)
                    self._set_presentation(playing_video=path.name)
        if step.get("wait_for_audio"):
            if speech_future is not None:
                self._wait_action_future(speech_future, cancelled)
            if audio_handle is not None:
                wait_for_playback(audio_handle, cancelled)
        if step.get("wait_for_completion"):
            for future in completions:
                self._wait_action_future(future, cancelled)
        return [motion_future,audio_handle,speech_future,*completions]

    def _submit_optional_presentation(self, executor, feature, callback, *args, cancellation=None, **kwargs):
        def invoke():
            if cancellation is None or not cancellation.is_set():
                return callback(*args, **kwargs)
        try:
            if cancellation is not None and cancellation.is_set():
                return None
            return self._submit_guarded(executor, invoke)
        except RuntimeError as error:
            # Motion in this action may already have started. Report skipped
            # presentation separately so callers do not retry applied motion.
            self._publish_event("presentation.skipped", {"feature": feature, "reason": str(error)})
            return None

    def _wait_action_future(self, future, cancelled, timeout_s=120):
        owner = current_intent()
        cancelled = PresentationCancellation(cancelled, owner)
        deadline = time.monotonic() + timeout_s
        while not future.done() and not cancelled.is_set() and time.monotonic() < deadline:
            cancelled.wait(0.05)
        if cancelled.is_set():
            future.cancel()
            return None
        if future.done() and time.monotonic() < deadline:
            return future.result()
        future.cancel()
        if owner is not None:
            owner.cancel('Presentation deadline exceeded')
        raise TimeoutError('Presentation deadline exceeded')

    def _sequence_wait_ms(self, step: dict, next_step: dict | None) -> int:
        explicit_wait_ms = step.get("wait_ms", 0) or 0
        if explicit_wait_ms:
            return max(0, int(explicit_wait_ms))
        if not isinstance(next_step, dict):
            return 0
        current_speak = str(step.get("speak") or "").strip()
        next_speak = str(next_step.get("speak") or "").strip()
        if current_speak and next_speak:
            return 300
        return 0

    def _optional_float(self, value) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except Exception:
            return None

    @record_tool("drive")
    def _execute_drive_action(self, drive: dict):
        if not isinstance(drive, dict):
            return
        mode = str(drive.get("mode") or drive.get("direction") or "forward").strip().lower()
        future = self.motion.move(
            direction=drive.get("direction", mode),
            mode=drive.get("mode"),
            speed=int(float(drive.get("speed", 100))),
            duration=float(drive.get("duration", 1.0)),
            distance_cm=self._optional_float(drive.get("distance_cm")),
            angle_deg=self._optional_float(drive.get("angle_deg")),
            radius_cm=self._optional_float(drive.get("radius_cm")),
            amplitude_cm=self._optional_float(drive.get("amplitude_cm")),
            cycles=self._optional_float(drive.get("cycles")),
            ignore_obstacles=bool(drive.get("ignore_obstacles") or drive.get("obstacle_override")),
            wait=False,
            cancel_event=self._action_token()[1] if hasattr(self, "_action_token") else None,
        )
        self.slam.track_drive_action(drive, future)
        self._set_presentation(last_command=f"drive:{mode}")
        return future

    @record_tool("slam")
    def _execute_slam_action(self, slam: dict):
        if not isinstance(slam, dict):
            return
        action = str(slam.get("action") or "").strip().lower()
        if action in {"scan", "scan_room", "start_scan"}:
            result = self.slam.start_scan()
        elif action in {"explore", "explore_room", "start_explore"}:
            result = self.slam.start_explore(max_steps=self._optional_int(slam.get("max_steps")))
        elif action in {"stop", "stop_mapping"}:
            result = self.slam.stop()
        elif action in {"reset", "reset_map"}:
            result = self.slam.reset()
        elif action in {"save", "save_map"}:
            result = self.slam.save()
        elif action in {"where_am_i", "status"}:
            result = self.slam.status()
        elif action in {"localize", "enable_localization", "localization_on"}:
            result = self.slam.set_localization_enabled(True)
        elif action in {"disable_localization", "localization_off"}:
            result = self.slam.set_localization_enabled(False)
        else:
            result = {"ok": False, "error": f"unknown slam action {action}"}
        self._publish_event("tool.called", {"tool": "slam", "action": action, "ok": result.get("ok", True)})
        self._set_presentation(last_command=f"slam:{action or 'unknown'}")
        return result

    def _optional_int(self, value) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            return None
