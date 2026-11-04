"""Offline HTTP/lifecycle integration: no robot, providers or external network."""
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, request

from robbi.config import load, merge, validate
from robbi.errors import UnavailableError, ValidationError
from robbi.health import HealthRegistry
from robbi.jobs import JobRegistry
from robbi.plugins import AppSpec, PluginRegistry
from robbi.runtime import Runtime
from robbi.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def config(tmp_path):
    return load(ROOT, tmp_path / "config.yaml", tmp_path / "state")


def until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("Condition did not become true before deadline")


class Controller:
    def __init__(self):
        self.motion = SimpleNamespace(stop=lambda: None)
        self.slam = SimpleNamespace(stop=lambda: None)
        self.media = SimpleNamespace(stop_audio=lambda: None)
    def start(self): pass
    def status(self): return {"offline_local_tools": True}
    def stop(self): pass


def test_json_and_multipart_reach_domain_routes_after_outer_validation(tmp_path):
    runtime = Runtime(config(tmp_path))
    runtime._controller = Controller()
    runtime._ready.set()
    legacy = Flask("domain-fixture")
    @legacy.post("/fixture/json")
    def json_endpoint():
        return jsonify(request.get_json())
    @legacy.post("/fixture/upload")
    def upload_endpoint():
        upload = request.files["file"]
        return jsonify(filename=upload.filename, data=upload.read().decode(), title=request.form["title"])
    runtime._legacy_app = legacy
    client = create_app(runtime).test_client()
    payload = {"title": "Water plants", "nested": {"value": 4}}
    response = client.post("/fixture/json", json=payload)
    assert response.status_code == 200
    assert response.get_json() == payload
    response = client.post("/fixture/upload", data={"title": "Voice sample", "file": (io.BytesIO(b"sample bytes"), "voice.wav")})
    assert response.status_code == 200
    assert response.get_json() == {"filename": "voice.wav", "data": "sample bytes", "title": "Voice sample"}
    runtime.stop()


