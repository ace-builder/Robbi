"""Notes, lists and research use cases, independent of the robot controller.

The composition root supplies storage, search, event publication and scheduling.
Importing or testing this module requires no robot configuration or hardware.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Protocol
from .productivity_undo import change_scope
from robbi.errors import ValidationError


class ProductivityRepository(Protocol):
    def summary(self) -> dict: ...
    def list_notes(self, limit: int = 50, include_deleted: bool = False) -> list[dict]: ...
    def create_note(self, *, title: str, raw_text: str, summary: str, structured_json: dict, source: str) -> dict: ...
    def update_note(self, note_id: int, fields: dict) -> dict | None: ...
    def delete_note(self, note_id: int) -> bool: ...
    def list_items(self, list_name: str | None = None, include_done: bool = True, limit: int = 200) -> list[dict]: ...
    def add_list_item(self, *, list_name: str, title: str, quantity: str, category: str, source: str) -> dict: ...
    def update_list_item(self, item_id: int, fields: dict) -> dict | None: ...
    def delete_list_item(self, item_id: int) -> bool: ...
    def list_research_notes(self, limit: int = 50) -> list[dict]: ...
    def list_jobs(self, limit: int = 50) -> list[dict]: ...
    def create_research_note(self, *, title: str, query: str, source: str) -> dict: ...
    def get_job(self, job_id: int) -> dict | None: ...
    def update_job(self, job_id: int, **fields) -> dict | None: ...
    def update_research_note(self, note_id: int, fields: dict) -> dict | None: ...


class SearchPort(Protocol):
    def search(self, query: str, *, max_results: int, search_depth: str) -> dict: ...


class WorkScheduler(Protocol):
    def submit(self, callback: Callable, *args) -> object: ...


@dataclass(frozen=True, slots=True)
class ResearchOptions:
    max_results: int = 5
    search_depth: str = "basic"


class ProductivityService:
    change_scope = staticmethod(change_scope)

    def history(self, source=None):
        callback = getattr(self._store, "change_history", None)
        return callback(source) if callback else []

    def last_change(self, source=None):
        return next(iter(self.history(source)), None)

    def undo(self, change_id, *, source=None):
        callback = getattr(self._store, "undo_change", None)
        if callback is None:
            raise ValidationError("This storage provider does not support undo.")
        result = callback(change_id, source=source)
        self._emit("tool.undone", {"id": change_id})
        return result

    def __init__(
        self,
        repository: ProductivityRepository,
        *,
        search: SearchPort,
        scheduler: WorkScheduler,
        publish_event: Callable[[str, dict], None],
        research_options: Callable[[], ResearchOptions] = ResearchOptions,
        clock: Callable[[], float] = time.time,
    ):
        self._store = repository
        self._search = search
        self._scheduler = scheduler
        self._publish = publish_event
        self._research_options = research_options
        self._clock = clock
        self._log = logging.getLogger(__name__)

    def _emit(self, kind: str, payload: dict) -> None:
        # A diagnostics failure must not make a committed write look unsuccessful
        # to a caller that may then retry the operation.
        try:
            self._publish(kind, payload)
        except Exception:
            self._log.exception("Could not publish productivity event %s", kind)

    def snapshot(self) -> dict:
        return {
            "summary": self._store.summary(),
            "notes": self._store.list_notes(limit=80),
            "todos": self._store.list_items("todo", include_done=True),
            "shopping": self._store.list_items("shopping", include_done=True),
            "research_notes": self._store.list_research_notes(limit=80),
            "jobs": self._store.list_jobs(limit=40),
        }

    def create_note(self, payload: dict) -> dict:
        note = self._store.create_note(
            title=str(payload.get("title") or ""),
            raw_text=str(payload.get("raw_text") or payload.get("text") or ""),
            summary=str(payload.get("summary") or ""),
            structured_json=payload.get("structured_json") or payload.get("structured") or {},
            source=str(payload.get("source") or "ui"),
        )
        self._emit("tool.called", {"tool": "create_note", "id": note.get("id")})
        return note

    def update_note(self, note_id: int, payload: dict) -> dict | None:
        return self._store.update_note(note_id, payload)

    def delete_note(self, note_id: int) -> bool:
        return self._store.delete_note(note_id)

    def add_list_item(self, payload: dict) -> dict:
        item = self._store.add_list_item(
            list_name=str(payload.get("list_name") or payload.get("list") or "todo"),
            title=str(payload.get("title") or payload.get("text") or ""),
            quantity=str(payload.get("quantity") or ""),
            category=str(payload.get("category") or ""),
            source=str(payload.get("source") or "ui"),
        )
        self._emit("tool.called", {"tool": "add_list_item", "id": item.get("id"), "list": item.get("list_name")})
        return item

    def update_list_item(self, item_id: int, payload: dict) -> dict | None:
        return self._store.update_list_item(item_id, payload)

    def delete_list_item(self, item_id: int) -> bool:
        return self._store.delete_list_item(item_id)

    def create_research_note(self, payload: dict) -> dict:
        note = self._store.create_research_note(
            title=str(payload.get("title") or ""),
            query=str(payload.get("query") or payload.get("text") or ""),
            source=str(payload.get("source") or "ui"),
        )
        job_id = int(note.get("job_id") or 0)
        if job_id:
            try:
                self._scheduler.submit(self.run_research_job, job_id)
            except Exception as error:
                # Admission can fail after the store has committed both rows.
                # Mark both failed instead of leaving an unstartable pending job.
                self._fail_research(job_id, int(note.get("id") or 0), error)
                raise
        self._emit("tool.called", {"tool": "create_research_note", "id": note.get("id"), "job_id": job_id})
        return note

    def _fail_research(self, job_id: int, note_id: int, error: Exception) -> None:
        completed_at = int(self._clock())
        if note_id:
            self._store.update_research_note(note_id, {"status": "failed", "completed_at": completed_at})
        self._store.update_job(job_id, status="failed", error=str(error), completed_at=completed_at)
        self._emit("tool.failed", {"tool": "create_research_note", "job_id": job_id, "error": str(error)})

    def run_research_job(self, job_id: int) -> None:
        job = self._store.get_job(job_id)
        if not job:
            return
        input_data = job.get("input") or {}
        note_id = int(input_data.get("research_note_id") or 0)
        query = str(input_data.get("query") or "").strip()
        try:
            self._store.update_job(job_id, status="running")
            if note_id:
                self._store.update_research_note(note_id, {"status": "running"})
            options = self._research_options()
            result = self._search.search(query, max_results=options.max_results, search_depth=options.search_depth)
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "web search failed"))
            links = result.get("results") or []
            summary_lines = []
            for item in links[:max(1, options.max_results)]:
                title = str(item.get("title") or "").strip()
                snippet = str(item.get("content") or item.get("snippet") or "").strip()
                url = str(item.get("url") or "").strip()
                if title:
                    summary_lines.append(f"{title}: {snippet[:220]} {url}".strip())
            summary = "\n".join(summary_lines)[:2500] or "Research completed, but no compact summary was returned."
            completed_at = int(self._clock())
            if note_id:
                self._store.update_research_note(note_id, {
                    "summary": summary, "links_json": links, "status": "ready", "completed_at": completed_at,
                })
            self._store.update_job(job_id, status="done", result_json={"summary": summary, "links": links}, completed_at=completed_at)
            self._emit("tool.called", {"tool": "research_note_ready", "job_id": job_id, "note_id": note_id})
        except Exception as error:
            self._log.warning("Research note job failed: %s", error)
            self._fail_research(job_id, note_id, error)
