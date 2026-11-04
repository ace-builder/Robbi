import json
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.errors import UnavailableError, ValidationError
from robbi.app_runtime.manifest import PERMISSIONS
from robbi.app_runtime.permissions import AppPermissionRequired, PermissionStore
from robbi.app_runtime.robot_port import RuntimeRobotPort
from robbi.app_runtime.sdk import AppCancelled, AppContext, Artifacts, Cancellation


def context(tmp_path, permissions, *, store=None, robot=None, batch=True, services=None):
    deadline = time.monotonic() + 30
    cancelled = Cancellation(threading.Event(), None, deadline)
    return AppContext(app_id="sample", config={}, permissions=permissions, directory=tmp_path,
        artifacts=Artifacts(tmp_path / "images", "sample"), cancelled=cancelled, deadline=deadline,
        robot=robot or SimpleNamespace(), robot_token=None, services=services or (lambda name: name),
        permission_check=(lambda permission: store.require("sample", permission, permissions)) if store else None,
        permission_check_many=(lambda items: store.require_many("sample", items, permissions)) if store and batch else None)


def states(store, declared):
    return {item["id"]: item["status"] for item in store.snapshot("sample", declared)["permissions"]}


def test_first_request_is_persisted_then_grants_survive_restart(tmp_path):
    path = tmp_path / "permissions.json"
    store = PermissionStore(path)
    with pytest.raises(AppPermissionRequired) as required:
        store.require("sample", "robot.tof", ["robot.tof"])
    assert required.value.details == {
        "permission_required": True, "state": "permission_required", "app": "sample",
        "permissions": ["robot.tof"], "text": str(required.value),
    }
    assert PermissionStore(path).snapshot("sample", ["robot.tof"])["pending"] == ["robot.tof"]
    store.decide("sample", ["robot.tof"], {"robot.tof": "granted"})
    restored = PermissionStore(path)
    restored.require("sample", "robot.tof", ["robot.tof"])
    assert states(restored, ["robot.tof"]) == {"robot.tof": "granted"}
    assert not issubclass(AppPermissionRequired, UnavailableError)


def test_denied_never_recreates_pending_or_writes_on_retry(tmp_path, monkeypatch):
    store = PermissionStore(tmp_path / "permissions.json")
    store.decide("sample", ["robot.camera"], {"robot.camera": "denied"})
    def unexpected_write(*_):
        raise AssertionError("A denied retry must not write or ask again")
    monkeypatch.setattr("robbi.app_runtime.permissions.atomic_write", unexpected_write)
    for _ in range(3):
        with pytest.raises(AppPermissionRequired) as required:
            store.require("sample", "robot.camera", ["robot.camera"])
        assert required.value.details["state"] == "permission_denied"
        assert store.snapshot("sample", ["robot.camera"])["pending"] == []


def test_batch_preflight_records_every_missing_capability_in_one_write(tmp_path, monkeypatch):
    store = PermissionStore(tmp_path / "permissions.json")
    permissions = ["network", "robot.display", "robot.audio"]
    store.decide("sample", permissions, {"robot.audio": "denied"})
    from robbi.app_runtime import permissions as module
    original, writes = module.atomic_write, []
    def record(*args):
        writes.append(args)
        original(*args)
    monkeypatch.setattr(module, "atomic_write", record)
    with pytest.raises(AppPermissionRequired) as required:
        store.require_many("sample", permissions, permissions)
    assert len(writes) == 1
    assert required.value.details["state"] == "permission_denied"
    assert required.value.details["permissions"] == sorted(permissions)
    assert states(store, permissions) == {"network": "pending", "robot.audio": "denied", "robot.display": "pending"}


def test_undeclared_batch_permission_rejects_before_any_pending_write(tmp_path):
    path = tmp_path / "permissions.json"
    store = PermissionStore(path)
    with pytest.raises(ValidationError, match="does not declare"):
        store.require_many("sample", ["network", "robot.camera"], ["network"])
    assert not path.exists()
    assert states(store, ["network"]) == {"network": "unrequested"}


def test_decision_object_validates_completely_before_mutating(tmp_path):
    path = tmp_path / "permissions.json"
    store = PermissionStore(path)
    store.decide("sample", ["network"], {"network": "denied"})
    before = path.read_bytes()
    with pytest.raises(ValidationError):
        store.decide("sample", ["network"], {"network": "granted", "robot.camera": "granted"})
    assert path.read_bytes() == before
    assert states(store, ["network"]) == {"network": "denied"}


