"""Runtime integration boundaries for day rhythm and background execution."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from robbi.actions import validate_action
from robbi.config import load, merge, validate
from robbi.errors import ValidationError
from robbi.resources import ExecutionResources
from robbi.runtime import Runtime
from robbi.web import create_app
from robot.lifecycle import LifecycleMixin

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("patch", [
    {"autonomy": {"allow_exploration": "false"}},
    {"background_work": {"notify_on_complete": "false"}},
    {"background_work": {"max_pending": 3}},
    {"background_work": {"max_history": 33}},
    {"background_work": {"foreground_budget_s": 0}},
    {"background_work": {"timeout_s": 301}},
    {"background_work": {"acknowledgement": "   "}},
    {"day_cycle": {"timezone": "Mars/Olympus"}},
    {"day_cycle": {"sleep_hour": 8}},
    {"day_cycle": {"weekend_wake_hour": 12}},
])
def test_bad_new_settings_do_not_overwrite_profile(tmp_path, patch):
    profile = load(ROOT, tmp_path / "profile.yaml", tmp_path / "data")
    with pytest.raises(ValidationError):
        validate(merge(profile.values, patch))
    assert not profile.source.exists()


def test_new_pi_requires_owner_to_enable_autonomy_and_motion():
    from robbi.setup.configuration import new_config
    profile = new_config()
    assert not profile['autonomy']['enabled']
    assert not profile['hardware']['enable_motors']
    assert not profile['runtime']['calibration_confirmed']



@pytest.mark.parametrize("action", [
    {"background_task": "work"},
    {"background_task": {"prompt": 12}},
    {"background_task": {"prompt": "hello", "text": "other"}},
    {"background_task": {"drive": "forward"}},
    {"sequence": [{"background_task": {}}]},
    {"background_task": {"prompt": "x" * 12001}},
])
def test_model_cannot_hide_actions_in_background_envelope(action):
    with pytest.raises(ValidationError):
        validate_action(action)


def test_blocked_adopted_provider_leaves_capacity_for_another_turn():
    resources = ExecutionResources(16)
    release = threading.Event()
    started = threading.Event()
    try:
        existing = resources.lane("llm-live").submit(lambda: (started.set(), release.wait(2)))
        assert started.wait(1)
        assert resources.lane("llm-live").submit(lambda: "next reply").result(1) == "next reply"
    finally:
        release.set()
        existing.result(1)
        resources.shutdown()


def test_stop_cancels_work_but_shutdown_does_not_persist_autonomy_pause():
    class Controller(LifecycleMixin):
        def __init__(self):
            self._init_lifecycle()
            self._running = True
            self.background_work = SimpleNamespace(cancel_all=Mock())
            self.autonomy_pause = Mock()
            self.microphone = SimpleNamespace(cancel_pending_input=Mock())
    controller = Controller()
    old_token = controller._action_token()[1]
    controller.cancel_actions()
    assert old_token.is_set()
    controller.background_work.cancel_all.assert_called_once()
    controller.autonomy_pause.assert_called_once()
    controller._running = False
    controller.cancel_actions()
    assert controller.background_work.cancel_all.call_count == 2
    assert controller.autonomy_pause.call_count == 1


def test_trusted_dashboard_owner_can_pause_and_resume_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBBI_WEB_TOKEN", raising=False)
    runtime = Runtime(load(ROOT, tmp_path / "config.yaml", tmp_path / "state"))
    pause, resume = Mock(return_value={"paused": True}), Mock(return_value={"paused": False})
    runtime._controller = SimpleNamespace(autonomy_pause=pause, autonomy_resume=resume,
        autonomy_status=lambda: {"policy": {"paused": True}}, stop=lambda: None)
    runtime._ready.set()
    client = create_app(runtime).test_client()
    remote = {"REMOTE_ADDR": "192.168.1.35"}
    try:
        assert client.get("/api/autonomy", environ_overrides=remote).get_json()["owner_can_resume"] is True
        assert client.post("/api/autonomy/pause", json={}, environ_overrides=remote).status_code == 200
        assert client.post("/api/autonomy/resume", json={}, environ_overrides=remote).status_code == 200
        resume.assert_called_once()
    finally:
        runtime.stop()


@pytest.mark.parametrize("operator_stop", [False, True])
def test_real_runtime_shutdown_preserves_owner_pause_state(tmp_path, operator_stop):
    class Controller(LifecycleMixin):
        def __init__(self):
            self._init_lifecycle()
            self._running = True
            self.autonomy_pause = Mock()
            self.background_work = SimpleNamespace(cancel_all=Mock())
            self.motion = SimpleNamespace(stop=Mock())
            self.slam = SimpleNamespace(stop=Mock())
            self.media = SimpleNamespace(stop_audio=Mock())

        def stop(self):
            self._running = False
            self.cancel_actions()

    runtime = Runtime(load(ROOT, tmp_path / "config.yaml", tmp_path / "state"))
    controller = runtime._controller = Controller()
    if operator_stop:
        runtime.emergency_stop()
    runtime.stop()
    assert controller.autonomy_pause.call_count == int(operator_stop)
    assert controller.motion.stop.called and controller.slam.stop.called
    assert controller.background_work.cancel_all.called


def test_heartbeat_enable_can_be_saved_live_without_ignoring_the_switch(tmp_path, monkeypatch):
    from robot import config
    effective, source = tmp_path / "effective.yaml", tmp_path / "local.yaml"
    effective.write_text("heartbeat: {enabled: false}\n")
    source.write_text("heartbeat: {enabled: false}\n")
    monkeypatch.setattr(config, "CONFIG_FILE", effective)
    monkeypatch.setattr(config, "CONFIG_SOURCE", source)
    monkeypatch.setattr(config.settings, "heartbeat_enabled", False)
    snapshot = config.update_settings_from_payload({"heartbeat": {"enabled": True}})
    assert config.settings.heartbeat_enabled
    assert snapshot["values"]["heartbeat"]["enabled"]
    assert "enabled: true" in source.read_text()


def test_enabling_heartbeat_concurrently_creates_only_one_loop(monkeypatch):
    from robot.controller import RobotController
    from robot.config import settings
    monkeypatch.setattr(settings, "autonomy_enabled", True)
    monkeypatch.setattr(settings, "heartbeat_enabled", False)
    controller = RobotController.__new__(RobotController)
    controller._running = True
    controller._heartbeat_start_lock = threading.Lock()
    controller._heartbeat_thread = None
    release, entered = threading.Event(), threading.Event()
    calls = []

    def run():
        calls.append(True)
        entered.set()
        release.wait(2)

    controller._heartbeat_loop = run
    controller._start_heartbeat_once()
    assert controller._heartbeat_thread is None
    monkeypatch.setattr(settings, "heartbeat_enabled", True)
    contenders = [threading.Thread(target=controller._start_heartbeat_once) for _ in range(8)]
    try:
        for thread in contenders:
            thread.start()
        for thread in contenders:
            thread.join(1)
        assert entered.wait(1)
        assert len(calls) == 1
    finally:
        release.set()
        if controller._heartbeat_thread:
            controller._heartbeat_thread.join(1)
