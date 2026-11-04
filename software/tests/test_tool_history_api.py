"""Real HTTP, app-host and SQLite undo boundaries, without provider/hardware I/O.

Browser scopes organize an owner's tool receipts; they do not replace dashboard
authentication or change the existing owner-wide legacy /api/jobs API.
"""
from pathlib import Path
import json
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.config import load
from robbi.runtime import Runtime
from robbi.services.productivity import ProductivityService
from robbi.services.tool_followups import scope_source
from robbi.web import create_app
from robot.productivity import ProductivityStore
from robot.webapp import create_app as create_legacy_app


ROOT = Path(__file__).resolve().parents[1]
FIRST = {"X-Robbi-Conversation": "browser_first-1"}
SECOND = {"X-Robbi-Conversation": "browser_second-2"}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBBI_WEB_TOKEN", raising=False)
    config = load(ROOT, tmp_path / "profile.yaml", tmp_path / "data")
    config.values["server"]["auth_enabled"] = False
    config.values.setdefault("apps", {})["permissions_enabled"] = True
    runtime = Runtime(config)
    store = ProductivityStore(tmp_path / "productivity.db", tmp_path / "missing.json")
    service = ProductivityService(store, search=None, scheduler=None,
                                  publish_event=lambda *_: None)
    cancelled = threading.Event()
    controller = SimpleNamespace(
        _action_token=lambda: (0, cancelled), stop=lambda: None,
        cancel_actions=cancelled.set,
        motion=SimpleNamespace(stop=lambda: None),
        slam=SimpleNamespace(stop=lambda: None),
        media=SimpleNamespace(stop_audio=lambda: None),
        productivity=store, productivity_service=service,
        create_note=service.create_note, update_note=service.update_note,
        delete_note=service.delete_note,
    )
    runtime._controller = controller
    runtime._ready.set()
    runtime._legacy_app = create_legacy_app(controller)
    runtime.app_host.scan()
    runtime.app_host.set_permissions("bookmarks", {"state": "granted"})
    app = create_app(runtime)
    yield SimpleNamespace(runtime=runtime, store=store, service=service,
                          app=app, client=app.test_client())
    runtime.stop()
    runtime.app_host._executor.shutdown(wait=True, cancel_futures=True)
    runtime.jobs.executor.shutdown(wait=True, cancel_futures=True)


def finished(client, job_id, *, headers=FIRST, app_job=True):
    endpoint = "/api/app-jobs/" if app_job else "/api/jobs/"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(endpoint + job_id, headers=headers)
        assert response.status_code == 200, response.get_json()
        record = response.get_json()["job"] if app_job else response.get_json()
        if record["state"] in {"done", "failed", "cancelled"}:
            return record
        threading.Event().wait(.005)
    pytest.fail("HTTP job did not finish")


def saved_bookmark(environment, *, headers=FIRST, suffix="first"):
    arguments = {"title": "Private bookmark " + suffix,
                 "url": "https://example.com/private-" + suffix}
    response = environment.client.post("/api/apps/bookmarks/tools/save",
                                       json={"arguments": arguments}, headers=headers)
    assert response.status_code == 202, response.get_json()
    record = finished(environment.client, response.get_json()["job"]["id"], headers=headers)
    assert record["state"] == "done", record
    return record, arguments


