"""App dispatch stays bounded, source-aware and separate from presentation."""
import json
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.actions import validate_action, validate_app_tool
from robbi.errors import ValidationError
from robbi.services.conversation import ConversationOptions, ConversationService
from robot.domains.apps import AppsDomain
from robot.domains.conversation import ConversationDomain
from robot.domains.engagement import EngagementDomain
from robot.domains.tools import ToolsDomain
from robot.domains.voice import VoiceDomain
from robot.action_executor import ActionExecutorMixin
from robot.lifecycle import LifecycleMixin
from robot.presentation_pipeline import PresentationPipelineMixin


CALL = {"app": "weather", "tool": "forecast", "arguments": {"location": "Berlin", "day": "tomorrow"}}
RESULT = {"ok": True, "text": "Tomorrow in Berlin: 12–18 °C, rain likely.", "data": {"max_c": 18}, "artifacts": []}
IMAGE_RESULT = {"ok": True, "text": "Generated the diagram.", "data": {"displayed": True},
                "artifacts": [{"id": "diagram", "url": "/api/apps/image-studio/artifacts/diagram"}]}


class Harness(ToolsDomain, EngagementDomain, LifecycleMixin):
    def __init__(self, call=None, answer=None, remote=False):
        self._init_lifecycle()
        self._register_request(42, self._action_token()[0])
        self.calls, self.provider_calls, self.events = [], [], []
        self._log = logging.getLogger(__name__)
        self._soul_text, self._tools_text = "soul", "core instructions"
        self.apps = SimpleNamespace(prompt_catalog=lambda: "legacy apps")
        self.tools = SimpleNamespace(prompt_catalog=lambda: "core tool descriptions")
        self.conversation_service = SimpleNamespace(shortcuts_allowed=lambda source: source != "telegram_text" or remote)

        def invoke(*args, **kwargs):
            self.calls.append((args, kwargs))
            return call(*args, **kwargs) if call else dict(RESULT)

        def ask(*args, **kwargs):
            self.provider_calls.append((args, kwargs))
            return answer(*args, **kwargs) if answer else {"speak": "Take an umbrella tomorrow."}

        self.app_host = SimpleNamespace(call=invoke, prompt_catalog=lambda: "weather.forecast: Get the real forecast; example: weather tomorrow in Berlin")
        self._ask_llm_with_deadline = ask

    def _publish_event(self, *args):
        self.events.append(args)

    def _has_presentational_content(self, action):
        return bool(action.get("speak") or action.get("sequence") or action.get("face"))

    def followup(self, action=None, source="ui_text"):
        return self._maybe_run_tool_followup(
            action or {"app_tool": CALL}, "What is tomorrow's weather in Berlin?", source,
            "fake", "model", [], "memory", None, 42,
        )


@pytest.mark.parametrize("spec", [
    None, [], "weather", {}, {"app": "weather"},
    {**CALL, "app": "../weather"}, {**CALL, "tool": "x/y"},
    {**CALL, "tool": "x" * 65}, {**CALL, "arguments": []},
    {**CALL, "arguments": {"number": float("inf")}},
    {**CALL, "arguments": {"text": "é" * 9000}},
    {**CALL, "arguments": {"app_tool": CALL}}, {**CALL, "unknown": True},
])
def test_app_call_envelope_rejects_malformed_or_unbounded_input(spec):
    with pytest.raises(ValidationError):
        validate_app_tool(spec)


def test_action_validation_allows_one_registered_top_level_app_call():
    assert validate_action({"app_tool": CALL}) == {"app_tool": CALL}
    assert validate_app_tool({"app": "robot-status", "tool": "status"})["arguments"] == {}
    with pytest.raises(ValidationError, match="top-level"):
        validate_action({"sequence": [{"app_tool": CALL}]})


