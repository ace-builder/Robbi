"""Independent review of composed read isolation and resource admission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from robbi.app_runtime.host import AppHost


def project(root, name, body, permissions=(), *, read_only=False):
    directory = root / "apps" / name
    directory.mkdir(parents=True)
    (directory / "main.py").write_text(body, encoding="utf-8")
    (directory / "app.yaml").write_text(yaml.safe_dump({"api_version": 2, "id": name, "name": name,
        "permissions": list(permissions), "tools": [{"name": "read", "read_only": read_only,
        "description": "Read bounded data", "timeout_s": 4, "input_schema": {"type": "object", "properties": {}}}]}), encoding="utf-8")


class Robot:
    def __init__(self):
        self.child_returned, self.finish_parent = threading.Event(), threading.Event()
        self.reservations = []

    def capture_token(self):
        return 0, threading.Event()

    @contextmanager
    def session(self, resources, token, cancelled, duration):
        self.reservations.append(resources)
        yield


@pytest.fixture
def make_host(tmp_path):
    hosts = []

    def make(*, enforced=False):
        host = AppHost(tmp_path, tmp_path / "data", Robot(), permissions_enabled=enforced)
        hosts.append(host)
        assert not host.scan()["errors"]
        return host

    yield make
    for host in hosts:
        host.robot.finish_parent.set()
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)
        host._read_executor.shutdown(wait=True, cancel_futures=True)


def test_read_only_child_cannot_reserve_or_preempt_robot_outputs(tmp_path, make_host):
    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','read',{})", ["apps.read"])
    project(tmp_path, "child", "def run(c,t,a):\n    with c.robot.session('display'):\n        return {'ok':True}\n", ["robot.display"], read_only=True)
    host = make_host()
    result = host.call("parent", "read", {})
    assert not result["ok"]
    assert host.robot.reservations == []


def test_read_only_child_cannot_register_a_later_mutation_callback(tmp_path, make_host):
    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','read',{})", ["apps.read"])
    project(tmp_path, "child", "def run(c,t,a):\n    c.register_undo(lambda fresh: fresh.write_state({'changed':True}), 'Unexpected mutation', permissions=('state',))\n    return {'ok':True}\n", ["state"], read_only=True)
    host = make_host()
    result = host.call("parent", "read", {})
    assert not result["ok"]
    assert not host.history("child")[0]["undo"]["available"]


@pytest.mark.parametrize("change", ["revoke_child", "disable_child", "revoke_parent"])
def test_completed_child_read_is_discarded_when_access_changes_before_parent_returns(tmp_path, make_host, change):
    project(tmp_path, "parent", "def run(c,t,a):\n    result=c.call_app('child','read',{})\n    c.robot._port.child_returned.set()\n    c.robot._port.finish_parent.wait(2)\n    c.checkpoint()\n    return result\n", ["apps.read"])
    project(tmp_path, "child", "def run(c,t,a): return {'ok':True,'data':c.read_state()}", ["state"], read_only=True)
    host = make_host(enforced=True)
    host.set_permissions("parent", {"apps.read": "granted"})
    host.set_permissions("child", {"state": "granted"})
    saved = host.directory / "data" / "child" / "state.json"
    saved.parent.mkdir(parents=True)
    saved.write_text('{"private":"PRIVATE CHILD DATA"}', encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(host.call, "parent", "read", {})
        assert host.robot.child_returned.wait(2)
        if change == "revoke_child":
            host.set_permissions("child", {"state": "denied"})
        elif change == "disable_child":
            host.set_enabled("child", False)
        else:
            host.set_permissions("parent", {"apps.read": "denied"})
        host.robot.finish_parent.set()
        result = future.result(timeout=2)
    assert result.get("cancelled") or result.get("permission_required"), result
    assert "PRIVATE CHILD DATA" not in json.dumps(result)


def test_eighth_child_read_is_last_and_ninth_never_runs(tmp_path, make_host):
    project(tmp_path, "parent", "def run(c,t,a):\n    for _ in range(9): c.call_app('child','read',{})\n    return {'ok':True}\n", ["apps.read"])
    project(tmp_path, "child", "def run(c,t,a): return {'ok':True,'text':'read'}", read_only=True)
    host = make_host()
    result = host.call("parent", "read", {})
    assert not result["ok"] and "eight" in result["text"]
    assert len(host.history("child")) == 8


def test_parent_deadline_cancels_child_and_never_returns_late_data(tmp_path, make_host):
    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','read',{})", ["apps.read"])
    project(tmp_path, "child", "def run(c,t,a):\n    c.wait(1)\n    return {'ok':True,'text':'PRIVATE LATE'}\n", read_only=True)
    host = make_host()
    result = host.call("parent", "read", {}, deadline=time.monotonic() + .12)
    assert result.get("cancelled")
    assert "PRIVATE LATE" not in json.dumps(result)


def test_completed_parent_followup_context_is_invalidated_when_child_consent_is_revoked(tmp_path, make_host):
    from robbi.services.tool_followups import ToolFollowups
    from robot.domains.tools import ToolsDomain

    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','read',{})", ["apps.read"])
    project(tmp_path, "child", "def run(c,t,a): return {'ok':True,'data':c.read_state()}", ["state"], read_only=True)
    host = make_host(enforced=True)
    host.set_permissions("parent", {"apps.read": "granted"})
    host.set_permissions("child", {"state": "granted"})
    saved = host.directory / "data" / "child" / "state.json"
    saved.parent.mkdir(parents=True)
    saved.write_text('{"private":"PRIVATE COMPOSED RESULT"}', encoding="utf-8")
    result = host.call("parent", "read", {}, source="browser-one", include_invocation=True)
    reference = result["invocation"]["id"]
    assert result["ok"] and host.invocation(reference)["context_valid"]
    subject = SimpleNamespace(app_host=host)
    allowed = lambda call: ToolsDomain._tool_receipt_allowed(subject, call)
    context = ToolFollowups()
    receipt = context.record("browser-one", "app_tool", {"app": "parent", "tool": "read", "arguments": {}},
                             result, undo_ref=reference, guard={"permissions": ["apps.read"]})
    context.remember_turn("browser-one", "prepare", "PRIVATE COMPOSED RESULT")
    assert allowed(receipt) and "PRIVATE COMPOSED RESULT" in context.prompt("browser-one", allowed)
    host.set_permissions("child", {"state": "denied"})
    assert not host.invocation(reference)["context_valid"] and not allowed(receipt)
    assert "PRIVATE COMPOSED RESULT" not in context.prompt("browser-one", allowed)
    assert context.messages("browser-one") == []
    host.set_permissions("child", {"state": "granted"})
    assert not host.invocation(reference)["context_valid"]
