from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError
from robbi.jobs import BoundedExecutor
from robbi.orchestration import Intent, current_intent, intent_scope
from robbi.services.background_work import BackgroundTaskService, BackgroundWorkOptions
from robbi.services.background_work import TaskContext
from robbi.services.conversation import ConversationOptions, ConversationService
from robot.domains.background_work import BackgroundWorkDomain
from robot.domains.conversation import ConversationDomain
from robot.lifecycle import LifecycleMixin, UnavailableComponent


def wait_until(check, timeout=2):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline
        threading.Event().wait(.005)


def submit(service, work, **kwargs):
    return service.submit(text="Research the topic", source="ui_text", provider="example", model="deep", work=work, **kwargs)


def test_requested_reply_delivers_in_current_session_but_never_replays_on_restart(tmp_path):
    with BoundedExecutor(1, 2) as executor:
        path = tmp_path / "tasks.db"
        service = BackgroundTaskService(path, scheduler=executor, options=lambda: BackgroundWorkOptions(notify_on_complete=False))
        requested = submit(service, lambda context: {"speak": "Here is the news."}, reply_requested=True)
        silent = submit(service, lambda context: {"speak": "An unsolicited result."})
        wait_until(lambda: service.get(silent["id"])["status"] == "completed")
        assert [t["id"] for t in service.pending_delivery()] == [requested["id"]]
        restarted = BackgroundTaskService(path, scheduler=executor, options=lambda: BackgroundWorkOptions(notify_on_complete=False))
        assert restarted.pending_delivery() == []
        assert restarted.get(requested["id"])["result"]["speak"] == "Here is the news."


def test_current_news_routes_early_but_physical_requests_do_not():
    assert BackgroundWorkDomain._long_work_request("What are today's technology headlines?")
    assert BackgroundWorkDomain._long_work_request("Was sind die aktuellen Nachrichten heute?")
    assert not BackgroundWorkDomain._long_work_request("Drive forward 10 centimeters")


def test_background_queue_is_bounded_and_cancelled_queued_work_never_runs(tmp_path):
    executor = BoundedExecutor(1, 2)
    subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def block(context):
        entered.set()
        assert release.wait(2)
        return {"speak": "done"}

    try:
        one = submit(subject, block)
        assert entered.wait(1)
        two = submit(subject, lambda context: calls.append(2) or {"speak": "two"})
        three = submit(subject, lambda context: calls.append(3) or {"speak": "three"})
        with pytest.raises(BusyError):
            submit(subject, block)
        assert subject.cancel(two["id"])["status"] == "cancelled"
        release.set()
        wait_until(lambda: subject.get(three["id"])["status"] == "completed")
        assert calls == [3]
        assert subject.get(one["id"])["status"] == "completed"
    finally:
        release.set()
        executor.shutdown()


def test_stop_is_immediate_even_when_storage_lock_is_held(tmp_path):
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor)
        locked, release = threading.Event(), threading.Event()
        def hold():
            with subject._lock:
                locked.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        assert locked.wait(1)
        try:
            started = time.monotonic()
            subject.cancel_all()
            assert time.monotonic() - started < .1
        finally:
            release.set()
            thread.join(1)


def test_durable_result_restart_reconciliation_and_bounded_history(tmp_path):
    path = tmp_path / "tasks.db"
    with BoundedExecutor(1, 2) as executor:
        options = lambda: BackgroundWorkOptions(max_history=4)
        subject = BackgroundTaskService(path, scheduler=executor, options=options)
        for index in range(6):
            record = submit(subject, lambda context, i=index: {"speak": str(i)})
            wait_until(lambda: subject.get(record["id"])["status"] == "completed")
        assert len(subject.snapshot()["tasks"]) == 4
        restored = BackgroundTaskService(path, scheduler=executor, options=options)
        assert restored.get(record["id"])["result"] == {"speak": "5"}
        interrupted = dict(record, id="f" * 32, status="running", result=None)
        restored._records[interrupted["id"]] = interrupted
        restored._store.save(interrupted, restored._records)
        restarted = BackgroundTaskService(path, scheduler=executor, options=options)
        assert restarted.get(interrupted["id"])["status"] == "interrupted"
        assert restarted.get(interrupted["id"])["result"] is None


