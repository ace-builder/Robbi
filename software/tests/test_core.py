import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from robbi.actions import validate_action
from robbi.config import RuntimeConfig, load, merge, parse_yaml, validate
from robbi.errors import BusyError, ValidationError
from robbi.jobs import BoundedExecutor, JobRegistry
from robbi.runtime import Runtime
from robbi.storage import atomic_write
from robbi.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def configuration(tmp_path):
    return load(ROOT, tmp_path / "config.yaml", tmp_path / "state")


def test_missing_config_has_safe_defaults(tmp_path):
    config = configuration(tmp_path)
    assert config.values["hardware"]["enable_motors"] is False
    assert config.values["telegram"]["enabled"] is False
    assert config.port == 8082


@pytest.mark.parametrize("text", ["x: [", "[1,2]", "camera: {fps: .nan}", "loop: &x {again: *x}", "providers: {api_key: secret}"])
def test_bad_config_is_reported_with_safe_startup(tmp_path, text):
    source = tmp_path / "config.yaml"
    source.write_text(text)
    config = configuration(tmp_path)
    assert config.issues
    assert not config.values["hardware"]["enable_motors"]
    assert source.read_text() == text


def test_invalid_save_keeps_valid_config(tmp_path):
    config = configuration(tmp_path)
    config.save("camera: {fps: 2}\n")
    with pytest.raises(ValidationError):
        config.save("camera: {fps: 500}\n")
    assert "fps: 2" in config.source.read_text()


def test_motor_enable_requires_calibration(tmp_path):
    config = configuration(tmp_path)
    with pytest.raises(ValidationError, match="calibration"):
        validate(merge(config.values, {"hardware": {"enable_motors": True}}))


@pytest.mark.parametrize("group,key,value", [
    ("runtime", "feature_task_limit", 7),
    ("runtime", "feature_task_limit", 16.5),
    ("runtime", "feature_task_limit", 33),
    ("voice", "local_wake_max_references", 100),
    ("voice", "local_wake_max_reference_s", 60),
    ("voice", "local_wake_max_reference_bytes", 100_000_000),
    ("voice", "local_wake_max_total_feature_frames", 100_000),
    ("voice", "local_wake_buffer_s", 60),
    ("voice", "local_wake_slide_s", 0),
    ("voice", "chunk_ms", 1),
    ("audio", "mic_sample_rate", 192000),
])
def test_workload_limits_reject_unbounded_configuration(tmp_path, group, key, value):
    config = configuration(tmp_path)
    with pytest.raises(ValidationError):
        config.save(f"{group}: {{{key}: {value}}}\n")
    assert not config.source.exists()


def test_queue_limit_and_shutdown_cancel_pending():
    ready = threading.Event()
    release = threading.Event()
    executor = BoundedExecutor(max_workers=1, max_pending=1)
    running = executor.submit(lambda: (ready.set(), release.wait(3)))
    assert ready.wait(1)
    pending = executor.submit(lambda: "should not run")
    with pytest.raises(BusyError):
        executor.submit(lambda: None)
    executor.shutdown(wait=False, cancel_futures=True)
    assert pending.cancelled()
    release.set()
    running.result(timeout=2)


def test_worker_recovers_after_exception():
    with BoundedExecutor() as executor:
        failed = executor.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            failed.result(2)
        assert executor.submit(lambda: 42).result(2) == 42


@pytest.mark.parametrize("action", [{"sequence": ["bad"]}, {"wait_ms": -1}, {"face": "../../file"},
                                         {"speak": {}}, {"drive": {"speed": float("nan")}}, {"sequence": [{}] * 65}])
def test_malformed_actions_rejected(action):
    with pytest.raises(ValidationError):
        validate_action(action)


def test_diagnostics_work_during_blocked_startup(tmp_path):
    release = threading.Event()
    class Controller:
        def start(self): release.wait(3)
        def status(self): return {"test": True}
        def stop(self): release.set()
    runtime = Runtime(configuration(tmp_path), Controller)
    app = create_app(runtime)
    runtime.start()
    started = time.monotonic()
    response = app.test_client().get("/api/health")
    assert response.status_code == 200
    assert time.monotonic() - started < 0.5
    assert app.test_client().get("/status").status_code == 503
    release.set()
    runtime.stop()


def test_stop_attempts_other_components_after_fault(tmp_path):
    calls = []
    runtime = Runtime(configuration(tmp_path))
    def failed():
        calls.append("motion")
        raise RuntimeError("missing")
    runtime._controller = SimpleNamespace(cancel_actions=lambda: calls.append("actions"),
        motion=SimpleNamespace(stop=failed), slam=SimpleNamespace(stop=lambda: calls.append("mapping")),
        media=SimpleNamespace(stop_audio=lambda: calls.append("audio")))
    assert not runtime.emergency_stop()["ok"]
    assert calls == ["actions", "motion", "mapping", "audio"]
    runtime.jobs.stop()


def test_http_rejects_bad_payloads_and_cross_origin(tmp_path):
    runtime = Runtime(configuration(tmp_path))
    client = create_app(runtime).test_client()
    assert client.post("/api/jobs", json=[1]).status_code == 400
    assert client.post("/api/jobs", data='{"n":NaN}', content_type="application/json").status_code == 400
    assert client.post("/api/stop", json={}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.post("/shutdown", json={}).status_code == 403
    assert client.post("/api/stop", json={}).status_code == 200
    runtime.jobs.stop()


def test_web_token_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "a-test-token")
    runtime = Runtime(configuration(tmp_path))
    runtime.config.values["server"]["auth_enabled"] = True
    client = create_app(runtime).test_client()
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"Authorization": "Bearer a-test-token"}).status_code == 200
    runtime.jobs.stop()


def test_atomic_write_preserves_original_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("old")
    def fail(*_): raise OSError("disk unavailable")
    monkeypatch.setattr("robbi.storage.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]