def test_controller_is_not_exposed_until_start_finishes(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Starting(Controller):
        def start(self):
            entered.set()
            release.wait(2)
        def stop(self): release.set()
    runtime = Runtime(config(tmp_path), Starting)
    runtime.start()
    assert entered.wait(1)
    client = create_app(runtime).test_client()
    assert client.get("/healthz").status_code == 200
    with pytest.raises(UnavailableError):
        runtime.controller()
    assert client.post("/api/jobs", json={"operation": "unknown", "payload": {}}).status_code == 400
    assert client.post("/api/jobs", json={"operation": "text", "payload": {"text": "hello"}}).status_code == 503
    release.set()
    until(runtime._ready.is_set)
    assert isinstance(runtime.controller(), Starting)
    runtime.stop()


def test_stop_during_start_does_not_resurrect_runtime(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Starting(Controller):
        def start(self):
            entered.set()
            release.wait(2)
        def stop(self): release.set()
    runtime = Runtime(config(tmp_path), Starting)
    runtime.start()
    assert entered.wait(1)
    runtime.stop()
    runtime._thread.join(1)
    assert runtime.health_snapshot()["state"] == "stopped"
    assert not runtime._ready.is_set()
    states = {entry["name"]: entry["state"] for entry in runtime.health.snapshot()}
    assert states["runtime"] == "stopped"
    with pytest.raises(UnavailableError):
        runtime.controller()


def test_optional_app_load_cannot_block_local_status(tmp_path):
    entered, release = threading.Event(), threading.Event()
    runtime = Runtime(config(tmp_path), Controller)
    # This tests startup isolation, not the speed of scanning assets on disk.
    # Discovery has its own tests; cloud-backed paths can exceed one second.
    runtime.app_host.scan = lambda: {}
    def blocked_plugin():
        entered.set()
        release.wait(5)
    runtime.plugins.load = blocked_plugin
    runtime.start()
    try:
        assert entered.wait(2)
        until(lambda: bool(runtime._snapshot))
        client = create_app(runtime).test_client()
        assert client.get("/status").get_json()["offline_local_tools"]
        assert client.get("/api/health").status_code == 200
    finally:
        release.set()
        runtime.stop()


def test_supervisor_reuses_component_health_and_diagnostics_are_read_only(tmp_path):
    class Sampled(Controller):
        def status(self):
            return {"components": [{"name": "camera", "state": "disabled"}]}
        def health_snapshot(self):
            raise AssertionError("Health must not be collected twice")
        def diagnostics_metrics(self):
            return {"camera": {"samples": 1, "interval_s": 2}}
    runtime = Runtime(config(tmp_path), Sampled)
    runtime.start()
    try:
        until(lambda: bool(runtime._snapshot))
        client = create_app(runtime).test_client()
        payload = client.get("/api/diagnostics").get_json()
        assert payload["snapshot_age_s"] is not None
        assert payload["sections"]["camera"]["samples"] == 1
        states = {entry["name"]: entry["state"] for entry in runtime.health.snapshot()}
        assert states["camera"] == "disabled"
        assert states["status"] == "ready"
    finally:
        runtime.stop()


def test_missing_optional_controller_package_keeps_http_available(tmp_path):
    def missing(): raise ImportError("optional provider SDK unavailable")
    runtime = Runtime(config(tmp_path), missing)
    runtime.start()
    runtime._thread.join(1)
    client = create_app(runtime).test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/health").get_json()["state"] == "degraded"
    assert client.get("/status").status_code == 503
    runtime.stop()


def test_plugin_failure_keeps_actual_cause_and_other_apps(tmp_path, monkeypatch):
    from robbi import plugins
    def broken(): raise RuntimeError("bad optional dependency")
    entries = [SimpleNamespace(name="broken", load=lambda: broken),
               SimpleNamespace(name="works", load=lambda: lambda: AppSpec("works", "Works", "Example", lambda *_: {"speak": "hello"}))]
    monkeypatch.setattr(plugins, "entry_points", lambda **kwargs: entries)
    health = HealthRegistry()
    registry = PluginRegistry(["broken", "works"], tmp_path, health)
    registry.load()
    assert registry.catalog()[0]["id"] == "works"
    failure = next(item for item in health.snapshot() if item["name"] == "app:broken")
    assert "bad optional dependency" in failure["message"]
    assert registry.run("works", "") == {"speak": "hello"}
    with pytest.raises(ValidationError):
        registry.run([], "")


@pytest.mark.parametrize("field", ["worker_threads", "command_queue_size"])
def test_worker_capacities_require_integers(tmp_path, field):
    with pytest.raises(ValidationError, match="integer"):
        validate(merge(config(tmp_path).values, {"runtime": {field: 2.5}}))


def test_expired_queued_job_never_runs_callback():
    entered, release = threading.Event(), threading.Event()
    jobs = JobRegistry(max_pending=1, lease_s=0.02)
    jobs.submit("blocked", lambda: (entered.set(), release.wait(2)))
    assert entered.wait(1)
    callback = Mock()
    queued = jobs.submit("stale", callback)
    threading.Event().wait(0.04)
    release.set()
    until(lambda: jobs.get(queued["id"])["state"] == "cancelled")
    callback.assert_not_called()
    jobs.stop()


def test_slow_legacy_requests_do_not_consume_stop_or_health_capacity(tmp_path):
    runtime = Runtime(config(tmp_path))
    runtime._controller = Controller()
    runtime._ready.set()
    entered, release = [], threading.Event()
    legacy = Flask("slow-domain")
    @legacy.post("/fixture/slow")
    def slow():
        entered.append(True)
        release.wait(2)
        return jsonify(ok=True)
    runtime._legacy_app = legacy
    app = create_app(runtime)
    def send():
        with app.test_client().post("/fixture/slow", json={}) as response:
            assert response.status_code == 200
    workers = [threading.Thread(target=send) for _ in range(2)]
    for worker in workers: worker.start()
    until(lambda: len(entered) == 2)
    client = app.test_client()
    assert client.post("/fixture/slow", json={}).status_code == 429
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/stop", json={}).status_code == 200
    assert client.post("/drive", json={"direction": "stop"}).status_code == 200
    assert client.post("/slam/stop", json={}).status_code == 200
    release.set()
    for worker in workers: worker.join(1)
    with client.post("/fixture/slow", json={}) as response:
        assert response.status_code == 200
    runtime.stop()


@pytest.mark.parametrize("content", [None, "camera: [", "hardware: {enable_motors: true}"])
def test_missing_or_invalid_default_config_keeps_safe_diagnostics(tmp_path, content):
    (tmp_path / "config").mkdir()
    if content is not None:
        (tmp_path / "config" / "desktop.yaml").write_text(content)
    configuration = load(tmp_path)
    assert configuration.issues
    assert not configuration.values["hardware"]["enable_motors"]
    assert not configuration.values["telegram"]["enabled"]
    runtime = Runtime(configuration)
    assert create_app(runtime).test_client().get("/api/health").status_code == 200
    runtime.stop()


def test_inherited_enable_flags_cannot_override_development_profile(tmp_path, monkeypatch):
    flags = ["ROBBI_ENABLE_MOTORS", "ROBBI_ENABLE_LCD", "ROBBI_ENABLE_SERVOS",
             "ROBBI_TELEGRAM_ENABLED", "ROBBI_VOICE_ENABLED", "ROBBI_AUTONOMY_ENABLED",
             "ROBBI_STARTUP_WARMUP_ENABLED", "ROBBI_HEARTBEAT_ENABLED"]
    for key in flags:
        monkeypatch.setenv(key, "true")
    # install_environment intentionally mutates env, isolate those mutations too.
    import os
    monkeypatch.setattr(os, "environ", os.environ.copy())
    configuration = config(tmp_path)
    configuration.install_environment()
    assert all(os.environ[key] == "false" for key in flags)
