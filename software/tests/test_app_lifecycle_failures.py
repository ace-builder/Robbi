"""Injected filesystem failures must not lose code, consent, settings or state."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
import yaml

from robbi.app_runtime import host as host_module
from robbi.app_runtime import packages as package_module
from robbi.app_runtime import permissions as permission_module
from robbi.app_runtime.host import AppHost
from robbi.errors import ValidationError


OLD_CODE = "def run(context, tool, arguments):\n    return {'ok': True, 'text': 'original', 'data': context.read_state()}\n"
NEW_CODE = OLD_CODE.replace("original", "replacement")


def manifest(version="1.0.0", permissions=None, **changes):
    value = {"api_version": 2, "id": "example", "version": version,
        "permissions": permissions or ["state", "robot.speak"],
        "tools": [{"name": "run", "description": "Read saved data", "input_schema": {"type": "object", "properties": {}}}],
        "config_schema": {"type": "object", "properties": {"greeting": {"type": "string", "maxLength": 50, "default": "Hello"}}}}
    return {**value, **changes}


def archive(*, old=False, **changes):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("app.yaml", yaml.safe_dump(manifest(**changes)))
        zipped.writestr("main.py", OLD_CODE if old else NEW_CODE)
    return output.getvalue()


@pytest.fixture
def make_host(tmp_path, trusted_app_runner):
    created = []
    def make(*, bundled=False):
        root = tmp_path / ("source-" + str(len(created)))
        if bundled:
            project = root / "apps" / "example"
            project.mkdir(parents=True)
            (project / "app.yaml").write_text(yaml.safe_dump(manifest()), encoding="utf-8")
            (project / "main.py").write_text(OLD_CODE, encoding="utf-8")
        host = AppHost(root, tmp_path / ("data-" + str(len(created))), SimpleNamespace(capture_token=lambda: None))
        created.append(host)
        host.scan()
        if not bundled:
            host.install(archive(old=True))
        host.set_enabled("example", True)
        host.set_permissions("example", {"state": "granted", "robot.speak": "granted"})
        host.configure("example", {"greeting": "Saved greeting"})
        state = host.directory / "data" / "example" / "state.json"
        state.write_text('{"private":"preserve me"}', encoding="utf-8")
        assert host.call("example", "run", {})["text"] == "original"
        return host
    yield make
    for host in created:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)
        host._read_executor.shutdown(wait=True, cancel_futures=True)


def snapshot(host):
    directory = host.directory / "data" / "example"
    return {"source": (host.project("example").root / "main.py").read_bytes(),
            "config": (directory / "config.json").read_bytes(), "state": (directory / "state.json").read_bytes(),
            "enabled": json.loads((host.directory / "enabled.json").read_text(encoding="utf-8")),
            "permissions": host.permission_store.lifecycle_snapshot("example")}


def assert_preserved(host, previous):
    assert snapshot(host) == previous
    assert host.enabled("example")
    assert host.permission_store.lifecycle_snapshot("example") == {"state": "granted", "robot.speak": "granted"}
    result = host.call("example", "run", {})
    assert result["ok"] and result["text"] == "original" and result["data"] == {"private": "preserve me"}


@pytest.mark.parametrize("bundled", [False, True])
def test_failed_update_copy_restores_previous_grants_and_enabled_state(make_host, monkeypatch, bundled):
    host = make_host(bundled=bundled)
    previous = snapshot(host)
    def fail(*args, **kwargs):
        raise OSError("simulated copy failure")
    monkeypatch.setattr(package_module.shutil, "copytree", fail)
    with pytest.raises(OSError, match="copy"):
        host.install(archive(version="1.1.0", permissions=["state", "network"]))
    assert_preserved(host, previous)


def test_failed_final_code_rename_restores_the_previous_installed_tree(make_host, monkeypatch):
    host = make_host()
    previous = snapshot(host)
    real_rename = Path.rename
    def fail_staging(path, target):
        if path.name.startswith(".ready-"):
            raise OSError("simulated final rename failure")
        return real_rename(path, target)
    monkeypatch.setattr(Path, "rename", fail_staging)
    with pytest.raises(OSError, match="rename"):
        host.install(archive(version="1.1.0", permissions=["state", "network"]))
    assert_preserved(host, previous)


def test_failed_consent_write_does_not_leave_app_disabled_or_remove_existing_grants(make_host, monkeypatch):
    host = make_host()
    previous = snapshot(host)
    real_write = permission_module.atomic_write
    failed = False
    def fail_once(path, value):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated consent write failure")
        return real_write(path, value)
    monkeypatch.setattr(permission_module, "atomic_write", fail_once)
    with pytest.raises(OSError, match="consent"):
        host.install(archive(version="1.1.0", permissions=["state", "network"]))
    assert_preserved(host, previous)


@pytest.mark.parametrize("bundled", [False, True])
def test_failed_removal_registry_commit_restores_grants_and_code(make_host, monkeypatch, bundled):
    host = make_host(bundled=bundled)
    previous = snapshot(host)
    def fail(*args):
        raise OSError("simulated tombstone write failure")
    monkeypatch.setattr(host.packages, "_save_removed", fail)
    with pytest.raises(OSError, match="tombstone"):
        host.remove("example")
    assert host.packages.removed() == []
    assert_preserved(host, previous)


@pytest.mark.parametrize("bundled", [False, True])
def test_failed_enabled_commit_after_removal_restores_the_app(make_host, monkeypatch, bundled):
    host = make_host(bundled=bundled)
    previous = snapshot(host)
    real_write = host_module.write_json
    failed = False
    def fail_once(path, value):
        nonlocal failed
        if Path(path).name == "enabled.json" and "example" not in value and not failed:
            failed = True
            raise OSError("simulated enabled-state write failure")
        return real_write(path, value)
    monkeypatch.setattr(host_module, "write_json", fail_once)
    with pytest.raises(OSError, match="enabled-state"):
        host.remove("example")
    assert host.packages.removed() == []
    assert_preserved(host, previous)


def test_preflight_failures_preserve_consent_enabled_source_and_saved_data(make_host):
    host = make_host()
    previous = snapshot(host)
    for data in (archive(version="0.9.0", permissions=["state", "network"]),
                 archive(version="1.1.0", requires={"sdk": 99}),
                 archive(version="1.1.0", config_schema={"type": "object", "properties": {}})):
        with pytest.raises(ValidationError):
            host.install(data)
        assert_preserved(host, previous)


def test_failed_restore_commit_keeps_removed_record_and_user_data(make_host, monkeypatch):
    host = make_host()
    data_file = host.directory / "data" / "example" / "state.json"
    before = data_file.read_bytes()
    host.remove("example")
    permissions = host.permission_store.lifecycle_snapshot("example")
    enabled = dict(host._enabled)
    def fail(*args):
        raise OSError("simulated restore write failure")
    monkeypatch.setattr(host.packages, "_save_removed", fail)
    with pytest.raises(OSError, match="restore"):
        host.restore("example")
    assert "example" not in host.packages.projects
    assert host.packages.removed()[0]["restore_possible"]
    assert host.permission_store.lifecycle_snapshot("example") == permissions
    assert host._enabled == enabled and data_file.read_bytes() == before
