"""Owner-consent integration: real HTTP/host/SDK, no hardware or provider I/O."""
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest
import yaml

from robbi.app_runtime.sdk import AppCancelled
from robbi.config import load, validate
from robbi.errors import ValidationError
from robbi.runtime import Runtime
from robbi.web import create_app


ROOT = Path(__file__).resolve().parents[1]
APP = "permissions-demo"
ACCESS = f"/api/apps/{APP}/permissions"
CODE = '''def run(context, tool, arguments):
    if tool == 'self-approve':
        context.set_permissions({'robot.speak': 'granted'})
    if tool == 'camera':
        context.robot.camera_snapshot()
    elif tool == 'optional':
        needed = ['robot.speak']
        if arguments.get('camera', False):
            needed.append('robot.camera')
        context.request_permissions(*needed)
        context.robot.speak('hello')
        if arguments.get('camera', False):
            context.robot.camera_snapshot()
    elif tool == 'speak':
        context.robot.speak('hello')
    return {'ok': True, 'text': 'Completed.'}
'''


class Robot:
    def __init__(self):
        self.cancelled = threading.Event()
        self.actions, self.cameras, self.signals = [], [], []

    def capture_token(self):
        return 0, self.cancelled

    def action(self, action, token, cancelled):
        if cancelled() or token[1].is_set():
            raise AppCancelled("cancelled")
        self.actions.append(action)
        self.signals.append(cancelled)
        return action

    def camera_snapshot(self, **kwargs):
        self.cameras.append(kwargs)
        return {"ok": True, "data": b"fake-jpeg", "age_s": 0}


