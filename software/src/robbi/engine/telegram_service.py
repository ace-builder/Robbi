from __future__ import annotations
from .state_io import atomic_text

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from robbi.configuration.settings import TELEGRAM_STATE_FILE, settings
from robbi.execution import BoundedExecutor


TextHandler = Callable[[str, int, bool], str]
VoiceHandler = Callable[[bytes, int], dict]
StatusHandler = Callable[[], str]
PhotoHandler = Callable[[], Path | None]
PhotoQuestionHandler = Callable[[str, int], str]
ReminderHandler = Callable[[str], str]
MemoryHandler = Callable[[str], str]
TodoHandler = Callable[[str], str]
BriefHandler = Callable[[int], str]
ForgetHandler = Callable[[str], str]
MemoryReviewHandler = Callable[[str], str]
NoteHandler = Callable[[str], str]


@dataclass(slots=True)
class TelegramMessage:
    update_id: int
    chat_id: int
    text: str
    file_id: str = ""
    file_kind: str = ""
    file_name: str = ""
    first_name: str = ""
    username: str = ""


class TelegramService:
    def __init__(
        self,
        text_handler: TextHandler,
        voice_handler: VoiceHandler,
        status_handler: StatusHandler,
        photo_handler: PhotoHandler,
        photo_question_handler: PhotoQuestionHandler,
        reminder_handler: ReminderHandler,
        memory_handler: MemoryHandler,
        todo_handler: TodoHandler,
        note_handler: NoteHandler,
        notes_handler: NoteHandler,
        shopping_handler: NoteHandler,
        brief_handler: BriefHandler,
        remember_handler: MemoryHandler,
        forget_handler: ForgetHandler,
        review_handler: MemoryReviewHandler,
    ) -> None:
        self._log = logging.getLogger(__name__)
        self._text_handler = text_handler
        self._voice_handler = voice_handler
        self._status_handler = status_handler
        self._photo_handler = photo_handler
        self._photo_question_handler = photo_question_handler
        self._reminder_handler = reminder_handler
        self._memory_handler = memory_handler
        self._todo_handler = todo_handler
        self._note_handler = note_handler
        self._notes_handler = notes_handler
        self._shopping_handler = shopping_handler
        self._brief_handler = brief_handler
        self._remember_handler = remember_handler
        self._forget_handler = forget_handler
        self._review_handler = review_handler
        self._state_file = TELEGRAM_STATE_FILE
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._known_chats: set[int] = set()
        self._last_error = ""
        self._last_update_at: int | None = None
        self._messages = BoundedExecutor(max_workers=1, max_pending=4, thread_name_prefix='telegram-message')
        self._messages_closed = False
        self._stop_event = threading.Event()
        self._load_state()

    def start(self) -> None:
        if self._running:
            return
        if self._thread is not None and self._thread.is_alive():
            self._last_error = 'Previous Telegram poll is still stopping; retry later.'
            return
        if not settings.telegram_enabled:
            return
        if not settings.telegram_bot_token:
            self._last_error = "TELEGRAM_BOT_TOKEN is not configured"
            return
        if not self._allowed_chat_ids():
            self._last_error = "Telegram allowlist is empty; run robbi-setup and add your private user ID"
            return
        self._running = True
        if self._messages_closed:
            self._messages = BoundedExecutor(max_workers=1, max_pending=4, thread_name_prefix='telegram-message')
            self._messages_closed = False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="telegram")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        self._messages.shutdown(wait=False, cancel_futures=True)
        self._messages_closed = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def restart(self) -> None:
        self.stop()
        self.start()

    def status(self) -> dict:
        return {
            "enabled": settings.telegram_enabled,
            "configured": bool(settings.telegram_bot_token),
            "running": self._running and self._thread is not None and self._thread.is_alive(),
            "known_chat_count": len(self._known_chats),
            "allowed_chat_ids": sorted(self._allowed_chat_ids()),
            "last_update_at": self._last_update_at,
            "last_error": self._last_error,
        }

    def notify_text(self, text: str, *, disable_notification: bool = False) -> None:
        message = str(text or "").strip()
        if not message:
            return
        for chat_id in self._notification_chat_ids():
            self.send_message(chat_id, message, disable_notification=disable_notification)

    def send_message(self, chat_id: int, text: str, *, disable_notification: bool = False) -> bool:
        if not self._is_allowed(chat_id):
            return False
        payload = {
            "chat_id": int(chat_id),
            "text": self._trim_message(text),
            "disable_notification": bool(disable_notification),
        }
        ok, _ = self._api("sendMessage", payload)
        return ok

    def send_photo(self, chat_id: int, photo_path: Path, caption: str = "") -> bool:
        if not self._is_allowed(chat_id) or not settings.telegram_allow_photos:
            return False
        if not photo_path or not Path(photo_path).exists():
            return False
        fields = {
            "chat_id": str(int(chat_id)),
            "caption": caption[:1024],
        }
        files = {"photo": Path(photo_path)}
        ok, _ = self._api_multipart("sendPhoto", fields, files)
        return ok

    def send_audio_bytes(self, chat_id: int, wav_bytes: bytes, caption: str = "") -> bool:
        if not self._is_allowed(chat_id):
            return False
        if not wav_bytes:
            return False
        fields = {
            "chat_id": str(int(chat_id)),
            "caption": caption[:1024],
        }
        files = {"audio": ("robbi-reply.wav", wav_bytes, "audio/wav")}
        ok, _ = self._api_multipart("sendAudio", fields, files)
        return ok

    def _loop(self) -> None:
        self._log.info("Telegram service started")
        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    message = self._parse_update(update)
                    if message is None:
                        continue
                    self._offset = max(self._offset, message.update_id + 1)
                    self._last_update_at = int(time.time())
                    self._save_state()
                    if not self._is_allowed(message.chat_id):
                        continue
                    self._remember_chat(message.chat_id)
                    if not self._running:
                        break
                    try:
                        self._messages.submit(self._handle_message, message)
                    except RuntimeError:
                        self._log.warning('Telegram queue full; dropped stale incoming command')
                self._last_error = ""
            except Exception as exc:
                self._last_error = str(exc)
                self._log.warning("Telegram polling failed: %s", exc)
                self._stop_event.wait(max(1.0, settings.telegram_poll_interval_s))
            self._stop_event.wait(max(0.2, settings.telegram_poll_interval_s))
        self._log.info("Telegram service stopped")

    def _get_updates(self) -> list[dict]:
        payload = {
            "offset": self._offset,
            "timeout": min(10, max(1, int(settings.network_request_timeout_s) - 2)),
            "allowed_updates": ["message"],
        }
        ok, data = self._api("getUpdates", payload, timeout=settings.network_request_timeout_s)
        if not ok:
            raise RuntimeError(str(data.get("description") or data))
        result = data.get("result")
        return result if isinstance(result, list) else []

    def _handle_message(self, message: TelegramMessage) -> None:
        if not self._running or not self._is_allowed(message.chat_id):
            return
        text = message.text.strip()
        if not text:
            if message.file_id:
                self._handle_audio_message(message)
            return
        try:
            if text.startswith("/") and settings.telegram_allow_commands:
                self._handle_command(message, text)
                return
            self.send_message(message.chat_id, "Thinking. Tiny robot, unfortunately not tiny latency.")
            reply = self._text_handler(text, message.chat_id, False)
            self.send_message(message.chat_id, reply or "Done.")
        except Exception as exc:
            self._log.exception("Telegram message failed")
            self.send_message(message.chat_id, f"Telegram handling failed: {exc}")

    def _handle_audio_message(self, message: TelegramMessage) -> None:
        if not settings.telegram_allow_voice:
            self.send_message(message.chat_id, "Voice/audio input is disabled in Robbi's Telegram config.")
            return
        try:
            self.send_message(message.chat_id, "Transcribing. Finally, a way to mishear you remotely.")
            audio_bytes = self._download_file_bytes(message.file_id)
            if not audio_bytes:
                self.send_message(message.chat_id, "Could not download that audio file.")
                return
            result = self._voice_handler(audio_bytes, message.chat_id)
            transcript = str(result.get("transcript") or "").strip()
            reply = str(result.get("reply") or "").strip()
            if transcript:
                self.send_message(message.chat_id, f"You said: {transcript}")
            self.send_message(message.chat_id, reply or "Done.")
            audio_reply = result.get("audio_reply")
            if settings.telegram_send_audio_replies and isinstance(audio_reply, (bytes, bytearray)) and audio_reply:
                self.send_audio_bytes(message.chat_id, bytes(audio_reply), caption="Robbi audio reply")
        except Exception as exc:
            self._log.exception("Telegram audio message failed")
            self.send_message(message.chat_id, f"Audio handling failed: {exc}")

    def _handle_command(self, message: TelegramMessage, text: str) -> None:
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        arg = arg.strip()
        if command in {"/start", "/help"}:
            self.send_message(message.chat_id, self._help_text(message.chat_id))
            return
        if command == "/id":
            self.send_message(message.chat_id, f"Chat ID: {message.chat_id}")
            return
        if command == "/status":
            self.send_message(message.chat_id, self._status_handler())
            return
        if command == "/photo":
            if not settings.telegram_allow_photos:
                self.send_message(message.chat_id, "Photos are disabled in Robbi's Telegram config.")
                return
            photo = self._photo_handler()
            if photo is None:
                self.send_message(message.chat_id, "Camera unavailable.")
            elif not self.send_photo(message.chat_id, photo, "Robbi camera snapshot"):
                self.send_message(message.chat_id, f"Photo capture worked, upload failed: {photo}")
            if arg:
                self.send_message(message.chat_id, "Looking at it. The camera and I are both doing our best.")
                self.send_message(message.chat_id, self._photo_question_handler(arg, message.chat_id))
            return
        if command == "/memory":
            if not arg:
                self.send_message(message.chat_id, "Usage: /memory search terms")
                return
            self.send_message(message.chat_id, self._memory_handler(arg))
            return
        if command == "/brief":
            self.send_message(message.chat_id, self._brief_handler(message.chat_id))
            return
        if command == "/remember":
            if not arg:
                self.send_message(message.chat_id, "Usage: /remember durable fact")
                return
            self.send_message(message.chat_id, self._remember_handler(arg))
            return
        if command == "/forget":
            if not arg:
                self.send_message(message.chat_id, "Usage: /forget memory id or search terms")
                return
            self.send_message(message.chat_id, self._forget_handler(arg))
            return
        if command == "/review":
            self.send_message(message.chat_id, self._review_handler(arg))
            return
        if command == "/remind":
            if not arg:
                self.send_message(message.chat_id, "Usage: /remind in 10 minutes check the oven")
                return
            self.send_message(message.chat_id, self._reminder_handler(arg))
            return
        if command == "/todo":
            self.send_message(message.chat_id, self._todo_handler(arg))
            return
        if command == "/note":
            if not arg:
                self.send_message(message.chat_id, "Usage: /note quick note text")
                return
            self.send_message(message.chat_id, self._note_handler(arg))
            return
        if command == "/notes":
            self.send_message(message.chat_id, self._notes_handler(arg))
            return
        if command == "/shopping":
            self.send_message(message.chat_id, self._shopping_handler(arg))
            return
        if command == "/plan":
            self.send_message(message.chat_id, self._todo_handler(f"plan {arg}".strip()))
            return
        if command == "/say":
            if not arg:
                self.send_message(message.chat_id, "Usage: /say text Robbi should speak aloud")
                return
            self.send_message(message.chat_id, self._text_handler(arg, message.chat_id, True) or "Said.")
            return
        self.send_message(message.chat_id, "Unknown command. Use /help. Astonishingly, guessing commands is not implemented.")

    def _help_text(self, chat_id: int) -> str:
        return (
            "Robbi Telegram commands:\n"
            f"/id - show this chat id ({chat_id})\n"
            "/status - compact robot status\n"
            "/photo - send a camera snapshot\n"
            "/photo <question> - take a photo and answer using vision\n"
            "/brief - today's reminders, todos, memory context, next action\n"
            "/memory <query> - search Robbi memory\n"
            "/remember <fact> - store a durable memory immediately\n"
            "/forget <id|query> - delete a memory\n"
            "/review [approve|reject <id>] - review extracted memories\n"
            "/remind <when> <what> - set a reminder\n"
            "/todo [add|done|clear] - manage a tiny task list\n"
            "/note <text> - save a structured note\n"
            "/notes - list recent notes\n"
            "/shopping [add|done] - manage shopping list\n"
            "/plan - get a short plan from current todos\n"
            "/say <text> - make Robbi speak aloud\n"
            "Any normal message goes through Robbi's LLM path and replies here. Voice notes work too."
        )

    def _parse_update(self, update: dict) -> TelegramMessage | None:
        if not isinstance(update, dict):
            return None
        raw_message = update.get("message")
        if not isinstance(raw_message, dict):
            return None
        text = raw_message.get("text")
        file_id = ""
        file_kind = ""
        file_name = ""
        if not isinstance(text, str):
            text = ""
        voice = raw_message.get("voice")
        audio = raw_message.get("audio")
        if isinstance(voice, dict):
            file_id = str(voice.get("file_id") or "")
            file_kind = "voice"
            file_name = "voice.ogg"
        elif isinstance(audio, dict):
            file_id = str(audio.get("file_id") or "")
            file_kind = "audio"
            file_name = str(audio.get("file_name") or "audio")
        if not text.strip() and not file_id:
            return None
        chat = raw_message.get("chat") or {}
        if not isinstance(chat, dict) or chat.get("id") is None:
            return None
        sender = raw_message.get("from") or {}
        return TelegramMessage(
            update_id=int(update.get("update_id") or 0),
            chat_id=int(chat.get("id")),
            text=text,
            file_id=file_id,
            file_kind=file_kind,
            file_name=file_name,
            first_name=str(sender.get("first_name") or ""),
            username=str(sender.get("username") or ""),
        )

    def _download_file_bytes(self, file_id: str) -> bytes:
        ok, data = self._api("getFile", {"file_id": file_id}, timeout=15)
        if not ok:
            raise RuntimeError(str(data.get("description") or data))
        file_path = ((data.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            return b""
        url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
        with urllib.request.urlopen(url, timeout=45) as response:
            return response.read()

    def _api(self, method: str, payload: dict, timeout: int = 15) -> tuple[bool, dict]:
        if not settings.telegram_enabled or not settings.telegram_bot_token:
            return False, {'description': 'Telegram is disabled or not configured'}
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
                return bool(data.get("ok")), data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except Exception:
                data = {"description": str(exc)}
            return False, data

    def _api_multipart(self, method: str, fields: dict[str, str], files: dict) -> tuple[bool, dict]:
        if not settings.telegram_enabled or not settings.telegram_bot_token:
            return False, {'description': 'Telegram is disabled or not configured'}
        boundary = f"----robbi-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        for name, path in files.items():
            mime_type = "application/octet-stream"
            if isinstance(path, tuple):
                filename, data, mime_type = path
                file_bytes = bytes(data)
            else:
                path = Path(path)
                filename = path.name
                file_bytes = path.read_bytes()
                if path.suffix.lower() in {".jpg", ".jpeg"}:
                    mime_type = "image/jpeg"
                elif path.suffix.lower() == ".wav":
                    mime_type = "audio/wav"
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode("utf-8")
            )
            chunks.append(file_bytes)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                return bool(data.get("ok")), data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except Exception:
                data = {"description": str(exc)}
            return False, data

    def _allowed_chat_ids(self) -> set[int]:
        raw = str(settings.telegram_allowed_chat_ids or "").strip()
        if not raw:
            return set()
        values: set[int] = set()
        for part in re.split(r"[,;\s]+", raw):
            if not part:
                continue
            try:
                values.add(int(part))
            except ValueError:
                continue
        return values

    def _is_allowed(self, chat_id: int) -> bool:
        allowed = self._allowed_chat_ids()
        return int(chat_id) in allowed

    def _notification_chat_ids(self) -> list[int]:
        allowed = self._allowed_chat_ids()
        return sorted(allowed)

    def _remember_chat(self, chat_id: int) -> None:
        with self._lock:
            self._known_chats.add(int(chat_id))

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        try:
            self._offset = int(raw.get("offset") or 0)
            self._known_chats = {int(value) for value in raw.get("known_chats", [])}
        except Exception:
            self._offset = 0
            self._known_chats = set()

    def _save_state(self) -> None:
        with self._lock:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(self._state_file, json.dumps(
                    {
                        "offset": self._offset,
                        "known_chats": sorted(self._known_chats),
                        "last_update_at": self._last_update_at,
                    },
                    indent=2,
                ))

    def _trim_message(self, text: str) -> str:
        text = str(text or "").strip()
        return text[:3900] + "\n..." if len(text) > 3900 else text