def saved_note(environment, *, headers=FIRST, text="Private note contents"):
    response = environment.client.post("/notes", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.get_json()
    note = response.get_json()["note"]
    response.close()  # Release the compatibility HTTP worker slot.
    source = scope_source("ui_text", headers.get("X-Robbi-Conversation", ""))
    receipt = environment.service.history(source)[0]
    assert receipt["item_id"] == note["id"]
    return note, receipt


def test_histories_are_scoped_metadata_and_inputs_are_explicit(environment):
    first, arguments = saved_bookmark(environment)
    second, _ = saved_bookmark(environment, headers=SECOND, suffix="second")
    client = environment.client
    for endpoint in ("/api/apps/bookmarks/history", "/api/tool-history"):
        response = client.get(endpoint, headers=FIRST)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        entries = response.get_json()["entries"]
        assert [entry["id"] for entry in entries] == [first["id"]]
        assert entries[0]["undo"]["available"]
        assert entries[0]["arguments_ref"] == entries[0]["result_ref"] == first["id"]
        assert b"Private bookmark" not in response.data
        assert b"https://example.com" not in response.data
        assert [entry["id"] for entry in client.get(endpoint, headers=SECOND).get_json()["entries"]] == [second["id"]]
        assert client.get(endpoint).get_json()["entries"] == []
    inputs = client.get(f"/api/app-jobs/{first['id']}/inputs", headers=FIRST)
    assert inputs.get_json() == {"app": "bookmarks", "tool": "save", "arguments": {**arguments, "tags": []}}
    assert inputs.headers["Cache-Control"] == "no-store"
    for headers in (SECOND, {}):
        response = client.get(f"/api/app-jobs/{first['id']}/inputs", headers=headers)
        assert response.status_code == 400
        assert arguments["title"] not in response.get_json()["error"]


def test_app_undo_is_scoped_one_use_and_restores_real_state(environment):
    original, _ = saved_bookmark(environment)
    client = environment.client
    endpoint = f"/api/app-jobs/{original['id']}/undo"
    for headers in (SECOND, {}):
        assert client.post(endpoint, json={}, headers=headers).status_code == 400
    response = client.post(endpoint, json={}, headers=FIRST)
    assert response.status_code == 202, response.get_json()
    undone = finished(client, response.get_json()["job"]["id"])
    assert undone["state"] == "done"
    assert undone["result"]["ok"]
    assert undone["undo_of"] == original["id"]
    assert not undone["undo"]["available"]
    history = client.get("/api/apps/bookmarks/history", headers=FIRST).get_json()["entries"]
    assert history[0]["kind"] == "undo"
    assert history[1]["undo"]["status"] == "undone"
    assert client.post(endpoint, json={}, headers=FIRST).status_code == 400
    assert not (environment.runtime.app_host.directory / "data" / "bookmarks" / "state.json").exists()


def test_revoked_state_permission_blocks_undo_without_consuming_receipt(environment):
    original, _ = saved_bookmark(environment)
    host = environment.runtime.app_host
    host.set_permissions("bookmarks", {"state": "denied"})
    endpoint = f"/api/app-jobs/{original['id']}/undo"
    response = environment.client.post(endpoint, json={}, headers=FIRST)
    assert response.status_code == 202
    denied = finished(environment.client, response.get_json()["job"]["id"])
    assert denied["state"] == "failed"
    assert denied["result"]["permission_required"]
    assert host.invocation(original["id"])["undo"]["available"]
    host.set_permissions("bookmarks", {"state": "granted"})
    response = environment.client.post(endpoint, json={}, headers=FIRST)
    assert finished(environment.client, response.get_json()["job"]["id"])["result"]["ok"]


def test_disabled_app_cannot_be_undone_until_enabled(environment):
    original, _ = saved_bookmark(environment)
    host = environment.runtime.app_host
    host.set_enabled("bookmarks", False)
    endpoint = f"/api/app-jobs/{original['id']}/undo"
    response = environment.client.post(endpoint, json={}, headers=FIRST)
    assert response.status_code == 503
    assert host.invocation(original["id"])["undo"]["available"]
    host.set_enabled("bookmarks", True)
    response = environment.client.post(endpoint, json={}, headers=FIRST)
    assert finished(environment.client, response.get_json()["job"]["id"])["result"]["ok"]


def test_read_only_tools_expose_non_undoable_reason(environment):
    response = environment.client.post("/api/apps/calculator/tools/calculate",
        json={"arguments": {"expression": "6*7"}}, headers=FIRST)
    original = finished(environment.client, response.get_json()["job"]["id"])
    assert original["result"]["data"]["value"] == 42
    assert not original["undo"]["available"]
    assert original["undo"]["reason"]
    response = environment.client.post(f"/api/app-jobs/{original['id']}/undo", json={}, headers=FIRST)
    assert response.status_code == 400
    assert response.get_json()["error"] == original["undo"]["reason"]


def test_not_ready_allows_history_and_inputs_but_refuses_mutating_undo(environment):
    original, _ = saved_bookmark(environment)
    _, note_receipt = saved_note(environment)
    environment.runtime._ready.clear()
    client = environment.client
    for endpoint in ("/api/apps/bookmarks/history", "/api/tool-history",
                     f"/api/app-jobs/{original['id']}/inputs"):
        assert client.get(endpoint, headers=FIRST).status_code == 200
    assert client.post(f"/api/app-jobs/{original['id']}/undo", json={}, headers=FIRST).status_code == 503
    assert client.post(f"/api/tool-history/{note_receipt['id']}/undo",
                       json={"kind": "productivity"}, headers=FIRST).status_code == 503


@pytest.mark.parametrize("body", [{"arguments": {}}, {"source": "ui_text"}, {"kind": "app"}, [], None])
def test_app_undo_rejects_replacement_arguments_and_invalid_objects(environment, body):
    original, _ = saved_bookmark(environment)
    response = environment.client.post(f"/api/app-jobs/{original['id']}/undo",
        data=json.dumps(body), content_type="application/json", headers=FIRST)
    assert response.status_code == 400
    assert environment.runtime.app_host.invocation(original["id"])["undo"]["available"]


@pytest.mark.parametrize("identifier", ["has space", "a/b", "a:b", "x" * 129])
def test_conversation_identifier_is_validated_before_creating_history(environment, identifier):
    headers = {"X-Robbi-Conversation": identifier}
    assert environment.client.get("/api/tool-history", headers=headers).status_code == 400
    assert environment.client.post("/api/apps/calculator/tools/calculate",
        json={"arguments": {"expression": "1+1"}}, headers=headers).status_code == 400
    assert environment.runtime.app_host.history() == []


def test_unknown_history_inputs_and_undo_have_safe_client_errors(environment):
    client = environment.client
    for endpoint in ("/api/apps/unknown/history", "/api/app-jobs/unknown/inputs"):
        response = client.get(endpoint, headers=FIRST)
        assert response.status_code == 400
        assert response.get_json()["error"]
    assert client.post("/api/app-jobs/unknown/undo", json={}, headers=FIRST).status_code == 400


def test_history_and_undo_require_dashboard_login_when_enabled(environment, monkeypatch):
    original, _ = saved_bookmark(environment)
    note, receipt = saved_note(environment)
    environment.runtime.config.values["server"]["auth_enabled"] = True
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "test-owner-secret")
    client = environment.client
    for endpoint in ("/api/tool-history", "/api/apps/bookmarks/history",
                     f"/api/app-jobs/{original['id']}/inputs"):
        assert client.get(endpoint, headers=FIRST).status_code == 401
        assert client.get(endpoint, headers={**FIRST, "Authorization": "Bearer test-owner-secret"}).status_code == 200
    assert client.post(f"/api/app-jobs/{original['id']}/undo", json={}, headers=FIRST).status_code == 401
    assert client.post(f"/api/tool-history/{receipt['id']}/undo",
                       json={"kind": "productivity"}, headers=FIRST).status_code == 401
    assert environment.store.get_note(note["id"]) is not None
    monkeypatch.delenv("ROBBI_WEB_TOKEN")
    assert client.get("/api/tool-history", headers=FIRST).status_code == 503


