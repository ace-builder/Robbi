import sqlite3

import pytest

from robbi.errors import ValidationError
from robbi.services.productivity_undo import change_scope
from robot.productivity import ProductivityStore


@pytest.fixture
def store(tmp_path):
    return ProductivityStore(tmp_path / "daily.db", tmp_path / "absent.json")


def test_create_update_delete_can_be_undone_in_reverse_order(store):
    item = store.add_list_item("shopping", "Milk", quantity="1")
    created = store.change_history()[0]["id"]
    store.update_list_item(item["id"], {"quantity": "2", "done": True})
    updated = store.change_history()[0]["id"]
    store.delete_list_item(item["id"])
    deleted = store.change_history()[0]["id"]
    assert store.undo_change(deleted)["ok"]
    assert store.get_list_item(item["id"])["quantity"] == "2"
    store.undo_change(updated)
    assert store.get_list_item(item["id"])["quantity"] == "1"
    assert store.get_list_item(item["id"])["done_at"] is None
    store.undo_change(created)
    assert store.get_list_item(item["id"]) is None
    with pytest.raises(ValidationError, match="undone"):
        store.undo_change(created)


def test_note_soft_delete_records_once_and_restores_full_note(store):
    note = store.create_note(raw_text="Private note", structured_json={"tags": ["trip"]})
    store.delete_note(note["id"])
    assert len(store.change_history()) == 2
    store.undo_change(store.change_history()[0]["id"])
    assert store.get_note(note["id"]) == note
    assert "Private note" not in str(store.change_history())


def test_conflicting_external_write_is_never_overwritten(store):
    item = store.add_list_item("todo", "First")
    receipt = store.change_history()[0]["id"]
    with sqlite3.connect(store.db_file) as connection:
        connection.execute("UPDATE list_items SET title = 'Changed externally' WHERE id = ?", (item["id"],))
    with pytest.raises(ValidationError, match="changed afterward"):
        store.undo_change(receipt)
    assert store.get_list_item(item["id"])["title"] == "Changed externally"


def test_source_scopes_and_history_limit(store):
    with change_scope("telegram_text:session:private"):
        note = store.create_note(raw_text="Remote")
    receipt = store.change_history()[0]["id"]
    assert store.change_history("ui_text") == []
    with pytest.raises(ValidationError, match="conversation"):
        store.undo_change(receipt, source="ui_text")
    for index in range(35):
        store.add_list_item("todo", str(index))
    assert len(store.change_history()) == 32
    with pytest.raises(ValidationError):
        store.undo_change(receipt)
    assert store.get_note(note["id"]) is not None


def test_missing_record_mutation_does_not_offer_undo(store):
    assert not store.delete_list_item(999)
    assert store.change_history() == []


def test_large_note_is_saved_but_undo_storage_is_bounded(store):
    note = store.create_note(raw_text="Large", structured_json={"text": "x" * 140000})
    entry = store.change_history()[0]
    assert not entry["undo"]["available"]
    assert store.get_note(note["id"]) is not None
    with pytest.raises(ValidationError, match="size"):
        store.undo_change(entry["id"])


def test_scoped_receipts_identify_exact_writes_even_with_another_writer(store):
    from concurrent.futures import ThreadPoolExecutor
    with change_scope("browser-one") as mine:
        note = store.create_note(raw_text="First writer")
        with ThreadPoolExecutor(max_workers=1) as pool:
            def other_writer():
                with change_scope("browser-one") as theirs:
                    store.create_note(raw_text="Another request, same browser")
                    return theirs
            theirs = pool.submit(other_writer).result()
        assert len(mine) == len(theirs) == 1
        assert mine[0]["id"] != theirs[0]["id"]
        assert mine[0]["item_id"] == note["id"]
        assert "_before" not in mine[0]
        store.undo_change(mine[0]["id"], source="browser-one")
    assert [item["raw_text"] for item in store.list_notes()] == ["Another request, same browser"]
