"""Intentional behavior stays bounded, evidence-backed and interruptible."""

from types import SimpleNamespace

from robbi.services.attention import Attention
from robbi.services.attention_signals import WorkingIntention, scene_key
from test_autonomous_runtime import autonomy_host as _autonomy_host_fixture

autonomy_host = _autonomy_host_fixture


def attach(host, clock, tmp_path):
    host.attention = Attention(tmp_path / "attention.json", clock=lambda: clock[0])
    host.assets = SimpleNamespace(names=lambda group: ["curious.png"])
    return host.attention


def test_intention_has_only_two_continuations_and_never_starts_from_elapsed_time():
    clock = [100.0]
    working = WorkingIntention(lambda: clock[0])
    assert not working.ready()
    token = working.begin()
    working.finish(
        token,
        focus="Understand the new object",
        outcome="observed",
        reason="Saw a ball",
        followup=True,
    )
    assert working.ready()
    for step in (1, 2):
        token = working.begin(continuation=True)
        working.finish(
            token,
            focus="A renamed goal cannot reset the step budget",
            outcome="observed",
            reason="More evidence",
            followup=True,
        )
        assert working.snapshot()["steps"] == step
        assert working.snapshot()["focus"] == "Understand the new object"
    assert not working.ready() and working.begin(continuation=True) is None
    clock[0] += 1000
    assert not working.ready()


def test_expired_or_interrupted_intention_never_revives_from_late_results():
    clock = [100.0]
    working = WorkingIntention(lambda: clock[0])
    token = working.begin()
    working.interrupt()
    working.finish(
        token, focus="Old goal", outcome="observed", reason="Late result", followup=True
    )
    assert working.snapshot()["phase"] == "interrupted" and not working.ready()
    token = working.begin()
    working.finish(
        token, focus="Look around", outcome="observed", reason="Done", followup=True
    )
    clock[0] += 46
    assert not working.ready() and working.snapshot()["phase"] == "expired"


def test_speech_context_does_not_create_self_triggered_novelty(tmp_path):
    attention = Attention(tmp_path / "attention.json")
    evidence = attention.evidence({}, {}, {}, [])
    assert attention.claim(evidence)
    with_speech = attention.evidence(
        {}, {}, {}, [{"role": "assistant", "content": "I noticed the ball."}]
    )
    assert with_speech["recent_robot_speech"] == ["I noticed the ball."]
    assert not attention.novel(with_speech)


def test_live_rewording_is_quiet_but_negation_and_direction_remain_distinct(tmp_path):
    clock = [100.0]
    attention = Attention(tmp_path / "attention.json", clock=lambda: clock[0])
    attention.observe_live(
        {
            "summary": "A red ball is on the left.",
            "changed": True,
            "salience": "medium",
        },
        react=True,
    )
    first = attention.live_evidence()
    clock[0] += 1
    attention.observe_live(
        {
            "summary": "The red ball is currently on the left!",
            "changed": True,
            "salience": "medium",
        },
        react=True,
    )
    assert attention.live_evidence() == first
    assert scene_key("No person is here.") != scene_key("A person is here.")
    assert scene_key("A ball on the left") != scene_key("A ball on the right")
    assert scene_key("Two objects") != scene_key("Three objects")


def test_actual_spoken_output_is_remembered_and_duplicate_survives_restart(tmp_path):
    clock = [100.0]
    attention = Attention(tmp_path / "attention.json", clock=lambda: clock[0])
    attention.record(
        "speak", "Reason", "action_requested", "Watch the ball", "There is a red ball!"
    )
    restarted = Attention(attention.path, clock=lambda: clock[0])
    assert restarted.duplicate_speech("There is a RED ball.")
    assert not restarted.duplicate_speech("The ball is gone.")
    assert "There is a red ball!" in restarted.context({})
    clock[0] += 601
    assert not restarted.duplicate_speech("There is a red ball!")


