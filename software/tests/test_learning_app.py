from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from robbi.app_runtime.host import AppHost
from app_project_testing import patch_on_load, project_module, source_module

learning = source_module("teach-me")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path, monkeypatch):
    host = AppHost(ROOT, tmp_path, SimpleNamespace(capture_token=lambda: None), permissions_enabled=False)
    patch_on_load(monkeypatch, host, {"teach-me"}, now=lambda: datetime(2026, 10, 26, 8, tzinfo=timezone.utc))
    host.scan()
    yield host
    host.stop()


def call(host, tool="study", **args):
    return host.call("teach-me", tool, args)


@pytest.mark.parametrize("topic", list(learning.LESSONS))
def test_lessons_reveal_and_schedule_explicit_difficulty(host, monkeypatch, topic):
    value = call(host, action="start", topic=topic)
    assert value["ok"] and value["data"]["minutes"] == 5
    assert "answer" not in value["data"]["lesson"]
    assert not call(host, action="grade", grade="easy")["ok"]
    assert call(host, action="reveal")["data"]["lesson"]["answer"]
    result = call(host, action="grade", grade="again")
    assert result["ok"] and result["data"]["due"] == "2026-10-26T08:10:00+00:00"
    assert not call(host, action="grade", grade="easy")["ok"]
    assert call(host, action="review", topic=topic)["data"]["next_review"] == result["data"]["due"]
    monkeypatch.setattr(project_module(host, "teach-me"), "now", lambda: datetime(2026, 10, 26, 8, 11, tzinfo=timezone.utc))
    assert call(host, action="review", topic=topic)["data"]["lesson"]["id"] == value["data"]["lesson"]["id"]
    assert call(host, "progress")["data"]["difficult"] == [topic + "-1"]


def test_card_edit_delete_persistence_and_undo(host):
    new = call(host, "cards", action="save", topic="My course", title="Recall", question="What matters?", answer="Attention.")
    assert new["ok"]
    identifier = new["data"]["id"]
    assert call(host, action="start", topic="MY COURSE")["ok"]
    assert call(host, action="reveal")["ok"]
    assert call(host, action="grade", grade="good")["ok"]
    assert call(host, "cards", action="save", id=identifier, answer="Practice.")["ok"]
    assert call(host, "progress")["data"]["studied"] == 0
    assert call(host, "cards", action="list")["data"]["cards"][0]["question"] == "What matters?"
    assert call(host, "cards", action="delete", id=identifier)["ok"]
    deletion = host.history("teach-me")[0]["id"]
    assert host.undo(deletion)["ok"]
    assert call(host, "cards", action="list")["data"]["cards"][0]["answer"] == "Practice."


def test_invalid_or_oversized_cards_cannot_overwrite_existing_progress(host):
    assert not call(host, "cards", action="save", topic="python", title="x", question="x", answer="x")["ok"]
    assert not call(host, "cards", action="save", id="unknown", answer="x")["ok"]
    for index in range(30):
        result = call(host, "cards", action="save", title=str(index), question="Why?", answer="Because.")
        assert result["ok"]
    assert not call(host, "cards", action="save", title="one too many", question="x", answer="x")["ok"]
    assert len(call(host, "cards")["data"]["cards"]) == 30


def test_corrupt_state_is_preserved(host):
    state_file = host.directory / "data" / "teach-me" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version":1,"cards":{},"reviews":{}}', encoding="utf-8")
    before = state_file.read_bytes()
    assert not call(host, "cards", action="save", title="x", question="x", answer="x")["ok"]
    assert state_file.read_bytes() == before


def test_learning_state_requires_consent(tmp_path):
    host = AppHost(ROOT, tmp_path, SimpleNamespace(capture_token=lambda: None))
    host.scan()
    try:
        assert host.call("teach-me", "study", {"action": "start"})["permission_required"]
        assert not (host.directory / "data" / "teach-me" / "state.json").exists()
    finally:
        host.stop()
