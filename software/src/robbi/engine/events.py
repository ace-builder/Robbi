from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from robbi.configuration.settings import EVENTS_DB_FILE, LOG_DIR, settings


@dataclass(slots=True)
class RobotEvent:
    event_type: str
    payload: dict
    source: str = "robot"
    ts: int = 0

    def normalized(self) -> "RobotEvent":
        if not self.ts:
            self.ts = int(time.time())
        if not isinstance(self.payload, dict):
            self.payload = {"value": self.payload}
        return self


class EventBus:
    """Non-blocking event capture for autonomy and debugging."""

    def __init__(self, db_path: Path = EVENTS_DB_FILE) -> None:
        self._log = logging.getLogger(__name__)
        self._db_path = db_path
        self._jsonl_path = LOG_DIR / "events.jsonl"
        self._queue: queue.Queue[RobotEvent | None] = queue.Queue(maxsize=max(10, settings.events_max_queue_size))
        self._running = False
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._last_drop_log = 0.0
        self._recent_lock = threading.Lock()
        self._recent: list[dict] = []

    def start(self) -> None:
        if not settings.events_enabled or self._running:
            return
        self._ensure_db()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="event-bus")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def publish(self, event_type: str, payload: dict | None = None, source: str = "robot", critical: bool = False) -> None:
        if not settings.events_enabled:
            return
        event = RobotEvent(event_type=event_type, payload=payload or {}, source=source).normalized()
        with self._recent_lock:
            self._recent.append(asdict(event))
            self._recent = self._recent[-100:]
        try:
            if critical:
                self._queue.put(event, timeout=0.05)
            else:
                self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            now = time.time()
            if now - self._last_drop_log > 30:
                self._last_drop_log = now
                self._log.warning("Event queue full; dropped %s non-critical events", self._dropped)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._recent_lock:
            return list(self._recent[-max(1, limit) :])

    def stats(self) -> dict:
        return {
            "enabled": settings.events_enabled,
            "queue_size": self._queue.qsize(),
            "dropped": self._dropped,
            "db_path": str(self._db_path),
            "jsonl_path": str(self._jsonl_path),
            "recent": self.recent(limit=8),
        }

    def _run(self) -> None:
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                self._write_event(event)
            except Exception as exc:
                self._log.warning("Failed to persist event %s: %s", event.event_type, exc)

    def _ensure_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts DESC)")

    def _write_event(self, event: RobotEvent) -> None:
        payload_json = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        if len(payload_json.encode('utf-8')) > 16384:
            event.payload = {'truncated': True, 'summary': payload_json[:4000]}
            payload_json = json.dumps(event.payload, ensure_ascii=False)
        with closing(sqlite3.connect(self._db_path, timeout=0.2)) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO events(ts, event_type, source, payload_json) VALUES (?, ?, ?, ?)",
                (event.ts, event.event_type, event.source, payload_json),
            )
            if cursor.lastrowid % 128 == 0:
                conn.execute('DELETE FROM events WHERE id <= ?', (cursor.lastrowid - 2000,))
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            if self._jsonl_path.exists() and self._jsonl_path.stat().st_size >= 2 * 1024 * 1024:
                backup = self._jsonl_path.with_suffix('.jsonl.1')
                self._jsonl_path.replace(backup)
            with self._jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            pass