def test_successful_observation_gets_a_prompt_bounded_followthrough(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    attention = attach(host, clock, tmp_path)
    answers = iter(
        [
            {"initiative": {"kind": "observe", "focus": "Understand the new object"}},
            {"speak": "A red ball sits nearby."},
            {
                "initiative": {"kind": "speak", "focus": "Comment on the ball"},
                "speak": "That red ball looks interesting.",
            },
        ]
    )
    host.conversation_service.ask = lambda *a, **k: (
        host.model_calls.append((a, k)) or next(answers)
    )
    host._run_heartbeat_turn()
    assert attention.intentions.ready() and len(host.model_calls) == 2
    clock[0] += 5
    host._run_heartbeat_turn()
    assert len(host.model_calls) == 3 and host.outputs == [
        {"speak": "That red ball looks interesting."}
    ]
    assert attention.status()["intention"]["focus"] == "Understand the new object"
    assert attention.status()["intention"]["phase"] == "requested"
    assert not attention.intentions.ready()
    assert host.autonomy_policy.status()["model_calls"] == 3
    clock[0] += 61
    host._run_heartbeat_turn()
    assert len(host.model_calls) == 3  # Its own reply does not start another cycle.


def test_failed_sensing_never_creates_an_automatic_retry_loop(autonomy_host, tmp_path):
    host, clock = autonomy_host
    attention = attach(host, clock, tmp_path)
    host.model_answer = {
        "initiative": {"kind": "observe", "focus": "Check surroundings"}
    }
    host.camera.get_llm_image_bytes = lambda **kwargs: None
    host._run_heartbeat_turn()
    assert not attention.intentions.ready() and not host.outputs
    assert attention.status()["intention"]["phase"] == "interrupted"


def test_registered_silent_expression_uses_existing_executor_without_motion(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    attach(host, clock, tmp_path)
    host.dynamics = SimpleNamespace(
        faces=SimpleNamespace(
            catalog=lambda: ["curious-glance", "happy-dance", "gag-tiny-violin"]
        )
    )
    host.model_answer = {
        "initiative": {"kind": "express", "focus": "Watch the ball"},
        "face_animation": "curious-glance",
        "face": "curious.png",
        "gesture": "happy-dance",
        "sound": "music",
        "speak": "No speech",
    }
    host._run_heartbeat_turn()
    assert host.outputs == [{"face_animation": "curious-glance"}]
    assert not host.motion_calls and not host.memory_calls
    assert "happy-dance" not in host.model_calls[0][1]["tools_text"]


def test_unregistered_or_motor_animation_cannot_expand_autonomy(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    attach(host, clock, tmp_path)
    host.dynamics = SimpleNamespace(
        faces=SimpleNamespace(catalog=lambda: ["happy-dance"])
    )
    host.model_answer = {
        "initiative": {"kind": "express"},
        "face_animation": "happy-dance",
        "gesture": "happy-dance",
    }
    host._run_heartbeat_turn()
    assert not host.outputs and not host.motion_calls


def test_repeated_proactive_sentence_is_suppressed_without_blocking_new_evidence(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    attention = attach(host, clock, tmp_path)
    host.model_answer = {"initiative": {"kind": "speak"}, "speak": "I see a ball!"}
    attention.observe_live(
        {"summary": "A ball arrived.", "changed": True, "salience": "medium"},
        react=True,
    )
    host._run_heartbeat_turn()
    clock[0] += 9
    attention.observe_live(
        {"summary": "The ball is moving away.", "changed": True, "salience": "medium"},
        react=True,
    )
    host._run_heartbeat_turn()
    assert len(host.model_calls) == 2 and len(host.outputs) == 1
    assert (
        attention.status()["episodes"][-1]["reason"]
        == "Repeated autonomous speech suppressed"
    )


def test_stale_visual_observations_are_not_presented_as_current_evidence(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    attach(host, clock, tmp_path)
    host.autonomy_policy.observe({"visual_summary": "An old image from yesterday."})
    clock[0] += 86400
    host.model_answer = {"initiative": {"kind": "quiet"}}
    host._run_heartbeat_turn()
    assert "An old image from yesterday" not in host.model_calls[0][1]["memory_text"]


def test_fresh_heading_changes_are_evidence_but_never_infer_a_person(tmp_path):
    attention = Attention(tmp_path / "attention.json")
    sensors = {
        "imu": {"fresh": True, "orientation_deg": {"yaw": 0}},
        "handling": {"pickup_calibrated": True, "pickup_detected": False},
    }
    first = attention.evidence(sensors, {}, {}, [])
    sensors["imu"]["orientation_deg"]["yaw"] = 90
    second = attention.evidence(sensors, {}, {}, [])
    assert (
        first["relative_direction_sector"] == 0
        and second["relative_direction_sector"] == 1
    )
    assert first["posture"] == "settled" and not any("person" in key for key in second)
    sensors["imu"]["fresh"] = False
    assert attention.evidence(sensors, {}, {}, [])["relative_direction_sector"] is None


def test_bounded_exploration_results_supply_new_evidence_for_followthrough(
    autonomy_host, tmp_path, monkeypatch
):
    from robbi.configuration.settings import settings

    host, clock = autonomy_host
    attention = attach(host, clock, tmp_path)
    for field in (
        "autonomy_allow_exploration",
        "enable_motors",
        "runtime_calibration_confirmed",
    ):
        monkeypatch.setattr(settings, field, True)
    host.slam.status = lambda: {
        "active": False,
        "pose": {"x_cm": 0},
        "scan_count": 2,
        "explore_steps": 1,
        "last_stop_reason": "step_limit_reached",
    }
    answers = iter(
        [
            {"initiative": {"kind": "explore", "focus": "Check nearby free space"}},
            {"initiative": {"kind": "express"}, "face": "curious.png"},
        ]
    )
    host.conversation_service.ask = lambda *a, **k: (
        host.model_calls.append((a, k)) or next(answers)
    )
    host._run_heartbeat_turn()
    assert attention.intentions.ready() and len(host.motion_calls) == 1
    assert host.motion_calls[0]["max_distance_cm"] == 30
    clock[0] += 5
    host._run_heartbeat_turn()
    assert len(host.model_calls) == 2 and host.outputs == [{"face": "curious.png"}]
    assert '"scan_count":2' in host.model_calls[1][1]["memory_text"]


def test_stopping_live_session_interrupts_its_pending_intention_only(tmp_path):
    from robbi.live_awareness.robot import make_live_awareness

    attention = Attention(tmp_path / "attention.json")
    token = attention.intentions.begin(source="live")
    attention.intentions.finish(
        token,
        focus="Watch the ball",
        outcome="observed",
        reason="Saw it",
        followup=True,
    )
    robot = SimpleNamespace(attention=attention, _live_reaction_active=False)
    live = make_live_awareness(robot)
    live.stop()
    assert not attention.intentions.ready()
    assert attention.intentions.snapshot()["phase"] == "interrupted"
    token = attention.intentions.begin()
    attention.intentions.finish(
        token,
        focus="An independent intention",
        outcome="observed",
        reason="Fresh evidence",
        followup=True,
    )
    live.stop()
    assert attention.intentions.ready()


def test_pending_intentions_never_resume_from_attention_journal(tmp_path):
    attention = Attention(tmp_path / "attention.json")
    token = attention.intentions.begin()
    attention.record(
        "observe", "Saw a ball", "observed", "Understand the object", "", token, True
    )
    assert attention.intentions.ready()
    restarted = Attention(attention.path)
    assert not restarted.intentions.ready()
    assert restarted.status()["episodes"][-1]["focus"] == "Understand the object"
