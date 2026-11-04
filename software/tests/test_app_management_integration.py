"""Real command -> manager -> signed store client -> ZIP/host lifecycle tests."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import yaml

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.manager import AppManager
from robbi.app_runtime.packages import inspect_archive
from robbi.app_runtime.store_client import StoreClient
from robbi.app_runtime.store_protocol import encode_public_key, fingerprint, sign_payload
from robbi.app_services.http import HttpResponse, checkpoint
from robbi.errors import BusyError
from test_tool_followups import make_conversation


CODE = """def run(context, tool, arguments):
    saved = context.read_state()
    if 'value' in arguments:
        saved['value'] = arguments['value']
        context.write_state(saved)
    if arguments.get('wait'):
        saved['started'] = True
        context.write_state(saved)
        context.wait(5)
    return {'ok': True, 'text': 'Saved record.', 'data': saved}
"""


def package(app_id="sample", *, name="Sample", version="1.0.0", permissions=("state",)):
    manifest = {"api_version": 2, "id": app_id, "name": name, "version": version,
        "description": "Local sample with retained records.", "permissions": list(permissions),
        "tools": [{"name": "save", "description": "Save a local value.", "timeout_s": 10,
                   "input_schema": {"type": "object", "properties": {
                       "value": {"type": "string", "maxLength": 40}, "wait": {"type": "boolean"}}}}]}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("app.yaml", yaml.safe_dump(manifest))
        archive.writestr("main.py", CODE)
    return output.getvalue()


class SignedStoreHTTP:
    """No sockets; real protocol signatures and exact package bytes at the port."""
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        self.records, self.archives, self.latest = {}, {}, {}
        self.revision, self.calls, self.on_request = 1, [], None

    def publish(self, data, **metadata_override):
        manifest = inspect_archive(data)["manifest"]
        digest = hashlib.sha256(data).hexdigest()
        record = {**manifest, "sha256": digest, "size": len(data),
                  "download_path": f"/api/v1/packages/{digest}.zip", "image_paths": {}, "image_hashes": {}, **metadata_override}
        self.records[manifest["id"], manifest["version"]] = record
        self.archives[digest] = data
        self.latest[manifest["id"]] = manifest["version"]
        self.revision += 1
        return record

    def envelope(self, **fields):
        return sign_payload({"schema_version": 1, "store_id": "test-store", "revision": self.revision,
                             "generated_at": self.now.isoformat(), "expires_at": (self.now + timedelta(hours=1)).isoformat(),
                             **fields}, self.key)

    def request(self, method, url, **kwargs):
        assert method == "GET" and kwargs["max_bytes"] <= 16 * 1024 * 1024
        checkpoint(kwargs.get("cancellation"), kwargs.get("deadline"))
        parsed = urlsplit(url)
        self.calls.append(parsed.path)
        if self.on_request:
            self.on_request(parsed.path)
        if parsed.path == "/api/v1/info":
            value = {"schema_version": 1, "store_id": "test-store", "name": "Trusted test store",
                     "public_key": encode_public_key(self.key.public_key()), "fingerprint": fingerprint(self.key.public_key())}
        elif parsed.path == "/api/v1/catalog":
            value = self.envelope(apps=[self.records[key, version] for key, version in self.latest.items()])
        elif parsed.path.startswith("/api/v1/apps/"):
            app_id = parsed.path.rsplit("/", 1)[1]
            version = parse_qs(parsed.query).get("version", [self.latest[app_id]])[0]
            value = self.envelope(app=self.records[app_id, version])
        elif parsed.path.startswith("/api/v1/packages/"):
            return HttpResponse(200, {}, self.archives[Path(parsed.path).stem])
        else:
            raise AssertionError(url)
        return HttpResponse(200, {}, json.dumps(value).encode())


@pytest.fixture
def setup_manager(tmp_path, trusted_app_runner):
    host = AppHost(tmp_path / "robot", tmp_path / "data", SimpleNamespace(capture_token=lambda: None))
    host.scan()
    transport = SignedStoreHTTP()
    store = StoreClient(tmp_path / "store.json", http=transport, clock=lambda: transport.now)
    store.connect("https://store.example", encode_public_key(transport.key.public_key()))
    manager = AppManager(host, tmp_path / "data", store=store)
    try:
        yield host, manager, transport
    finally:
        manager.stop()
        host.stop()


def command(manager, operation, app_id="sample", **kwargs):
    return manager.execute({"operation": operation, "app_id": app_id, **kwargs},
                           source="ui_text", user_text=f"{operation} {app_id}", deadline=time.monotonic() + 10)


def permissions(host, app_id="sample"):
    return {item["id"]: item["status"] for item in host.permission_snapshot(app_id)["permissions"]}


def test_real_conversation_installs_signed_package_then_requires_consent(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package())
    controller = make_conversation()
    controller.app_host, controller.app_manager = host, manager
    controller._ask_llm_with_deadline = lambda *a, **k: pytest.fail("No model needed")
    reply = controller.think_and_respond("Install the Sample app")
    assert reply["app_management_result"]["state"] == "installed"
    assert host.project("sample").manifest["version"] == "1.0.0" and host.enabled("sample")
    assert permissions(host) == {"state": "unrequested"}
    assert host.call("sample", "save", {"value": "important"})["permission_required"]
    host.set_permissions("sample", {"state": "granted"})
    assert host.call("sample", "save", {"value": "important"})["data"]["value"] == "important"
    assert len([path for path in transport.calls if "/packages/" in path]) == 1


def test_real_remove_restore_preserves_data_and_resets_consent(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package())
    assert command(manager, "install")["ok"]
    host.set_permissions("sample", {"state": "granted"})
    assert host.call("sample", "save", {"value": "keep me"})["ok"]
    assert command(manager, "remove")["state"] == "removed"
    assert not host.catalog()
    calls_before = len(transport.calls)
    assert command(manager, "restore")["state"] == "restored"
    assert len(transport.calls) == calls_before and host.enabled("sample")
    assert permissions(host) == {"state": "unrequested"}
    host.set_permissions("sample", {"state": "granted"})
    assert host.call("sample", "save", {})["data"]["value"] == "keep me"


def test_remove_bundled_is_persistent_hide_restore_keeps_original_files(tmp_path):
    bundled = tmp_path / "robot" / "apps" / "sample"
    bundled.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(package())) as archive:
        for filename in archive.namelist():
            (bundled / filename).write_bytes(archive.read(filename))
    host = AppHost(tmp_path / "robot", tmp_path / "data", SimpleNamespace(capture_token=lambda: None))
    host.scan()
    manager = AppManager(host, tmp_path / "data")
    try:
        host.set_permissions("sample", {"state": "granted"})
        host.call("sample", "save", {"value": "bundled data"})
        assert command(manager, "remove")["ok"]
        assert (bundled / "main.py").is_file()
        host.scan()
        assert not host.catalog()
        assert command(manager, "restore")["ok"]
        assert host.project("sample").bundled
        host.set_permissions("sample", {"state": "granted"})
        assert host.call("sample", "save", {})["data"]["value"] == "bundled data"
    finally:
        manager.stop()
        host.stop()


def test_update_changes_version_preserves_existing_consent_and_requires_new_permission(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package())
    assert command(manager, "install")["ok"]
    host.set_permissions("sample", {"state": "granted"})
    host.call("sample", "save", {"value": "preserved"})
    upgrade = package(version="2.0.0", permissions=("state", "network"))
    transport.publish(upgrade)
    manager.store.catalog(refresh=True)
    assert host.packages.preview(upgrade)["permission_changes"]["added"] == ["network"]
    assert command(manager, "update")["state"] == "updated"
    assert host.project("sample").manifest["version"] == "2.0.0"
    assert permissions(host) == {"network": "unrequested", "state": "granted"}
    assert host.call("sample", "save", {})["data"]["value"] == "preserved"


def test_other_running_app_is_not_cancelled_or_blocked_by_install(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package("running", name="Running"))
    transport.publish(package())
    assert command(manager, "install", "running")["ok"]
    host.set_permissions("running", {"state": "granted"})
    job = host.submit("running", "save", {"wait": True})
    state_path = host.directory / "data" / "running" / "state.json"
    limit = time.monotonic() + 2
    while not state_path.exists() and time.monotonic() < limit:
        time.sleep(.01)
    assert state_path.exists()
    try:
        assert command(manager, "install")["ok"]
        assert host.job(job["id"])["state"] == "running"
    finally:
        host.cancel(job["id"])


def test_cancellation_mid_fetch_never_installs_or_retries(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package())
    transport.on_request = lambda path: manager.cancel_all() if "/packages/" in path else None
    assert command(manager, "install")["state"] == "cancelled"
    assert not host.catalog()
    assert len([path for path in transport.calls if "/packages/" in path]) == 1


def test_stop_after_code_commit_keeps_installed_package_disabled(setup_manager, monkeypatch):
    host, manager, transport = setup_manager
    transport.publish(package())
    original = host.install
    def commit_then_stop(*args, **kwargs):
        detail = original(*args, **kwargs)
        manager.cancel_all()
        return detail
    monkeypatch.setattr(host, "install", commit_then_stop)
    assert command(manager, "install")["state"] == "cancelled"
    assert host.project("sample").manifest["version"] == "1.0.0"
    assert not host.enabled("sample") and permissions(host) == {"state": "unrequested"}


@pytest.mark.parametrize("override", [{"name": "Claimed name"}, {"permissions": ["state", "network"]}])
def test_signed_metadata_must_match_actual_zip_before_commit(setup_manager, override):
    host, manager, transport = setup_manager
    transport.publish(package(), **override)
    reply = command(manager, "install")
    assert not reply["ok"] and "manifest differs" in reply["text"]
    assert not host.catalog()


def test_exact_id_does_not_become_ambiguous_with_another_display_name(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package("sample", name="First"))
    transport.publish(package("another", name="sample"))
    assert command(manager, "install", "sample")["state"] == "installed"
    assert set(host.packages.projects) == {"sample"}


def test_explicit_info_version_uses_requested_signed_release(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package())
    assert command(manager, "install")["ok"]
    transport.publish(package(version="2.0.0"))
    manager.store.catalog(refresh=True)
    reply = command(manager, "info", version="2.0.0")
    assert reply["data"]["app"]["version"] == "2.0.0"
    assert host.project("sample").manifest["version"] == "1.0.0"


def test_downgrade_is_refused_before_existing_consent_changes(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package(version="2.0.0"))
    assert command(manager, "install")["ok"]
    host.set_permissions("sample", {"state": "granted"})
    transport.publish(package(version="1.0.0", permissions=()))
    manager.store.catalog(refresh=True)
    reply = command(manager, "update", version="1.0.0")
    assert not reply["ok"] and "downgrade" in reply["text"]
    assert host.project("sample").manifest["version"] == "2.0.0"
    assert permissions(host) == {"state": "granted"}


@pytest.mark.parametrize("spec", [{"operation": []}, {"operation": "install", "app_id": "sample", "query": "other"},
                                  {"operation": "update", "app_id": "sample", "version": "1.0.0-dev"}])
def test_owner_manager_rejects_malformed_envelopes_without_effects(setup_manager, spec):
    host, manager, transport = setup_manager
    before = list(transport.calls)
    assert manager.execute(spec)["state"] == "invalid"
    assert not host.catalog() and transport.calls == before


def test_lowercase_display_name_resolves_and_explicit_id_disambiguates(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package("free-weather", name="Weather"))
    controller = make_conversation()
    controller.app_host, controller.app_manager = host, manager
    assert controller.think_and_respond("install weather")["app_management_result"]["state"] == "installed"
    transport.publish(package("another", name="free-weather"))
    manager.store.catalog(refresh=True)
    assert controller.think_and_respond("disable app ID free-weather")["app_management_result"]["state"] == "disabled"
    assert not host.enabled("free-weather")


def test_trust_revocation_after_download_before_commit_rejects_install(setup_manager, monkeypatch):
    host, manager, transport = setup_manager
    transport.publish(package())
    original = host.packages.preview
    def revoke_after_preview(data):
        preview = original(data)
        manager.store.disconnect()
        return preview
    monkeypatch.setattr(host.packages, "preview", revoke_after_preview)
    reply = command(manager, "install")
    assert not reply["ok"] and "Store connection changed" in reply["text"]
    assert not host.catalog()


def test_full_store_connection_probe_excludes_concurrent_package_changes(setup_manager, monkeypatch):
    host, manager, transport = setup_manager
    transport.publish(package())
    started, release = threading.Event(), threading.Event()
    original = manager.store.probe
    def blocked_probe(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(manager.store, "probe", blocked_probe)
    replies = []
    worker = threading.Thread(target=lambda: replies.append(manager.connect_store(
        "https://store.example", encode_public_key(transport.key.public_key()))))
    worker.start()
    try:
        assert started.wait(2)
        assert command(manager, "install")["state"] == "busy"
        with pytest.raises(BusyError):
            manager.disconnect_store()
        assert not host.catalog()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and replies[0]["configured"]
    assert command(manager, "install")["state"] == "installed"


def test_download_excludes_owner_store_replacement_and_disconnect(setup_manager):
    host, manager, transport = setup_manager
    transport.publish(package())
    attempts = []
    def try_store_change(path):
        if "/packages/" in path:
            for callback in (manager.disconnect_store, lambda: manager.connect_store(
                    "https://store.example", encode_public_key(transport.key.public_key()))):
                with pytest.raises(BusyError):
                    callback()
                attempts.append(path)
    transport.on_request = try_store_change
    assert command(manager, "install")["state"] == "installed"
    assert len(attempts) == 2 and host.enabled("sample")
