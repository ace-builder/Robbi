"""Offline Runtime/HTTP checks including owner gates and Stop handoff races."""
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.app_services.http import HttpClient
from robbi.config import load
from robbi.orchestration import AUTONOMY, current_intent
from robbi.runtime import Runtime
from robbi.web import create_app

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)


class Controller:
    def __init__(self):
        self.token = (0, threading.Event())
        self._action_local = threading.local()
        self.paused = False
        self.resources = {}
        self.prepared, self.spoken = [], []
        self.motion = SimpleNamespace(stop=lambda: None)
        self.slam = SimpleNamespace(stop=lambda: None)
        self.orchestrator = SimpleNamespace(snapshot=lambda: {"resources": self.resources})
        self.media = SimpleNamespace(stop_audio=lambda: None,
                                     health=lambda: {"audio": {"available": True}},
                                     speak_wav_bytes=self.speak)

    def _action_token(self):
        return self.token

    def _action_current(self, generation):
        return generation == self.token[0] and not self.token[1].is_set()

    def cancel_actions(self):
        self.token[1].set()
        self.token = (self.token[0] + 1, threading.Event())
        self.paused = True

    cancel_actions_for_shutdown = cancel_actions

    def autonomy_status(self):
        return {"policy": {"operator_paused": self.paused}}

    def _prepare_tts_audio(self, text):
        self.prepared.append((text, current_intent()))
        return {"wav": b"offline speech fixture"}

    def speak(self, wav, *, wait, cancel_event):
        assert not wait and not cancel_event()
        self.spoken.append((wav, current_intent()))
        return SimpleNamespace(wait=lambda timeout: 0)

    def stop(self):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **kw: pytest.fail("Unexpected live provider request"))
    runtime = Runtime(load(ROOT, tmp_path / "profile.yaml", tmp_path / "state"))
    runtime._controller = Controller()
    runtime._ready.set()
    runtime._snapshot = {"behavior": {"awake": True}, "presentation": {"thinking": False}}
    runtime.morning_briefing._now = lambda: NOW
    runtime.app_host.scan()
    yield runtime
    runtime.stop()
    runtime.briefing_jobs.executor.shutdown(wait=True, cancel_futures=True)
    runtime.app_host._executor.shutdown(wait=True, cancel_futures=True)


def finished(client, identifier):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/morning-briefing/jobs/{identifier}")
        assert response.status_code == 200
        job = response.get_json()["job"]
        if job["state"] in {"done", "failed", "cancelled"}:
            return job
        threading.Event().wait(.005)
    pytest.fail("Briefing job did not finish")


def habits_only(runtime):
    runtime.morning_briefing.configure({"sections": {"weather": False, "calendar": False}})
    runtime.app_host.set_permissions("habit-tracker", {"state": "granted"})


def test_settings_remain_optional_and_editable_before_controller_is_ready(runtime):
    runtime._ready.clear()
    client = create_app(runtime).test_client()
    response = client.get("/api/morning-briefing")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    state = response.get_json()
    assert not state["config"]["enabled"] and not state["config"]["sections"]["mail"]
    assert not state["scheduler_running"] and state["next_run"] is None
    assert client.post("/api/morning-briefing/run", json={"preview": True}).status_code == 503
    patch = {"enabled": True, "time": "09:25", "timezone": "Asia/Kathmandu", "sections": {"calendar": False}}
    response = client.patch("/api/morning-briefing", json=patch)
    assert response.status_code == 200 and response.get_json()["config"]["time"] == "09:25"
    assert response.get_json()["next_run"].endswith("09:25:00+05:45")
    assert client.patch("/api/morning-briefing", json={"enabled": False}).get_json()["next_run"] is None
    restored = Runtime(runtime.config)
    try:
        saved = restored.morning_briefing.snapshot()["config"]
        assert not saved["enabled"] and saved["time"] == "09:25" and saved["timezone"] == "Asia/Kathmandu"
        assert not saved["sections"]["calendar"]
    finally:
        restored.stop()


@pytest.mark.parametrize("method,path", [("get", "/api/morning-briefing"),
                                         ("patch", "/api/morning-briefing"),
                                         ("post", "/api/morning-briefing/run"),
                                         ("get", "/api/morning-briefing/jobs/unknown")])
