"""Day rhythm, durable activity budgets and real autonomy composition offline."""
from datetime import datetime, timezone
from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from robbi.orchestration import APP, Intent, Orchestrator, intent_scope
from robbi.services.autonomy_policy import AutonomyPolicy
from robbi.services.day_cycle import DayCycle
from robot import behavior as behavior_module
from robot.autonomy import InitiativeEngine
from robot.behavior import BehaviorEngine
from robot.config import settings
from robot.daily_loop import DailyLoop
from robot.domains.engagement import EngagementDomain
from robot.lifecycle import LifecycleMixin
from robot.slam import MappingHalted, SlamService


def stamp(text):
    return datetime.fromisoformat(text).timestamp()


@pytest.mark.parametrize("when,phase,quiet,weekend", [
    ("2026-10-12T04:59:00+00:00", "night", True, False),
    ("2026-10-12T05:00:00+00:00", "morning", False, False),
    ("2026-10-12T10:00:00+00:00", "day", False, False),
    ("2026-10-12T17:00:00+00:00", "evening", False, False),
    ("2026-10-12T20:00:00+00:00", "night", True, False),
    ("2026-10-10T06:00:00+00:00", "night", True, True),
    ("2026-10-10T07:00:00+00:00", "morning", False, True),
])
def test_daily_phases_use_configured_timezone_and_weekend_schedule(when, phase, quiet, weekend):
    cycle = DayCycle()
    result = cycle.snapshot(stamp(when))
    assert (result["phase"], result["quiet"], result["weekend"]) == (phase, quiet, weekend)
    assert result["next_transition_at"] > stamp(when)


def test_day_cycle_handles_dst_and_live_options():
    options = {"timezone": "Europe/Berlin"}
    cycle = DayCycle(lambda: options)
    for when in ("2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00"):
        result = cycle.snapshot(stamp(when))
        assert result["quiet"] and result["date"] == "2026-10-25"
    options["enabled"] = False
    assert not cycle.snapshot(stamp("2026-10-25T01:30:00+00:00"))["quiet"]
    options.update(enabled=True, timezone="not/a_timezone")
    assert cycle.snapshot()["error"]


def policy(tmp_path, clock, **changes):
    options = {"observe_interval_s": 60, "checkin_interval_s": 60, "explore_interval_s": 60, **changes}
    cycle = DayCycle(lambda: {"timezone": "UTC"}, clock=lambda: clock[0])
    return AutonomyPolicy(tmp_path / "autonomy.json", cycle, lambda: options, clock=lambda: clock[0])


def test_idle_and_failed_attempts_consume_durable_budget_before_work(tmp_path):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock, max_attempts_per_day=2, max_model_calls_per_day=2)
    assert subject.begin("checkin", uses_model=True)
    saved = json.loads(subject.path.read_text())
    assert saved["attempts"] == saved["model_calls"] == 1
    subject.finish("idle", "Nothing useful to say")
    assert subject.eligible("checkin", uses_model=True) == "activity_cooldown"
    clock[0] += 61
    assert subject.begin("checkin", uses_model=True)
    subject.finish("failed", "Wi-Fi interrupted")
    restarted = policy(tmp_path, clock, max_attempts_per_day=2, max_model_calls_per_day=2)
    assert restarted.eligible("observe") == "daily_attempt_budget"
    assert [row["outcome"] for row in restarted.status()["history"]] == ["idle", "failed"]


def test_pause_survives_restart_and_day_rollover_never_resumes_it(tmp_path):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock)
    subject.pause("Operator Stop")
    clock[0] += 86400
    restarted = policy(tmp_path, clock)
    assert restarted.eligible("explore") == "paused"
    restarted.resume()
    assert restarted.eligible("explore") == ""


def test_model_exploration_budgets_and_status_reads_do_not_write(tmp_path, monkeypatch):
    from robbi.services import autonomy_policy as module
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock, max_model_calls_per_day=1, max_explorations_per_day=1)
    assert subject.begin("observe", uses_model=True)
    subject.finish("observed")
    assert subject.eligible("checkin", uses_model=True) == "daily_model_budget"
    assert subject.begin("explore")
    subject.finish("explored")
    assert subject.eligible("explore") == "daily_exploration_budget"
    monkeypatch.setattr(module, "write_json", lambda *_: pytest.fail("Status must not write state"))
    clock[0] += 86400
    assert subject.status()["attempts"] == 0


