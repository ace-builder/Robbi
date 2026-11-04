from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from robbi.configuration.settings import PRODUCTIVITY_DB_FILE, TODO_STATE_FILE
from robbi.services.productivity_undo import ChangeJournal, recorded
from robbi.errors import ValidationError


def _now() -> int:
    return int(time.time())


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _load_json(text: str, default):
    try:
        return json.loads(text) if text else default
    except Exception:
        return default


class ProductivityStore:
    """SQLite-backed notes, todos, shopping lists, research notes, and jobs."""

    def __init__(self, db_file: Path = PRODUCTIVITY_DB_FILE, legacy_todos_file: Path = TODO_STATE_FILE) -> None:
        self.db_file = Path(db_file)
        self.legacy_todos_file = Path(legacy_todos_file)
        self._lock = threading.RLock()
        self._undo_journal = ChangeJournal()
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()
        self._migrate_legacy_todos()

    def summary(self) -> dict:
        with closing(self._connect()) as conn, conn:
            notes = conn.execute("SELECT COUNT(*) FROM notes WHERE status != 'deleted'").fetchone()[0]
            open_todos = conn.execute("SELECT COUNT(*) FROM list_items WHERE list_name = 'todo' AND done_at IS NULL").fetchone()[0]
            shopping_open = conn.execute("SELECT COUNT(*) FROM list_items WHERE list_name = 'shopping' AND done_at IS NULL").fetchone()[0]
            research_pending = conn.execute("SELECT COUNT(*) FROM research_notes WHERE status IN ('pending','running')").fetchone()[0]
        return {
            "notes": int(notes),
            "open_todos": int(open_todos),
            "shopping_open": int(shopping_open),
            "research_pending": int(research_pending),
        }

    @recorded("notes", creates=True)
    def create_note(
        self,
        title: str = "",
        raw_text: str = "",
        summary: str = "",
        structured_json=None,
        source: str = "",
        status: str = "active",
    ) -> dict:
        title = self._clean(title) or self._title_from_text(raw_text) or "Untitled note"
        now = _now()
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO notes (title, summary, raw_text, structured_json, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title[:180],
                    self._clean(summary)[:1200],
                    self._clean(raw_text)[:8000],
                    _json(structured_json),
                    self._clean(source)[:80],
                    self._status(status),
                    now,
                    now,
                ),
            )
            conn.commit()
            return self.get_note(int(cur.lastrowid)) or {}

    def list_notes(self, limit: int = 50, include_deleted: bool = False) -> list[dict]:
        where = "" if include_deleted else "WHERE status != 'deleted'"
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"SELECT * FROM notes {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [self._note_row(row) for row in rows]

    def get_note(self, note_id: int) -> dict | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone()
        return self._note_row(row) if row else None

    @recorded("notes")
    def update_note(self, note_id: int, fields: dict) -> dict | None:
        allowed = {"title", "summary", "raw_text", "structured_json", "source", "status"}
        updates = {key: fields[key] for key in allowed if key in fields}
        if not updates:
            return self.get_note(note_id)
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [self._field_value(key, value) for key, value in updates.items()]
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE notes SET {assignments} WHERE id = ?", (*values, int(note_id)))
            conn.commit()
        return self.get_note(note_id)

    @recorded("notes")
    def delete_note(self, note_id: int) -> bool:
        return bool(self.update_note(note_id, {"status": "deleted"}))

    @recorded("list_items", creates=True)
    def add_list_item(
        self,
        list_name: str,
        title: str,
        quantity: str = "",
        category: str = "",
        source: str = "",
    ) -> dict:
        list_name = self._list_name(list_name)
        title = self._clean(title)
        if not title:
            raise ValueError("title is required")
        now = _now()
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO list_items (list_name, title, quantity, category, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (list_name, title[:300], self._clean(quantity)[:80], self._clean(category)[:120], self._clean(source)[:80], now, now),
            )
            conn.commit()
            return self.get_list_item(int(cur.lastrowid)) or {}

    def list_items(self, list_name: str | None = None, include_done: bool = True, limit: int = 200) -> list[dict]:
        clauses = []
        params: list = []
        if list_name:
            clauses.append("list_name = ?")
            params.append(self._list_name(list_name))
        if not include_done:
            clauses.append("done_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(500, int(limit))))
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"SELECT * FROM list_items {where} ORDER BY done_at IS NOT NULL, created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._item_row(row) for row in rows]

    def get_list_item(self, item_id: int) -> dict | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM list_items WHERE id = ?", (int(item_id),)).fetchone()
        return self._item_row(row) if row else None

    @recorded("list_items")
    def update_list_item(self, item_id: int, fields: dict) -> dict | None:
        updates = {}
        for key in ("list_name", "title", "quantity", "category", "source"):
            if key in fields:
                updates[key] = self._field_value(key, fields[key])
        if "done" in fields:
            updates["done_at"] = _now() if bool(fields.get("done")) else None
        if "done_at" in fields:
            updates["done_at"] = fields.get("done_at")
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE list_items SET {assignments} WHERE id = ?", (*updates.values(), int(item_id)))
            conn.commit()
        return self.get_list_item(item_id)

    @recorded("list_items")
    def delete_list_item(self, item_id: int) -> bool:
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM list_items WHERE id = ?", (int(item_id),))
            conn.commit()
            return cur.rowcount > 0

    def _undo_row(self, table, item_id):
        if table not in {"notes", "list_items"}:
            raise ValidationError("Unsupported undo record")
        with closing(self._connect()) as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(item_id),)).fetchone()
            return dict(row) if row is not None else None

    def change_history(self, source=None):
        with self._lock:
            return self._undo_journal.history(source)

    def undo_change(self, change_id, *, source=None):
        with self._lock, closing(self._connect()) as conn, conn:
            record = self._undo_journal.select(change_id, source)
            table, item_id = record["table"], record["item_id"]
            # The comparison and compensation share a SQLite write transaction,
            # including protection from other processes modifying the database.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
            current = dict(row) if row is not None else None
            if current != record["_after"]:
                raise ValidationError("This item changed afterward. Undo would overwrite a newer change.")
            before = record["_before"]
            if before is None:
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
            elif current is None:
                names = list(before)
                conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                             [before[name] for name in names])
            else:
                names = [name for name in before if name != "id"]
                conn.execute(f"UPDATE {table} SET {', '.join(name + ' = ?' for name in names)} WHERE id = ?",
                             [before[name] for name in names] + [item_id])
            conn.commit()
            record.update(undone=True, undo={"available": False, "reason": "Already undone."})
            return {"ok": True, "text": "Undid the saved change.", "change_id": change_id, "item_id": item_id}

    def create_research_note(self, title: str, query: str, source: str = "") -> dict:
        query = self._clean(query)
        if not query:
            raise ValueError("query is required")
        title = self._clean(title) or self._title_from_text(query) or "Research note"
        now = _now()
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO research_notes (title, query, summary, links_json, status, created_at)
                VALUES (?, ?, '', '[]', 'pending', ?)
                """,
                (title[:180], query[:1000], now),
            )
            note_id = int(cur.lastrowid)
            job = conn.execute(
                """
                INSERT INTO background_jobs (kind, status, input_json, result_json, error, created_at)
                VALUES ('research_note', 'pending', ?, '{}', '', ?)
                """,
                (_json({"research_note_id": note_id, "title": title, "query": query, "source": source}), now),
            )
            conn.commit()
            note = self.get_research_note(note_id) or {}
            note["job_id"] = int(job.lastrowid)
            return note

    def update_research_note(self, note_id: int, fields: dict) -> dict | None:
        allowed = {"title", "query", "summary", "links_json", "status", "completed_at"}
        updates = {key: fields[key] for key in allowed if key in fields}
        if not updates:
            return self.get_research_note(note_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [self._field_value(key, value) for key, value in updates.items()]
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE research_notes SET {assignments} WHERE id = ?", (*values, int(note_id)))
            conn.commit()
        return self.get_research_note(note_id)

    def get_research_note(self, note_id: int) -> dict | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM research_notes WHERE id = ?", (int(note_id),)).fetchone()
        return self._research_row(row) if row else None

    def list_research_notes(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM research_notes ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [self._research_row(row) for row in rows]

    def update_job(self, job_id: int, **updates) -> dict | None:
        if not updates:
            return self.get_job(job_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [self._field_value(key, value) for key, value in updates.items()]
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE background_jobs SET {assignments} WHERE id = ?", (*values, int(job_id)))
            conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> dict | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM background_jobs WHERE id = ?", (int(job_id),)).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM background_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [self._job_row(row) for row in rows]

    def _ensure_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL DEFAULT '',
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS list_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    quantity TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    done_at INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    query TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    links_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_list_items_name_done ON list_items(list_name, done_at, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_notes_status_updated ON notes(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_research_status_created ON research_notes(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON background_jobs(status, created_at DESC);
                """
            )

    def _migrate_legacy_todos(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            done = conn.execute("SELECT value FROM meta WHERE key = 'legacy_todos_migrated'").fetchone()
            if done:
                return
            imported = 0
            if self.legacy_todos_file.exists():
                try:
                    raw = json.loads(self.legacy_todos_file.read_text(encoding="utf-8"))
                except Exception:
                    raw = []
                if isinstance(raw, list):
                    existing = {
                        " ".join(str(row["title"] or "").lower().split())
                        for row in conn.execute("SELECT title FROM list_items WHERE list_name = 'todo'").fetchall()
                    }
                    now = _now()
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        title = self._clean(item.get("title") or item.get("text") or "")
                        if not title:
                            continue
                        key = " ".join(title.lower().split())
                        if key in existing:
                            continue
                        done_at = item.get("done_at")
                        conn.execute(
                            """
                            INSERT INTO list_items (list_name, title, source, done_at, created_at, updated_at)
                            VALUES ('todo', ?, 'legacy_todos', ?, ?, ?)
                            """,
                            (title[:300], done_at, int(item.get("created_at") or now), now),
                        )
                        imported += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_todos_migrated', ?)",
                (json.dumps({"imported": imported, "ts": _now()}),),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def _note_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "summary": row["summary"],
            "raw_text": row["raw_text"],
            "structured_json": _load_json(row["structured_json"], {}),
            "source": row["source"],
            "status": row["status"],
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    def _item_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "list_name": row["list_name"],
            "title": row["title"],
            "quantity": row["quantity"],
            "category": row["category"],
            "done_at": row["done_at"],
            "done": row["done_at"] is not None,
            "source": row["source"],
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    def _research_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "query": row["query"],
            "summary": row["summary"],
            "links": _load_json(row["links_json"], []),
            "status": row["status"],
            "created_at": int(row["created_at"]),
            "completed_at": row["completed_at"],
        }

    def _job_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "kind": row["kind"],
            "status": row["status"],
            "input": _load_json(row["input_json"], {}),
            "result": _load_json(row["result_json"], {}),
            "error": row["error"],
            "created_at": int(row["created_at"]),
            "completed_at": row["completed_at"],
        }

    def _field_value(self, key: str, value):
        if key in {"structured_json", "links_json", "input_json", "result_json"}:
            return _json(value)
        if key == "list_name":
            return self._list_name(value)
        if key == "status":
            return self._status(value)
        return value

    def _clean(self, value) -> str:
        return " ".join(str(value or "").split()).strip()

    def _title_from_text(self, text: str) -> str:
        text = self._clean(text)
        if not text:
            return ""
        return text[:60].rstrip(" .,;:") or "Note"

    def _list_name(self, value) -> str:
        name = self._clean(value).lower().replace(" ", "_")
        return name or "todo"

    def _status(self, value) -> str:
        status = self._clean(value).lower().replace(" ", "_")
        return status or "active"