def test_all_briefing_routes_require_existing_dashboard_authentication(runtime, monkeypatch, method, path):
    runtime.config.values["server"]["auth_enabled"] = True
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "offline-dashboard-secret")
    client = create_app(runtime).test_client()
    kwargs = {"json": {}} if method in {"post", "patch"} else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert client.get("/api/morning-briefing", headers={"Authorization": "Bearer offline-dashboard-secret"}).status_code == 200
    assert not runtime.morning_briefing.path.exists() and not runtime.briefing_jobs._jobs


@pytest.mark.parametrize("method,path", [("patch", "/api/morning-briefing"), ("post", "/api/morning-briefing/run")])
def test_cross_origin_requests_cannot_change_or_run_briefing(runtime, method, path):
    client = create_app(runtime).test_client()
    response = getattr(client, method)(path, json={}, headers={"Origin": "https://another.example"})
    assert response.status_code == 403
    assert not runtime.morning_briefing.path.exists() and not runtime.briefing_jobs._jobs


@pytest.mark.parametrize("path,body", [("/api/morning-briefing", {"time": "24:00"}),
                                       ("/api/morning-briefing", {"enabled": "true"}),
                                       ("/api/morning-briefing", {"sections": {"mail": 1}}),
                                       ("/api/morning-briefing/run", {"preview": "true"}),
                                       ("/api/morning-briefing/run", {"source": "owner"}),
                                       ("/api/morning-briefing/run", [])])
def test_bad_api_settings_and_run_arguments_never_enqueue(runtime, path, body):
    client = create_app(runtime).test_client()
    method = client.post if path.endswith("/run") else client.patch
    assert method(path, json=body).status_code == 400
    assert not runtime.briefing_jobs._jobs and not runtime.morning_briefing.path.exists()


def test_preview_job_preserves_app_permissions_and_uses_its_own_source(runtime):
    client = create_app(runtime).test_client()
    response = client.post("/api/morning-briefing/run", json={"preview": True},
                           headers={"X-Robbi-Conversation": "preview-owner"})
    assert response.status_code == 202
    job = finished(client, response.get_json()["job"]["id"])
    assert job["state"] == "done" and job["operation"] == "morning_briefing"
    result = job["result"]
    assert result["preview"] and not result["ok"] and not result["delivered"]
    assert all(section["permission_required"] for section in result["sections"])
    assert {item["source"] for item in runtime.app_host.history()} == {"morning_briefing"}
    assert not runtime._controller.spoken and not runtime.morning_briefing.snapshot()["config"]["enabled"]
    assert client.get("/api/morning-briefing/jobs/missing").status_code == 404
    assert client.post("/api/morning-briefing/run", json={"preview": True},
                       headers={"X-Robbi-Conversation": "bad value"}).status_code == 400


def test_real_local_app_preview_and_delivery_use_separate_workers_and_output_intent(runtime):
    habits_only(runtime)
    client = create_app(runtime).test_client()
    preview = client.post("/api/morning-briefing/run", json={"preview": True}).get_json()["job"]
    result = finished(client, preview["id"])["result"]
    assert result["ok"] and result["preview"] and not result["delivered"]
    assert result["sections"][0]["id"] == "habits" and not runtime._controller.spoken
    delivery = client.post("/api/morning-briefing/run", json={}).get_json()["job"]
    result = finished(client, delivery["id"])["result"]
    assert result["ok"] and result["delivered"]
    assert len(runtime._controller.spoken) == 1
    intent = runtime._controller.spoken[0][1]
    assert intent.owner == "morning-briefing" and intent.priority == AUTONOMY
    assert time.monotonic() < intent.deadline <= time.monotonic() + 90
    assert runtime.morning_briefing.snapshot()["last_result"]["delivered"]
    assert runtime.morning_briefing.snapshot()["last_scheduled_date"] is None


@pytest.mark.parametrize("snapshot", [{"thinking": True}, {"presentation": {"thinking": True}},
                                       {"behavior": {"awake": False}}, {"focus_timer": {"active": True}},
                                       {"voice": {"processing": True}}, {"audio": {"playing": True}},
                                       {"microphone": {"voice_state": "recording_command"}},
                                       {"microphone": {"voice_state": "armed"}}])