def test_restart_does_not_replay_work_and_corrupt_state_pauses(tmp_path):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock)
    assert subject.begin("explore")
    assert policy(tmp_path, clock).status()["history"][-1]["outcome"] == "interrupted"
    subject.path.write_text("{corrupt")
    damaged = policy(tmp_path, clock)
    assert damaged.is_paused() and damaged.status()["storage_error"]


def test_observations_are_bounded_separate_records_not_approved_facts(tmp_path):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock)
    for index in range(20):
        subject.observe({"visual_summary": f"Possibly a box {index}", "relative_pose": {"x_cm": index}})
    records = subject.status()["observations"]
    assert len(records) == 12
    assert all(row["source"] == "autonomy_observation" and row["review_status"] == "observation" for row in records)
    assert policy(tmp_path, clock).status()["observations"] == records


def test_day_cycle_controls_sleep_without_rapid_energy_oscillation(tmp_path, monkeypatch):
    monkeypatch.setattr(behavior_module, "STATE_FILE", tmp_path / "behavior.json")
    clock = [stamp("2026-10-12T23:00:00+00:00")]
    subject = BehaviorEngine(day_cycle=DayCycle(lambda: {"timezone": "UTC"}, clock=lambda: clock[0]))
    subject._state.last_interaction_ts = clock[0] - 600
    subject._last_vitals_tick = subject._last_saved = clock[0]
    subject._tick(clock[0])
    assert not subject.snapshot().awake
    subject._state.energy = 100
    subject._tick(clock[0] + 30)
    assert not subject.snapshot().awake
    subject._tick(stamp("2026-10-13T08:00:00+00:00"))
    assert subject.snapshot().awake


class AutonomyHost(EngagementDomain, LifecycleMixin):
    def __init__(self, tmp_path, clock):
        self._init_lifecycle()
        self._state_lock = threading.Lock()
        self._heartbeat_state = {}
        self._log = logging.getLogger(__name__)
        self._running = True
        self._filming_mode = self._voice_processing = False
        self._last_proactive_ts = self._last_activity_ts = 0
        self._soul_text = "Test robot"
        self.day_cycle = DayCycle(lambda: {"timezone": "UTC"}, clock=lambda: clock[0])
        self.autonomy_policy = policy(tmp_path, clock)
        self.orchestrator = Orchestrator()
        self.behavior = SimpleNamespace(snapshot=lambda: SimpleNamespace(last_interaction_ts=0, awake=True, muted=False))
        self.memory_calls, self.outputs, self.model_calls, self.motion_calls = [], [], [], []
        self.memory = SimpleNamespace(recent_messages=lambda: [], last_user_message_ts=lambda: 0,
            prepare_prompt_context=lambda *_, **__: {"prompt_text": ""},
            remember=lambda *args, **kwargs: self.memory_calls.append((args, kwargs)), add_assistant_message=lambda *_, **__: None)
        self.events = SimpleNamespace(recent=lambda **_: [])
        self.daily_loop = DailyLoop(tmp_path / "daily.json", day_cycle=self.day_cycle)
        self.initiative = InitiativeEngine()
        self.focus_timer = SimpleNamespace(is_active=lambda: False)
        self.media = SimpleNamespace(is_audio_playing=lambda: False)
        self.microphone = SimpleNamespace(health=lambda: {"voice_state": "idle"})
        self.motion = SimpleNamespace(status=lambda: {"running": False, "available": True, "encoder_enabled": True})
        self.tof = SimpleNamespace(status=lambda: {"enabled": True, "available": True, "fresh": True, "distance_cm": 100,
            "sensors": {"h1": {"enabled": True, "available": True, "fresh": True, "distance_cm": 100},
                        "h2": {"enabled": True, "available": False, "fresh": False, "distance_cm": None}}})
        self.imu = SimpleNamespace(status=lambda: {"enabled": True, "available": False, "fresh": False})
        self.camera = SimpleNamespace(health=lambda: {"enabled": True, "fresh": True}, get_llm_image_bytes=lambda **_: b"cached image")
        self.slam = SimpleNamespace(status=lambda: {"active": False, "pose": {"x_cm": 0}, "last_stop_reason": "step_limit_reached"},
            start_explore=lambda **kwargs: self.motion_calls.append(kwargs) or {"ok": True}, is_running=lambda: False, stop=lambda: None)
        self.llm = SimpleNamespace(available=lambda *_: True, ask=lambda *_: pytest.fail("Direct provider calls are forbidden"))
        self.model_answer = {"speak": "There may be a box on the table."}
        self.conversation_service = SimpleNamespace(ask=lambda *args, **kwargs: self.model_calls.append((args, kwargs)) or self.model_answer)
        self.execute_action = lambda action, **_: self.outputs.append(action) or action
        self._collect_spoken_text = lambda action: action.get("speak", "")
        self._publish_event = lambda *_, **__: None
        self.today_state = lambda **_: {"open_todos": [], "active_reminders": [], "pending_memories": []}
        self.heartbeat_status = lambda: dict(self._heartbeat_state)


