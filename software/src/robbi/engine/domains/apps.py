from __future__ import annotations
import re
import time
from pathlib import Path
from robbi.configuration.settings import settings
from ..apps import AppPackage
from ..activity import activity
from robbi.orchestration import BACKGROUND, AUTONOMY

class AppsDomain:
    def _maybe_run_special_app_request(self, text: str, source: str) -> dict | None:
        command = (text or "").strip()
        if not command:
            return None
        if self._looks_like_focus_timer_stop_request_v2(command):
            return self._stop_focus_timer_action(user_input=command)
        if self._looks_like_focus_timer_request_v2(command) and self.apps.get("focus-timer") is not None:
            return self.run_app("focus-timer", user_input=command)
        return None

    def _run_focus_timer_app(self, user_input: str | dict = "", request_id: int | None = None) -> dict:
        if self._looks_like_focus_timer_stop_request_v2(user_input):
            return self._stop_focus_timer_action(user_input=user_input, request_id=request_id)
        duration_s = self._parse_focus_timer_duration_s_v2(user_input)
        if duration_s is None:
            action = {
                "face": "thinking",
                "speak": "Tell me how long the focus timer should run. Minutes or seconds. I am good, not psychic.",
            }
            executed = self.execute_action(action, request_id=request_id)
            self._remember_interaction(user_input or "focus timer", "app:focus-timer", executed)
            return executed
        self.behavior.note_interaction()
        self._touch_activity()
        timer_state = self.focus_timer.start(duration_s)
        action = {
            "face": "happy",
            "speak": f"Focus timer started for {self._format_focus_duration(duration_s)}.",
        }
        executed = self.execute_action(action, request_id=request_id)
        self._remember_interaction(user_input or f"focus timer {duration_s}", "app:focus-timer", executed)
        self._set_presentation(last_command=f"app:focus-timer:{timer_state['remaining_text']}")
        return executed

    def _stop_focus_timer_action(self, user_input: str | dict = "", request_id: int | None = None) -> dict:
        was_active = self.focus_timer.is_active()
        self.focus_timer.stop()
        action = {
            "face": "happy",
            "speak": "Focus timer stopped." if was_active else "There is no focus timer running. A shocking lack of urgency.",
        }
        executed = self.execute_action(action, request_id=request_id)
        self._remember_interaction(user_input or "stop focus timer", "app:focus-timer", executed)
        self._set_presentation(last_command="app:focus-timer:stopped")
        return executed

    def _run_reminder_app(self, user_input: str | dict = "", request_id: int | None = None) -> dict:
        parsed = self._parse_reminder_request(user_input)
        if parsed.get("action") == "cancel":
            canceled = self.reminders.cancel()
            action = {
                "face": "happy",
                "speak": "Reminder canceled." if canceled is not None else "There is no reminder waiting. A rare moment of peace.",
            }
            executed = self.execute_action(action, request_id=request_id)
            self._remember_interaction(user_input or "cancel reminder", "app:reminder", executed)
            self._set_presentation(last_command="app:reminder:cancel")
            return executed
        delay_s = float(parsed.get("delay_seconds") or 0.0)
        message = str(parsed.get("message") or "").strip()
        if delay_s <= 0 or not message:
            action = {
                "face": "thinking",
                "speak": "Tell me when I should remind you, and what about.",
            }
            executed = self.execute_action(action, request_id=request_id)
            self._remember_interaction(user_input or "reminder", "app:reminder", executed)
            return executed
        item = self.reminders.add(message=message, delay_s=delay_s)
        action = {
            "face": "happy",
            "speak": f"Reminder set for {self._format_focus_duration(delay_s)}. I will remind you to {message}.",
        }
        executed = self.execute_action(action, request_id=request_id)
        self._remember_interaction(user_input or f"reminder {delay_s}", "app:reminder", executed)
        self._set_presentation(last_command=f"app:reminder:{item.id}")
        return executed

    def record_wake_reference(
        self,
        duration_s: float | None = None,
        sample_name: str | None = None,
        category: str = "positive",
        prep_delay_s: float = 0.0,
    ) -> dict:
        duration = float(duration_s or settings.voice_reference_record_s)
        wav_bytes = self.microphone.record_wav_bytes(duration, prep_delay_s=prep_delay_s)
        if not wav_bytes:
            raise RuntimeError("microphone capture failed")
        saved = self.microphone.save_wake_sample(category, wav_bytes, sample_name=sample_name)
        return {"saved": saved, "status": self.wake_reference_status()}

    def delete_wake_reference(self, category: str, name: str) -> dict:
        deleted = self.microphone.delete_wake_sample(category, name)
        return {"deleted": deleted, "status": self.wake_reference_status()}

    def clear_wake_references(self, category: str = "positive") -> dict:
        removed = self.microphone.clear_wake_samples(category)
        return {"removed": removed, "status": self.wake_reference_status()}

    def wake_reference_file(self, category: str, name: str):
        return self.microphone.wake_sample_file(category, name)

    def export_wake_dataset_zip(self):
        return self.microphone.export_wake_dataset_zip()

    def upload_wake_model(self, filename: str, model_bytes: bytes) -> dict:
        models = self.microphone.upload_nanowake_model(filename, model_bytes)
        return {"models": models, "status": self.wake_reference_status()}

    def delete_wake_model(self, name: str) -> dict:
        models = self.microphone.delete_nanowake_model(name)
        return {"models": models, "status": self.wake_reference_status()}

    def apps_snapshot(self) -> dict:
        return {"apps": self.apps.list_apps(), "errors": list(self.apps.errors)}

    def install_app_zip(self, filename: str, payload: bytes) -> dict:
        if not settings.apps_enabled:
            raise RuntimeError("apps are disabled")
        result = self.apps.install_zip(filename, payload)
        self.assets.refresh(force=True)
        self._set_presentation(last_command=f"app_install:{result['installed']['id']}")
        return result

    def remove_app(self, app_id: str) -> dict:
        result = self.apps.remove(app_id)
        self.assets.refresh(force=True)
        self._set_presentation(last_command=f"app_remove:{app_id}")
        return result

    def run_app(self, app_id: str, user_input: str | dict = "", request_id: int | None = None) -> dict:
        if request_id is None:
            request_id = time.time_ns()
            self._register_request(request_id, self._action_token()[0])
        if not self._request_current(request_id):
            return {"cancelled": True}
        app = self.apps.get(app_id)
        if app is None:
            return {"speak": f"I could not find the app {app_id}."}
        self._publish_event("app.started", {"id": app.app_id, "input": user_input, "request_id": request_id})
        if app.app_id == "focus-timer":
            result = self._run_focus_timer_app(user_input=user_input, request_id=request_id)
            self._publish_event("app.finished", {"id": app.app_id, "request_id": request_id, "result_keys": sorted(result.keys())})
            return result
        if app.app_id == "reminder":
            result = self._run_reminder_app(user_input=user_input, request_id=request_id)
            self._publish_event("app.finished", {"id": app.app_id, "request_id": request_id, "result_keys": sorted(result.keys())})
            return result
        if app.app_id == "kids":
            if isinstance(user_input, dict):
                result = self.kids_start(user_input)
            else:
                result = self.kids_start({"mode": str(user_input or "riddle")})
            action = {"speak": result.get("speak") or "Kids mode started.", "face": result.get("face") or "happy"}
            self.execute_action(action, request_id=request_id)
            self._publish_event("app.finished", {"id": app.app_id, "request_id": request_id, "result_keys": sorted(action.keys())})
            return action
        self._touch_activity()
        memory_context = self.memory.prepare_prompt_context(
            user_input or app.description or app.name,
            recent_messages=self.memory.recent_messages(),
        )
        if app.prompt_text.strip():
            action = self.llm.ask(
                user_input or f"Run the app {app.name}.",
                provider=settings.ui_text_provider,
                model=settings.ui_text_model,
                recent_messages=self.memory.recent_messages(),
                memory_text=memory_context["prompt_text"],
                soul_text=self._soul_text,
                tools_text=self._tools_prompt_context(),
                app_catalog_text=self.apps.prompt_catalog(),
                extra_instructions=app.prompt_context(),
            )
        elif app.workflow:
            action = dict(app.workflow)
        else:
            action = {"speak": f"The app {app.name} has no workflow yet."}
        executed = self.execute_action(action, request_id=request_id, app=app)
        self._remember_interaction(user_input or f"run app {app.app_id}", f"app:{app.app_id}", executed)
        self._set_presentation(last_command=f"app:{app.app_id}")
        self._publish_event("app.finished", {"id": app.app_id, "request_id": request_id, "result_keys": sorted(executed.keys())})
        return executed

    def parse_debug_command(self, text: str) -> dict | None:
        command = (text or "").strip()
        if not command.startswith("/"):
            return None
        parts = command[1:].split()
        if not parts:
            return {"speak": "Empty command."}
        cmd = parts[0].lower()
        args = parts[1:]
        if cmd == "drive":
            if not args:
                return {"speak": "Usage: /drive <forward|backward|left|right|forward_slight_left|forward_slight_right|stop> [speed] [duration]"}
            try:
                speed = int(args[1]) if len(args) > 1 else 100
                duration = float(args[2]) if len(args) > 2 else 1.0
            except ValueError:
                return {"speak": "Drive speed and duration must be numeric."}
            return {"drive": {"direction": args[0], "speed": speed, "duration": duration}}
        if cmd in {"face", "sound", "video", "animation"}:
            if not args:
                return {"speak": f"Usage: /{cmd} <name>"}
            return {cmd: args[0]}
        if cmd in {"sequence", "filming_sequence", "film"}:
            if not args:
                return {"speak": "Usage: /sequence <name>"}
            return {"filming_sequence": args[0]}
        if cmd == "mute":
            muted = True
            if args and args[0].lower() in {"off", "false", "0", "no"}:
                muted = False
            self.set_muted(muted)
            return {"speak": "Muted." if muted else "Unmuted."}
        if cmd in {"volume", "vol"}:
            if not args:
                return {"speak": f"Volume is {self.media.volume()} percent."}
            try:
                volume = self.set_volume(int(args[0]))
            except ValueError:
                return {"speak": "Volume must be a number from 0 to 100."}
            return {"speak": f"Volume set to {volume} percent."}
        if cmd == "help":
            return {
                "speak": "Commands: /drive /face /sound /video /animation /mute /volume",
                "face": "happy.png",
            }
        if cmd == "timer":
            requested = " ".join(args).strip() or "5 seconds"
            return {"app": {"id": "focus-timer", "input": requested}}
        return {"speak": f"Unknown command: {cmd}"}

    def _looks_like_focus_timer_request(self, text: str) -> bool:
        lower = (text or "").strip().lower()
        compact = re.sub(r"[^a-z0-9Ã¤Ã¶Ã¼ÃŸ]+", "", lower)
        timer_words = ("focus timer", "focus time", "pomodoro", "focus session", "start timer", "start a timer")
        if any(word in lower for word in timer_words):
            return True
        return any(token in compact for token in ("focustimer", "focustime", "pomodoro", "focussession"))

    def _looks_like_focus_timer_stop_request(self, text: str) -> bool:
        lower = (text or "").strip().lower()
        if not lower:
            return False
        compact = re.sub(r"[^a-z0-9Ã¤Ã¶Ã¼ÃŸ]+", "", lower)
        mentions_timer = any(word in lower for word in ("focus timer", "focus time", "pomodoro", "focus session", "timer", "timer running"))
        mentions_timer = mentions_timer or any(token in compact for token in ("focustimer", "focustime", "pomodoro", "timer"))
        wants_stop = any(word in lower for word in ("stop", "cancel", "end", "abort", "beenden", "stopp", "stoppe"))
        return mentions_timer and wants_stop

    def _parse_focus_timer_duration_s(self, text: str) -> float | None:
        command = (text or "").strip().lower()
        if not command:
            return None
        total_s = 0.0
        pattern = r"(\d+(?:[.,]\d+)?)\s*(hours?|hrs?|hr|h|stunden?|std|minutes?|mins?|min|m|minuten?|seconds?|secs?|sec|s|sekunden?|sek|sekunde)\b"
        for raw_value, unit in re.findall(pattern, command):
            value = float(raw_value.replace(",", "."))
            normalized = unit.strip().lower()
            if normalized.startswith("h") or normalized.startswith("st"):
                total_s += value * 3600.0
            elif normalized.startswith("m") or normalized.startswith("min"):
                total_s += value * 60.0
            else:
                total_s += value
        if total_s > 0:
            return total_s
        plain_number = re.search(r"\b(\d+(?:[.,]\d+)?)\b", command)
        if plain_number:
            return float(plain_number.group(1).replace(",", ".")) * 60.0
        return None

    def _timer_text_v2(self, text: str | dict) -> str:
        if isinstance(text, dict):
            if isinstance(text.get("text"), str) and text.get("text", "").strip():
                return str(text.get("text", "")).strip().lower()
            parts: list[str] = []
            for value in text.values():
                if isinstance(value, (str, int, float)):
                    parts.append(str(value))
                elif isinstance(value, dict):
                    nested_text = value.get("text")
                    if isinstance(nested_text, str) and nested_text.strip():
                        parts.append(nested_text)
            return " ".join(parts).strip().lower()
        return str(text or "").strip().lower()

    def _looks_like_focus_timer_request_v2(self, text: str | dict) -> bool:
        lower = self._timer_text_v2(text)
        if not lower:
            return False
        compact = re.sub(r"[^a-z0-9]+", "", lower)
        timer_words = (
            "focus timer",
            "focus time",
            "pomodoro",
            "focus session",
            "start timer",
            "start a timer",
            "fokus timer",
            "fokustimer",
            "fokus session",
            "fokus-session",
        )
        if any(word in lower for word in timer_words):
            return True
        return any(
            token in compact
            for token in (
                "focustimer",
                "focustime",
                "pomodoro",
                "focussession",
                "fokustimer",
                "fokussession",
            )
        )

    def _looks_like_focus_timer_stop_request_v2(self, text: str | dict) -> bool:
        lower = self._timer_text_v2(text)
        if not lower:
            return False
        compact = re.sub(r"[^a-z0-9]+", "", lower)
        stop_words = ("stop", "cancel", "end", "abort", "beenden", "stopp", "stoppe")
        timer_words = (
            "focus timer",
            "focus time",
            "pomodoro",
            "focus session",
            "timer",
            "timer running",
            "fokus timer",
            "fokustimer",
            "fokus session",
            "fokussession",
        )
        mentions_timer = any(word in lower for word in timer_words) or any(
            token in compact
            for token in ("focustimer", "focustime", "pomodoro", "timer", "fokustimer", "fokussession")
        )
        wants_stop = any(word in lower for word in stop_words)
        if mentions_timer and wants_stop:
            return True
        return wants_stop and "time r" in lower

    def _parse_focus_timer_duration_s_v2(self, text: str | dict) -> float | None:
        if isinstance(text, dict):
            duration_seconds = text.get("duration_seconds")
            if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
                return float(duration_seconds)
        command = self._timer_text_v2(text)
        if not command:
            return None
        total_s = 0.0
        pattern = (
            r"(\d+(?:[.,]\d+)?)\s*"
            r"(hours?|hrs?|hr|h|stunden?|std|minutes?|mins?|min|m|minuten?|seconds?|secs?|sec|s|sekunden?|sek|sekunde)\b"
        )
        for raw_value, unit in re.findall(pattern, command):
            value = float(raw_value.replace(",", "."))
            normalized = unit.strip().lower()
            if normalized.startswith("h") or normalized.startswith("st"):
                total_s += value * 3600.0
            elif normalized.startswith("m"):
                total_s += value * 60.0
            else:
                total_s += value
        if total_s > 0:
            return total_s
        plain_number = re.search(r"\b(\d+(?:[.,]\d+)?)\b", command)
        if plain_number:
            return float(plain_number.group(1).replace(",", ".")) * 60.0
        return None

    def _looks_like_reminder_request(self, text: str | dict) -> bool:
        lower = self._timer_text_v2(text)
        if not lower:
            return False
        tokens = ("remind me", "set a reminder", "set reminder", "reminder", "erinnere mich", "erinnerung")
        return any(token in lower for token in tokens)

    def _looks_like_reminder_cancel_request(self, text: str | dict) -> bool:
        lower = self._timer_text_v2(text)
        if not lower:
            return False
        reminder_words = ("reminder", "remind me", "erinnerung", "erinnere")
        stop_words = ("stop", "cancel", "delete", "remove", "clear", "beenden", "loeschen", "lÃ¶schen", "stopp", "stoppe")
        return any(word in lower for word in reminder_words) and any(word in lower for word in stop_words)

    def _parse_reminder_request(self, text: str | dict) -> dict:
        if isinstance(text, dict):
            action = str(text.get("action") or "").strip().lower()
            if action == "cancel":
                return {"action": "cancel"}
            delay_seconds = text.get("delay_seconds")
            message = str(text.get("message") or text.get("text") or "").strip()
            if isinstance(delay_seconds, (int, float)) and delay_seconds > 0 and message:
                return {"action": "create", "delay_seconds": float(delay_seconds), "message": message}
            text = message
        lower = self._timer_text_v2(text)
        if self._looks_like_reminder_cancel_request(lower):
            return {"action": "cancel"}
        delay_seconds = self._parse_focus_timer_duration_s_v2(lower)
        if delay_seconds is None:
            return {"action": "create", "delay_seconds": 0.0, "message": ""}
        message = lower
        patterns = [
            r"remind me in .+? to (.+)$",
            r"remind me in .+? about (.+)$",
            r"in .+? remind me to (.+)$",
            r"in .+? remind me about (.+)$",
            r"set (?:a )?reminder .+? to (.+)$",
            r"erinnere mich in .+? daran[,]? (.+)$",
            r"erinnere mich in .+? an (.+)$",
            r"in .+? erinnere mich daran[,]? (.+)$",
            r"in .+? erinnere mich an (.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                message = match.group(1).strip(" .,!?:;")
                break
        for prefix in ("to ", "about ", "daran ", "an "):
            if message.startswith(prefix):
                message = message[len(prefix) :].strip()
        return {"action": "create", "delay_seconds": delay_seconds, "message": message}

    def _format_focus_duration(self, duration_s: float) -> str:
        total_seconds = max(1, int(round(duration_s)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
        if minutes:
            parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
        if seconds and not hours and minutes < 2:
            parts.append(f"{seconds} second" + ("s" if seconds != 1 else ""))
        return " ".join(parts) if parts else "0 seconds"

    def _defer_focus_timer_for_action(self, action: dict) -> None:
        if not self.focus_timer.is_active():
            return
        if not isinstance(action, dict):
            return
        defer_s = 0.0
        if action.get("sequence"):
            defer_s = max(defer_s, 1.2)
        if action.get("animation") or action.get("filming_sequence"):
            defer_s = max(defer_s, 1.5)
        if action.get("video"):
            defer_s = max(defer_s, 2.0)
        if action.get("drive"):
            defer_s = max(defer_s, float((action.get("drive") or {}).get("duration", 0.8)))
        if action.get("face") or action.get("sound") or action.get("speak"):
            defer_s = max(defer_s, 0.8)
        if defer_s > 0:
            self.focus_timer.defer_restore(defer_s)

    @activity("focus-timer", BACKGROUND, replace=True)
    def _show_focus_timer_face(self, image_path: Path, remaining_text: str) -> None:
        self.media.show_face(image_path)
        self._set_presentation(face=f"focus_timer:{remaining_text}", thinking=False, playing_video=None)

    @activity("focus-timer-finished", AUTONOMY, replace=True)
    def _handle_focus_timer_finished(self) -> None:
        self._publish_event("timer.completed", self.focus_timer.status())
        action = {
            "face": "joyful",
            "sound": "beeping",
        }
        self.execute_action(action)
        self._set_presentation(last_command="app:focus-timer:finished")

    @activity("reminder", AUTONOMY, replace=True)
    def _handle_due_reminder(self, item) -> None:
        message = str(getattr(item, "message", "") or "").strip()
        self._publish_event("reminder.due", {"id": getattr(item, "id", ""), "message": message})
        action = {
            "face": "joyful",
            "sound": "wow",
            "speak": f"Reminder. {message}" if message else "Reminder.",
        }
        self.execute_action(action)
        if settings.telegram_notify_reminders:
            self.telegram.notify_text(f"Reminder: {message}" if message else "Reminder.")
        self._set_presentation(last_command=f"app:reminder:due:{getattr(item, 'id', '')}")

    def _can_present_reminder(self) -> bool:
        if self._filming_mode:
            return False
        if self._voice_processing:
            return False
        if self.media.is_audio_playing():
            return False
        if self.media.is_video_playing():
            return False
        snapshot = self.behavior.snapshot()
        if not snapshot.awake:
            return False
        presentation = self.presentation_state()
        if presentation.get("thinking"):
            return False
        return True

    def _can_show_focus_timer(self) -> bool:
        if not self.focus_timer.is_active():
            return False
        if self._voice_processing:
            return False
        if self.media.is_audio_playing():
            return False
        if self.media.is_video_playing():
            return False
        microphone = self.microphone.health()
        voice_state = str(microphone.get("voice_state") or "idle")
        if voice_state in {"armed", "recording_command", "transcribing_command"}:
            return False
        presentation = self.presentation_state()
        if presentation.get("thinking"):
            return False
        return True

    def _resolve_asset(
        self,
        category: str,
        name: str,
        app: AppPackage | None = None,
        extensions: tuple[str, ...] = (),
    ) -> Path | None:
        if app is not None:
            local = app.resolve_asset(category, name, extensions=extensions)
            if local is not None:
                return local
        return self.assets.get(category, name, extensions=extensions)

    def _parse_app_spec(self, value) -> tuple[str, object]:
        if isinstance(value, str):
            return value.strip(), ""
        if isinstance(value, dict):
            return str(value.get("id") or "").strip(), value.get("input") or ""
        return "", ""
