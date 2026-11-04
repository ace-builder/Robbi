from robot import memory as memory_module
from robot.memory import MemoryStore


def test_extracted_memories_are_pending_and_approved_memories_searchable(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MEMORY_DB_FILE", tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "MEMORY_FILE", tmp_path / "MEMORY.md")
    monkeypatch.setattr(memory_module, "RECENT_CONTEXT_FILE", tmp_path / "recent_context.json")
    monkeypatch.setattr(memory_module.settings, "memory_export_markdown_enabled", True)
    store = MemoryStore()

    pending = store.remember("User likes oat milk.", source="memory_extractor:ui_text")["added"][0]
    assert pending["review_status"] == "pending"
    assert store.search("oat milk") == []

    approved = store.approve(pending["id"])
    assert approved["review_status"] == "approved"
    assert store.search("oat milk")[0]["content"] == "User likes oat milk."


def test_explicit_telegram_memory_is_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MEMORY_DB_FILE", tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "MEMORY_FILE", tmp_path / "MEMORY.md")
    monkeypatch.setattr(memory_module, "RECENT_CONTEXT_FILE", tmp_path / "recent_context.json")
    store = MemoryStore()

    result = store.remember("User is lactose intolerant.", source="telegram:remember")
    entry = result["added"][0]

    assert entry["review_status"] == "approved"
    assert entry["confidence"] == 0.7
    assert store.delete(entry["id"])
    assert store.search("lactose") == []