def test_cross_origin_undo_is_rejected_before_mutation(environment):
    original, _ = saved_bookmark(environment)
    note, receipt = saved_note(environment)
    headers = {**FIRST, "Origin": "https://other.example"}
    assert environment.client.post(f"/api/app-jobs/{original['id']}/undo", json={}, headers=headers).status_code == 403
    assert environment.client.post(f"/api/tool-history/{receipt['id']}/undo",
        json={"kind": "productivity"}, headers=headers).status_code == 403
    assert environment.store.get_note(note["id"]) is not None
    assert environment.runtime.app_host.invocation(original["id"])["undo"]["available"]


def test_productivity_http_changes_share_only_their_browser_history(environment):
    original, _ = saved_bookmark(environment)
    note, receipt = saved_note(environment)
    _, second = saved_note(environment, headers=SECOND, text="Other browser private note")
    client = environment.client
    response = client.get("/api/tool-history", headers=FIRST)
    entries = response.get_json()["entries"]
    assert [entry["id"] for entry in entries] == [receipt["id"], original["id"]]
    assert [entry["kind"] for entry in entries] == ["productivity", "app"]
    assert b"Private note contents" not in response.data
    assert b"Private bookmark" not in response.data
    assert [entry["id"] for entry in client.get("/api/tool-history", headers=SECOND).get_json()["entries"]] == [second["id"]]
    # A later request without a header must not inherit the previous request's ContextVar.
    _, unscoped = saved_note(environment, headers={}, text="Unscoped note")
    assert unscoped["source"] == "ui_text"
    assert [entry["id"] for entry in client.get("/api/tool-history").get_json()["entries"]] == [unscoped["id"]]
    assert environment.store.get_note(note["id"])["raw_text"] == "Private note contents"


def test_productivity_undo_is_source_checked_and_one_use(environment):
    note, receipt = saved_note(environment)
    endpoint = f"/api/tool-history/{receipt['id']}/undo"
    response = environment.client.post(endpoint, json={"kind": "productivity"}, headers=SECOND)
    assert response.status_code == 202
    refused = finished(environment.client, response.get_json()["job"]["id"], headers=SECOND, app_job=False)
    assert refused["state"] == "failed"
    assert "conversation" in refused["error"]
    assert environment.store.get_note(note["id"]) is not None
    response = environment.client.post(endpoint, json={"kind": "productivity"}, headers=FIRST)
    undone = finished(environment.client, response.get_json()["job"]["id"], app_job=False)
    assert undone["state"] == "done"
    assert undone["result"]["ok"]
    assert environment.store.get_note(note["id"]) is None
    history = environment.client.get("/api/tool-history", headers=FIRST).get_json()["entries"]
    assert history[0]["undone"]
    assert not history[0]["undo"]["available"]
    response = environment.client.post(endpoint, json={"kind": "productivity"}, headers=FIRST)
    repeated = finished(environment.client, response.get_json()["job"]["id"], app_job=False)
    assert repeated["state"] == "failed"
    assert "undone" in repeated["error"]


