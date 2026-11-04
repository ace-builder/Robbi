from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from robbi.app_runtime.host import AppHost


def project(root, name, code, *, permissions=(), read_only=False):
    path = root / "apps" / name
    path.mkdir(parents=True)
    (path / "main.py").write_text(code, encoding="utf-8")
    (path / "app.yaml").write_text(yaml.safe_dump({"api_version": 2, "id": name, "name": name,
        "permissions": list(permissions), "tools": [{"name": "run", "read_only": read_only,
        "description": "Read selected values", "timeout_s": 4,
        "input_schema": {"type": "object", "properties": {}}}]}), encoding="utf-8")


@pytest.fixture
def factory(tmp_path):
    hosts = []
    def create(*, permissions=True):
        host = AppHost(tmp_path, tmp_path / "data", SimpleNamespace(capture_token=lambda: (0, threading.Event())), permissions_enabled=permissions)
        assert not host.scan()["errors"]
        hosts.append(host)
        return host
    yield create
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True)
        host._read_executor.shutdown(wait=True)


def test_composition_requires_parent_and_target_consent_and_keeps_child_out_of_conversation(tmp_path, factory):
    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','run',{})", permissions=["apps.read"])
    project(tmp_path, "child", "def run(c,t,a): return {'ok':True,'data':c.read_state()}", permissions=["state"], read_only=True)
    host = factory()
    first = host.call("parent", "run", {}, source="browser-one")
    assert first["permission_required"] and first["app"] == "parent"
    host.set_permissions("parent", {"apps.read": "granted"})
    second = host.call("parent", "run", {}, source="browser-one")
    assert second["permission_required"] and second["app"] == "child"
    host.set_permissions("child", {"state": "granted"})
    assert host.call("parent", "run", {}, source="browser-one")["ok"]
    assert all(entry["app"] == "parent" for entry in host.history(source="browser-one"))
    host.set_permissions("child", {"state": "denied"})
    assert host.call("parent", "run", {})["state"] == "permission_denied"
    host.set_enabled("child", False)
    assert not host.call("parent", "run", {})["ok"]


@pytest.mark.parametrize("read_only,body", [(False, "return {'ok':True}"),
    (True, "c.write_state({'changed':True}); return {'ok':True}"),
    (True, "return c.call_app('another','run',{})")])
def test_composed_reads_cannot_write_or_recurse(tmp_path, factory, read_only, body):
    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','run',{})", permissions=["apps.read"])
    project(tmp_path, "child", "def run(c,t,a): " + body, permissions=["state", "apps.read"], read_only=read_only)
    host = factory(permissions=False)
    result = host.call("parent", "run", {})
    assert not result["ok"]
    assert not (host.directory / "data" / "child" / "state.json").exists()


def test_two_parent_workers_can_read_without_waiting_on_their_own_pool(tmp_path, factory):
    for suffix in ("one", "two"):
        project(tmp_path, "parent-" + suffix, f"def run(c,t,a): return c.call_app('child-{suffix}','run',{{}})", permissions=["apps.read"])
        project(tmp_path, "child-" + suffix, "def run(c,t,a): c.wait(.1); return {'ok':True,'text':'child completed'}", read_only=True)
    host = factory(permissions=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(host.call, "parent-" + suffix, "run", {}) for suffix in ("one", "two")]
        assert all(future.result(timeout=3)["ok"] for future in futures)


def test_stop_cancels_composed_read_and_does_not_publish_late_result(tmp_path, factory):
    project(tmp_path, "parent", "def run(c,t,a): return c.call_app('child','run',{})", permissions=["apps.read"])
    project(tmp_path, "child", "def run(c,t,a): c.wait(3); c.checkpoint(); return {'ok':True}", read_only=True)
    host = factory(permissions=False)
    job = host.submit("parent", "run", {})
    limit = time.monotonic() + 2
    while not host.history("child") and time.monotonic() < limit:
        time.sleep(.005)
    assert host.history("child")
    host.cancel_all()
    result = host._wait_for_job(job)
    assert result["cancelled"]
    host._read_executor.shutdown(wait=True)
    assert all(entry["state"] == "cancelled" for entry in host.history("child"))
