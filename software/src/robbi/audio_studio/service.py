"""Durable, bounded audio drafts and kept clips, independent of the robot loop.

One lazily started worker, no automatic retries of paid requests. SQLite owns
metadata; only opaque IDs become paths. Interrupted jobs are never replayed.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

from ..errors import BusyError, UnavailableError, ValidationError
from ..execution import BoundedExecutor
from ..storage import write_json
from .provider import DEFAULT_VOICE, MAX_TEXT, MODEL, ElevenLabs, ProviderError

ACTIVE = {"queued", "generating"}
PLAYABLE = {"draft", "kept"}


class AudioStudio:
    def __init__(self, data_dir, *, provider=None, max_storage_mb=128,
                 max_clip_mb=16, history_limit=500):
        self.directory = Path(data_dir) / "audio-studio"
        self.provider = provider or ElevenLabs()
        self.max_storage = int(max_storage_mb * 1024 * 1024)
        self.max_clip = int(max_clip_mb * 1024 * 1024)
        self.history_limit = int(history_limit)
        self._lock = threading.RLock()
        self._voice_lock = threading.Lock()
        self._executor = BoundedExecutor(1, 0, "audio-studio")
        self._initialized = False
        self._stopped = False
        self._active = None
        self._cancel = threading.Event()
        self._voice_cache = None

    @contextmanager
    def _db(self):
        try:
            with closing(sqlite3.connect(self.directory / "library.sqlite3", timeout=2)) as db:
                db.row_factory = sqlite3.Row
                with db:
                    yield db
        except sqlite3.Error:
            raise UnavailableError("The audio library could not be read or saved. Check disk space and the audio-studio data directory.") from None

    def _ensure(self):
        # Caller holds _lock. Lazy initialization keeps missing/corrupt storage
        # and optional provider dependencies from breaking robot startup.
        if self._initialized:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, created REAL NOT NULL, text TEXT NOT NULL,
                voice_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                bytes INTEGER NOT NULL DEFAULT 0, parent_id TEXT)""")
            db.execute("CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("UPDATE clips SET status='failed', bytes=0, error=? WHERE status IN ('queued','generating')",
                       ("Robbi restarted during generation. The request was not retried; check ElevenLabs before regenerating.",))
            known = {row["id"] for row in db.execute("SELECT id FROM clips WHERE status IN ('draft','kept')")}
            for clip_id in known:
                if not self._path(clip_id).is_file():
                    db.execute("UPDATE clips SET status='failed',bytes=0,error=? WHERE id=?",
                               ("The audio file is missing. Regenerate this clip to recover it.", clip_id))
        # Only our opaque generated filenames are eligible for recovery cleanup.
        for path in self.directory.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}\.(part|mp3)", path.name):
                if path.suffix == ".part" or path.stem not in known:
                    path.unlink(missing_ok=True)
        os.chmod(self.directory / "library.sqlite3", 0o600)
        self._initialized = True

    def _path(self, clip_id, suffix=".mp3"):
        if not isinstance(clip_id, str) or not re.fullmatch(r"[0-9a-f]{32}", clip_id):
            raise ValidationError("Invalid clip ID")
        return self.directory / (clip_id + suffix)

    def _key(self):
        path = self.directory / "credential.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))["api_key"]
            except (ValueError, KeyError, TypeError):
                raise UnavailableError("The saved ElevenLabs credential is unreadable. Replace it in Connection settings.") from None
        return os.getenv("ELEVENLABS_API_KEY", "")

    @staticmethod
    def _voice(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValidationError("Enter a valid ElevenLabs voice ID (letters, digits, underscores or hyphens)")
        return value

    def settings(self):
        with self._lock:
            self._ensure()
            with self._db() as db:
                row = db.execute("SELECT value FROM preferences WHERE key='voice_id'").fetchone()
            return {"voice_id": row[0] if row else DEFAULT_VOICE, "model": MODEL,
                    "api_key_configured": bool(self._key()), "max_text": MAX_TEXT,
                    "max_storage_bytes": self.max_storage, "max_clip_bytes": self.max_clip,
                    "history_limit": self.history_limit}

    def configure(self, payload):
        if not payload or set(payload) - {"voice_id", "api_key"}:
            raise ValidationError("Supply voice_id and/or api_key")
        voice = self._voice(payload["voice_id"]) if "voice_id" in payload else None
        key = payload.get("api_key")
        if "api_key" in payload and (not isinstance(key, str) or not 1 <= len(key) <= 512 or
                                     any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise ValidationError("Enter a nonempty API key without whitespace")
        with self._lock:
            self._ensure()
            if key is not None:
                write_json(self.directory / "credential.json", {"api_key": key})
                os.chmod(self.directory / "credential.json", 0o600)
                self._voice_cache = None
            if voice is not None:
                with self._db() as db:
                    db.execute("INSERT OR REPLACE INTO preferences VALUES ('voice_id',?)", (voice,))
            return self.settings()

    def voices(self, page_token=""):
        if not isinstance(page_token, str) or len(page_token) > 2048:
            raise ValidationError("Invalid voice page token")
        with self._lock:
            self._ensure()
            key = self._key()
            if not key:
                raise ValidationError("Add your ElevenLabs API key in Connection settings first.")
            if not page_token and self._voice_cache and time.monotonic() < self._voice_cache[0]:
                return self._voice_cache[1]
        if not self._voice_lock.acquire(blocking=False):
            raise BusyError("The voice list is already loading")
        try:
            result = self.provider.voices(key, page_token)
            with self._lock:
                if not page_token and key == self._key():
                    self._voice_cache = (time.monotonic() + 60, result)
            return result
        finally:
            self._voice_lock.release()

    @staticmethod
    def _public(row):
        result = dict(row)
        result["audio_url"] = f"/api/audio-studio/clips/{result['id']}/audio" if result["status"] in PLAYABLE else None
        return result

    def history(self, *, offset=0, limit=20, status="all"):
        if status not in {"all", "kept", "draft"} or not 0 <= offset <= self.history_limit or not 1 <= limit <= 50:
            raise ValidationError("Invalid history filter or page")
        with self._lock:
            self._ensure()
            where, params = ("", ()) if status == "all" else (" WHERE status=?", (status,))
            with self._db() as db:
                total = db.execute("SELECT COUNT(*) FROM clips" + where, params).fetchone()[0]
                used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM clips").fetchone()[0]
                rows = db.execute("SELECT * FROM clips" + where + " ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
                                  (*params, limit, offset)).fetchall()
            return {"clips": [self._public(r) for r in rows], "total": total, "offset": offset,
                    "storage_bytes": used, "active_id": self._active}

    def get(self, clip_id):
        self._path(clip_id)
        with self._lock:
            self._ensure()
            with self._db() as db:
                row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
            return self._public(row) if row else None

    def generate(self, text, voice_id, *, parent_id=None):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValidationError(f"Enter between 1 and {MAX_TEXT:,} characters of text")
        voice_id = self._voice(voice_id)
        with self._lock:
            self._ensure()
            if self._stopped:
                raise UnavailableError("Audio studio is stopping")
            if self._active:
                raise BusyError("An audio generation is already in progress. Wait for it to finish.")
            key = self._key()
            if not key:
                raise ValidationError("Add your ElevenLabs API key in Connection settings first.")
            with self._db() as db:
                count, used = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM clips").fetchone()
                if count >= self.history_limit:
                    raise ValidationError("History is full. Delete old entries before generating another clip.")
                # Reserve enough room for a full clip before a paid request.
                if self.max_storage - used < self.max_clip:
                    raise ValidationError("Clip storage is nearly full. Discard or delete old audio before generating.")
                if shutil.disk_usage(self.directory).free < self.max_clip + 16 * 1024 * 1024:
                    raise ValidationError("Robbi's disk is nearly full. Free some space before generating audio.")
                clip_id = uuid.uuid4().hex
                db.execute("INSERT INTO clips(id,created,text,voice_id,model,status,parent_id) VALUES (?,?,?,?,?,'queued',?)",
                           (clip_id, time.time(), text, voice_id, MODEL, parent_id))
            self._active = clip_id
            self._cancel = threading.Event()
            try:
                self._executor.submit(self._generate, clip_id, key, text, voice_id, self._cancel)
            except Exception:
                self._active = None
                with self._db() as db:
                    db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
                raise
            return self.get(clip_id)

    def regenerate(self, clip_id):
        with self._lock:
            clip = self.get(clip_id)
            if not clip:
                raise ValidationError("This generation no longer exists")
            return self.generate(clip["text"], clip["voice_id"], parent_id=clip_id)

    def _generate(self, clip_id, key, text, voice_id, cancelled):
        temporary = self._path(clip_id, ".part")
        final = self._path(clip_id)
        try:
            with self._lock:
                if cancelled.is_set():
                    return
                with self._db() as db:
                    db.execute("UPDATE clips SET status='generating' WHERE id=?", (clip_id,))
            self.provider.generate(key=key, text=text, voice_id=voice_id, destination=temporary,
                                   max_bytes=self.max_clip, cancelled=cancelled)
            with self._lock:
                if cancelled.is_set() or not self.get(clip_id):
                    return
                size = temporary.stat().st_size
                if not 0 < size <= self.max_clip:
                    raise ProviderError("The audio file is empty or exceeds the clip limit")
                os.replace(temporary, final)
                with self._db() as db:
                    db.execute("UPDATE clips SET status='draft',bytes=? WHERE id=?", (size, clip_id))
        except Exception as error:
            # Never expose SDK/request exceptions, which may include credentials.
            message = str(error) if isinstance(error, ProviderError) else "Could not finish or save the clip. Check free disk space and your connection, then try again."
            with self._lock:
                final.unlink(missing_ok=True)
                with self._db() as db:
                    db.execute("UPDATE clips SET status='failed',bytes=0,error=? WHERE id=? AND status IN ('queued','generating')",
                               (message, clip_id))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                with self._lock:
                    if self._active == clip_id:
                        self._active = None

    def keep(self, clip_id, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or any(ord(c) < 32 for c in name):
            raise ValidationError("Give the clip a name between 1 and 100 characters")
        with self._lock:
            clip = self.get(clip_id)
            if not clip or clip["status"] not in PLAYABLE:
                raise ValidationError("Only finished audio can be kept or renamed")
            with self._db() as db:
                db.execute("UPDATE clips SET status='kept',name=? WHERE id=?", (name.strip(), clip_id))
            return self.get(clip_id)

    def discard(self, clip_id, *, delete=False):
        with self._lock:
            if not self.get(clip_id):
                return
            if self._active == clip_id:
                self._cancel.set()
            self._path(clip_id).unlink(missing_ok=True)
            with self._db() as db:
                if delete:
                    db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
                else:
                    db.execute("UPDATE clips SET status='discarded',bytes=0,name='',error='' WHERE id=?", (clip_id,))

    def audio(self, clip_id):
        with self._lock:
            clip = self.get(clip_id)
            if not clip or clip["status"] not in PLAYABLE or not self._path(clip_id).is_file():
                return None
            # Open under lock so deletion cannot race between validation and open.
            return self._path(clip_id).open("rb"), clip

    def stop(self):
        with self._lock:
            self._stopped = True
            self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
