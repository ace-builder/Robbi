"""Real server and real stores, no robot, credentials or external services."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("robbi-http")
    config = sandbox / "local.yaml"
    config.write_text("camera: {enabled: false}\n", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("ROBBI_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    log = (sandbox / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "robbi", "--root", str(ROOT), "--config", str(config),
                               "--data-dir", str(sandbox / "data"), "--port", str(port)], env=env, stdout=log, stderr=log)
    def request(path, data=None, method=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            response = urllib.request.urlopen(req, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail((sandbox / "server.log").read_text(encoding="utf-8"))
            try:
                if request("/status")[0] == 200:
                    break
            except (OSError, TimeoutError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Offline server did not become ready: " + (sandbox / "server.log").read_text(encoding="utf-8"))
        yield request
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def test_all_local_read_surfaces_work_without_hardware(server):
    for route in ["/api/health", "/status", "/settings_data", "/apps", "/today_data", "/productivity_data",
                  "/memory_data", "/kids/status", "/onboarding_data", "/slam/status", "/slam/map", "/imu/data", "/wake_refs",
                  "/api/background-work", "/api/autonomy"]:
        status, payload = server(route)
        assert status == 200, (route, payload)
    health = server("/api/health")[1]
    assert any(entry["name"] == "motion" and entry["state"] == "disabled" for entry in health["components"])
    assert server("/photo")[0] == 503
    assert server("/video_feed")[0] == 503


def test_autonomy_controls_and_day_cycle_are_available_without_devices(server):
    status, payload = server("/api/autonomy")
    assert status == 200 and payload["owner_can_resume"]
    assert payload["day_cycle"]["phase"] in {"night", "morning", "day", "evening"}
    assert server("/api/autonomy/pause", {})[0] == 200
    assert server("/api/autonomy")[1]["policy"]["paused"]
    assert server("/api/autonomy/resume", {})[0] == 200
    assert not server("/api/autonomy")[1]["policy"]["paused"]
    assert server("/api/stop", {})[0] == 200
    assert server("/api/autonomy")[1]["policy"]["paused"]
    assert server("/api/autonomy/resume", {})[0] == 200


def test_invalid_autonomy_settings_preserve_current_profile(server):
    before = server("/settings_data")[1]
    for patch in ({"day_cycle": {"timezone": "Mars/Olympus"}},
                  {"day_cycle": {"sleep_hour": 8}},
                  {"autonomy": {"allow_exploration": "false"}},
                  {"background_work": {"max_pending": 50}}):
        assert server("/settings_data", patch)[0] == 400
    assert server("/settings_data")[1] == before


def test_real_http_notes_and_lists_crud(server):
    status, result = server("/notes", {"title": "Integration note", "body": "Offline persistence works"})
    assert status == 200, result
    note_id = result["note"]["id"]
    assert server(f"/notes/{note_id}", {"title": "Updated note"}, "PATCH")[1]["note"]["title"] == "Updated note"
    assert any(note["id"] == note_id for note in server("/notes")[1]["notes"])
    assert server(f"/notes/{note_id}", method="DELETE")[1]["ok"]
    status, result = server("/lists/items", {"title": "Offline task", "list_name": "todo"})
    assert status == 200, result
    item_id = result["item"]["id"]
    assert server(f"/lists/items/{item_id}", {"done": True}, "PATCH")[1]["ok"]
    assert server(f"/lists/items/{item_id}", method="DELETE")[1]["ok"]


def test_malformed_settings_and_missing_devices_do_not_break_http(server):
    assert server("/settings_data", {"motion": {"wheel_base_cm": -1}})[0] == 400
    assert server("/settings_data", {"motion": {"encoder_enabled": "yes"}})[0] == 400
    assert server("/api/stop", {})[0] == 200
    assert server("/api/health")[0] == 200


def test_real_http_keeps_original_route_surface(server):
    routes = {(item["path"], item["method"]) for item in server("/api/routes")[1]["routes"]}
    required = {("/text", "POST"), ("/drive", "POST"), ("/drive_raw", "POST"), ("/apps/install", "POST"),
                ("/kids/input", "POST"), ("/onboarding_apply", "POST"), ("/wake_model", "POST"),
                ("/slam/start_explore", "POST"), ("/filming_sequence", "POST"), ("/imu/calibrate", "POST")}
    assert required <= routes
