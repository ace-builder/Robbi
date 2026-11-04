"""Controller activity scopes and async presentation, without hardware I/O."""
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from robbi.errors import UnavailableError
from robbi.jobs import BoundedExecutor
from robbi.orchestration import (APP, BACKGROUND, INTERACTIVE, Intent,
                                 Orchestrator, ResourceBusy, current_intent, intent_scope)
from robot.activity import activity
from robot.controller import RobotController
from robot.domains.engagement import EngagementDomain
from robot.domains.voice import VoiceDomain
from robot.lifecycle import LifecycleMixin
from robot.media import AudioPlayer, MediaManager
from robot.presentation_pipeline import PresentationPipelineMixin


class Host(LifecycleMixin):
    def __init__(self):
        self._init_lifecycle()
        self.orchestrator = Orchestrator()


class RecordingExecutor(BoundedExecutor):
    def submit(self, callback, /, *args, **kwargs):
        self.last_future = super().submit(callback, *args, **kwargs)
        return self.last_future


def test_guarded_executor_inherits_intent_and_action_generation():
    host, executor = Host(), BoundedExecutor(max_workers=1)
    intent = Intent("app:scene", APP)
    token = host._action_token()
    def work():
        with host.orchestrator.acquire(("display",)) as lease:
            return current_intent(), host._action_token(), lease.intent
    try:
        with intent_scope(intent):
            future = host._submit_guarded(executor, work)
        inherited, captured_token, lease_owner = future.result(timeout=1)
        assert inherited is intent and lease_owner is intent
        assert captured_token == token
        assert current_intent() is None
        assert host.orchestrator.snapshot()["resources"] == {}
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_stop_prevents_pending_guarded_callback_from_running():
    host, executor = Host(), BoundedExecutor(max_workers=1, max_pending=1)
    entered, release = threading.Event(), threading.Event()
    executor.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(1)
    called = []
    try:
        with intent_scope(Intent("app:pending", APP)):
            future = host._submit_guarded(executor, lambda: called.append(current_intent()))
        host.cancel_actions()
        release.set()
        assert future.result(timeout=1) is None
        assert called == []
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("error", [ResourceBusy("occupied", ("display",)), UnavailableError("display missing")])
def test_optional_background_activity_drops_failure_and_next_iteration_runs(error):
    class Background(Host):
        @activity("passive", BACKGROUND, replace=True)
        def present(self, fail):
            if fail:
                raise error
            return current_intent()
    host = Background()
    results = [host.present(True), host.present(True), host.present(False)]
    assert results[:2] == [None, None]
    assert results[2].priority == BACKGROUND
    assert current_intent() is None


def test_interactive_activity_reports_busy_instead_of_claiming_success():
    class Interactive(Host):
        @activity("conversation", INTERACTIVE)
        def present(self):
            raise ResourceBusy("occupied", ("display",))
    with pytest.raises(ResourceBusy):
        Interactive().present()


def test_actual_ambient_loop_survives_contention_and_uses_fresh_background_intents():
    class Ambient(EngagementDomain, Host):
        pass
    host = Ambient()
    host._running, host._filming_mode = True, False
    iterations = iter((False, False, False, True))
    host._lifecycle_stop = SimpleNamespace(wait=lambda *_: next(iterations))
    host.focus_timer = SimpleNamespace(is_active=lambda: False)
    host.behavior = SimpleNamespace(passive_presentation=lambda: {"face": "neutral"})
    seen = []
    def execute(action):
        seen.append(current_intent())
        if len(seen) == 1:
            raise ResourceBusy("app owns screen", ("display",))
        if len(seen) == 2:
            raise UnavailableError("screen missing")
    host.execute_action = execute
    host._ambient_loop()
    assert len(seen) == 3 and all(intent.priority == BACKGROUND for intent in seen)
    assert len({intent.id for intent in seen}) == 3


class Overlay(PresentationPipelineMixin, Host):
    def __init__(self, executor, show_face):
        super().__init__()
        self._overlay_executor = executor
        self._filming_mode = False
        self.focus_timer = SimpleNamespace(is_active=lambda: False)
        self.behavior = SimpleNamespace(set_current_face=lambda *_: None)
        self._set_presentation = lambda **_: None
        self._update_pipeline_metrics = lambda *_, **__: None
        self._resolve_speaking_face_pattern = lambda _: [Path("neutral.png"), Path("speaking.png")]
        self.media = SimpleNamespace(show_face=show_face,
                                     speak_wav_bytes=lambda *_, **__: SimpleNamespace(wait=lambda **_: 0))


