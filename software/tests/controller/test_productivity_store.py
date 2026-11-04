import json

from robot.productivity import ProductivityStore


def test_productivity_crud(tmp_path):
    store = ProductivityStore(db_file=tmp_path / "productivity.db", legacy_todos_file=tmp_path / "todos.json")

    note = store.create_note(raw_text="remember this weird idea", summary="weird idea", source="test")
    assert note["id"] > 0
    assert store.list_notes()[0]["title"].startswith("remember")

    todo = store.add_list_item("todo", "finish robot tests", source="test")
    shopping = store.add_list_item("shopping", "M2 screws", quantity="20", source="test")
    assert todo["list_name"] == "todo"
    assert shopping["quantity"] == "20"

    store.update_list_item(todo["id"], {"done": True})
    assert store.get_list_item(todo["id"])["done"] is True
    assert len(store.list_items("todo", include_done=False)) == 0


def test_legacy_todo_migration(tmp_path):
    legacy = tmp_path / "todos.json"
    legacy.write_text(json.dumps([{"title": "legacy task", "created_at": 1, "done_at": None}]), encoding="utf-8")
    store = ProductivityStore(db_file=tmp_path / "productivity.db", legacy_todos_file=legacy)

    todos = store.list_items("todo", include_done=True)
    assert len(todos) == 1
    assert todos[0]["title"] == "legacy task"

    store_again = ProductivityStore(db_file=tmp_path / "productivity.db", legacy_todos_file=legacy)
    assert len(store_again.list_items("todo", include_done=True)) == 1


def test_research_note_creates_job(tmp_path):
    store = ProductivityStore(db_file=tmp_path / "productivity.db", legacy_todos_file=tmp_path / "todos.json")
    note = store.create_research_note(title="Plan", query="coding plan comparison", source="test")

    assert note["status"] == "pending"
    assert note["job_id"] > 0
    job = store.get_job(note["job_id"])
    assert job["kind"] == "research_note"
    assert job["input"]["query"] == "coding plan comparison"
