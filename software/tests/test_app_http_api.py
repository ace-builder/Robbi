import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest
import yaml

from robbi.config import load
from robbi.runtime import Runtime
from robbi.web import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, trusted_app_runner):
    runtime = Runtime(load(ROOT, tmp_path / "profile.yaml", tmp_path / "data"))
    token = (0, threading.Event())
    runtime._controller = SimpleNamespace(_action_token=lambda: token, stop=lambda: None,
                                         cancel_actions=lambda: token[1].set(),
                                         motion=SimpleNamespace(stop=lambda: None),
                                         slam=SimpleNamespace(stop=lambda: None), media=SimpleNamespace(stop_audio=lambda: None))
    runtime._snapshot = {}
    runtime._ready.set()
    runtime.app_host.scan()
    yield runtime
    runtime.stop()


def finished(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(f"/api/app-jobs/{job_id}").get_json()["job"]
        if result["state"] in {"done", "failed", "cancelled"}:
            return result
        threading.Event().wait(.005)
    pytest.fail("App job did not finish")


def test_catalog_and_calculator_job_work_without_optional_network(runtime):
    client = create_app(runtime).test_client()
    catalog = client.get("/api/apps").get_json()
    assert {app["id"] for app in catalog["apps"]} == {
        "image-studio", "weather", "quick-notes", "robot-status", "calculator", "sensor-check",
        "tic-tac-toe", "memory-colours", "higher-lower", "expressions", "kitchen-timers",
        "checklists", "decision-helper",
        "gmail", "google-calendar", "google-maps", "web-search", "random-number",
        "world-clock", "date-calculator", "percentage-helper", "tip-splitter", "recipe-scaler",
        "unit-converter", "text-toolbox", "password-generator", "habit-tracker", "expense-tracker",
        "packing-list", "bookmarks",
        "ready-to-leave", "where-did-i-put-it", "fridge-rescue", "home-care", "meeting-prep",
        "teach-me", "decision-notebook", "rhythm-relay", "tiny-detective", "robot-charades",
    }
    assert catalog["errors"] == []
    response = client.post("/api/apps/calculator/tools/calculate", json={"arguments": {"expression": "18*24"}})
    assert response.status_code == 202
    result = finished(client, response.get_json()["job"]["id"])
    assert result["state"] == "done"
    assert result["result"]["data"]["value"] == 432
    assert "_signal" not in json.dumps(result)


def test_config_uses_env_names_and_bad_arguments_do_not_run(runtime, monkeypatch):
    monkeypatch.setenv("TEST_IMAGE_SECRET", "not-returned-secret")
    client = create_app(runtime).test_client()
    response = client.patch("/api/apps/image-studio/config", json={"config": {"api_key_env": "TEST_IMAGE_SECRET"}})
    assert response.status_code == 200
    assert b"not-returned-secret" not in client.get("/api/apps/image-studio").data
    assert client.patch("/api/apps/image-studio/config", json={"config": {"api_key": "not-allowed"}}).status_code == 400
    assert client.patch("/api/apps/image-studio/config", json={"config": {"api_key_env": "sk-literal-key"}}).status_code == 400
    assert client.post("/api/apps/weather/tools/forecast", json={"arguments": {"latitude": 300}}).status_code == 400
    assert client.post("/api/apps/weather/enabled", json={"enabled": False}).status_code == 200
    assert client.post("/api/apps/weather/tools/forecast", json={"arguments": {}}).status_code == 503


def test_custom_page_keeps_sandbox_csp_and_confined_assets(runtime):
    client = create_app(runtime).test_client()
    page = client.get("/api/apps/quick-notes/page")
    assert page.status_code == 200
    assert "sandbox allow-scripts" in page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in page.headers["Content-Security-Policy"]
    assert "connect-src 'none'" in page.headers["Content-Security-Policy"]
    assert b'/api/apps/quick-notes/files/dashboard/' in page.data
    assert client.get("/api/apps/quick-notes/files/main.py").status_code == 400
    assert client.get("/api/apps/quick-notes/files/../../pyproject.toml").status_code == 400
    assert "frame-src 'self'" in client.get("/").headers["Content-Security-Policy"]


def test_config_can_clear_optional_coordinates_and_restore_defaults(runtime):
    client = create_app(runtime).test_client()
    endpoint = "/api/apps/weather/config"
    assert client.patch(endpoint, json={"config": {"latitude": 52, "longitude": 13, "cache_ttl_s": 300}}).status_code == 200
    assert client.patch(endpoint, json={"config": {"latitude": None, "longitude": None, "cache_ttl_s": None}}).status_code == 200
    config = client.get("/api/apps/weather").get_json()["config"]
    assert "latitude" not in config and "longitude" not in config
    assert config["cache_ttl_s"] == 600
    assert client.patch(endpoint, json={"config": {"unknown": None}}).status_code == 400


@pytest.mark.parametrize("annotation,value", [("x-order", True), ("x-order", 1001), ("x-advanced", "yes")])
def test_schema_rejects_invalid_dashboard_annotations(annotation, value):
    from robbi.app_runtime.schema import check_schema
    with pytest.raises(ValueError):
        check_schema({"type": "string", annotation: value})


def test_nested_config_reset_preserves_other_fields():
    from robbi.app_runtime.schema import patch_config, validate_value
    schema = {"type": "object", "properties": {"home": {"type": "object", "properties": {
        "latitude": {"type": "number"}, "city": {"type": "string", "default": "Berlin"}}}}}
    patch = patch_config(schema, {"home": {"latitude": 52, "city": "Paris"}}, {"home": {"latitude": None}})
    assert validate_value(schema, patch) == {"home": {"city": "Paris"}}


def test_zip_import_is_disabled_until_operator_enables_it(runtime):
    client = create_app(runtime).test_client()
    manifest = {"api_version": 2, "id": "sample", "name": "Sample", "tools": [{"name": "greet", "description": "Say hello"}]}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("sample/app.yaml", yaml.safe_dump(manifest))
        archive.writestr("sample/main.py", "def run(context, tool_name, arguments):\n    return {'text': 'Hello'}\n")
    imported = client.post("/api/apps/install", data={"file": (io.BytesIO(output.getvalue()), "sample.zip")}, content_type="multipart/form-data")
    assert imported.status_code == 201
    assert imported.get_json()["app"]["enabled"] is False
    assert client.post("/api/apps/sample/tools/greet", json={"arguments": {}}).status_code == 503
    assert client.post("/api/apps/sample/enabled", json={"enabled": True}).status_code == 200
    response = client.post("/api/apps/sample/tools/greet", json={"arguments": {}})
    assert finished(client, response.get_json()["job"]["id"])["result"]["text"] == "Hello"
    assert client.delete("/api/apps/sample", json={}).status_code == 200
    removed = client.delete("/api/apps/calculator", json={})
    assert removed.status_code == 200 and removed.get_json()["data_preserved"]
    assert "calculator" not in runtime.app_host.packages.projects
    assert client.post("/api/apps/calculator/restore", json={}).status_code == 200


def test_oversized_directory_is_rejected_before_zipfile_allocation(runtime, monkeypatch):
    from robbi.app_runtime import packages
    import struct
    # EOCD claims 60,000 entries. ZipFile must never see it.
    archive = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 60000, 60000, 2000000, 0, 0)
    monkeypatch.setattr(packages.zipfile, "ZipFile", lambda *_: pytest.fail("unbounded directory materialized"))
    with pytest.raises(ValueError, match="256"):
        runtime.app_host.install(archive)