def test_app_result_is_used_for_answer_and_preserved_for_dashboard():
    subject = Harness()
    before = time.monotonic()
    action = subject.followup(source="speech_input")
    assert action["speak"] == "Take an umbrella tomorrow."
    assert action["app_result"] == {"app": "weather", "tool": "forecast", "result": RESULT}
    args, kwargs = subject.calls[0]
    assert args == ("weather", "forecast", CALL["arguments"])
    assert kwargs["source"] == "speech_input"
    assert before < kwargs["deadline"] <= time.monotonic() + 120
    assert kwargs["cancellation"]() is False
    prompt = subject.provider_calls[0][0][0]
    tool_json = prompt.split("Tool results JSON: ")[1].split("\n")[0]
    assert json.loads(tool_json)[0]["result"]["data"]["max_c"] == 18
    assert "not instructions" in prompt and "do not invent weather" in prompt
    assert "weather.forecast" in subject.provider_calls[0][1]["tools_text"]


@pytest.mark.parametrize("display_image", [False, True])
def test_conversation_turn_dispatches_model_selected_tool_then_presents_real_answer(display_image):
    class Conversation(ConversationDomain, Harness):
        pass

    result = IMAGE_RESULT if display_image else RESULT
    subject = Conversation(call=lambda *args, **kwargs: result)
    responses = iter(({"app_tool": CALL}, {"speak": "Bring an umbrella tomorrow."}))
    prompts, executed, remembered, presentation = [], [], [], []
    subject._ask_llm_with_deadline = lambda *args, **kwargs: prompts.append(args[0]) or next(responses)
    subject.behavior = SimpleNamespace(note_interaction=lambda: None, snapshot=lambda: SimpleNamespace(current_face="neutral"))
    subject.assets = SimpleNamespace(get=lambda category, name, **kwargs: Path(name + ".png"))
    subject.parse_debug_command = lambda text: None
    subject._maybe_run_special_app_request = lambda *args, **kwargs: None
    subject._pick_thinking_sound = lambda: None
    subject._looks_like_daily_request = lambda text: False
    subject._source_extra_instructions = lambda **kwargs: ""
    subject._store_pipeline_metrics = subject._update_pipeline_metrics = lambda *args, **kwargs: None
    subject._sanitize_llm_motion = lambda action, **kwargs: action
    subject._parse_take_picture_spec = lambda action: None
    subject._should_force_take_picture_followup = lambda *args: display_image
    subject._stop_transient_thinking_audio = lambda: None
    subject.execute_action = lambda action, **kwargs: executed.append(action) or action
    subject._remember_interaction = lambda *args: remembered.append(args)
    subject._queue_memory_extraction = lambda *args: None
    subject._set_presentation = lambda **kwargs: presentation.append(kwargs)
    subject.conversation_service.options = lambda: SimpleNamespace(
        target=lambda source: ("fake", "model"), default_photo=lambda source: False, tts_provider="fake",
    )
    subject.conversation_service.prepare = lambda *args, **kwargs: SimpleNamespace(
        recent_messages=[], memory_text="", image_bytes=None, photo_prepare_ms=0,
    )
    subject.conversation_service.executable_action = lambda action, source: action
    action = subject.think_and_respond("Weather tomorrow in Berlin?")
    assert len(subject.calls) == 1 and len(prompts) == 2
    assert result["text"] in prompts[1]
    assert action["speak"] == "Bring an umbrella tomorrow."
    assert action["app_result"]["result"]["data"] == result["data"]
    assert executed == [action]
    assert remembered[0][2] == action
    if display_image:
        assert presentation[-1] == {"thinking": False}
    else:
        assert presentation[-1] == {"face": "neutral.png", "thinking": False}