@pytest.mark.parametrize("body", [{}, {"kind": "app"}, {"kind": "productivity", "source": "ui_text"}])
def test_productivity_undo_rejects_wrong_kind_or_extra_fields(environment, body):
    note, receipt = saved_note(environment)
    response = environment.client.post(f"/api/tool-history/{receipt['id']}/undo", json=body, headers=FIRST)
    assert response.status_code == 400
    assert environment.store.get_note(note["id"]) is not None


def test_productivity_undo_cancels_stale_queued_generation(environment):
    note, receipt = saved_note(environment)
    started, release = threading.Event(), threading.Event()
    def block():
        started.set()
        assert release.wait(5)
    blocker = environment.runtime.jobs.executor.submit(block)
    assert started.wait(2)
    try:
        response = environment.client.post(f"/api/tool-history/{receipt['id']}/undo",
            json={"kind": "productivity"}, headers=FIRST)
        assert response.status_code == 202
        environment.runtime._generation += 1
    finally:
        release.set()
        blocker.result(timeout=3)
    cancelled = finished(environment.client, response.get_json()["job"]["id"], app_job=False)
    assert cancelled["state"] == "cancelled"
    assert cancelled["result"]["cancelled"]
    assert environment.store.get_note(note["id"]) is not None
    assert environment.service.history(receipt["source"])[0]["undo"]["available"]


def test_browser_header_controls_conversation_id_for_text_jobs(environment, monkeypatch):
    received = []
    monkeypatch.setattr(environment.runtime, "submit", lambda operation, payload:
                        received.append((operation, payload)) or {"id": "captured"})
    response = environment.client.post("/api/jobs", headers=FIRST,
        json={"operation": "text", "payload": {"text": "again", "conversation_id": "spoofed"}})
    assert response.status_code == 202
    assert received == [("text", {"text": "again", "conversation_id": FIRST["X-Robbi-Conversation"]})]


def test_existing_job_results_and_cancellation_respect_browser_scope(environment):
    original, _ = saved_bookmark(environment)
    client = environment.client
    for headers in (SECOND, {}):
        response = client.get(f"/api/app-jobs/{original['id']}", headers=headers)
        assert response.status_code == 400
        assert b"Private bookmark" not in response.data
        assert client.post(f"/api/app-jobs/{original['id']}/cancel", json={}, headers=headers).status_code == 400
    assert client.get(f"/api/app-jobs/{original['id']}", headers=FIRST).status_code == 200


def test_running_app_cannot_be_cancelled_from_another_browser(environment, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def waiting(context, tool, arguments):
        entered.set()
        assert release.wait(5)
        context.checkpoint()
        return {"ok": True}
    host = environment.runtime.app_host
    monkeypatch.setattr(host, "_handler", lambda project: waiting)
    response = environment.client.post("/api/apps/calculator/tools/calculate",
        json={"arguments": {"expression": "1+1"}}, headers=FIRST)
    assert response.status_code == 202
    identifier = response.get_json()["job"]["id"]
    try:
        assert entered.wait(2)
        endpoint = f"/api/app-jobs/{identifier}/cancel"
        assert environment.client.post(endpoint, json={}, headers=SECOND).status_code == 400
        assert host.job(identifier)["state"] == "running"
        response = environment.client.post(endpoint, json={}, headers=FIRST)
        assert response.status_code == 200
        assert response.get_json()["job"]["state"] == "cancelled"
    finally:
        release.set()
        host._jobs[identifier]["_future"].result(timeout=3)


def test_http_undo_conflict_preserves_newer_state_and_allows_reverse_order(environment):
    first, _ = saved_bookmark(environment)
    second, _ = saved_bookmark(environment, suffix="second")
    endpoint = f"/api/app-jobs/{first['id']}/undo"
    response = environment.client.post(endpoint, json={}, headers=FIRST)
    conflict = finished(environment.client, response.get_json()["job"]["id"])
    assert conflict["state"] == "failed"
    assert "newer changes" in conflict["error"]
    host = environment.runtime.app_host
    assert host.invocation(first["id"])["undo"]["available"]
    saved = host.directory / "data" / "bookmarks" / "state.json"
    assert "private-first" in saved.read_text(encoding="utf-8")
    assert "private-second" in saved.read_text(encoding="utf-8")
    for original in (second, first):
        response = environment.client.post(f"/api/app-jobs/{original['id']}/undo", json={}, headers=FIRST)
        assert response.status_code == 202
        assert finished(environment.client, response.get_json()["job"]["id"])["result"]["ok"]
    assert not saved.exists()