@pytest.fixture
def autonomy_host(tmp_path, monkeypatch):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    monkeypatch.setattr("robot.domains.engagement.time.time", lambda: clock[0])
    for key, value in dict(autonomy_enabled=True, heartbeat_enabled=True, autonomy_level="normal",
        autonomy_allow_vision=True, autonomy_allow_exploration=False, runtime_calibration_confirmed=False,
        enable_motors=False, slam_enabled=True, daily_loop_enabled=True, daily_loop_mode="manual",
        daily_loop_max_proactive_per_day=3, autonomy_model_timeout_s=25, autonomy_explore_timeout_s=90,
        autonomy_explore_max_steps=1, autonomy_explore_max_distance_cm=30).items():
        monkeypatch.setattr(settings, key, value, raising=False)
    return AutonomyHost(tmp_path, clock), clock


def test_periodic_observation_actually_attaches_cached_camera_image_without_output(autonomy_host):
    host, _ = autonomy_host
    host._run_heartbeat_turn()
    assert len(host.model_calls) == 1
    _, kwargs = host.model_calls[0]
    assert kwargs["image_bytes"] == b"cached image" and kwargs["background"] is True
    assert kwargs["timeout_s"] == kwargs["request_timeout_s"] == 25
    assert host.outputs == host.memory_calls == host.motion_calls == []
    entry = host.autonomy_policy.status()["observations"][0]["data"]
    assert entry["visual_summary"] and entry["sensors"]["imu"]["orientation_deg"] is None


def test_missing_camera_and_sensors_still_produce_honest_local_observation(autonomy_host):
    host, _ = autonomy_host
    host.camera.health = lambda: {"fresh": False, "available": False}
    host.tof.status = lambda: {"enabled": True, "available": False, "fresh": False, "distance_cm": 42}
    host._run_heartbeat_turn()
    assert host.model_calls == host.motion_calls == []
    entry = host.autonomy_policy.status()["observations"][0]["data"]
    assert entry["sensors"]["tof"]["h1"]["distance_cm"] is None
    assert entry["visual_summary"] == ""


def test_exploration_passes_all_three_finite_budgets(autonomy_host, monkeypatch):
    host, _ = autonomy_host
    for key in ("autonomy_allow_exploration", "enable_motors", "runtime_calibration_confirmed"):
        monkeypatch.setattr(settings, key, True)
    host._run_heartbeat_turn()
    assert host.motion_calls == [{"max_steps": 1, "max_distance_cm": 30.0, "timeout_s": 90.0}]
    assert host.model_calls == []
    assert host.autonomy_policy.status()["explorations"] == 1