def test_background_does_not_inherit_or_interrupt_foreground_intent(tmp_path):
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor)
        owner = Intent("foreground")
        observed = []
        with intent_scope(owner):
            task = submit(subject, lambda context: observed.append(current_intent()) or {"speak": "done"})
        wait_until(lambda: subject.get(task["id"])["status"] == "completed")
        assert observed == [None]
        assert not owner.is_set()


def test_results_are_persisted_before_completion_notification_and_stop_suppresses_delivery(tmp_path):
    path = tmp_path / "tasks.db"
    observed = []
    def event(kind, payload):
        if kind == "background.completed":
            restored = BackgroundTaskService(path, scheduler=None)
            observed.append(restored.get(payload["task_id"])["status"])
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(path, scheduler=executor, publish_event=event,
                                        options=lambda: BackgroundWorkOptions(notify_on_complete=True))
        task = submit(subject, lambda context: {"speak": "saved"})
        wait_until(lambda: bool(observed))
        assert observed == ["completed"]
        assert subject.pending_delivery()
        subject.cancel_all()
        assert not subject.pending_delivery()
        assert subject.get(task["id"])["result"]["speak"] == "saved"


def test_oversized_or_failed_results_are_not_announced_as_success(tmp_path):
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor,
                                        options=lambda: BackgroundWorkOptions(notify_on_complete=True))
        huge = submit(subject, lambda context: {"speak": "x" * 90000})
        wait_until(lambda: subject.get(huge["id"])["status"] == "failed")
        failed = submit(subject, lambda context: {"speak": "Provider unavailable", "error": "provider_error"})
        wait_until(lambda: subject.get(failed["id"])["status"] == "failed")
        assert {row["status"] for row in subject.pending_delivery()} == {"failed"}


class Host(BackgroundWorkDomain, ConversationDomain, LifecycleMixin):
    PROGRESS_DELAY_S = .03
    def __init__(self, path, ask, options=None):
        self._init_lifecycle()
        self.options = options or BackgroundWorkOptions(foreground_budget_s=.03, timeout_s=2, provider="strong", model="deep", notify_on_complete=True)
        self._background_work_options = lambda: self.options
        self.live = BoundedExecutor(2, 0)
        self.work = BoundedExecutor(1, 2)
        self.background_work = BackgroundTaskService(path, scheduler=self.work, options=self._background_work_options)
        self.llm = SimpleNamespace(ask=ask)
        self.memory = SimpleNamespace(recent_messages=lambda: [], prepare_prompt_context=lambda *a, **k: {"prompt_text": "memory"},
            add_user_message=lambda *a, **k: None, add_assistant_message=lambda *a, **k: None)
        self.conversation_service = ConversationService(memory=self.memory, capture_image=lambda **k: None,
            provider=self.llm, scheduler=self.live, options=lambda: ConversationOptions(text_provider="fast", text_model="small"),
            daily_state=lambda: {}, publish_event=lambda *a: None)
        self._soul_text = ""
        self.apps = SimpleNamespace(prompt_catalog=lambda: "")
        self._tools_prompt_context = lambda query="": ""
        self._looks_like_daily_request = lambda text: False
        self._set_presentation = lambda **kwargs: None
        self._publish_event = lambda *args: None
        self.actions = []
        self.execute_action = lambda action, **kwargs: self.actions.append(action)
        self._maybe_run_tool_followup = lambda **kwargs: kwargs["action"]
        self._collect_spoken_text = lambda action: action.get("speak", "")
        self._register_request(1, 0)

    def planned_research(self, text, source="ui_text", request_id=1):
        getter = getattr(self, '_tool_scope_source', None)
        recent = self._tool_followup_store().messages(getter(source, request_id)) if getter else []
        prepared = self.conversation_service.prepare(text, source=source, include_photo=False, recent_messages=recent)
        return self._defer_background_action({'speak': 'Let me investigate that.', 'background_task': {}},
            text, source, request_id, prepared, 'fast', 'small')

    def close(self):
        self.background_work.shutdown()
        self.live.shutdown()
        self.work.shutdown()


