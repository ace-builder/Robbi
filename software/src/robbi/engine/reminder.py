from __future__ import annotations
from .state_io import atomic_text

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class ReminderItem:
    id: str
    message: str
    due_at: int
    created_at: int
    delivered_at: int | None = None
    canceled_at: int | None = None

    @property
    def active(self) -> bool:
        return self.delivered_at is None and self.canceled_at is None

    def status(self) -> str:
        if self.canceled_at is not None:
            return "canceled"
        if self.delivered_at is not None:
            return "delivered"
        return "scheduled"


class ReminderManager:
    def __init__(self, state_file: Path, due_callback, can_present_callback) -> None:
        self._state_file = Path(state_file)
        self._due_callback = due_callback
        self._can_present_callback = can_present_callback
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._items: list[ReminderItem] = []
        self._load()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reminders")
        self._thread.start()

    def shutdown(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def add(self, message: str, delay_s: float) -> ReminderItem:
        now = int(time.time())
        due_at = now + max(1, int(round(delay_s)))
        item = ReminderItem(
            id=f"rem-{uuid.uuid4().hex[:16]}",
            message=str(message or "").strip(),
            due_at=due_at,
            created_at=now,
        )
        with self._lock:
            self._items.append(item)
            self._save()
        return item

    def cancel(self, reminder_id: str | None = None) -> ReminderItem | None:
        with self._lock:
            active = [item for item in self._items if item.active]
            if not active:
                return None
            target: ReminderItem | None = None
            if reminder_id:
                for item in active:
                    if item.id == reminder_id:
                        target = item
                        break
            if target is None and reminder_id:
                return None
            if target is None:
                target = sorted(active, key=lambda item: item.due_at)[0]
            target.canceled_at = int(time.time())
            self._save()
            return target

    def next_active(self) -> ReminderItem | None:
        with self._lock:
            active = [item for item in self._items if item.active]
        if not active:
            return None
        return sorted(active, key=lambda item: item.due_at)[0]

    def list_items(self, include_completed: bool = False, limit: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._items)
        if not include_completed:
            items = [item for item in items if item.active]
        items = sorted(items, key=lambda item: (item.due_at, item.created_at))
        return [self._asdict(item) for item in items[: max(1, limit)]]

    def status(self) -> dict:
        next_item = self.next_active()
        now = time.time()
        return {
            "active_count": len([item for item in self._items if item.active]),
            "next": self._asdict(next_item) if next_item is not None else None,
            "next_due_in_s": max(0.0, round(next_item.due_at - now, 1)) if next_item is not None else None,
        }

    def _loop(self) -> None:
        while self._running:
            time.sleep(0.5)
            if not self._can_present_callback():
                continue
            due_item = self._next_due_ready()
            if due_item is None:
                continue
            try:
                self._due_callback(due_item)
            finally:
                with self._lock:
                    due_item.delivered_at = int(time.time())
                    self._save()

    def _next_due_ready(self) -> ReminderItem | None:
        now = int(time.time())
        with self._lock:
            due = [item for item in self._items if item.active and item.due_at <= now]
        if not due:
            return None
        return sorted(due, key=lambda item: item.due_at)[0]

    def _load(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        if not self._state_file.exists():
            self._items = []
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            self._items = []
            return
        items: list[ReminderItem] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                items.append(
                    ReminderItem(
                        id=str(entry.get("id") or ""),
                        message=str(entry.get("message") or "").strip(),
                        due_at=int(entry.get("due_at") or 0),
                        created_at=int(entry.get("created_at") or 0),
                        delivered_at=int(entry["delivered_at"]) if entry.get("delivered_at") is not None else None,
                        canceled_at=int(entry["canceled_at"]) if entry.get("canceled_at") is not None else None,
                    )
                )
            except Exception:
                continue
        self._items = [item for item in items if item.id and item.message and item.due_at > 0]

    def _save(self) -> None:
        atomic_text(self._state_file, json.dumps([self._asdict(item) for item in self._items], ensure_ascii=False, indent=2))

    def _asdict(self, item: ReminderItem | None) -> dict | None:
        if item is None:
            return None
        data = asdict(item)
        data["status"] = item.status()
        return data