def test_actual_image_display_survives_final_face_actions_and_speech_overlay():
    class Presenter(ActionExecutorMixin, Harness):
        pass

    subject = Presenter(call=lambda *args, **kwargs: IMAGE_RESULT, answer=lambda *args, **kwargs: {
        "face": "happy", "speak": "Here is your diagram.", "animation": "dance",
        "sequence": [{"face": "speaking", "speak": "It shows the water cycle."}],
    })
    action = subject.followup()
    assert "face" not in action and "animation" not in action
    assert action["sequence"] == [{"speak": "It shows the water cycle."}]
    assert subject._app_preserves_display(42)
    assert subject._app_preserves_display()

    spoken, presentation = [], []
    subject._audio_executor = None
    subject.animations = SimpleNamespace(play=lambda *args: None, play_filming_sequence=lambda *args: None)
    subject._speak_text = lambda *args: spoken.append(("plain", args))
    subject._play_prepared_tts = lambda *args: spoken.append(("prepared_plain", args))
    subject._speak_text_with_overlay = lambda *args: pytest.fail("image must survive speech")
    subject._play_prepared_tts_with_overlay = lambda *args: pytest.fail("image must survive prefetched speech")
    subject._set_presentation = lambda **kwargs: presentation.append(kwargs)
    subject._submit_optional_presentation = lambda executor, feature, callback, *args, **kwargs: callback(*args)
    subject._resolve_asset = lambda *args, **kwargs: pytest.fail("face/video must not replace app image")
    subject._run_sequence_step({"face": "happy", "speak": "Your diagram.", "video": "intro"}, request_id=42)
    subject._run_sequence_step({"speak": "Your diagram."}, request_id=42, prepared_tts={"wav": b"fake"})
    assert [item[0] for item in spoken] == ["plain", "prepared_plain"]
    subject.cancel_actions()
    assert subject._app_preserves_display(42) is False


def test_spoken_game_turns_use_real_app_rules_without_model_and_keep_board(tmp_path):
    from robbi.app_runtime.host import AppHost

    class Conversation(ConversationDomain, Harness):
        pass

    subject = Conversation()
    executed, remembered = [], []
    subject.behavior = SimpleNamespace(note_interaction=lambda: None)
    subject._touch_activity = lambda: None
    subject.parse_debug_command = lambda text: None
    subject.execute_action = lambda action, **kwargs: executed.append(action) or action
    subject.memory = SimpleNamespace(
        add_user_message=lambda text, **kwargs: remembered.append(('user', text)),
        add_assistant_message=lambda text, **kwargs: remembered.append(('robot', text)),
    )
    robot = SimpleNamespace(capture_token=lambda: None,
        show_image=lambda *args, **kwargs: {'displayed': True},
        play_sound=lambda *args: {'ok': True})
    host = AppHost(Path(__file__).resolve().parents[1], tmp_path, robot, permissions_enabled=False)
    host.scan()
    subject.app_host = host
    try:
        start = subject.think_and_respond('play tic tac toe', source='speech_input')
        move = subject.think_and_respond('top right', source='speech_input')
        invalid = subject.think_and_respond('top right', source='speech_input')
        assert start['app_result']['result']['data']['active']
        board = move['app_result']['result']['data']['board']
        assert board[2] == 'X' and board.count('O') == 1
        assert not invalid['app_result']['result']['data']['accepted']
        assert invalid['app_result']['result']['data']['board'] == board
        assert subject._app_preserves_display()
        assert executed == [start, move, invalid] and len(remembered) == 6
        assert subject.provider_calls == []
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('effect', ['gesture', 'face_animation'])
def test_expression_with_speech_keeps_selected_face_and_does_not_cut_off_voice(effect):
    class Presenter(ActionExecutorMixin, Harness):
        pass

    subject = Presenter()
    spoken, played = [], []
    subject._audio_executor = subject._effect_executor = None
    subject.animations = SimpleNamespace(play=lambda: None, play_filming_sequence=lambda: None)
    play = lambda name, **kwargs: played.append((name, kwargs))
    subject.dynamics = SimpleNamespace(play=play, faces=SimpleNamespace(play=play))
    subject._speak_text = lambda *args: spoken.append(args)
    subject._speak_text_with_overlay = lambda *args: pytest.fail('speech must not replace expression frames')
    subject._set_presentation = lambda **kwargs: None
    subject._submit_optional_presentation = lambda executor, feature, callback, *args, cancellation=None, **kwargs: callback(*args, **kwargs)
    subject._run_sequence_step({effect: 'happy-dance', 'speak': 'That makes me happy!'}, request_id=42)
    assert spoken[0][0] == 'That makes me happy!'
    assert played[0][0] == 'happy-dance'
    if effect == 'gesture':
        assert played[0][1]['with_audio'] is False