def test_known_long_task_acknowledges_before_provider_and_uses_selected_model(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def ask(text, **kwargs):
        calls.append((text, kwargs))
        entered.set()
        release.wait(2)
        return {"speak": "research result", "drive": {"direction": "forward"}}
    host = Host(tmp_path / "tasks.db", ask)
    try:
        started = time.monotonic()
        ack = host.planned_research("Research lunar geology")
        assert time.monotonic() - started < .3
        assert ack["background_task"]["id"]
        assert entered.wait(1)
        assert calls[0][1]["provider"] == "strong"
        assert calls[0][1]["model"] == "deep"
        release.set()
        task_id = ack["background_task"]["id"]
        wait_until(lambda: host.background_work.get(task_id)["status"] == "completed")
        assert host.background_work.get(task_id)["result"] == {"speak": "research result"}
        assert all("drive" not in action for action in host.actions)
    finally:
        release.set()
        host.close()


def test_slow_foreground_adopts_same_future_and_next_chat_still_runs(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def ask(text, **kwargs):
        calls.append(text)
        if text == "slow":
            entered.set()
            release.wait(2)
        return {"speak": text + " answer"}
    host = Host(tmp_path / "tasks.db", ask)
    try:
        ack = host._ask_responsive_conversation("slow", source="speech_input", request_id=1, provider="fast", model="small")
        assert entered.is_set()
        assert ack["background_task"]["provider"] == "fast"
        # Keep the first handoff fast, but do not race OS worker startup against
        # a 30 ms assertion while the full suite is running on a busy machine.
        host.options = replace(host.options, foreground_budget_s=.5)
        quick = host._ask_responsive_conversation("quick", source="ui_text", request_id=1, provider="fast", model="small")
        assert quick == {"speak": "quick answer"}
        release.set()
        task_id = ack["background_task"]["id"]
        wait_until(lambda: host.background_work.get(task_id)["status"] == "completed")
        assert calls.count("slow") == 1
        assert host.background_work.get(task_id)["result"] == {"speak": "slow answer"}
    finally:
        release.set()
        host.close()


def test_typed_chat_returns_real_answer_after_voice_handoff_budget(tmp_path):
    def ask(text, **kwargs):
        time.sleep(.08)  # Longer than this fixture's 30 ms spoken handoff budget.
        return {"speak": "The actual answer"}
    host = Host(tmp_path / "tasks.db", ask)
    host.PROGRESS_DELAY_S = .2
    try:
        result = host._ask_responsive_conversation("hello", source="ui_text", request_id=1,
                                                   provider="fast", model="small")
        assert result == {"speak": "The actual answer"}
        assert host.background_work.snapshot()["tasks"] == []
    finally:
        host.close()


def test_stop_cancels_adopted_task_and_discards_late_provider_response(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def ask(text, **kwargs):
        entered.set()
        release.wait(2)
        return {"speak": "late"}
    host = Host(tmp_path / "tasks.db", ask)
    try:
        ack = host._ask_responsive_conversation("slow", source="speech_input", request_id=1, provider="fast", model="small")
        host.cancel_actions()
        host.background_work.cancel_all()
        wait_until(lambda: host.background_work.get(ack["background_task"]["id"])["status"] == "cancelled")
        release.set()
        wait_until(lambda: not host.background_work._active)
        assert host.background_work.get(ack["background_task"]["id"])["result"] is None
        assert not host.background_work.pending_delivery()
    finally:
        release.set()
        host.close()


def test_unavailable_background_store_keeps_ordinary_chat_usable(tmp_path):
    host = Host(tmp_path / "tasks.db", lambda *a, **k: {"speak": "foreground"})
    service = host.background_work
    try:
        host.background_work = UnavailableComponent("background_work", "database read-only")
        assert not host._background_enabled()
        assert host.background_work_snapshot()["error"] == "database read-only"
        assert host._ask_responsive_conversation("hi", source="ui_text", request_id=1) == {"speak": "foreground"}
        host.drain_background_work_delivery()
    finally:
        host.background_work = service
        host.close()


def test_compact_snapshot_keeps_large_results_out_of_status(tmp_path):
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor)
        task = subject.submit(text="request " * 1000, source="ui_text", provider="example", model="deep",
                              work=lambda context: {"speak": "x" * 50000})
        wait_until(lambda: subject.get(task["id"])["status"] == "completed")
        summary = subject.snapshot(include_results=False)["tasks"][0]
        assert "result" not in summary
        assert len(summary["result_summary"]) == 240
        assert len(summary["text"]) == 240
        assert len(subject.get(task["id"])["text"]) == 8000
        assert len(subject.get(task["id"])["result"]["speak"]) == 50000


@pytest.mark.parametrize("permissions, app, tool, eligible", [
    (["robot.motion"], "driving", "move", False),
    (["robot.display"], "screen", "show", False),
    (["state"], "quick-notes", "save", False),
    ([], "calculator", "calculate", False),
    (["robot.status", "robot.tof"], "sensor-check", "inspect", False),
    (["network"], "weather", "forecast", True),
    (["network", "robot.display"], "image-studio", "generate", True),
    (["network", "robot.display"], "image-studio", "show", False),
])
def test_only_expensive_nonphysical_app_tools_are_deferred(tmp_path, permissions, app, tool, eligible):
    host = Host(tmp_path / "tasks.db", lambda *a, **k: {})
    host.app_host = SimpleNamespace(project=lambda app_id: SimpleNamespace(manifest={"permissions": permissions}, bundled=True))
    try:
        assert host._background_app_eligible({"app": app, "tool": tool, "arguments": {}}) is eligible
    finally:
        host.close()


def test_invalid_planner_action_is_rejected_before_task_admission(tmp_path):
    from robbi.errors import ValidationError
    host = Host(tmp_path / "tasks.db", lambda *a, **k: pytest.fail("must not call provider"))
    try:
        with pytest.raises(ValidationError):
            host._defer_background_action({"background_task": {"untrusted": "field"}}, "question", "ui_text", 1, None, "fast", "small")
        assert host.background_work.snapshot()["tasks"] == []
    finally:
        host.close()


def test_expensive_tool_handoff_keeps_original_source_and_returns_before_blocked_tool(tmp_path):
    from robbi.services.conversation import PreparedConversation
    entered, release = threading.Event(), threading.Event()
    sources = []
    host = Host(tmp_path / "tasks.db", lambda *a, **k: pytest.fail("initial provider must not run twice"))
    host.app_host = SimpleNamespace(project=lambda app: SimpleNamespace(manifest={"permissions": ["network"]}, bundled=True))
    def tool(**kwargs):
        sources.append(kwargs["source"])
        entered.set()
        release.wait(2)
        return {"speak": "tool result"}
    host._maybe_run_tool_followup = tool
    try:
        started = time.monotonic()
        ack = host._defer_background_action({"app_tool": {"app": "weather", "tool": "forecast", "arguments": {}}},
            "weather", "telegram_text", 1, PreparedConversation([], "memory", None, 0), "fast", "small")
        assert time.monotonic() - started < .3
        assert entered.wait(1)
        assert sources == ["telegram_text"]
        release.set()
        wait_until(lambda: host.background_work.get(ack["background_task"]["id"])["status"] == "completed")
    finally:
        release.set()
        host.close()


def test_cancel_during_delivery_synthesis_keeps_result_and_suppresses_audio(tmp_path):
    host = Host(tmp_path / "tasks.db", lambda *a, **k: {})
    audio = BoundedExecutor(1, 1)
    entered, release = threading.Event(), threading.Event()
    played = []
    host._audio_executor = audio
    host._last_activity_ts = time.time() - 10
    host.behavior = SimpleNamespace(snapshot=lambda: SimpleNamespace(muted=False, awake=True))
    host.media = SimpleNamespace(is_audio_playing=lambda: False, is_video_playing=lambda: False,
                                 health=lambda: {"audio": {"available": True}})
    host._filming_mode = False
    host.focus_timer = SimpleNamespace(is_active=lambda: False)
    host.microphone = SimpleNamespace(voice_state=lambda: "idle")
    host.orchestrator = SimpleNamespace(snapshot=lambda: {"resources": {}})
    def speak(*args):
        entered.set()
        release.wait(2)
        if not current_intent().is_set():
            played.append(True)
            return SimpleNamespace(wait=lambda **kwargs: 0)
        return None
    host._speak_text = speak
    try:
        task = submit(host.background_work, lambda context: {"speak": "saved answer"})
        wait_until(lambda: host.background_work.get(task["id"])["status"] == "completed")
        host.drain_background_work_delivery()
        assert entered.wait(1)
        host.background_work.cancel(task["id"])
        release.set()
        wait_until(lambda: not host._background_delivery_inflight)
        assert played == []
        assert host.background_work.get(task["id"])["result"]["speak"] == "saved answer"
        assert host.background_work.get(task["id"])["delivery"] == "retained"
    finally:
        release.set()
        audio.shutdown()
        host.close()


@pytest.mark.parametrize("provider", ["local", "gemini"])
def test_real_provider_failure_becomes_failed_task_not_completed(tmp_path, monkeypatch, provider):
    from robot.llm import LLMClient
    from robot.config import settings
    client = LLMClient(SimpleNamespace(), SimpleNamespace())
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(client, "_ensure_chat_client", lambda provider: None)
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor)
        task = submit(subject, lambda context: client.ask("hello", provider=provider))
        wait_until(lambda: subject.get(task["id"])["status"] == "failed")
        assert subject.get(task["id"])["result"]["error"] == "provider_error"
        assert client.ask("again", provider=provider)["error"] == "provider_unavailable"
    client.shutdown()


def test_per_request_provider_timeout_does_not_change_foreground_default(monkeypatch):
    from robot.llm import LLMClient
    from robot.config import settings
    client = LLMClient(SimpleNamespace(), SimpleNamespace())
    seen = []
    def complete(**kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"speak":"done"}'))])
    monkeypatch.setattr(client, "_ensure_chat_client", lambda provider: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete))))
    monkeypatch.setattr(client, "_system_prompt", lambda **kwargs: "test")
    monkeypatch.setattr(client, "_trace_openai_request_async", lambda *args: None)
    try:
        assert client.ask("long", provider="local", request_timeout_s=75)["speak"] == "done"
        assert client.ask("short", provider="local")["speak"] == "done"
        assert seen[0].read == 75
        assert seen[1].read == settings.network_request_timeout_s
    finally:
        client.shutdown()


