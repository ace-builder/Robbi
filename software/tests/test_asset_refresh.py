from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from robbi.config import load
from robbi.runtime import Runtime
from robbi.web import create_app
from robot import assets as asset_module
from robot.assets import AssetRegistry
from robot.controller import RobotController


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_module, "ASSETS_DIR", tmp_path / "assets")
    monkeypatch.setattr(asset_module, "ANIMATIONS_DIR", tmp_path / "animations")
    faces = tmp_path / "assets" / "images" / "faces"
    faces.mkdir(parents=True)
    (faces / "old.png").write_bytes(b"catalogue test")
    registry = AssetRegistry()
    registry.scan()
    controller = SimpleNamespace(assets=registry, _diagnostics=SimpleNamespace(invalidate=Mock()),
        motion=SimpleNamespace(stop=lambda: None), slam=SimpleNamespace(stop=lambda: None),
        media=SimpleNamespace(stop_audio=lambda: None), stop=lambda: None)
    controller.refresh_asset_catalog = RobotController.refresh_asset_catalog.__get__(controller)
    controller.status = Mock(side_effect=AssertionError("Asset refresh must not run full diagnostics"))
    runtime = Runtime(load(Path(__file__).resolve().parents[1], tmp_path / "config.yaml", tmp_path / "state"))
    runtime._controller = controller
    runtime._ready.set()
    runtime._snapshot = {"assets": {"faces": ["old.png"]}, "other_section": "unchanged"}
    return runtime, create_app(runtime), controller, registry, faces


def test_explicit_refresh_returns_new_assets_without_full_status(tmp_path, monkeypatch):
    runtime, app, controller, _, faces = fixture(tmp_path, monkeypatch)
    (faces / "new.png").write_bytes(b"new catalogue entry")
    client = app.test_client()
    assert client.get("/status").get_json()["assets"]["faces"] == ["old.png"]
    response = client.get("/status?refresh_assets=1")
    assert response.status_code == 200
    assert response.get_json()["assets"]["faces"] == ["new.png", "old.png"]
    assert response.get_json()["other_section"] == "unchanged"
    assert client.get("/status").get_json()["assets"]["faces"] == ["new.png", "old.png"]
    controller._diagnostics.invalidate.assert_called_once_with("assets")
    controller.status.assert_not_called()
    runtime.stop()


def test_slow_scan_keeps_catalogue_health_and_stop_available(tmp_path, monkeypatch):
    runtime, app, _, registry, faces = fixture(tmp_path, monkeypatch)
    (faces / "new.png").write_bytes(b"new catalogue entry")
    entered, release = threading.Event(), threading.Event()
    original = registry._load_dir
    def blocked(directory, suffix=None):
        if directory == faces:
            entered.set()
            assert release.wait(2)
        return original(directory, suffix)
    monkeypatch.setattr(registry, "_load_dir", blocked)
    responses = []
    worker = threading.Thread(target=lambda: responses.append(app.test_client().get("/status?refresh_assets=1")))
    worker.start()
    try:
        assert entered.wait(1)
        client = app.test_client()
        assert registry.names("faces") == ["old.png"]
        assert client.get("/status?refresh_assets=true").status_code == 429
        assert client.get("/api/health").status_code == 200
        assert client.get("/status").status_code == 200
        assert client.post("/api/stop", json={}).status_code == 200
    finally:
        release.set()
        worker.join(2)
        runtime.stop()
    assert not worker.is_alive()
    assert responses[0].get_json()["assets"]["faces"] == ["new.png", "old.png"]


def test_scan_failure_returns_error_and_keeps_previous_complete_catalogue(tmp_path, monkeypatch):
    runtime, app, _, registry, faces = fixture(tmp_path, monkeypatch)
    (faces / "new.png").write_bytes(b"new catalogue entry")
    original = registry._load_dir
    def failing(directory, suffix=None):
        if directory.name == "sounds":
            raise OSError("Asset directory unavailable")
        return original(directory, suffix)
    monkeypatch.setattr(registry, "_load_dir", failing)
    response = app.test_client().get("/status?refresh_assets=1")
    assert response.status_code == 503
    assert "Asset directory unavailable" in response.get_json()["error"]
    assert registry.names("faces") == ["old.png"]
    assert runtime.snapshot()["assets"]["faces"] == ["old.png"]
    monkeypatch.setattr(registry, "_load_dir", original)
    assert app.test_client().get("/status?refresh_assets=1").status_code == 200
    runtime.stop()


def test_direct_controller_refresh_overrides_concurrently_cached_assets():
    host = SimpleNamespace(refresh_asset_catalog=lambda: {"faces": ["new.png"]},
        _diagnostics=SimpleNamespace(sample=lambda **_: {"assets": {"faces": ["old.png"]}}))
    assert RobotController.status(host, refresh_assets=True)["assets"]["faces"] == ["new.png"]