def test_speech_scene_keeps_resources_until_actual_overlay_work_exits():
    executor = RecordingExecutor(max_workers=1)
    entered, release = threading.Event(), threading.Event()
    seen = []
    def show_face(face):
        seen.append((face, current_intent()))
        entered.set()
        assert release.wait(2)
    host = Overlay(executor, show_face)
    intent = Intent("conversation:reply", INTERACTIVE)
    try:
        with intent_scope(intent):
            host._play_prepared_tts_with_overlay({"wav": b"fake wav"})
        assert entered.wait(1)
        claims = host.orchestrator.snapshot()["resources"]
        assert set(claims) == {"audio", "display"}
        assert {claim["intent_id"] for claim in claims.values()} == {intent.id}
        with intent_scope(Intent("app:picture", APP)), pytest.raises(ResourceBusy):
            host.orchestrator.acquire(("display",))
        release.set()
        executor.last_future.result(timeout=1)
        assert host.orchestrator.snapshot()["resources"] == {}
        assert all(owner is intent for _, owner in seen)
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("cancel_future", [True, False], ids=["future-cancelled", "stop-skips-callback"])
def test_pending_overlay_releases_its_transferred_lease_when_it_never_runs(cancel_future):
    executor = RecordingExecutor(max_workers=1, max_pending=1)
    entered, release = threading.Event(), threading.Event()
    executor.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(1)
    shown = []
    host = Overlay(executor, shown.append)
    try:
        with intent_scope(Intent("conversation:pending", INTERACTIVE)):
            host._play_prepared_tts_with_overlay({"wav": b"fake wav"})
        pending = executor.last_future
        assert set(host.orchestrator.snapshot()["resources"]) == {"audio", "display"}
        if cancel_future:
            assert pending.cancel()
        else:
            host.cancel_actions()
        release.set()
        if not cancel_future:
            pending.result(timeout=1)
        assert host.orchestrator.snapshot()["resources"] == {}
        assert shown == []
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_thinking_cleanup_only_cancels_jobs_owned_by_that_exact_activity():
    class Voice(VoiceDomain, Host):
        pass
    host = Voice()
    # Real owner-filtering methods with a synthetic job table: no player worker
    # or audio process is needed to verify controller cleanup authority.
    audio = AudioPlayer.__new__(AudioPlayer)
    audio._lock = threading.Lock()
    old, current = Intent("conversation", INTERACTIVE), Intent("conversation", INTERACTIVE)
    old_cancel, current_cancel = threading.Event(), threading.Event()
    audio._jobs = {1: {"intent": old, "owner_cancel": old_cancel},
                   2: {"intent": current, "owner_cancel": current_cancel}}
    audio.stop_all = lambda: pytest.fail("Thinking cleanup must never issue a global audio stop")
    host.media = MediaManager.__new__(MediaManager)
    host.media.audio = audio
    with intent_scope(old):
        host._stop_transient_thinking_audio()
    assert old_cancel.is_set() and not current_cancel.is_set()
    host._stop_transient_thinking_audio()  # No owner is no cleanup authority.
    assert not current_cancel.is_set()


@pytest.mark.parametrize("sensor", ["tof", "imu"])
def test_lifecycle_reports_stale_sensor_as_degraded(sensor):
    host = Host()
    host._record_health(sensor, "ready", "")
    setattr(host, sensor, SimpleNamespace(status=lambda: {
        "enabled": True, "available": True, "fresh": False, "last_error": ""}))
    state = host.health_snapshot()[0]
    assert state["state"] == "degraded" and "stale" in state["message"]


def test_controller_enabling_imu_reloads_sampler_and_clears_disabled_health(monkeypatch):
    from robot import controller as controller_module
    from robot.config import settings
    host = RobotController.__new__(RobotController)
    host._init_lifecycle()
    host._record_health("imu", "disabled", "Disabled by configuration")
    calls = []
    host.imu = SimpleNamespace(reload_runtime_settings=lambda: calls.append("reload"), status=lambda: {
        "enabled": True, "available": True, "fresh": True})
    monkeypatch.setattr(settings, "imu_enabled", True)
    monkeypatch.setattr(controller_module, "update_settings_from_payload", lambda payload: payload)
    host._diagnostics = SimpleNamespace(invalidate=lambda: calls.append("invalidate"))
    host._set_presentation = lambda **_: None
    host._publish_event = lambda *_, **__: None
    host.status = lambda: {"components": host.health_snapshot()}
    result = host.apply_settings({"imu": {"enabled": True}})
    assert calls == ["reload", "invalidate"]
    assert result["status"]["components"][0]["state"] == "ready"