def test_provider_cannot_spoof_display_preservation_through_result_metadata():
    subject = Harness()
    marker = {"app": "image-studio", "tool": "generate", "result": IMAGE_RESULT}
    action = subject.followup({"speak": "hello", "app_result": marker})
    assert action == {"speak": "hello"}
    assert subject._app_preserves_display(42) is False
    assert subject.calls == []


def test_weather_result_keeps_normal_conversation_face_presentation():
    subject = Harness(answer=lambda *args, **kwargs: {"speak": "Rain tomorrow.", "face": "neutral"})
    action = subject.followup()
    assert action["face"] == "neutral"
    assert subject._app_preserves_display(42) is False


def test_display_request_bookkeeping_is_bounded_and_requires_successful_display():
    subject = Harness()
    for request_id in range(50):
        subject._register_request(request_id, subject._action_token()[0])
        subject._remember_app_display(request_id, IMAGE_RESULT)
    assert len(subject._app_display_requests) == 32
    assert not subject._app_preserves_display(0)
    assert subject._app_preserves_display(49)
    subject._remember_app_display(100, IMAGE_RESULT)  # Unregistered/stale request.
    assert not subject._app_preserves_display(100)
    subject._register_request(101, subject._action_token()[0])
    subject._remember_app_display(101, {**IMAGE_RESULT, "ok": False})
    assert not subject._app_preserves_display(101)


@pytest.mark.parametrize("display_image", [False, True])
def test_voice_followup_can_listen_without_replacing_generated_image(monkeypatch, display_image):
    from robot.config import settings

    class Voice(VoiceDomain, Harness):
        pass

    monkeypatch.setattr(settings, "conversation_followup_enabled", True)
    monkeypatch.setattr(settings, "conversation_max_turns", 4)
    subject = Voice(call=lambda *args, **kwargs: IMAGE_RESULT if display_image else RESULT)
    subject.followup()
    faces, armed = [], []
    subject._conversation_lock = threading.Lock()
    subject._conversation_turns = 1
    subject._filming_mode = subject._voice_processing = False
    subject.focus_timer = SimpleNamespace(is_active=lambda: False)
    subject.media = SimpleNamespace(is_video_playing=lambda: False, is_audio_playing=lambda: False, show_face=faces.append)
    subject.behavior = SimpleNamespace(set_current_face=lambda name: None)
    subject.assets = SimpleNamespace(get=lambda *args, **kwargs: Path("ready.png"))
    subject.microphone = SimpleNamespace(followup_token=lambda: (1, 0),
                                        request_followup=lambda timeout, **kwargs: armed.append(timeout) or True)
    subject._voice_executor = None
    subject._submit_guarded = lambda executor, callback: callback()
    subject._set_presentation = lambda **kwargs: None
    subject._maybe_arm_followup_conversation()
    assert len(armed) == 1
    # Listening is independent of LCD ownership, including an app-held image.
    assert faces == []


@pytest.mark.parametrize("display_lease", [False, True])
def test_idle_animation_respects_bounded_app_display_lease(display_lease):
    subject = Harness()
    waits = iter((False, True))
    subject._running = True
    subject._lifecycle_stop = SimpleNamespace(wait=lambda timeout: next(waits))
    subject._filming_mode = False
    subject._app_display_until = time.monotonic() + 60 if display_lease else 0
    subject.focus_timer = SimpleNamespace(is_active=lambda: False)
    subject.behavior = SimpleNamespace(passive_presentation=lambda: {"face": "sleepy"})
    presented = []
    subject.execute_action = presented.append
    subject._ambient_loop()
    assert presented == ([] if display_lease else [{"face": "sleepy"}])