def package(permissions):
    schema = {"type": "object", "properties": {"camera": {"type": "boolean", "default": False}}}
    manifest = {"api_version": 2, "id": APP, "permissions": permissions,
                "tools": [{"name": name, "description": name, "input_schema": schema}
                          for name in ("speak", "camera", "optional", "self-approve")]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("app.yaml", yaml.safe_dump(manifest))
        archive.writestr("main.py", CODE)
    return stream.getvalue()


@pytest.fixture
def make_runtime(tmp_path, monkeypatch, trusted_app_runner):
    monkeypatch.delenv("ROBBI_WEB_TOKEN", raising=False)
    created = []

    def make(*, enabled=True, data_dir=None, install=True, declared=None, auth_enabled=False):
        config = load(ROOT, tmp_path / f"profile-{len(created)}.yaml", data_dir or tmp_path / f"data-{len(created)}")
        config.values.setdefault("apps", {})["permissions_enabled"] = enabled
        config.values["server"]["auth_enabled"] = auth_enabled
        if auth_enabled:
            monkeypatch.setenv("ROBBI_WEB_TOKEN", "owner-secret")
        runtime = Runtime(config)
        runtime._controller = SimpleNamespace(stop=lambda: None, cancel_actions=lambda: None,
                                             motion=SimpleNamespace(stop=lambda: None), slam=SimpleNamespace(stop=lambda: None),
                                             media=SimpleNamespace(stop_audio=lambda: None))
        runtime._ready.set()
        runtime.app_host.robot = Robot()
        runtime.app_host.scan()
        if install:
            runtime.app_host.install(package(declared if declared is not None else ["robot.speak", "robot.camera"]))
            runtime.app_host.set_enabled(APP, True)
        created.append(runtime)
        return runtime, create_app(runtime).test_client()

    yield make
    for runtime in created:
        runtime.stop()
        runtime.app_host._executor.shutdown(wait=True, cancel_futures=True)


def statuses(access):
    return {item["id"]: item["status"] for item in access["permissions"]}


def call(client, tool="speak", arguments=None):
    response = client.post(f"/api/apps/{APP}/tools/{tool}", json={"arguments": arguments or {}})
    assert response.status_code == 202, response.get_data(as_text=True)
    job_id = response.get_json()["job"]["id"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = client.get(f"/api/app-jobs/{job_id}").get_json()["job"]
        if job["state"] in {"done", "failed", "cancelled"}:
            return job
        threading.Event().wait(.005)
    pytest.fail("App did not finish its bounded permission check")


def decide(client, **decisions):
    response = client.patch(ACCESS, json={"decisions": decisions})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["access"]


def test_first_use_records_pending_without_hardware_or_marking_app_broken(make_runtime):
    runtime, client = make_runtime()
    initial = client.get(ACCESS).get_json()
    assert initial["owner_can_decide"] is True
    assert initial["access"]["enforced"] is True
    assert statuses(initial["access"]) == {"robot.camera": "unrequested", "robot.speak": "unrequested"}
    job = call(client)
    assert job["state"] == "failed"
    assert job["result"]["ok"] is False
    assert job["result"]["permission_required"] is True
    assert job["result"]["state"] == "permission_required"
    assert job["result"]["app"] == APP and job["result"]["permissions"] == ["robot.speak"]
    assert runtime.app_host.robot.actions == []
    access = client.get(ACCESS).get_json()["access"]
    assert access["pending"] == ["robot.speak"]
    assert client.get(f"/api/apps/{APP}").get_json()["app"]["health"]["state"] == "ready"
    assert next(app for app in client.get("/api/apps").get_json()["apps"] if app["id"] == APP)["access"] == access


def test_grant_does_not_replay_and_survives_restart(make_runtime):
    runtime, client = make_runtime()
    first = call(client)
    access = decide(client, **{"robot.speak": "granted"})
    assert access["pending"] == []
    assert runtime.app_host.robot.actions == []
    assert client.get(f"/api/app-jobs/{first['id']}").get_json()["job"]["result"]["permission_required"]
    assert call(client)["state"] == "done"
    assert runtime.app_host.robot.actions == [{"speak": "hello"}]
    data_dir = runtime.config.data_dir
    runtime.stop()
    restarted, second = make_runtime(data_dir=data_dir, install=False)
    assert statuses(second.get(ACCESS).get_json()["access"])["robot.speak"] == "granted"
    assert call(second)["result"]["ok"] is True
    assert restarted.app_host.robot.actions == [{"speak": "hello"}]


def test_denial_survives_repeated_calls_without_reprompt(make_runtime):
    runtime, client = make_runtime()
    call(client)
    decide(client, **{"robot.speak": "denied"})
    before = runtime.app_host.permission_store.path.read_bytes()
    for _ in range(2):
        job = call(client)
        assert job["result"]["state"] == "permission_denied"
        assert client.get(ACCESS).get_json()["access"]["pending"] == []
    assert runtime.app_host.permission_store.path.read_bytes() == before
    assert runtime.app_host.robot.actions == []


def test_optional_capability_is_requested_only_when_selected_and_batch_preflight_prevents_partial_action(make_runtime):
    runtime, client = make_runtime()
    decide(client, **{"robot.speak": "granted"})
    assert call(client, "optional")["result"]["ok"]
    assert statuses(client.get(ACCESS).get_json()["access"])["robot.camera"] == "unrequested"
    result = call(client, "optional", {"camera": True})["result"]
    assert result["permissions"] == ["robot.camera"]
    assert len(runtime.app_host.robot.actions) == 1 and runtime.app_host.robot.cameras == []
    decide(client, **{"robot.camera": "granted"})
    assert call(client, "optional", {"camera": True})["result"]["ok"]
    assert len(runtime.app_host.robot.actions) == 2 and len(runtime.app_host.robot.cameras) == 1


@pytest.mark.parametrize("address,host,headers", [
    ("192.168.1.50", "localhost", {}), ("10.0.0.3", "robbi.local", {"X-Forwarded-For": "127.0.0.1"}),
    ("127.0.0.1", "evil.example", {}), ("::1", "evil.example", {}),
])
def test_unauthenticated_network_or_rebinding_cannot_decide_permissions(make_runtime, address, host, headers):
    runtime, client = make_runtime(auth_enabled=True)
    options = {"environ_overrides": {"REMOTE_ADDR": address}, "headers": {"Host": host, **headers}}
    assert client.get(ACCESS, **options).status_code == (403 if host == "evil.example" else 401)
    response = client.patch(ACCESS, json={"decisions": {"robot.speak": "granted"}}, **options)
    assert response.status_code == (403 if host == "evil.example" else 401)
    assert statuses(runtime.app_host.permission_snapshot(APP))["robot.speak"] == "unrequested"


@pytest.mark.parametrize("operation", ["scan", "install", "enable", "remove"])
def test_remote_cannot_replace_or_reenable_approved_project_when_sign_in_is_enabled(make_runtime, monkeypatch, operation):
    runtime, client = make_runtime()
    decide(client, **{"robot.speak": "granted"})
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "owner-secret")
    runtime.config.values["server"]["auth_enabled"] = True
    options = {"environ_overrides": {"REMOTE_ADDR": "192.168.1.50"}}
    if operation == "scan":
        response = client.post("/api/apps/scan", json={}, **options)
    elif operation == "install":
        response = client.post("/api/apps/install", data={"file": (io.BytesIO(package(["robot.speak"])), "replacement.zip")},
                               content_type="multipart/form-data", **options)
    elif operation == "enable":
        response = client.post(f"/api/apps/{APP}/enabled", json={"enabled": False}, **options)
    else:
        response = client.delete(f"/api/apps/{APP}", json={}, **options)
    assert response.status_code == 401
    assert runtime.app_host.enabled(APP) is True
    assert statuses(runtime.app_host.permission_snapshot(APP))["robot.speak"] == "granted"
    assert "robot.camera" in runtime.app_host.project(APP).manifest["permissions"]


@pytest.mark.parametrize("operation", ["scan", "install", "enable", "remove"])
def test_local_owner_can_manage_trusted_projects(make_runtime, operation):
    runtime, client = make_runtime()
    if operation == "scan":
        response = client.post("/api/apps/scan", json={})
    elif operation == "install":
        response = client.post("/api/apps/install", data={"file": (io.BytesIO(package(["robot.speak"])), "replacement.zip")},
                               content_type="multipart/form-data")
    elif operation == "enable":
        response = client.post(f"/api/apps/{APP}/enabled", json={"enabled": False})
    else:
        response = client.delete(f"/api/apps/{APP}", json={})
    assert response.status_code == (201 if operation == "install" else 200)
    if operation in {"install", "enable"}:
        assert runtime.app_host.enabled(APP) is False
    if operation == "remove":
        assert APP not in runtime.app_host.packages.projects


@pytest.mark.parametrize("authorization", ["Bearer owner-secret", "Basic b3duZXI6b3duZXItc2VjcmV0"])
def test_existing_authenticated_network_owner_can_decide(make_runtime, monkeypatch, authorization):
    _, client = make_runtime(auth_enabled=True)
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "owner-secret")
    options = {"environ_overrides": {"REMOTE_ADDR": "192.168.1.50"}, "headers": {"Host": "robbi.local"}}
    assert client.patch(ACCESS, json={"decisions": {"robot.speak": "granted"}}, **options).status_code == 401
    options["headers"]["Authorization"] = authorization
    response = client.patch(ACCESS, json={"decisions": {"robot.speak": "granted"}}, **options)
    assert response.status_code == 200
    assert response.get_json()["owner_can_decide"] is True
    assert statuses(response.get_json()["access"])["robot.speak"] == "granted"


