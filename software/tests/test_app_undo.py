"""Undo runs through the same bounded host, permissions and fresh SDK contexts."""
import json
from pathlib import Path
import threading
import time

import pytest
import yaml

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.sdk import AppCancelled
from robbi.errors import BusyError, ValidationError


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {"type": "object", "properties": {"value": {"type": "string", "default": "value", "maxLength": 100}}}
CODE = """def run(context, tool, arguments):
    if tool == 'read':
        return {'ok': True, 'data': context.read_state()}
    state = context.read_state()
    state['value'] = arguments['value']
    context.write_state(state)
    state['second_write'] = True
    context.write_state(state)
    if tool == 'fail':
        raise ValueError('Failed after the write')
    return {'ok': True, 'text': arguments['value']}
"""


class Robot:
    def __init__(self):
        self.cancelled = threading.Event()
        self.timers = {}
        self.actions = []

    def capture_token(self):
        return 0, self.cancelled

    def action(self, value, token, cancellation):
        self.actions.append(value)
        return {"ok": True}

    def timer(self, operation, arguments, token, cancellation):
        if operation == "start":
            self.timers["timer-1"] = arguments
            return {"ok": True, "data": {"id": "timer-1", "due_at": time.time() + arguments["seconds"]}}
        if operation == "cancel":
            if arguments["id"] not in self.timers:
                return {"ok": False, "text": "Timer is no longer active."}
            del self.timers[arguments["id"]]
            return {"ok": True, "text": "Timer cancelled."}
        return {"ok": True, "data": {"timers": self.timers}}


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    def make(*, bundled=False, permissions=False, code=CODE, capabilities=("state",)):
        source = ROOT if bundled else tmp_path / ("source-" + str(len(hosts)))
        if not bundled:
            project = source / "apps" / "example"
            project.mkdir(parents=True)
            manifest = {"api_version": 2, "id": "example", "name": "Example", "permissions": list(capabilities),
                "tools": [{"name": name, "description": name, "input_schema": SCHEMA} for name in ("write", "read", "fail")]}
            (project / "app.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
            (project / "main.py").write_text(code, encoding="utf-8")
        host = AppHost(source, tmp_path / ("data-" + str(len(hosts))), Robot(), permissions_enabled=permissions)
        hosts.append(host)
        assert not host.scan()["errors"]
        return host
    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def invoke(host, value="private mail body", *, source="ui_text", tool="write"):
    result = host.call("example", tool, {"value": value}, source=source, include_invocation=True)
    return result, result["invocation"]["id"]


def test_state_undo_restores_first_snapshot_after_multiple_writes_once(make_host):
    host = make_host()
    result, job_id = invoke(host)
    assert result["invocation"]["undo"]["available"]
    state_path = host.directory / "data" / "example" / "state.json"
    assert json.loads(state_path.read_text())["second_write"]
    undone = host.undo(job_id)
    assert undone["ok"] and not state_path.exists()
    assert host.job(job_id)["undo"]["status"] == "undone"
    assert not host.undo(job_id)["ok"]


def test_history_uses_references_not_private_arguments_or_results(make_host):
    host = make_host()
    result, job_id = invoke(host, source="voice:chat-a")
    history = host.history(source="voice:chat-a")
    assert len(history) == 1 and history[0]["arguments_ref"] == history[0]["result_ref"] == job_id
    assert "private mail body" not in json.dumps(history)
    detail = host.invocation(job_id, source="voice:chat-a")
    assert detail["arguments"]["value"] == detail["result"]["text"] == "private mail body"
    with pytest.raises(ValidationError):
        host.invocation(job_id, source="voice:chat-b")
    assert not host.history(source="voice:chat-b")
    assert not host.undo(job_id, source="voice:chat-b")["ok"]
    assert host.undo(job_id, source="voice:chat-a")["ok"]


def test_conflicting_newer_state_is_preserved_and_chain_can_be_undone_in_order(make_host):
    host = make_host()
    _, first = invoke(host, "first")
    _, second = invoke(host, "second")
    conflict = host.undo(first)
    assert not conflict["ok"] and conflict["conflict"]
    assert host.call("example", "read", {})["data"]["value"] == "second"
    assert host.undo(second)["ok"]
    assert host.undo(first)["ok"]


def test_nonmutating_call_is_not_undoable_and_latest_does_not_skip_it(make_host):
    host = make_host()
    _, first = invoke(host)
    _, read = invoke(host, tool="read")
    latest = host.undo()
    assert not latest["ok"] and "no recorded change" in latest["text"]
    assert not host.job(read)["undo"]["available"]
    assert host.undo(first)["ok"]


def test_failed_handler_preserves_a_confirmed_state_write_for_undo(make_host):
    host = make_host()
    failed, job_id = invoke(host, tool="fail")
    assert not failed["ok"] and host.job(job_id)["undo"]["available"]
    assert host.undo(job_id)["ok"]


def test_permission_revocation_prevents_undo_without_consuming_it(make_host):
    host = make_host(permissions=True)
    host.set_permissions("example", {"state": "granted"})
    _, job_id = invoke(host)
    host.set_permissions("example", {"state": "denied"})
    refused = host.undo(job_id)
    assert not refused["ok"] and refused["permission_required"]
    assert host.job(job_id)["undo"]["available"]
    host.set_permissions("example", {"state": "granted"})
    assert host.undo(job_id)["ok"]


def test_disabled_app_cannot_be_undone_until_reenabled(make_host):
    host = make_host()
    _, job_id = invoke(host)
    host.set_enabled("example", False)
    assert not host.undo(job_id)["ok"]
    host.set_enabled("example", True)
    assert host.undo(job_id)["ok"]


def test_undo_admission_cannot_race_an_active_app_call(make_host, monkeypatch):
    host = make_host()
    _, first = invoke(host)
    entered, release = threading.Event(), threading.Event()
    def wait(context, tool, arguments):
        entered.set()
        assert release.wait(3)
        return {"ok": True}
    monkeypatch.setattr(host, "_handler", lambda project: wait)
    try:
        running = host.submit("example", "read", {})
        assert entered.wait(1)
        with pytest.raises(BusyError):
            host.submit_undo(first)
        assert "finish" in host.undo()["text"]
    finally:
        release.set()
        host._jobs[running["id"]]["_future"].result(3)


def test_cancel_before_undo_does_not_consume_or_change_state(make_host):
    host = make_host()
    _, job_id = invoke(host)
    refused = host.undo(job_id, cancellation=lambda: True)
    assert not refused["ok"]
    assert host.job(job_id)["undo"]["available"]
    assert host.undo(job_id)["ok"]


def test_registered_compensation_gets_fresh_context_and_is_not_replayed(make_host):
    code = """def run(context, tool, arguments):
    def undo(fresh):
        fresh.checkpoint()
        fresh.robot.speak('compensated')
        return {'ok': True}
    context.register_undo(undo, 'Undo a test change', permissions=('robot.speak',))
    return {'ok': True}
"""
    host = make_host(code=code, capabilities=("robot.speak",))
    _, job_id = invoke(host)
    host.cancel_all()  # Old callback context would now fail.
    assert host.undo(job_id)["ok"]
    assert host.robot.actions == [{"speak": "compensated"}]
    assert not host.undo(job_id)["ok"]
    assert len(host.robot.actions) == 1


def test_failed_compensation_is_consumed_and_not_retried(make_host):
    code = """def run(context, tool, arguments):
    def undo(fresh):
        fresh.robot.speak('one attempt')
        raise TimeoutError('Remote result is uncertain')
    context.register_undo(undo, 'Undo a test change', permissions=('robot.speak',))
    return {'ok': True}
"""
    host = make_host(code=code, capabilities=("robot.speak",))
    _, job_id = invoke(host)
    assert not host.undo(job_id)["ok"]
    assert host.job(job_id)["undo"]["status"] == "used"
    assert not host.undo(job_id)["ok"] and len(host.robot.actions) == 1


def test_timer_start_undo_cancels_only_its_exact_timer_and_read_is_nonmutating(make_host):
    host = make_host(bundled=True)
    result = host.call("kitchen-timers", "start", {"seconds": 30, "label": "Tea"}, include_invocation=True)
    assert result["ok"] and result["invocation"]["undo"]["available"]
    host.robot.timers["unrelated"] = {"seconds": 50}
    assert host.undo(result["invocation"]["id"])["ok"]
    assert set(host.robot.timers) == {"unrelated"}


def test_physical_output_has_truthful_nonundoable_reason(make_host):
    code = "def run(context, tool, arguments):\n    return context.robot.speak('Hello')\n"
    host = make_host(code=code, capabilities=("robot.speak",))
    result, job_id = invoke(host)
    assert not result["invocation"]["undo"]["available"]
    assert "cannot be reversed" in host.undo(job_id)["text"]
    assert len(host.robot.actions) == 1


def test_history_bound_expires_old_receipts_without_persisting_arguments(make_host):
    host = make_host()
    _, first = invoke(host, "first private body")
    for number in range(34):
        invoke(host, str(number))
    assert len(host.history()) == 32
    assert not host.undo(first)["ok"]
    assert not list(host.directory.glob("*history*"))


def test_stateful_bundled_utility_undo_restores_deleted_bookmark(make_host):
    host = make_host(bundled=True)
    saved = host.call("bookmarks", "save", {"title": "Example", "url": "https://example.org"})
    removed = host.call("bookmarks", "remove", {"id": saved["data"]["bookmark"]["id"]}, include_invocation=True)
    assert host.undo(removed["invocation"]["id"])["ok"]
    restored = host.call("bookmarks", "search", {})
    assert restored["data"]["bookmarks"][0]["url"] == "https://example.org"


def test_state_conflict_distinguishes_json_booleans_from_numbers(make_host):
    code = "def run(context, tool, arguments):\n    context.write_state({'value': 1})\n    return {'ok': True}\n"
    host = make_host(code=code)
    _, job_id = invoke(host)
    path = host.directory / "data" / "example" / "state.json"
    path.write_text('{"value": true}', encoding="utf-8")
    failed = host.undo(job_id)
    assert not failed["ok"] and failed["conflict"]
    assert json.loads(path.read_text())["value"] is True


def test_cancelled_undo_queue_releases_claim_without_consuming_receipt(make_host):
    host = make_host()
    _, job_id = invoke(host)
    entered = threading.Barrier(3)
    release = threading.Event()
    def occupy():
        entered.wait(3)
        assert release.wait(3)
    first = host._executor.submit(occupy)
    second = host._executor.submit(occupy)
    entered.wait(3)
    try:
        pending = host.submit_undo(job_id)
        assert not host.job(job_id)["undo"]["available"]
        host.cancel(pending["id"])
        assert host.job(job_id)["undo"]["available"]
    finally:
        release.set()
        first.result(3)
        second.result(3)
    assert host.undo(job_id)["ok"]


def test_bounded_state_remains_readable_and_undoable_without_pretty_print_growth(make_host):
    code = """def run(context, tool, arguments):
    if tool == 'read':
        return {'ok': True, 'count': len(context.read_state().get('rows', []))}
    context.write_state({'rows': [{'value': 'x' * 55} for _ in range(800)]})
    return {'ok': True}
"""
    host = make_host(code=code)
    created, job_id = invoke(host)
    assert created["ok"]
    assert host.call("example", "read", {})["count"] == 800
    path = host.directory / "data" / "example" / "state.json"
    assert path.stat().st_size <= 65536
    assert host.undo(job_id)["ok"]