@pytest.mark.parametrize("field,value", [
    ("status", "broken"), ("phase", []), ("text", "x" * 12001),
    ("result", []), ("result", {"speak": "x" * 60001}),
    ("delivery", "sent"), ("created_at", "yesterday"), ("source", None),
])
def test_malformed_stored_task_is_rejected_at_construction(tmp_path, field, value):
    import json
    import sqlite3
    from robbi.errors import ValidationError
    path = tmp_path / "tasks.db"
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(path, scheduler=executor)
        task = submit(subject, lambda context: {"speak": "valid"})
        wait_until(lambda: subject.get(task["id"])["status"] == "completed")
        task = subject.get(task["id"])
        task[field] = value
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE tasks SET record = ? WHERE id = ?", (json.dumps(task), task["id"]))
        with pytest.raises(ValidationError):
            BackgroundTaskService(path, scheduler=executor)


@pytest.mark.parametrize("source, should_speak", [("speech_input", True), ("telegram_text", False)])
def test_status_and_cancel_shortcuts_reply_and_remember_according_to_source(tmp_path, source, should_speak):
    host = Host(tmp_path / "tasks.db", lambda *a, **k: pytest.fail("local task command must not call a provider"))
    remembered = []
    host.memory.add_assistant_message = lambda text, **kwargs: remembered.append(text)
    try:
        status = host._route_background_request("what are you working on", source, 1, False)
        task = submit(host.background_work, lambda context: {"speak": "done"})
        wait_until(lambda: host.background_work.get(task["id"])["status"] == "completed")
        cancelled = host._route_background_request("cancel task " + task["id"], source, 1, False)
        assert remembered == [status["speak"], cancelled["speak"]]
        assert len(host.actions) == (2 if should_speak else 0)
        assert host.background_work.get(task["id"])["delivery"] == "retained"
    finally:
        host.close()


