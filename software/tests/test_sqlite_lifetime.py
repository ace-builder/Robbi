"""Connections must release SD-card handles/cache promptly, including errors."""
from contextlib import closing
import sqlite3

import pytest

from robot import memory as memory_module
from robot.events import EventBus, RobotEvent
from robot.memory import MemoryStore
from robot.productivity import ProductivityStore


@pytest.fixture
def connections(monkeypatch):
    original = sqlite3.connect
    opened = []
    statements = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=TrackedConnection)
        connection.set_trace_callback(statements.append)
        # Hold references deliberately: passing must not depend on GC closing
        # sqlite's reference cycles sometime after a diagnostic poll.
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened, statements


def configure_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MEMORY_DB_FILE", tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "MEMORY_FILE", tmp_path / "MEMORY.md")
    monkeypatch.setattr(memory_module, "RECENT_CONTEXT_FILE", tmp_path / "recent.json")
    monkeypatch.setattr(memory_module.settings, "memory_export_markdown_enabled", False)


def test_productivity_reads_and_writes_close_connections(tmp_path, connections):
    store = ProductivityStore(tmp_path / "productivity.db", tmp_path / "old-todos.json")
    note = store.create_note(raw_text="Use the repaired encoder pin")
    store.update_note(note["id"], {"summary": "BCM7"})
    store.add_list_item("todo", "Verify wheelbase")
    assert store.summary()["open_todos"] == 1
    assert store.list_notes()[0]["summary"] == "BCM7"
    opened, _ = connections
    assert opened and all(connection.closed for connection in opened)


def test_failed_sql_still_closes_connection(tmp_path, connections):
    store = ProductivityStore(tmp_path / "productivity.db", tmp_path / "old-todos.json")
    with closing(sqlite3.connect(store.db_file)) as connection, connection:
        connection.execute("DROP TABLE notes")
    with pytest.raises(sqlite3.OperationalError):
        store.summary()
    assert all(connection.closed for connection in connections[0])


def test_memory_diagnostics_can_search_without_writes(tmp_path, monkeypatch, connections):
    configure_memory(tmp_path, monkeypatch)
    store = MemoryStore()
    entry = store.remember("Robbi has quadrature motor encoders", source="telegram:remember")["added"][0]
    with closing(sqlite3.connect(memory_module.MEMORY_DB_FILE)) as connection, connection:
        connection.execute("UPDATE memories SET last_accessed_at = 0 WHERE id = ?", (entry["id"],))
    monkeypatch.setattr(memory_module.time, "time", lambda: 12345)
    opened, statements = connections
    statements.clear()
    assert store.search("quadrature", touch=False)[0]["id"] == entry["id"]
    assert not any(statement.lstrip().upper().startswith("UPDATE") for statement in statements)
    store.stats()
    assert store.list_entries()[0]["last_accessed_at"] == 0
    store.search("quadrature")
    assert store.list_entries()[0]["last_accessed_at"] == 12345
    assert all(connection.closed for connection in opened)


def test_event_connection_closes_after_commit(tmp_path, connections):
    bus = EventBus(tmp_path / "events.db")
    bus._jsonl_path = tmp_path / "events.jsonl"
    bus._ensure_db()
    bus._write_event(RobotEvent("test.event", {"source": "offline test"}, ts=1234))
    with closing(sqlite3.connect(bus._db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert all(connection.closed for connection in connections[0])