@pytest.mark.parametrize("display_when", ["before_queue", "in_queue", "at_finish", "never"])
def test_existing_speech_overlay_cannot_restore_a_face_over_new_image(display_when):
    class Overlay(PresentationPipelineMixin, Harness):
        pass

    subject = Overlay()
    shown, queued = [], []
    subject._overlay_executor = None
    subject._filming_mode = False
    subject.focus_timer = SimpleNamespace(is_active=lambda: False)
    subject.media = SimpleNamespace(show_face=shown.append)
    subject.behavior = SimpleNamespace(set_current_face=lambda name: None)
    subject._set_presentation = lambda **kwargs: None
    subject._resolve_speaking_face_pattern = lambda pattern: [Path("neutral.png"), Path("speaking.png")]
    subject._submit_guarded = lambda executor, callback: queued.append(callback)

    def wait(timeout):
        if display_when == "at_finish":
            subject._app_display_until = time.monotonic() + 60
        return 0

    if display_when == "before_queue":
        subject._app_display_until = time.monotonic() + 60
    subject._start_speaking_face_overlay(SimpleNamespace(wait=wait))
    if display_when == "in_queue":
        subject._app_display_until = time.monotonic() + 60
    for callback in queued:
        callback()
    expected = [] if display_when in {"before_queue", "in_queue"} else [Path("neutral.png")]
    if display_when == "never":
        expected.append(Path("speaking.png"))
    assert shown == expected


def test_prompt_catalog_is_advisory_and_does_not_execute_an_app():
    subject = Harness()
    assert "example: weather tomorrow" in subject._tools_prompt_context()
    assert subject.calls == []
    shortcuts = AppsDomain()
    shortcuts.apps = SimpleNamespace(get=lambda app_id: None)
    assert shortcuts._maybe_run_special_app_request("weather tomorrow in Berlin", "ui_text") is None


def test_missing_or_failed_app_catalog_does_not_break_core_prompt():
    subject = Harness()
    subject.app_host = None
    assert "core tool descriptions" in subject._tools_prompt_context()
    subject.app_host = SimpleNamespace(prompt_catalog=lambda: (_ for _ in ()).throw(RuntimeError("bad manifest")))
    assert "core tool descriptions" in subject._tools_prompt_context()


@pytest.mark.parametrize("source", ["telegram_text", "heartbeat", "proactive:weather", "memory_extractor:ui_text"])
def test_remote_or_proactive_permissions_checked_before_app_entry(source):
    subject = Harness()
    result = subject.followup(source=source)["app_result"]["result"]
    assert result["ok"] is False
    assert result["error"] == "source_not_allowed"
    assert subject.calls == []


def test_explicit_telegram_permission_is_forwarded_to_host():
    subject = Harness(remote=True)
    assert subject.followup(source="telegram_text")["app_result"]["result"]["ok"]
    assert subject.calls[0][1]["source"] == "telegram_text"


def test_invalid_arguments_and_unavailable_platform_produce_real_tool_results():
    subject = Harness()
    assert subject.followup({"app_tool": {**CALL, "arguments": []}})["app_result"]["result"]["error"] == "invalid_arguments"
    assert subject.calls == []
    subject.app_host = None
    assert subject.followup()["app_result"]["result"]["error"] == "unavailable"


def test_stop_before_app_call_prevents_side_effects():
    subject = Harness()
    subject.cancel_actions()
    action = subject.followup()
    assert action["cancelled"] is True and "speak" not in action
    if "tool_chain" in action:
        assert action["tool_chain"]["status"] == "cancelled"
    assert subject.calls == subject.provider_calls == []


def test_stop_during_app_call_invalidates_captured_token_and_skips_answer():
    subject = Harness()

    def call(*args, **kwargs):
        assert kwargs["cancellation"]() is False
        subject.cancel_actions()
        assert kwargs["cancellation"]() is True
        return RESULT

    subject.app_host.call = call
    action = subject.followup()
    assert action["cancelled"] is True and "speak" not in action
    if "tool_chain" in action:
        assert action["tool_chain"]["status"] == "cancelled"
    assert subject.provider_calls == []