@pytest.mark.parametrize("gate", ["not_armed", "not_calibrated", "stale_tof", "missing_encoders", "quiet", "occupied", "paused"])
def test_exploration_never_bypasses_hardware_activity_or_day_gates(autonomy_host, monkeypatch, gate):
    host, clock = autonomy_host
    for key in ("autonomy_allow_exploration", "enable_motors", "runtime_calibration_confirmed"):
        monkeypatch.setattr(settings, key, True)
    lease = None
    if gate == "not_armed": monkeypatch.setattr(settings, "enable_motors", False)
    if gate == "not_calibrated": monkeypatch.setattr(settings, "runtime_calibration_confirmed", False)
    if gate == "stale_tof": host.tof.status = lambda: {"available": True, "fresh": False, "distance_cm": 100}
    if gate == "missing_encoders": host.motion.status = lambda: {"available": True, "encoder_enabled": False}
    if gate == "quiet": clock[0] = stamp("2026-10-12T23:00:00+00:00")
    if gate == "paused": host.autonomy_policy.pause("Stop")
    if gate == "occupied":
        lease = host.orchestrator.acquire(("display",), intent=Intent("app:picture", APP))
    try:
        host._run_heartbeat_turn()
        assert host.motion_calls == []
    finally:
        if lease: lease.release()


def test_proactive_model_output_cannot_drive_call_apps_or_approve_inferred_memories(autonomy_host, monkeypatch):
    host, _ = autonomy_host
    monkeypatch.setattr(settings, "daily_loop_mode", "active")
    host.today_state = lambda **_: {"open_todos": [{"title": "Review a task"}]}
    host.model_answer = {"speak": "Which task would you like to start?", "drive": {"direction": "forward"},
                        "slam": {"action": "explore_room"}, "app_tool": {"app": "image-studio"},
                        "sequence": [{"drive": {}}], "remember": [{"content": "Unverified impression", "review_status": "approved"}]}
    host._run_heartbeat_turn()
    assert host.outputs == [{"speak": "Which task would you like to start?"}]
    assert host.motion_calls == []
    payload = host.memory_calls[0][0][0][0]
    assert payload["review_status"] == "pending" and payload["source"] == "auto:reflection"


def test_idle_checkin_attempt_cooldown_covers_heartbeat_ok(autonomy_host, monkeypatch):
    host, _ = autonomy_host
    monkeypatch.setattr(settings, "daily_loop_mode", "active")
    host.today_state = lambda **_: {"open_todos": [{"title": "Review a task"}]}
    host.model_answer = {"heartbeat": "ok"}
    host._run_heartbeat_turn()
    assert host.autonomy_policy.eligible("checkin", uses_model=True) == "activity_cooldown"
    assert host.outputs == []


def test_unexpected_failure_does_not_kill_heartbeat_loop(autonomy_host):
    host, _ = autonomy_host
    iterations = iter((False, False, True))
    host._lifecycle_stop = SimpleNamespace(wait=lambda *_: next(iterations))
    calls = []
    def turn():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary storage failure")
    host._run_heartbeat_turn = turn
    host._heartbeat_loop()
    assert len(calls) == 2


def test_slam_guard_enforces_deadline_and_total_travel(monkeypatch):
    monkeypatch.setattr(settings, "slam_enabled", True)
    stopped = []
    service = SlamService(SimpleNamespace(stop=lambda: stopped.append(True)), lambda: 100, lambda: (True, ""))
    service._run_limits.value = {"deadline": 0, "distance_cm": 30, "travelled_cm": 0}
    with pytest.raises(MappingHalted, match="exploration_deadline"):
        service._check_guard()
    service._run_limits.value = {"deadline": None, "distance_cm": 30, "travelled_cm": 30}
    with pytest.raises(MappingHalted, match="travel_budget_reached"):
        service._check_guard()
    assert len(stopped) == 2