def test_revoke_reset_and_removal_are_durable(tmp_path):
    path = tmp_path / "permissions.json"
    store = PermissionStore(path)
    store.decide("sample", ["robot.motion"], {"robot.motion": "granted"})
    store.decide("sample", ["robot.motion"], {"robot.motion": "denied"})
    with pytest.raises(AppPermissionRequired) as required:
        PermissionStore(path).require("sample", "robot.motion", ["robot.motion"])
    assert required.value.details["state"] == "permission_denied"
    store.decide("sample", ["robot.motion"], {"robot.motion": "unrequested"})
    assert states(PermissionStore(path), ["robot.motion"])["robot.motion"] == "unrequested"
    store.decide("sample", ["robot.motion"], {"robot.motion": "granted"})
    store.forget("sample")
    assert states(PermissionStore(path), ["robot.motion"])["robot.motion"] == "unrequested"


def test_debug_mode_bypasses_undeclared_capabilities_without_changing_decisions(tmp_path):
    path = tmp_path / "permissions.json"
    PermissionStore(path).decide("sample", ["robot.camera"], {"robot.camera": "denied"})
    before = path.read_bytes()
    debug = PermissionStore(path, enabled=False)
    debug.require_many("sample", ["robot.camera", "robot.motion"], [])
    debug.decide("sample", ["robot.camera"], {"robot.camera": "granted"})
    access = debug.snapshot("sample", [])
    assert access["enforced"] is False
    assert {item["id"] for item in access["permissions"]} == PERMISSIONS
    assert {item["status"] for item in access["permissions"]} == {"bypassed"}
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", [
    b"broken", b"x" * 65537, b'{"version":1,"apps":{"sample":{"robot.tof":"yes"}}}',
    b'{"version":1,"apps":{"sample":{"robot.tof":"granted"}},"apps":{}}',
    b'{"version":true,"apps":{"sample":{"robot.tof":"granted"}}}',
    json.dumps({"version": 1, "apps": {f"app-{i}": {"state": "granted"} for i in range(49)}}).encode(),
], ids=["broken", "oversized", "bad-status", "duplicate", "bad-version", "too-many-apps"])
def test_corrupt_or_oversized_state_fails_closed_and_requires_explicit_recovery(tmp_path, content):
    path = tmp_path / "permissions.json"
    path.write_bytes(content)
    store = PermissionStore(path)
    assert store.snapshot("sample", ["robot.tof"])["error"]
    with pytest.raises(AppPermissionRequired):
        store.require("sample", "robot.tof", ["robot.tof"])
    assert path.read_bytes() == content
    assert states(store, ["robot.tof"])["robot.tof"] == "unrequested"
    store.decide("sample", ["robot.tof"], {"robot.tof": "granted"})
    assert not store.snapshot("sample", ["robot.tof"])["error"]
    PermissionStore(path).require("sample", "robot.tof", ["robot.tof"])


def test_unreadable_path_fails_closed(tmp_path):
    store = PermissionStore(tmp_path)
    with pytest.raises(AppPermissionRequired):
        store.require("sample", "state", ["state"])
    assert store.snapshot("sample", ["state"])["error"]


def test_failed_durable_grant_never_appears_in_memory_or_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "permissions.json"
    store = PermissionStore(path)
    store.decide("sample", ["state"], {"state": "denied"})
    def broken_write(*_):
        raise OSError("SD card failure")
    monkeypatch.setattr("robbi.app_runtime.permissions.atomic_write", broken_write)
    with pytest.raises(OSError):
        store.decide("sample", ["state"], {"state": "granted"})
    assert states(store, ["state"]) == {"state": "denied"}
    assert states(PermissionStore(path), ["state"]) == {"state": "denied"}
    assert store.snapshot("sample", ["state"])["error"]


def test_failed_pending_write_still_returns_structured_permission_result(tmp_path, monkeypatch):
    store = PermissionStore(tmp_path / "permissions.json")
    def broken_write(*_):
        raise OSError("No space")
    monkeypatch.setattr("robbi.app_runtime.permissions.atomic_write", broken_write)
    with pytest.raises(AppPermissionRequired) as required:
        store.require_many("sample", ["network", "robot.display"], ["network", "robot.display"])
    assert "could not be saved" in required.value.details["text"]
    assert required.value.details["permissions"] == ["network", "robot.display"]
    assert set(states(store, ["network", "robot.display"]).values()) == {"unrequested"}


