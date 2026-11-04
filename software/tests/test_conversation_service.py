from concurrent.futures import Future
from dataclasses import replace
import threading
import time
import logging
from types import SimpleNamespace

import pytest

from robbi.jobs import BoundedExecutor
from robbi.services.conversation import ConversationOptions, ConversationService


def service(*, scheduler=None, background_scheduler=None, ask=None, options=None, camera=None, publish=None, daily=None):
    return ConversationService(
        memory=SimpleNamespace(
            recent_messages=lambda: [{"role": "user", "content": "earlier"}],
            prepare_prompt_context=lambda text, **kwargs: {"prompt_text": "remembered " + text},
        ),
        capture_image=(camera or SimpleNamespace(get_llm_image_bytes=lambda **kwargs: b"image")).get_llm_image_bytes,
        provider=SimpleNamespace(ask=ask or (lambda *args, **kwargs: {"speak": "answer"})),
        scheduler=scheduler, options=lambda: options or ConversationOptions(),
        background_scheduler=background_scheduler,
        daily_state=daily or (lambda: {"open_todos": [{"title": "Bücher"}]}),
        publish_event=publish or (lambda *args: None),
    )


def test_context_reads_only_requested_camera_and_daily_ports():
    camera_calls = []
    daily_calls = []
    subject = service(
        camera=SimpleNamespace(get_llm_image_bytes=lambda **kwargs: camera_calls.append(kwargs) or None),
        daily=lambda: daily_calls.append(True) or {"open_todos": [{"title": "Bücher"}]},
    )
    text = subject.prepare("hello", source="ui_text", include_photo=None)
    assert text.memory_text == "remembered hello"
    assert text.recent_messages == [{"role": "user", "content": "earlier"}]
    assert text.image_bytes is None
    assert camera_calls == daily_calls == []
    photo = subject.prepare("today", source="ui_text", include_photo=True, include_daily_state=True)
    assert "Bücher" in photo.memory_text
    assert photo.image_bytes is None  # Missing camera does not prevent a text request.
    assert camera_calls == [{"max_width": 640, "quality": 75}]
    assert daily_calls == [True]


def test_source_policy_and_explicit_photo_override():
    options = ConversationOptions(
        speech_provider="speech", speech_model="one", text_provider="text", text_model="two",
        attach_speech_photo=True, attach_text_photo=False,
    )
    assert options.target("speech_input") == ("speech", "one")
    assert options.target("telegram_text") == ("text", "two")
    assert options.attach_photo("speech_input", None)
    assert not options.attach_photo("speech_input", False)
    assert options.attach_photo("ui_text", True)
    assert not options.attach_photo("ui_text", None)


def test_recovered_camera_is_resolved_on_each_capture():
    current_camera = SimpleNamespace(get_llm_image_bytes=lambda **kwargs: None)
    subject = service(camera=SimpleNamespace(
        get_llm_image_bytes=lambda **kwargs: current_camera.get_llm_image_bytes(**kwargs),
    ))
    assert subject.prepare("look", source="ui_text", include_photo=True).image_bytes is None
    current_camera = SimpleNamespace(get_llm_image_bytes=lambda **kwargs: b"recovered")
    assert subject.prepare("look", source="ui_text", include_photo=True).image_bytes == b"recovered"


def test_remote_action_policy_does_not_modify_original():
    action = {"speak": "hello", "drive": {}, "sequence": [], "remember": "name"}
    assert service().executable_action(action, "telegram_text") == {"remember": "name"}
    assert "speak" in action
    allowed = ConversationOptions(telegram_speak=True, telegram_actions=True)
    assert service(options=allowed).executable_action(action, "telegram_text") == action
    assert service(options=replace(allowed, telegram_actions=False)).executable_action(action, "telegram_text") == {
        "speak": "hello", "sequence": [], "remember": "name",
    }


def test_provider_arguments_are_forwarded_without_robot_dependencies():
    calls = []
    with BoundedExecutor(1, 0) as executor:
        subject = service(scheduler=executor, ask=lambda *args, **kwargs: calls.append((args, kwargs)) or {"speak": "ok"})
        assert subject.ask("hello", image_bytes=b"jpeg", provider="example") == {"speak": "ok"}
    assert calls == [(("hello",), {"image_bytes": b"jpeg", "provider": "example"})]


def test_cancel_before_submission_never_enters_provider():
    cancelled = threading.Event()
    cancelled.set()
    subject = service(scheduler=SimpleNamespace(submit=lambda *_: pytest.fail("must not submit")))
    assert subject.ask("hello", cancelled=cancelled) == {"cancelled": True}
    assert subject.ask("hello", is_current=lambda: False) == {"cancelled": True}