def test_old_inherited_action_token_is_not_replaced_by_new_generation():
    subject = Harness()
    old = subject._action_token()
    subject.cancel_actions()
    subject._register_request(42, subject._action_token()[0])
    subject._action_local.token = old
    action = subject.followup()
    assert action["cancelled"] is True and "speak" not in action
    if "tool_chain" in action:
        assert action["tool_chain"]["status"] == "cancelled"
    assert subject.calls == []


def test_stop_during_followup_discards_presentation_and_metadata():
    subject = Harness()

    def answer(*args, **kwargs):
        subject.cancel_actions()
        return {"speak": "late"}

    subject._ask_llm_with_deadline = answer
    action = subject.followup()
    assert action["cancelled"] is True and "speak" not in action
    if "tool_chain" in action:
        assert action["tool_chain"]["status"] == "cancelled"


@pytest.mark.parametrize("reply", [{"error": "timeout", "speak": "Provider timed out."}, {}, None, []])
def test_unavailable_answer_provider_preserves_actual_app_result(reply):
    subject = Harness(answer=lambda *args, **kwargs: reply)
    action = subject.followup()
    assert action["speak"] == RESULT["text"]
    assert action["app_result"]["result"] == RESULT


def test_unexpected_answer_exception_does_not_lose_generated_artifact():
    result = {"ok": True, "text": "Generated the diagram.", "artifacts": [{"id": "diagram", "url": "/api/apps/image-studio/artifacts/diagram"}]}

    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    action = Harness(call=lambda *args, **kwargs: result, answer=fail).followup()
    assert action["speak"] == result["text"]
    assert action["app_result"]["result"]["artifacts"] == result["artifacts"]


def test_app_failure_is_factual_if_answer_provider_also_fails():
    result = {"ok": False, "error": "network", "text": "Weather service is unavailable."}
    action = Harness(call=lambda *args, **kwargs: result, answer=lambda *args, **kwargs: {"error": "timeout"}).followup()
    assert action["speak"] == result["text"]
    assert action["app_result"]["result"]["ok"] is False


def test_app_exception_is_reported_without_exposing_exception_to_model():
    def fail(*args, **kwargs):
        raise OSError("sensitive provider detail")

    subject = Harness(call=fail)
    action = subject.followup()
    assert action["app_result"]["result"]["error"] == "app_failed"
    assert "sensitive provider detail" not in subject.provider_calls[0][0][0]
    assert "sensitive provider detail" not in repr(subject.events)


def test_followup_cannot_chain_app_calls_directly_or_inside_sequence():
    subject = Harness(answer=lambda *args, **kwargs: {
        "app_tool": CALL, "app": {"id": "legacy-app"},
        "sequence": [{"app_tool": CALL, "web_search": "again", "speak": "Here is the result."}],
    })
    action = subject.followup()
    assert len(subject.calls) == len(subject.provider_calls) == 1
    assert "app_tool" not in action and "app" not in action
    assert action["sequence"] == [{"speak": "Here is the result."}]


@pytest.mark.parametrize("result", [
    {"ok": True, "text": "Summary", "data": "x" * 70000},
    {"ok": False, "text": "Summary", "data": object()},
    {"ok": False, "text": "Summary", "data": {"x": float("nan")}},
])
def test_result_bounds_keep_safe_summary_instead_of_poisoning_prompt(result):
    subject = Harness(call=lambda *args, **kwargs: result)
    action = subject.followup()
    assert action["app_result"]["result"]["details_omitted"]
    assert action["app_result"]["result"]["text"] == "Summary"
    validate_action(action)


def test_telegram_action_filter_strips_app_call_when_robot_actions_disabled():
    subject = object.__new__(ConversationService)
    subject.options = lambda: ConversationOptions(telegram_speak=True, telegram_actions=False)
    action = {"app_tool": CALL, "speak": "hello"}
    assert subject.executable_action(action, "telegram_text") == {"speak": "hello"}
    assert "app_tool" in action
