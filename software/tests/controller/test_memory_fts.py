import sqlite3

from robot import memory as memory_module
from robot.memory import MemoryStore


def test_memory_search_uses_fts_and_stays_in_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MEMORY_DB_FILE", tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "MEMORY_FILE", tmp_path / "MEMORY.md")
    monkeypatch.setattr(memory_module, "RECENT_CONTEXT_FILE", tmp_path / "recent_context.json")
    monkeypatch.setattr(memory_module.settings, "memory_export_markdown_enabled", True)
    monkeypatch.setattr(memory_module.settings, "memory_search_limit", 5)

    store = MemoryStore()
    result = store.remember(
        {
            "upsert": [
                {"kind": "preferences", "content": "User is lactose intolerant.", "tags": ["food"]},
                {"kind": "project", "content": "Robbi uses encoder motors and a VL53L1X ToF sensor.", "tags": ["robot"]},
            ]
        }
    )
    assert len(result["added"]) == 2

    with sqlite3.connect(tmp_path / "memory.db") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert "memories_fts" in tables

    matches = store.search("lactose", limit=3)
    assert matches
    assert "lactose" in matches[0]["content"].lower()

    entry_id = matches[0]["id"]
    store.apply_memory_ops({"update": [{"id": entry_id, "content": "User can drink normal milk again."}]})
    assert not store.search("lactose", limit=3)
    assert store.search("normal milk", limit=3)

    store.apply_memory_ops({"delete": [entry_id]})
    assert not store.search("normal milk", limit=3)
    assert (tmp_path / "MEMORY.md").exists()
