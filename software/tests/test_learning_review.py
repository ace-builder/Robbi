"""Independent review regressions for explicit feedback and saved progress."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from robbi.app_runtime.host import AppHost
from app_project_testing import patch_on_load, project_module, source_module

learning = source_module("teach-me")


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path, monkeypatch):
    source = tmp_path / "source"
    shutil.copytree(ROOT / "apps" / "teach-me", source / "apps" / "teach-me",
                    ignore=shutil.ignore_patterns("__pycache__"))
    host = AppHost(source, tmp_path / "data", SimpleNamespace(capture_token=lambda: None), permissions_enabled=False)
    patch_on_load(monkeypatch, host, {"teach-me"}, now=lambda: datetime(2026, 10, 26, 8, tzinfo=timezone.utc))
    assert not host.scan()["errors"]
    yield host
    host.stop()
    host._executor.shutdown(wait=True, cancel_futures=True)


def call(host, tool="study", **arguments):
    return host.call("teach-me", tool, arguments)


def saved(host):
    return host.directory / "data" / "teach-me" / "state.json"


def save_state(host, value):
    file = saved(host)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value), encoding="utf-8")
    return file.read_bytes()


def test_grading_without_explicit_recall_feedback_does_not_assume_good(host):
    assert call(host, action="start", topic="python")["ok"]
    assert call(host, action="reveal")["ok"]
    before = saved(host).read_bytes()
    response = call(host, action="grade")
    assert not response["ok"]
    assert "grade" in response["text"].lower() or "recall" in response["text"].lower()
    assert saved(host).read_bytes() == before
    assert call(host, action="grade", grade="hard")["ok"]


def test_duplicate_custom_card_ids_are_rejected_without_ambiguous_edit(host):
    response = call(host, "cards", action="save", topic="biology", title="Cell", question="What is a cell?", answer="A basic unit of life.")
    assert response["ok"]
    state = json.loads(saved(host).read_text(encoding="utf-8"))
    state["cards"].append({**state["cards"][0], "title": "A different card"})
    before = save_state(host, state)
    response = call(host, "cards", action="save", id=state["cards"][0]["id"], answer="Changed")
    assert not response["ok"]
    assert saved(host).read_bytes() == before


@pytest.mark.parametrize("attempts,difficult", [(True, 0), ("one", 0), (1, 2), (-1, 0)])
def test_corrupt_progress_counts_cannot_be_silently_regraded(host, attempts, difficult):
    assert call(host, action="start", topic="python")["ok"]
    assert call(host, action="reveal")["ok"]
    state = json.loads(saved(host).read_text(encoding="utf-8"))
    state["reviews"]["python-1"] = {"interval_days": 1, "due": "2026-10-26T07:00:00+00:00",
        "attempts": attempts, "difficult": difficult, "last_grade": "hard"}
    before = save_state(host, state)
    response = call(host, action="grade", grade="good")
    assert not response["ok"]
    assert saved(host).read_bytes() == before


def test_due_review_order_uses_instants_not_timestamp_text(host):
    state = {"version": 1, "cards": [], "current": None, "reviews": {
        "python-1": {"interval_days": 1, "due": "2026-10-26T09:00:00+02:00", "attempts": 1, "difficult": 0, "last_grade": "good"},
        "python-2": {"interval_days": 1, "due": "2026-10-26T07:30:00+00:00", "attempts": 1, "difficult": 0, "last_grade": "good"}}}
    save_state(host, state)
    response = call(host, action="review", topic="python")
    assert response["ok"]
    assert response["data"]["lesson"]["id"] == "python-1"


def test_reviews_for_nonexistent_cards_are_not_reported_as_real_learning(host):
    state = {"version": 1, "cards": [], "current": None, "reviews": {
        "unknown-card": {"interval_days": 1, "due": "2026-10-26T07:00:00+00:00", "attempts": 1, "difficult": 0, "last_grade": "good"}}}
    before = save_state(host, state)
    response = call(host, "progress")
    assert not response["ok"]
    assert saved(host).read_bytes() == before


def test_next_future_review_uses_earliest_instant_across_offsets(host):
    state = {"version": 1, "cards": [], "current": None, "reviews": {
        "python-1": {"interval_days": 1, "due": "2026-10-26T09:00:00+00:00", "attempts": 1, "difficult": 0, "last_grade": "good"},
        "python-2": {"interval_days": 1, "due": "2026-10-26T10:30:00+02:00", "attempts": 1, "difficult": 0, "last_grade": "good"}}}
    save_state(host, state)
    response = call(host, action="review", topic="python")
    assert response["ok"]
    assert response["data"]["next_review"] == "2026-10-26T10:30:00+02:00"


@pytest.mark.parametrize("corruption", ["boolean_version", "unknown_root", "unknown_card_field", "wrong_current_topic", "out_of_range_timestamp"])
def test_full_learning_state_validation_preserves_corruption_for_repair(host, corruption):
    assert call(host, "cards", action="save", topic="geography", title="City", question="Capital of France?", answer="Paris.")["ok"]
    assert call(host, action="start", topic="geography")["ok"]
    state = json.loads(saved(host).read_text(encoding="utf-8"))
    if corruption == "boolean_version":
        state["version"] = True
    elif corruption == "unknown_root":
        state["future_version_field"] = ["must be preserved"]
    elif corruption == "unknown_card_field":
        state["cards"][0]["future_version_field"] = "must be preserved"
    elif corruption == "wrong_current_topic":
        state["current"]["topic"] = "python"
    else:
        state["reviews"][state["cards"][0]["id"]] = {"interval_days": 1, "due": "0001-01-01T00:00:00+01:00",
            "attempts": 1, "difficult": 0, "last_grade": "good"}
    before = save_state(host, state)
    assert not call(host, "cards", action="save", title="New", question="Q?", answer="A.")["ok"]
    assert saved(host).read_bytes() == before


def test_topic_casefold_expansion_cannot_create_an_unreadable_card(host):
    response = call(host, "cards", action="save", topic="ß" * 30, title="Unicode", question="Q?", answer="A.")
    assert not response["ok"] and "40" in response["text"]
    assert not saved(host).exists()


def test_card_id_collision_is_bounded_and_never_creates_duplicates(host, monkeypatch):
    response = call(host, "cards", action="save", title="First", question="Q?", answer="A.")
    assert response["ok"]
    existing = response["data"]["id"]
    before = saved(host).read_bytes()
    # Patch only this module's UUID provider, not the host's invocation UUIDs.
    monkeypatch.setattr(project_module(host, "teach-me"), "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=existing + "0" * 20)))
    response = call(host, "cards", action="save", title="Second", question="Q?", answer="A.")
    assert not response["ok"] and "distinct" in response["text"]
    assert saved(host).read_bytes() == before