def test_stop_pause_blocks_admission_immediately_while_storage_is_blocked(autonomy_host, monkeypatch):
    from robbi.services import autonomy_policy as module
    host, _ = autonomy_host
    entered, release = threading.Event(), threading.Event()
    write_json = module.write_json
    writes = []
    def delayed_write(*args):
        writes.append(True)
        if len(writes) == 1:
            entered.set()
            assert release.wait(2)
        return write_json(*args)
    monkeypatch.setattr(module, "write_json", delayed_write)
    stopped = threading.Event()
    host.slam.stop = stopped.set
    with ThreadPoolExecutor(max_workers=1) as writer, ThreadPoolExecutor(max_workers=1) as maintenance, ThreadPoolExecutor(max_workers=1) as caller:
        host._maintenance_executor = maintenance
        admitting = writer.submit(host.autonomy_policy.begin, "observe")
        assert entered.wait(1)
        try:
            stopped_call = caller.submit(host.autonomy_pause, "Operator Stop", persist_async=True)
            assert stopped_call.result(timeout=.5)["paused"]
            assert stopped.is_set() and host.autonomy_policy.is_paused()
            assert host.autonomy_policy.eligible("explore") == "paused"
            assert host.autonomy_policy.begin("explore") is False
        finally:
            release.set()
        assert admitting.result(timeout=1) is False
        maintenance.submit(lambda: None).result(timeout=1)
    saved = json.loads(host.autonomy_policy.path.read_text())
    assert saved["paused"] and saved["history"][-1]["outcome"] == "cancelled"


def test_explicit_resume_invalidates_queued_pause_persistence(tmp_path):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock)
    pending = subject.request_pause("Stop")
    subject.resume()
    subject.persist_pause(pending)
    assert not subject.is_paused()
    assert not policy(tmp_path, clock).is_paused()


def test_rejected_pause_persistence_remains_paused_and_reports_failure(autonomy_host):
    host, _ = autonomy_host
    def rejected(*_):
        future = Future()
        future.set_exception(RuntimeError("maintenance queue full"))
        return future
    host._maintenance_executor = SimpleNamespace(submit=rejected)
    host.autonomy_pause("Stop", persist_async=True)
    assert host.autonomy_policy.is_paused()
    assert "maintenance queue full" in host.autonomy_policy.status()["storage_error"]


@pytest.mark.parametrize("change", ["quiet", "interaction", "disabled", "unarmed", "uncalibrated"])
def test_running_exploration_yields_when_conditions_change(autonomy_host, monkeypatch, change):
    host, clock = autonomy_host
    for key in ("autonomy_allow_exploration", "enable_motors", "runtime_calibration_confirmed"):
        monkeypatch.setattr(settings, key, True)
    stopped = []
    host.slam.stop = lambda: stopped.append(True)
    def running():
        if change == "quiet": clock[0] = stamp("2026-10-12T23:00:00+00:00")
        if change == "interaction": host._last_activity_ts = clock[0]
        if change == "disabled": monkeypatch.setattr(settings, "autonomy_enabled", False)
        if change == "unarmed": monkeypatch.setattr(settings, "enable_motors", False)
        if change == "uncalibrated": monkeypatch.setattr(settings, "runtime_calibration_confirmed", False)
        return True
    host.slam.is_running = running
    host._run_heartbeat_turn()
    assert stopped == [True]
    assert host.autonomy_policy.status()["history"][-1]["outcome"] == "cancelled"


def test_late_vision_response_after_stop_never_becomes_observation(autonomy_host):
    host, _ = autonomy_host
    def result(*_, **__):
        host.autonomy_pause("Stop")
        return {"speak": "Late visual interpretation"}
    host.conversation_service.ask = result
    host._run_heartbeat_turn()
    assert host.autonomy_policy.status()["observations"] == []
    assert host.outputs == []


@pytest.mark.parametrize("change", [{"day": []}, {"paused": "false"}, {"history": [5]}, {"last_by_kind": {"explore": -1}}])
def test_damaged_state_schema_pauses_instead_of_resetting_budgets(tmp_path, change):
    clock = [stamp("2026-10-12T12:00:00+00:00")]
    subject = policy(tmp_path, clock)
    subject.begin("observe")
    value = json.loads(subject.path.read_text())
    subject.path.write_text(json.dumps({**value, **change}))
    restarted = policy(tmp_path, clock)
    assert restarted.is_paused() and restarted.status()["storage_error"]