@pytest.mark.parametrize("origin", ["null", "https://unrelated.example"])
def test_custom_iframe_or_cross_origin_write_cannot_approve(make_runtime, origin):
    _, client = make_runtime()
    response = client.patch(ACCESS, json={"decisions": {"robot.speak": "granted"}}, headers={"Origin": origin})
    assert response.status_code == 403


def test_toolkit_has_no_approval_api_and_http_tool_arguments_cannot_grant(make_runtime):
    runtime, client = make_runtime()
    result = call(client, "self-approve")["result"]
    assert result is None or not result.get("ok", False)
    assert statuses(client.get(ACCESS).get_json()["access"])["robot.speak"] == "unrequested"
    response = client.post(f"/api/apps/{APP}/tools/speak", json={"arguments": {"decisions": {"robot.speak": "granted"}}})
    assert response.status_code == 400
    assert runtime.app_host.robot.actions == []


@pytest.mark.parametrize("decisions", [{}, {"robot.motion": "granted"}, {"robot.speak": True}, {"robot.speak": "allow"}])
def test_owner_decisions_are_schema_checked_and_cannot_add_undeclared_access(make_runtime, decisions):
    _, client = make_runtime()
    assert client.patch(ACCESS, json={"decisions": decisions}).status_code == 400


def test_development_bypass_allows_undeclared_sdk_operation_without_changing_grants(make_runtime):
    runtime, client = make_runtime(enabled=False, declared=[])
    access = client.get(ACCESS).get_json()["access"]
    assert access["enforced"] is False
    assert set(statuses(access).values()) == {"bypassed"}
    assert call(client, "camera")["result"]["ok"]
    assert len(runtime.app_host.robot.cameras) == 1
    assert not runtime.app_host.permission_store.path.exists()