def test_store_capacity_is_bounded_and_a_removed_app_releases_space(tmp_path):
    store = PermissionStore(tmp_path / "permissions.json")
    for index in range(48):
        store.decide(f"app-{index}", ["state"], {"state": "granted"})
    before = store.path.read_bytes()
    with pytest.raises(ValidationError):
        store.decide("overflow", ["state"], {"state": "granted"})
    assert store.path.read_bytes() == before
    store.forget("app-0")
    store.decide("overflow", ["state"], {"state": "granted"})
    assert store.path.stat().st_size <= 65536


def test_existing_context_checks_consent_again_before_every_output(tmp_path):
    store, played = PermissionStore(tmp_path / "permissions.json"), []
    robot = SimpleNamespace(speak=lambda *args: played.append(args[0]) or {"ok": True})
    app = context(tmp_path, ["robot.speak"], store=store, robot=robot)
    with pytest.raises(AppPermissionRequired):
        app.robot.speak("Hello")
    assert played == []
    store.decide("sample", ["robot.speak"], {"robot.speak": "granted"})
    assert app.robot.speak("Hello")["ok"]
    store.decide("sample", ["robot.speak"], {"robot.speak": "denied"})
    with pytest.raises(AppPermissionRequired):
        app.robot.speak("Again")
    assert played == ["Hello"]


@pytest.mark.parametrize("batch", [True, False])
def test_sdk_preflight_collects_permissions_without_entering_provider_or_device(tmp_path, batch):
    store, calls = PermissionStore(tmp_path / "permissions.json"), []
    app = context(tmp_path, ["robot.display", "robot.audio"], store=store,
                  robot=SimpleNamespace(play_video=lambda *_: calls.append("video")), batch=batch)
    with pytest.raises(AppPermissionRequired) as required:
        app.robot.play_video("demo")
    assert required.value.details["permissions"] == ["robot.audio", "robot.display"]
    assert store.snapshot("sample", app.permissions)["pending"] == ["robot.audio", "robot.display"]
    assert not calls


def test_debug_context_bypasses_manifest_but_standalone_context_still_checks_it(tmp_path):
    debug = PermissionStore(tmp_path / "permissions.json", enabled=False)
    robot = SimpleNamespace(imu=lambda: {"fresh": True})
    app = context(tmp_path, [], store=debug, robot=robot)
    assert app.robot.imu()["fresh"]
    with pytest.raises(ValidationError):
        context(tmp_path, [], robot=robot).robot.imu()


def test_health_status_cannot_bypass_sensor_camera_or_audio_data_permissions(tmp_path):
    payload = {
        "health": {"state": "ready", "components": [{"name": "imu", "state": "ready", "quaternion": [1, 2]}]},
        "components": {
            "tof": {"available": True, "fresh": True, "distance_cm": 42, "sensors": {"h1": {"distance_cm": 42}}},
            "imu": {"available": True, "quaternion": [1, 2], "orientation_deg": [10, 20]},
            "camera": {"available": True, "data": b"private image"},
            "microphone": {"available": True, "transcript": "private speech", "audio": b"audio"},
            "motion": {"available": True, "counts": {"left": 10}},
        },
        "outputs": {"resources": {"motion": {"owner": "manual", "state": "active", "distance_cm": 42}}},
    }
    app = context(tmp_path, ["robot.status"], robot=SimpleNamespace(status=lambda: payload))
    result = app.robot.status()
    assert result["components"]["tof"] == {"available": True, "fresh": True}
    assert result["components"]["camera"] == {"available": True}
    assert result["outputs"]["resources"]["motion"] == {"owner": "manual", "state": "active"}
    assert "private" not in json.dumps(result)
    assert "quaternion" not in json.dumps(result)
    runtime = SimpleNamespace(snapshot=lambda: payload["components"], health_snapshot=lambda: payload["health"],
                              controller=lambda: SimpleNamespace(orchestrator=None))
    assert RuntimeRobotPort(runtime).status()["components"]["imu"] == {"available": True}