def test_engaged_robot_defers_delivery_but_still_allows_explicit_preview(runtime, snapshot):
    habits_only(runtime)
    runtime._snapshot = snapshot
    client = create_app(runtime).test_client()
    job = client.post("/api/morning-briefing/run", json={}).get_json()["job"]
    result = finished(client, job["id"])["result"]
    assert result["state"] == "busy" and not result["delivered"]
    assert not runtime.app_host.history() and not runtime._controller.spoken
    job = client.post("/api/morning-briefing/run", json={"preview": True}).get_json()["job"]
    assert finished(client, job["id"])["result"]["ok"]


@pytest.mark.parametrize("paused,resources", [(True, {}), (False, {"audio": {"owner": "conversation", "priority": 60}})])
def test_pause_and_current_output_ownership_defer_briefing(runtime, paused, resources):
    runtime._controller.paused = paused
    runtime._controller.resources = resources
    assert not runtime._briefing_ready()
    result = runtime.morning_briefing.run()
    assert result["state"] == "busy" and not runtime.app_host.history()


def test_stop_during_collection_cancels_job_and_does_not_block_http(runtime, monkeypatch):
    entered = threading.Event()

    def call(*args, **kwargs):
        entered.set()
        deadline = time.monotonic() + 2
        while not kwargs["cancellation"]() and time.monotonic() < deadline:
            threading.Event().wait(.005)
        return {"ok": True, "data": {"summary": "Must not be spoken"}}

    monkeypatch.setattr(runtime.app_host, "call", call)
    client = create_app(runtime).test_client()
    response = client.post("/api/morning-briefing/run", json={})
    assert response.status_code == 202 and entered.wait(1)
    assert client.get("/api/morning-briefing").get_json()["running"]
    assert client.post("/api/morning-briefing/run", json={}).status_code == 429
    assert client.post("/api/stop", json={}).status_code == 200
    job = finished(client, response.get_json()["job"]["id"])
    assert job["state"] == "cancelled" and job["result"]["sections"] == []
    assert not runtime._controller.spoken and not runtime.morning_briefing.snapshot()["running"]


def test_runtime_generation_cancels_job_before_worker_starts(runtime, monkeypatch):
    admitted = []
    monkeypatch.setattr(runtime.briefing_jobs, "submit", lambda operation, callback: admitted.append(callback) or {"id": "held"})
    runtime.submit_briefing(preview=True)
    runtime.emergency_stop()
    result = admitted[0]()
    assert result["cancelled"] and not runtime.app_host.history()


@pytest.mark.parametrize("change", ["stop", "disable", "time"])
def test_stop_or_edit_at_worker_handoff_cannot_revive_admitted_job(runtime, monkeypatch, change):
    habits_only(runtime)
    original = runtime.morning_briefing.run

    def at_handoff(**kwargs):
        if change == "stop":
            runtime.emergency_stop()
        else:
            runtime.morning_briefing.configure({"enabled": False} if change == "disable" else {"time": "09:00"})
        return original(**kwargs)

    monkeypatch.setattr(runtime.morning_briefing, "run", at_handoff)
    client = create_app(runtime).test_client()
    response = client.post("/api/morning-briefing/run", json={"preview": True})
    job = finished(client, response.get_json()["job"]["id"])
    assert job["state"] == "cancelled" and job["result"]["cancelled"]
    assert not runtime.app_host.history() and not runtime._controller.spoken


def test_stop_during_tts_preparation_prevents_audio_output(runtime, monkeypatch):
    habits_only(runtime)

    def prepare(text):
        runtime.emergency_stop()
        return {"wav": b"cancelled speech"}

    monkeypatch.setattr(runtime._controller, "_prepare_tts_audio", prepare)
    client = create_app(runtime).test_client()
    response = client.post("/api/morning-briefing/run", json={})
    job = finished(client, response.get_json()["job"]["id"])
    assert job["state"] == "cancelled" and not runtime._controller.spoken


def test_runtime_shutdown_stops_scheduler_and_prevents_new_manual_jobs(runtime):
    runtime.morning_briefing.start()
    assert runtime.morning_briefing.snapshot()["scheduler_running"]
    runtime.stop()
    state = runtime.morning_briefing.snapshot()
    assert state["stopped"] and not state["scheduler_running"]
    client = create_app(runtime).test_client()
    assert client.post("/api/morning-briefing/run", json={"preview": True}).status_code == 503
    assert client.patch("/api/morning-briefing", json={"enabled": True}).status_code == 503