def test_removal_clears_grants_but_replacement_keeps_per_id_owner_decisions(make_runtime):
    runtime, client = make_runtime()
    decide(client, **{"robot.speak": "granted"})
    runtime.app_host.install(package(["robot.speak", "robot.camera", "robot.tof"]))
    access = client.get(ACCESS).get_json()["access"]
    assert statuses(access)["robot.speak"] == "granted" and statuses(access)["robot.tof"] == "unrequested"
    assert runtime.app_host.enabled(APP) is False
    assert client.delete(f"/api/apps/{APP}", json={}).status_code == 200
    runtime.app_host.install(package(["robot.speak", "robot.camera"]))
    assert statuses(client.get(ACCESS).get_json()["access"])["robot.speak"] == "unrequested"


def test_revoking_grant_invalidates_completed_sdk_output_signal(make_runtime):
    runtime, client = make_runtime()
    decide(client, **{"robot.speak": "granted"})
    assert call(client)["result"]["ok"]
    signal = runtime.app_host.robot.signals[0]
    assert signal() is False
    decide(client, **{"robot.speak": "denied"})
    assert signal() is True
    assert call(client)["result"]["state"] == "permission_denied"
    assert len(runtime.app_host.robot.actions) == 1


def test_revoking_active_grant_releases_caller_and_blocks_late_hardware(make_runtime, monkeypatch):
    runtime, client = make_runtime()
    decide(client, **{"robot.speak": "granted"})
    entered, release = threading.Event(), threading.Event()
    results = []

    def handler(context, tool, arguments):
        context.require("robot.speak")
        entered.set()
        assert release.wait(3)
        context.robot.speak("late")
        return {"ok": True}

    from robbi.app_runtime import sandbox
    monkeypatch.setattr(sandbox, "run", lambda project, context, tool, arguments: handler(context, tool, arguments))
    waiter = threading.Thread(target=lambda: results.append(runtime.app_host.call(APP, "speak", {})))
    try:
        waiter.start()
        assert entered.wait(1)
        decide(client, **{"robot.speak": "unrequested"})
        waiter.join(.5)
        assert not waiter.is_alive() and results[0]["cancelled"]
        release.set()
        deadline = time.monotonic() + 2
        while runtime.app_host._active and time.monotonic() < deadline:
            threading.Event().wait(.005)
        assert not runtime.app_host._active
        assert runtime.app_host.robot.actions == []
    finally:
        release.set()
        waiter.join(3)


def test_permission_bypass_profile_changes_require_owner_and_restart(make_runtime):
    runtime, client = make_runtime(auth_enabled=True)
    payload = {"yaml": "apps:\n  permissions_enabled: false\n"}
    response = client.post("/api/config", json=payload, environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert response.status_code == 401
    assert not runtime.config.source.exists()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer owner-secret"
    response = client.post("/api/config", json=payload)
    assert response.status_code == 200 and response.get_json()["restart_required"]
    assert client.get(ACCESS).get_json()["access"]["enforced"] is True
    response = client.post("/settings_data", json={"apps": {"permissions_enabled": False}})
    assert response.status_code == 400 and "restart" in response.get_json()["error"]


def test_trusted_network_owner_must_still_manually_grant_each_app_capability(make_runtime, monkeypatch):
    runtime, client = make_runtime()
    # A previously configured password must not silently turn sign-in back on.
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "unused-existing-password")
    remote = {"REMOTE_ADDR": "192.168.1.50"}
    access = client.get(ACCESS, environ_overrides=remote).get_json()
    assert access["owner_can_decide"] is True and access["access"]["enforced"] is True
    assert call(client)["result"]["permission_required"]
    assert runtime.app_host.robot.actions == []
    result = client.patch(ACCESS, json={"decisions": {"robot.speak": "granted"}}, environ_overrides=remote)
    assert result.status_code == 200
    assert runtime.app_host.robot.actions == []  # Approval does not replay work.
    assert call(client)["result"]["ok"] is True
    assert statuses(runtime.app_host.permission_snapshot(APP))["robot.camera"] == "unrequested"