@pytest.mark.parametrize("name,method,args", [
    ("images", "generate", ({}, "A diagram")),
    ("weather", "forecast", ({}, "Berlin", "tomorrow")),
    ("google", "calendar_events", ({},)),
    ("google", "calendar_create", ({},)),
    ("google", "gmail_search", ({},)),
    ("google", "gmail_read", ({}, "message-id")),
    ("google", "gmail_draft", ({},)),
    ("search", "search", ({}, "weather")),
    ("maps", "geocode", ({}, "Berlin")),
    ("maps", "route", ({}, "Berlin", "Hamburg")),
])
def test_cached_service_rechecks_consent_before_every_provider_call(tmp_path, name, method, args):
    store, calls = PermissionStore(tmp_path / "permissions.json"), []
    permissions = ['network'] + (['account.gmail' if method.startswith('gmail_') else 'account.calendar'] if name == 'google' else [])
    store.decide("sample", permissions, {permission: 'granted' for permission in permissions})
    provider = SimpleNamespace(**{method: lambda *args, **kwargs: calls.append(kwargs) or {"ok": True}})
    app = context(tmp_path, permissions, store=store, services=lambda _: provider)
    cached = getattr(app.services, name)
    assert cached is not provider
    assert getattr(cached, method)(*args)["ok"]
    store.decide("sample", permissions, {"network": "denied"})
    with pytest.raises(AppPermissionRequired):
        getattr(cached, method)(*args)
    assert len(calls) == 1


def test_provider_facade_supplies_context_deadline_and_cancellation_when_omitted(tmp_path):
    observed = []
    provider = SimpleNamespace(generate=lambda *args, **kwargs: observed.append(kwargs) or "image")
    app = context(tmp_path, ["network"], services=lambda _: provider)
    assert app.services.images.generate({}, "diagram") == "image"
    assert observed[0]["deadline"] == app.deadline
    signal = observed[0]["cancellation"]
    assert callable(signal) and not signal()
    app.cancelled.event.set()
    assert signal()


def test_provider_facade_preserves_stricter_caller_limits_and_never_extends_app_deadline(tmp_path):
    observed = []
    provider = SimpleNamespace(forecast=lambda *args, **kwargs: observed.append(kwargs) or "weather")
    app = context(tmp_path, ["network"], services=lambda _: provider)
    cached = app.services.weather
    cached.forecast({}, "Berlin", deadline=app.deadline+100)
    assert observed[-1]["deadline"] == app.deadline
    earlier, caller = app.deadline-10, threading.Event()
    cached.forecast({}, "Berlin", deadline=earlier, cancellation=caller)
    assert observed[-1]["deadline"] == earlier
    caller.set()
    assert observed[-1]["cancellation"]()
    with pytest.raises(AppCancelled):
        cached.forecast({}, "Berlin", cancellation=caller)
    with pytest.raises(AppCancelled):
        cached.forecast({}, "Berlin", deadline=time.monotonic()-1)
    assert len(observed) == 2


@pytest.mark.parametrize("service,method,args", [
    ("images", "generate", ({}, "diagram")), ("google", "gmail_read", ({}, "message")),
    ("search", "search", ({}, "query")), ("maps", "route", ({}, "start", "end")),
])
def test_provider_response_is_discarded_when_context_is_cancelled_during_call(tmp_path, service, method, args):
    app = None
    def generate(*args, **kwargs):
        app.cancelled.event.set()
        assert kwargs["cancellation"]()
        return "late private image"
    permissions = ['network'] + (['account.gmail'] if service == 'google' else [])
    app = context(tmp_path, permissions, services=lambda _: SimpleNamespace(**{method: generate}))
    with pytest.raises(AppCancelled):
        getattr(getattr(app.services, service), method)(*args)


def test_provider_response_is_discarded_when_permission_revoked_during_call(tmp_path):
    store = PermissionStore(tmp_path / "permissions.json")
    store.decide("sample", ["network"], {"network": "granted"})
    def generate(*args, **kwargs):
        store.decide("sample", ["network"], {"network": "denied"})
        return "late private image"
    app = context(tmp_path, ["network"], store=store, services=lambda _: SimpleNamespace(generate=generate))
    with pytest.raises(AppPermissionRequired):
        app.services.images.generate({}, "diagram")


@pytest.mark.parametrize('method,permission', [('gmail_search', 'account.gmail'), ('calendar_events', 'account.calendar')])
@pytest.mark.parametrize('revoke_during_call', [False, True])
def test_google_account_consent_is_separate_from_network(tmp_path, method, permission, revoke_during_call):
    store, calls = PermissionStore(tmp_path / 'permissions.json'), []
    permissions = ['network', permission]
    store.decide('sample', permissions, {'network': 'granted', permission: 'granted' if revoke_during_call else 'denied'})
    def provider(*args, **kwargs):
        calls.append(method)
        store.decide('sample', permissions, {permission: 'denied'})
        return {'ok': True, 'text': 'private result'}
    app = context(tmp_path, permissions, store=store, services=lambda _: SimpleNamespace(**{method: provider}))
    with pytest.raises(AppPermissionRequired):
        getattr(app.services.google, method)({})
    assert len(calls) == int(revoke_during_call)