def test_planner_protocol_is_only_advertised_when_background_work_is_available(tmp_path):
    host = Host(tmp_path / "tasks.db", lambda *a, **k: {})
    try:
        assert '"background_task": {}' in host._source_extra_instructions("ui_text", False, False)
        host.options = replace(host.options, enabled=False)
        assert "background_task" not in host._source_extra_instructions("ui_text", False, False)
    finally:
        host.close()


@pytest.mark.parametrize("stop", [False, True])
def test_announced_delivery_allows_voice_followup_until_cancel_or_stop(tmp_path, monkeypatch, stop):
    from robot.config import settings
    from robot.domains.voice import VoiceDomain
    host = Host(tmp_path / "tasks.db", lambda *a, **k: {})
    predicates = []
    host._conversation_lock = threading.Lock()
    host._conversation_turns = 0
    host._can_open_spoken_followup = lambda: True
    host._active_game_session = lambda: VoiceDomain._active_game_session(host)
    host.media = SimpleNamespace(is_audio_playing=lambda: False)
    host.microphone = SimpleNamespace(
        request_followup=lambda *args, **kwargs: predicates.append(kwargs["cancelled"]) or True)
    monkeypatch.setattr(settings, "conversation_followup_enabled", True)
    try:
        task = submit(host.background_work, lambda context: {"speak": "answer"})
        task_id = task["id"]
        wait_until(lambda: host.background_work.get(task_id)["status"] == "completed")
        generation, cancelled = host._action_token()
        owner = Intent("background-result:" + task_id, cancelled=lambda: not host.background_work.delivery_current(task_id))
        context = {"microphone": host.microphone, "token": 1, "generation": generation,
                   "cancelled": cancelled, "owner": owner}
        assert VoiceDomain._maybe_arm_followup_conversation(host, context=context)
        host.background_work.mark_delivered(task_id)
        assert not predicates[0]()
        if stop:
            host.background_work.cancel_all()
        else:
            host.background_work.cancel(task_id)
        assert predicates[0]()
    finally:
        host.close()


