from __future__ import annotations
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from robbi.configuration.settings import settings
from robbi.activity import record_context

class TelegramDomain:
    def _handle_telegram_text(self, text: str, chat_id: int, speak_remote: bool = False) -> str:
        clean_text = str(text or "").strip()
        if not clean_text:
            return "Empty message. A bold strategy."
        self._publish_event("telegram.message_received", {"chat_id": chat_id, "text": clean_text})
        if speak_remote:
            action = {"speak": clean_text}
            self.execute_action(action)
            self.memory.add_user_message(f"/say {clean_text}", source="telegram")
            self.memory.add_assistant_message(clean_text, source="telegram")
            return f"Robbi said: {clean_text}"
        action = self.think_and_respond(clean_text, include_photo=False, source="telegram_text", conversation_id=str(chat_id))
        reply = self._collect_spoken_text(action)
        if not reply:
            if action.get("heartbeat") == "ok":
                reply = "Noted."
            elif action.get("remember") or action.get("memory_ops"):
                reply = "Memory updated."
            else:
                reply = "Done."
        self._publish_event("telegram.reply_sent", {"chat_id": chat_id, "chars": len(reply)})
        return reply

    @record_context
    def _handle_telegram_voice(self, audio_bytes: bytes, chat_id: int) -> dict:
        self._publish_event("telegram.voice_received", {"chat_id": chat_id, "audio_bytes": len(audio_bytes or b"")})
        wav_bytes = self._telegram_audio_to_wav(audio_bytes)
        transcript = self.llm.transcribe(
            wav_bytes,
            provider=settings.stt_provider,
            model=settings.stt_model,
            language=settings.stt_language,
        )
        transcript = (transcript or "").strip()
        recorder = getattr(self, "recording", None)
        if recorder is not None:
            mime = "audio/wav" if wav_bytes.startswith(b"RIFF") else "audio/ogg"
            recorder.emit("speech.received", {"text": transcript, "source": "telegram"},
                          media=[(wav_bytes, mime)] if wav_bytes else [])
        if not transcript:
            return {"transcript": "", "reply": "I could not transcribe that. Apparently even the bits were mumbling."}
        action = self.think_and_respond(transcript, include_photo=False, source="telegram_text", conversation_id=str(chat_id))
        reply = self._collect_spoken_text(action) or "Done."
        result = {"transcript": transcript, "reply": reply}
        if settings.telegram_send_audio_replies and reply:
            result["audio_reply"] = self.llm.synthesize(reply)
        if recorder is not None:
            recorder.emit("speech.output", {"text": reply, "source": "telegram", "status": "prepared"},
                          media=[(result["audio_reply"], "audio/wav")] if result.get("audio_reply") else [])
        return result

    def _telegram_audio_to_wav(self, audio_bytes: bytes) -> bytes:
        if not audio_bytes:
            return b""
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "pipe:0",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    "pipe:1",
                ],
                input=audio_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            self._log.warning("Telegram audio ffmpeg conversion failed: %s", result.stderr.decode("utf-8", "replace"))
        except Exception as exc:
            self._log.warning("Telegram audio conversion failed: %s", exc)
        return audio_bytes

    def _telegram_status_text(self) -> str:
        status = self.status()
        behavior = status.get("behavior", {})
        motion = status.get("motion", {})
        tof = status.get("tof", {})
        focus = status.get("focus_timer", {})
        reminders = status.get("reminders", {})
        llm = status.get("llm", {})
        parts = [
            "Robbi status",
            f"LLM: {llm.get('ui_text_provider') or llm.get('provider')} / {llm.get('ui_text_model') or llm.get('model')}",
            f"Vitals: energy {behavior.get('energy')} / social {behavior.get('social')} / stimulation {behavior.get('stimulation')}",
            f"Mood: {behavior.get('mood')} | awake: {behavior.get('awake')}",
            f"Wake: {'on' if status.get('microphone', {}).get('wake_enabled') else 'off'}",
            f"Focus timer: {focus.get('remaining_text') if focus.get('active') else 'off'}",
            f"Reminders: {reminders.get('active_count', 0)} active",
            f"ToF: {tof.get('distance_cm') if tof.get('available') else tof.get('last_error') or 'unavailable'} cm",
            f"Motion: {'running' if motion.get('running') else (motion.get('last_result') or {}).get('reason', 'idle')}",
        ]
        return "\n".join(parts)

    def _telegram_capture_photo(self) -> Path | None:
        self._publish_event("telegram.photo_requested", {})
        return self.capture_photo()

    def _telegram_photo_question(self, question: str, chat_id: int) -> str:
        clean_question = str(question or "").strip()
        if not clean_question:
            return "Photo sent. Question not found, because apparently punctuation was too ambitious."
        self._publish_event("telegram.photo_question", {"chat_id": chat_id, "question": clean_question})
        action = self.think_and_respond(clean_question, include_photo=True, source="telegram_text", conversation_id=str(chat_id))
        return self._collect_spoken_text(action) or "I looked, but the answer came back empty."

    def _telegram_reminder_command(self, text: str) -> str:
        natural = self._parse_telegram_natural_reminder(text)
        if natural is not None:
            item = self.reminders.add(message=natural["message"], delay_s=natural["delay_seconds"])
            self._publish_event(
                "telegram.reminder_created",
                {"id": item.id, "message": item.message, "delay_s": natural["delay_seconds"], "mode": "natural"},
            )
            return f"Reminder set for {natural['when_text']}: {natural['message']}"
        parsed = self._parse_reminder_request(f"remind me {text}")
        if parsed.get("action") == "cancel":
            item = self.reminders.cancel()
            return "Reminder canceled." if item else "No active reminder."
        delay_s = float(parsed.get("delay_seconds") or 0.0)
        message = str(parsed.get("message") or "").strip()
        if delay_s <= 0 or not message:
            parsed = self._parse_reminder_request(text)
            delay_s = float(parsed.get("delay_seconds") or 0.0)
            message = str(parsed.get("message") or "").strip()
        if delay_s <= 0 or not message:
            return "Could not parse that reminder. Try: /remind in 10 minutes check the oven"
        item = self.reminders.add(message=message, delay_s=delay_s)
        self._publish_event("telegram.reminder_created", {"id": item.id, "message": message, "delay_s": delay_s})
        return f"Reminder set for {self._format_focus_duration(delay_s)}: {message}"

    def _parse_telegram_natural_reminder(self, text: str) -> dict | None:
        raw = " ".join(str(text or "").strip().split())
        if not raw:
            return None
        lower = raw.lower()
        now = datetime.now()
        match = re.match(
            r"^(?:tomorrow|morgen)(?:\s+at|\s+um)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(?:to\s+|about\s+|daran\s+|an\s+)?(.+)$",
            lower,
        )
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            meridiem = match.group(3)
            message = match.group(4).strip(" .,!?:;")
            due = self._time_for_day(now + timedelta(days=1), hour, minute, meridiem)
            return self._telegram_reminder_result(due, message)
        match = re.match(
            r"^(?:today|heute)(?:\s+at|\s+um)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(?:to\s+|about\s+|daran\s+|an\s+)?(.+)$",
            lower,
        )
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            meridiem = match.group(3)
            message = match.group(4).strip(" .,!?:;")
            due = self._time_for_day(now, hour, minute, meridiem)
            if due <= now:
                due += timedelta(days=1)
            return self._telegram_reminder_result(due, message)
        match = re.match(
            r"^(?:at\s+|um\s+)?(\d{1,2})(?::(\d{2}))\s*(am|pm)?\s+(?:to\s+|about\s+|daran\s+|an\s+)?(.+)$",
            lower,
        )
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            meridiem = match.group(3)
            message = match.group(4).strip(" .,!?:;")
            due = self._time_for_day(now, hour, minute, meridiem)
            if due <= now:
                due += timedelta(days=1)
            return self._telegram_reminder_result(due, message)
        return None

    def _time_for_day(self, base: datetime, hour: int, minute: int, meridiem: str | None) -> datetime:
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        hour = max(0, min(23, hour))
        minute = max(0, min(59, minute))
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _telegram_reminder_result(self, due: datetime, message: str) -> dict | None:
        message = str(message or "").strip(" .,!?:;")
        if not message:
            return None
        delay_seconds = max(1.0, (due - datetime.now()).total_seconds())
        when_text = due.strftime("%Y-%m-%d %H:%M")
        return {"delay_seconds": delay_seconds, "message": message, "when_text": when_text}

    def _telegram_memory_search(self, query: str) -> str:
        result = self.memory_search(query, limit=5)
        entries = result.get("results") or []
        if not entries:
            return "No matching memory. Even my tiny database shrugged."
        lines = [f"Memory results for: {query}"]
        for index, entry in enumerate(entries, start=1):
            category = entry.get("kind") or entry.get("category") or "memory"
            text = entry.get("text") or entry.get("content") or ""
            lines.append(f"{index}. id {entry.get('id')} [{category}] {text}")
        return "\n".join(lines)

    def _telegram_remember_command(self, text: str) -> str:
        fact = " ".join(str(text or "").strip().split())
        if not fact:
            return "Usage: /remember durable fact"
        result = self.memory.remember(
            {
                "upsert": [
                    {
                        "kind": "fact",
                        "content": fact[:800],
                        "tags": ["telegram", "explicit"],
                        "importance": 4,
                        "confidence": 0.95,
                        "review_status": "approved",
                    }
                ]
            },
            source="telegram:remember",
        )
        changed = len(result.get("added") or []) + len(result.get("updated") or [])
        return f"Memory saved: {fact}" if changed else "Memory unchanged. Apparently I already knew. Disturbing."

    def _telegram_forget_command(self, text: str) -> str:
        query = " ".join(str(text or "").strip().split())
        if not query:
            return "Usage: /forget memory id or search terms"
        if query.isdigit():
            return "Memory deleted." if self.memory.delete(int(query)) else "Memory id not found."
        matches = self.memory.search(query, limit=5)
        if not matches:
            return "No matching memory to forget."
        if len(matches) == 1:
            entry = matches[0]
            return f"Memory deleted: {entry.get('content')}" if self.memory.delete(int(entry.get("id"))) else "Memory delete failed."
        lines = ["Multiple matches. Use /forget <id>:"]
        for entry in matches:
            lines.append(f"- id {entry.get('id')}: {entry.get('content')}")
        return "\n".join(lines)

    def _telegram_review_command(self, text: str) -> str:
        command = " ".join(str(text or "").strip().split())
        lower = command.lower()
        if lower.startswith("approve "):
            token = lower.split(maxsplit=1)[1].strip()
            if token.isdigit() and self.memory.approve(int(token)):
                return f"Memory approved: {token}"
            return "Memory approval failed."
        if lower.startswith("reject ") or lower.startswith("delete "):
            token = lower.split(maxsplit=1)[1].strip()
            if token.isdigit() and self.memory.reject(int(token)):
                return f"Memory rejected: {token}"
            return "Memory rejection failed."
        pending = self.memory.pending_entries(limit=10)
        if not pending:
            return "No pending memory reviews."
        lines = ["Pending memory reviews:"]
        for entry in pending:
            lines.append(f"- id {entry.get('id')} [{entry.get('kind')}] {entry.get('content')}")
        lines.append("Use /review approve <id> or /review reject <id>.")
        return "\n".join(lines)

    def _telegram_brief_command(self, chat_id: int) -> str:
        if not settings.daily_loop_telegram_brief_enabled:
            return "Daily Telegram briefs are disabled."
        try:
            action = self.today_brief(source="telegram")
            reply = self._collect_spoken_text(action)
            if reply:
                return reply
        except Exception as exc:
            self._log.warning("Telegram brief failed: %s", exc)
        state = self.today_state()
        return self._format_today_state_text(state)

    def _telegram_todo_command(self, text: str) -> str:
        command = " ".join(str(text or "").strip().split())
        lower = command.lower()
        todos = self._load_todos()
        if not command or lower in {"list", "ls"}:
            return self._format_todos(todos)
        if lower.startswith("add "):
            title = command[4:].strip(" .")
            if not title:
                return "Usage: /todo add finish the thing"
            item = self.productivity.add_list_item("todo", title[:300], source="telegram")
            self.memory.remember(f"Open todo: {title}", source="telegram:todo")
            return f"Todo added: {title}"
        if lower.startswith("done "):
            token = lower[5:].strip()
            active = [item for item in todos if not item.get("done_at")]
            target = None
            try:
                index = int(token) - 1
                if 0 <= index < len(active):
                    target = active[index]
            except Exception:
                for item in active:
                    if token and token in str(item.get("title", "")).lower():
                        target = item
                        break
            if target is None:
                return "Todo not found."
            self.productivity.update_list_item(int(target.get("id")), {"done": True})
            return f"Todo done: {target.get('title')}"
        if lower == "clear":
            for item in todos:
                if not item.get("done_at"):
                    self.productivity.update_list_item(int(item.get("id")), {"done": True})
            return "Open todos cleared."
        if lower.startswith("plan"):
            open_titles = [str(item.get("title") or "") for item in todos if not item.get("done_at")]
            if not open_titles:
                return "No open todos. Suspiciously peaceful."
            prompt = (
                "Create a compact practical plan for these open todos. "
                "Return a direct Telegram-friendly answer, no JSON. "
                f"Todos: {open_titles}. Extra user note: {command[4:].strip()}"
            )
            action = self.think_and_respond(prompt, include_photo=False, source="telegram_text")
            return self._collect_spoken_text(action) or self._format_todos(todos)
        return "Usage: /todo, /todo add <task>, /todo done <number>, /todo clear, or /plan"