@pytest.mark.parametrize("value", [0, 1, "false", None, []])
def test_permission_enforcement_requires_strict_boolean(value):
    with pytest.raises(ValidationError, match="true or false"):
        validate({"apps": {"permissions_enabled": value}})


def test_safe_default_and_corrupt_boot_profile_keep_permission_checks_on(tmp_path):
    root = tmp_path / "empty-project"
    root.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text("apps:\n  permissions_enabled: 'false'\n")
    config = load(root, profile, tmp_path / "state")
    assert config.issues
    assert config.values["apps"]["permissions_enabled"] is True


def test_bundled_image_preflight_requires_network_and_display_before_provider_call(make_runtime):
    from PIL import Image
    from robbi.app_services.images import GeneratedImage

    runtime, client = make_runtime()
    image = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(image, "PNG")
    generated = []

    def generate(*args, **kwargs):
        generated.append(True)
        return GeneratedImage(image.getvalue(), "image/png", "offline", "test", 40, 30)

    runtime.app_host._services["images"] = SimpleNamespace(generate=generate)
    args = {"prompt": "A simple diagram", "show_on_screen": False}
    first = runtime.app_host.call("image-studio", "generate", args)
    assert first["permission_required"] and first["permissions"] == ["network", "robot.display"]
    assert generated == []
    endpoint = "/api/apps/image-studio/permissions"
    assert client.patch(endpoint, json={"decisions": {"network": "granted"}}).status_code == 200
    second = runtime.app_host.call("image-studio", "generate", args)
    assert second["permissions"] == ["robot.display"]
    assert generated == []
    assert client.patch(endpoint, json={"decisions": {"robot.display": "granted"}}).status_code == 200
    assert generated == []  # Approval never replays the provider request.
    third = runtime.app_host.call("image-studio", "generate", args)
    assert third["ok"] and len(third["artifacts"]) == 1
    assert generated == [True]


def test_bundled_sensor_preflight_requests_display_only_for_selected_screen_output(make_runtime):
    runtime, client = make_runtime()
    reads = []
    runtime.app_host.robot.tof = lambda sensor: reads.append(sensor) or {
        "sensor": sensor, "enabled": True, "available": True, "fresh": True, "distance_cm": 42,
    }
    runtime.app_host.robot.imu = lambda: reads.append("imu") or {
        "enabled": True, "available": True, "fresh": True, "orientation_deg": {"roll": 0, "pitch": 0, "yaw": 0},
        "acceleration_m_s2": [0, 0, 9.81], "gyro_deg_s": [0, 0, 0],
    }
    endpoint = "/api/apps/sensor-check/permissions"
    first = runtime.app_host.call("sensor-check", "inspect", {})
    assert first["permissions"] == ["robot.imu", "robot.tof"]
    assert reads == []
    access = client.get(endpoint).get_json()["access"]
    assert statuses(access)["robot.display"] == "unrequested"
    assert client.patch(endpoint, json={"decisions": {"robot.tof": "granted", "robot.imu": "granted"}}).status_code == 200
    assert runtime.app_host.call("sensor-check", "inspect", {})["ok"]
    assert reads == ["h1", "h2", "imu"]
    optional = runtime.app_host.call("sensor-check", "inspect", {"show_on_screen": True})
    assert optional["permissions"] == ["robot.display"]
    assert reads == ["h1", "h2", "imu"]  # Display preflight precedes even sensor reads.