def test_known_long_request_marks_interaction_before_handoff(tmp_path):
    host = Host(tmp_path / "tasks.db", lambda *a, **k: {})
    observed = []
    host.behavior = SimpleNamespace(note_interaction=lambda: observed.append("interaction"))
    host._touch_activity = lambda: observed.append("activity")
    host.parse_debug_command = lambda text: None
    host._maybe_run_special_app_request = lambda *args, **kwargs: None
    def route(*args):
        assert observed == ["interaction", "activity"]
        return {"speak": "acknowledged"}
    host._route_background_request = route
    try:
        assert host.think_and_respond("research the moon")["speak"] == "acknowledged"
    finally:
        host.close()


def test_restarting_retains_results_without_replaying_speech(tmp_path):
    path = tmp_path / "tasks.db"
    with BoundedExecutor(1, 2) as executor:
        options = lambda: BackgroundWorkOptions(notify_on_complete=True)
        subject = BackgroundTaskService(path, scheduler=executor, options=options)
        task = submit(subject, lambda context: {"speak": "A saved answer"})
        wait_until(lambda: subject.get(task["id"])["status"] == "completed")
        assert subject.pending_delivery()
        restarted = BackgroundTaskService(path, scheduler=executor, options=options)
        assert restarted.get(task["id"])["result"]["speak"] == "A saved answer"
        assert not restarted.pending_delivery()
        assert restarted.get(task["id"])["delivery"] == "retained"


def test_background_notifications_are_opt_in_and_do_not_queue_old_speech(tmp_path):
    with BoundedExecutor(1, 2) as executor:
        subject = BackgroundTaskService(tmp_path / "tasks.db", scheduler=executor)
        task = submit(subject, lambda context: {"speak": "Still available in the dashboard"})
        wait_until(lambda: subject.get(task["id"])["status"] == "completed")
        assert not subject.pending_delivery()
        subject.options = lambda: BackgroundWorkOptions(notify_on_complete=True)
        assert not subject.pending_delivery()
        assert subject.get(task["id"])["result"]["speak"]


def test_background_context_and_result_stay_in_requesting_browser_scope(tmp_path):
    from robbi.services.tool_followups import ToolFollowups
    calls=[]
    entered,release=threading.Event(),threading.Event()
    def ask(text,**kwargs):
        calls.append(kwargs);entered.set();release.wait(2)
        return {'speak':'Verified research result'}
    host=Host(tmp_path/'tasks.db',ask)
    store=ToolFollowups()
    scope_a=store.begin('ui_text','browser-a',1)
    store.remember_turn(scope_a,'Research topic A','Understood')
    host._tool_followup_store=lambda:store
    host._tool_scope_source=lambda source,request_id:store.scope(source,request_id)
    host.memory.recent_messages=lambda:[{'role':'user','content':'PRIVATE OTHER SESSION'}]
    try:
        ack=host.planned_research('Research this topic in detail')
        assert entered.wait(1)
        scope_b=store.begin('ui_text','browser-b',2)
        store.remember_turn(scope_b,'Different topic B','Okay')
        release.set()
        wait_until(lambda:host.background_work.get(ack['background_task']['id'])['status']=='completed')
        assert any(row['content']=='Research topic A' for row in calls[0]['recent_messages'])
        assert 'PRIVATE OTHER SESSION' not in str(calls[0])
        assert any('Verified research result' in row['content'] for row in store.messages(scope_a))
        assert not any('Verified research result' in row['content'] for row in store.messages(scope_b))
    finally:
        release.set();host.close()


@pytest.mark.parametrize('text',['Search the web for current astronomy news','Look up the latest results','Please research this in detail'])
def test_explicit_lookup_routes_immediately_without_classifier_call(text):
    assert BackgroundWorkDomain._long_work_request(text)