def test_stop_releases_waiter_while_provider_is_blocked():
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    result = []

    def ask(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return {"speak": "late response"}

    executor = BoundedExecutor(1, 0)
    subject = service(scheduler=executor, ask=ask)
    worker = threading.Thread(target=lambda: result.append(subject.ask("hello", cancelled=cancelled)))
    try:
        worker.start()
        assert entered.wait(1)
        cancelled.set()
        worker.join(0.5)
        assert not worker.is_alive()
        assert result == [{"cancelled": True}]
    finally:
        release.set()
        worker.join(2)
        executor.shutdown()


def test_cancelled_queued_request_never_enters_provider():
    busy = threading.Event()
    release = threading.Event()
    submitted = threading.Event()
    cancelled = threading.Event()
    called = []
    result = []
    executor = BoundedExecutor(1, 1)
    executor.submit(lambda: busy.set() or release.wait(2))
    assert busy.wait(1)

    def submit(callback):
        future = executor.submit(callback)
        submitted.set()
        return future

    subject = service(scheduler=SimpleNamespace(submit=submit), ask=lambda *args: called.append(True) or {})
    worker = threading.Thread(target=lambda: result.append(subject.ask("hello", cancelled=cancelled)))
    try:
        worker.start()
        assert submitted.wait(1)
        cancelled.set()
        worker.join(0.5)
        assert result == [{"cancelled": True}]
    finally:
        release.set()
        worker.join(2)
        executor.shutdown()
    assert called == []


def test_deadline_expires_in_queue_without_provider_io():
    release = threading.Event()
    called = []
    events = []
    executor = BoundedExecutor(1, 1)
    executor.submit(lambda: release.wait(2))
    try:
        subject = service(scheduler=executor, ask=lambda *args: called.append(True) or {}, publish=lambda *args: events.append(args))
        assert subject.ask("hello", timeout_s=0.04)["face"] == "error"
    finally:
        release.set()
        executor.shutdown()
    assert called == []
    assert events[0][0] == "llm.timeout"


def test_worker_rechecks_deadline_before_provider_entry():
    calls = []
    saved = []
    subject = service(scheduler=SimpleNamespace(submit=lambda callback: saved.append(callback) or Future()), ask=lambda *args: calls.append(True) or {})
    subject.ask("hello", timeout_s=0.01)
    with pytest.raises(TimeoutError):
        saved[0]()
    assert calls == []


def test_provider_timeout_does_not_spin_until_outer_deadline():
    def fail(*args, **kwargs):
        raise TimeoutError("provider timeout")

    with BoundedExecutor(1, 0) as executor:
        started = time.monotonic()
        assert service(scheduler=executor, ask=fail).ask("hello", timeout_s=20)["face"] == "error"
        assert time.monotonic() - started < 1


def test_failed_diagnostic_publication_does_not_hide_provider_error():
    def fail(*args, **kwargs):
        raise RuntimeError("failed")

    with BoundedExecutor(1, 0) as executor:
        assert service(scheduler=executor, ask=fail, publish=fail).ask("hello")["face"] == "error"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadlines_fail_without_submission(timeout):
    with pytest.raises(ValueError):
        service().ask("hello", timeout_s=timeout)


def test_turn_policies_keep_vision_and_motion_instructions():
    speech = ConversationService.extra_instructions("speech_input", False, False)
    assert "spoken flow" in speech
    assert "prefer take_picture" in speech
    assert "Only drive if the user explicitly asks" in speech
    followup = ConversationService.extra_instructions("ui_text", True, True)
    assert "fresh camera image" in followup
    assert "prefer take_picture" not in followup


def test_controller_deadline_facade_rejects_old_request_after_stop():
    from robot.domains.conversation import ConversationDomain
    from robot.lifecycle import LifecycleMixin

    class Host(ConversationDomain, LifecycleMixin):
        pass

    host = Host()
    host._init_lifecycle()
    host.conversation_service = service(scheduler=SimpleNamespace(submit=lambda *_: pytest.fail("stale work submitted")))
    host._register_request(1, host._action_token()[0])
    host.cancel_actions()
    assert host._ask_llm_with_deadline("old question", request_id=1) == {"cancelled": True}


def test_vision_stop_during_capture_skips_provider_and_memory():
    from robot.domains.vision import VisionDomain
    from robot.lifecycle import LifecycleMixin

    class Host(VisionDomain, LifecycleMixin):
        def _submit_guarded(self, executor, callback):
            callback()

    host = Host()
    host._init_lifecycle()
    host._register_request(1, host._action_token()[0])
    host._research_executor = None

    def capture(**kwargs):
        host.cancel_actions()
        return b"late image"

    host.camera = SimpleNamespace(get_llm_image_bytes=capture)
    host._ask_llm_with_deadline = lambda *args, **kwargs: pytest.fail("provider used after Stop")
    host._remember_followup_result = lambda *args: pytest.fail("stale assistant response remembered")
    host._start_take_picture_followup(
        1, "what do you see", "ui_text", "example", "model", [], "memory",
        {"speak": "looking"}, {"save_photo": True},
    )


def test_tool_followup_stop_during_search_skips_provider():
    from robot.domains.tools import ToolsDomain
    from robot.lifecycle import LifecycleMixin

    class Host(ToolsDomain, LifecycleMixin):
        pass

    host = Host()
    host._init_lifecycle()
    host._register_request(1, host._action_token()[0])

    def search(*args):
        host.cancel_actions()
        return {"results": []}

    host._execute_web_search_tool = search
    host._ask_llm_with_deadline = lambda *args, **kwargs: pytest.fail("provider used after Stop")
    action = host._maybe_run_tool_followup(
        {"web_search": "weather"}, "weather", "ui_text", "example", "model", [], "memory", None, 1,
    )
    assert action["cancelled"] is True and "speak" not in action
    assert action["tool_chain"]["status"] == "cancelled"


def test_optional_memory_extraction_overload_does_not_fail_delivered_reply(monkeypatch):
    from robbi.errors import BusyError
    from robot.domains.tools import ToolsDomain
    from robot.config import settings

    monkeypatch.setattr(settings, "memory_extraction_enabled", True)
    events = []
    host = ToolsDomain()
    host._log = logging.getLogger("test")
    host._memory_extractor_executor = object()
    host._publish_event = lambda *args: events.append(args)

    def busy(*args):
        raise BusyError("shared work capacity reached")

    host._submit_guarded = busy
    host._queue_memory_extraction("hello", "ui_text", {"speak": "already delivered"})
    assert events[0][0] == "memory.extraction_skipped"


def test_vision_overload_returns_clear_failure_instead_of_raising():
    from robbi.errors import BusyError
    from robot.domains.vision import VisionDomain
    from robot.lifecycle import LifecycleMixin

    class Host(VisionDomain, LifecycleMixin):
        pass

    host = Host()
    host._init_lifecycle()
    host._register_request(1, host._action_token()[0])
    host._log = logging.getLogger("test")
    host._research_executor = object()
    events = []
    host._publish_event = lambda *args: events.append(args)

    def busy(*args):
        raise BusyError("shared work capacity reached")

    host._submit_guarded = busy
    result = host._start_take_picture_followup(1, "look", "ui_text", "example", "model", [], "", {}, {})
    assert result["error"] == "busy"
    assert "try again" in result["speak"]
    assert events[0][0] == "vision.followup_failed"


def test_remote_motion_permission_cannot_be_bypassed_by_nested_workflows():
    subject = service(options=ConversationOptions(telegram_speak=True, telegram_actions=False))
    action = {"app": {"id": "dance"}, "animation": "dance", "sequence": [
        {"drive": {"direction": "forward"}, "speak": "hello"},
        {"sequence": [{"filming_sequence": "drive", "slam": {}, "face": "happy"}]},
    ]}
    assert subject.executable_action(action, "telegram_text") == {"sequence": [
        {"speak": "hello"}, {"sequence": [{"face": "happy"}]},
    ]}
    assert "drive" in action["sequence"][0]  # Caller data remains intact.
    assert subject.executable_action(action, "ui_text") is action
    allowed = service(options=ConversationOptions(telegram_speak=True, telegram_actions=True))
    assert allowed.executable_action(action, "telegram_text") == action


@pytest.mark.parametrize("text", ["/drive forward", "/volume 100", "start focus timer"])
def test_remote_debug_and_special_shortcuts_check_permission_before_side_effects(text):
    from robot.domains.conversation import ConversationDomain
    from robot.domains.apps import AppsDomain
    from robot.lifecycle import LifecycleMixin

    class Host(ConversationDomain, AppsDomain, LifecycleMixin):
        pass

    host = Host()
    host._init_lifecycle()
    host.conversation_service = service()
    host._publish_event = lambda *args: None
    host.parse_debug_command = lambda *args: pytest.fail("side-effectful parser was called")
    host._maybe_run_special_app_request = lambda *args, **kwargs: pytest.fail("app shortcut executed")
    assert "disabled" in host.think_and_respond(text, source="telegram_text")["speak"]


def test_background_memory_stop_discards_late_provider_result():
    from robot.domains.tools import ToolsDomain
    from robot.lifecycle import LifecycleMixin

    class Host(ToolsDomain, LifecycleMixin):
        pass

    host = Host()
    host._init_lifecycle()
    host._collect_spoken_text = lambda action: action["speak"]
    host.memory = SimpleNamespace(
        prepare_prompt_context=lambda *args, **kwargs: {"prompt_text": ""}, recent_messages=lambda: [],
        remember=lambda *args, **kwargs: pytest.fail("stale memory committed"),
    )
    host._log = logging.getLogger("test")
    host._publish_event = lambda *args: None

    def stopped(*args, **kwargs):
        host.cancel_actions()
        return {"remember": "late memory"}

    host._ask_llm_with_deadline = stopped
    host._run_memory_extraction("hello", "ui_text", {"speak": "reply"})


def test_blocked_background_request_does_not_delay_chat_and_stop_cancels_both_waiters():
    background_entered = threading.Event()
    foreground_entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    results = {}

    def ask(text, **kwargs):
        if text == "quick chat":
            return {"speak": "immediate answer"}
        (background_entered if text == "memory" else foreground_entered).set()
        assert release.wait(2)
        return {"speak": "late result"}

    foreground = BoundedExecutor(1, 0)
    background = BoundedExecutor(1, 0)
    subject = service(scheduler=foreground, background_scheduler=background, ask=ask)
    memory_worker = threading.Thread(target=lambda: results.update(
        memory=subject.ask("memory", background=True, cancelled=cancelled),
    ))
    chat_worker = threading.Thread(target=lambda: results.update(
        chat=subject.ask("slow chat", cancelled=cancelled),
    ))
    try:
        memory_worker.start()
        assert background_entered.wait(1)
        # The only background provider worker remains blocked, but the live
        # provider worker can still complete a foreground request immediately.
        assert subject.ask("quick chat", timeout_s=0.3) == {"speak": "immediate answer"}
        chat_worker.start()
        assert foreground_entered.wait(1)
        cancelled.set()
        memory_worker.join(0.5)
        chat_worker.join(0.5)
        assert not memory_worker.is_alive()
        assert not chat_worker.is_alive()
        assert results == {"memory": {"cancelled": True}, "chat": {"cancelled": True}}
    finally:
        release.set()
        memory_worker.join(2)
        if chat_worker.ident is not None:
            chat_worker.join(2)
        foreground.shutdown()
        background.shutdown()


def test_background_scheduler_defaults_to_supplied_scheduler():
    with BoundedExecutor(1, 0) as executor:
        assert service(scheduler=executor).ask("memory", background=True) == {"speak": "answer"}


def test_debug_app_shortcut_returns_executed_app_result():
    from robot.domains.conversation import ConversationDomain
    from robot.lifecycle import LifecycleMixin

    class Host(ConversationDomain, LifecycleMixin):
        pass

    host = Host()
    host._init_lifecycle()
    host.conversation_service = service()
    host._publish_event = lambda *args: None
    host.parse_debug_command = lambda text: {"app": {"id": "focus-timer", "input": "5 seconds"}}
    host.execute_action = lambda *args, **kwargs: {"speak": "Focus timer started for 5 seconds."}
    assert host.think_and_respond("/timer 5 seconds") == {"speak": "Focus timer started for 5 seconds."}


def test_saturated_provider_lane_reports_busy_not_timeout():
    from robbi.errors import BusyError

    events = []

    def saturated(callback):
        raise BusyError("Feature task budget is full")

    subject = service(scheduler=SimpleNamespace(submit=saturated), publish=lambda *args: events.append(args))
    result = subject.ask("hello")
    assert result["error"] == "capacity"
    assert "busy" in result["speak"]
    assert [kind for kind, _ in events] == ["llm.busy"]


def test_provider_exception_is_reported_as_failure_not_timeout():
    events = []

    def unavailable(*args, **kwargs):
        raise RuntimeError("Provider dependency unavailable")

    with BoundedExecutor(1, 0) as executor:
        result = service(scheduler=executor, ask=unavailable, publish=lambda *args: events.append(args)).ask("hello")
    assert result["error"] == "provider_error"
    assert "did not respond in time" not in result["speak"]
    assert [kind for kind, _ in events] == ["llm.failed"]


def test_provider_timeout_retains_timeout_diagnostic():
    events = []

    def timed_out(*args, **kwargs):
        raise TimeoutError("network request timed out")

    with BoundedExecutor(1, 0) as executor:
        result = service(scheduler=executor, ask=timed_out, publish=lambda *args: events.append(args)).ask("hello")
    assert result["error"] == "timeout"
    assert [kind for kind, _ in events] == ["llm.timeout"]


def test_cancelled_admission_failure_does_not_emit_error():
    from robbi.errors import BusyError

    cancelled = threading.Event()
    events = []

    def saturated(callback):
        cancelled.set()
        raise BusyError("Feature task budget is full")

    subject = service(scheduler=SimpleNamespace(submit=saturated), publish=lambda *args: events.append(args))
    assert subject.ask("hello", cancelled=cancelled) == {"cancelled": True}
    assert events == []
