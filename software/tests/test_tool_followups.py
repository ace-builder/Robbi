"""Actual receipts, source isolation and deliberate replay across any catalog app."""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from robbi.actions import validate_action
from robbi.errors import ValidationError
from robbi.services.conversation import ConversationOptions, ConversationService
from robbi.services.tool_followups import ToolFollowups, FollowupUnavailable, scope_source, shortcut, explicit_operation
from robot.domains.conversation import ConversationDomain
from robot.domains.tools import ToolsDomain
from robot.lifecycle import LifecycleMixin


class FakeApps:
    def __init__(self):
        self.calls, self.undos, self.jobs = [], [], []
        self.active, self.granted = True, True
        self.next_result = {"ok": True, "text": "Completed.", "data": {"results": [{"id": "first"}, {"id": "second"}]}}

    def call(self, app, tool, arguments, *, source, cancellation, deadline, include_invocation=False):
        self.calls.append((app, tool, arguments, source))
        result = dict(self.next_result)
        metadata = {"id": "job-" + str(len(self.jobs) + 1), "app": app, "tool": tool,
                    "source": source, "state": "done", "kind": "tool", "completed_at": time.time(),
                    "undo": {"available": True}}
        self.jobs.append({**metadata, "arguments": arguments, "result": result})
        return {**result, "invocation": metadata} if include_invocation else result

    def history(self, *, source):
        return [item for item in reversed(self.jobs) if item["source"] == source]

    def invocation(self, job_id, source=None):
        return next(item for item in self.jobs if item["id"] == job_id and (source is None or item["source"] == source))

    def undo(self, job_id, **kwargs):
        self.undos.append((job_id, kwargs))
        return {"ok": True, "text": "Undone."}

    def enabled(self, app):
        return self.active

    def permission_snapshot(self, app):
        return {"permissions": [{"id": "network", "status": "granted" if self.granted else "denied"}]}


class Harness(ToolsDomain, LifecycleMixin):
    def __init__(self):
        self._init_lifecycle()
        self.app_host = FakeApps()
        self.conversation_service = SimpleNamespace(shortcuts_allowed=lambda source: True)
        self._log = logging.getLogger(__name__)
        self.events = []
        self._publish_event = lambda *args: self.events.append(args)
        self._collect_spoken_text = lambda action: str(action.get("speak", ""))
        self._has_presentational_content = lambda action: bool(action.get("speak") or action.get("face"))
        self.begin()

    def begin(self, source="ui_text", conversation_id="browser-a", request_id=1):
        self.source, self.request_id = source, request_id
        self._register_request(request_id, self._action_token()[0])
        return self._begin_tool_followup_turn(source, conversation_id, request_id)

    def call(self, app="test-app", tool="execute", arguments=None):
        return self._execute_app_tool({"app": app, "tool": tool, "arguments": arguments or {}},
                                      source=self.source, request_id=self.request_id)

    def resolve(self, operation, text=None, **spec):
        return self._resolve_tool_followup({"operation": operation, **spec}, user_text=text or operation,
                                           source=self.source, request_id=self.request_id)

    def receipts(self):
        return self._tool_followup_store().calls(self._tool_scope_source(self.source, self.request_id))


@pytest.mark.parametrize(("text", "operation"), [("Do that again!", "repeat"), ("noch einmal", "repeat"),
    ("Mach das rückgängig.", "undo"), ("undo the last change", "undo")])
def test_language_shortcuts(text, operation):
    assert shortcut(text) == operation
    assert explicit_operation(text, operation)


@pytest.mark.parametrize("text", ["don't repeat that", "never repeat that", "repeat that but tomorrow", "what does repeat mean?", "do not undo that"])
def test_negation_questions_and_modified_requests_do_not_authorize_replay(text):
    assert shortcut(text) is None
    assert not explicit_operation(text, "repeat")
    assert not explicit_operation(text, "undo")


def test_store_expires_each_call_and_message_even_when_scope_is_active():
    now = [10.0]
    store = ToolFollowups(clock=lambda: now[0])
    store.record("a", "web_search", "first", {"ok": True})
    store.remember_turn("a", "private old question", "private old answer")
    now[0] += 599
    store.remember_turn("a", "new question", "new answer")
    now[0] += 1
    assert store.calls("a") == []
    assert [item["content"] for item in store.messages("a")] == ["new question", "new answer"]
    with pytest.raises(FollowupUnavailable, match="no recent"):
        store.select("a")


def test_store_bounds_calls_scopes_arguments_and_result_selections():
    store = ToolFollowups()
    for index in range(8):
        store.record("a", "app_tool", {"query": index}, {"ok": True, "data": {"items": [{"id": n} for n in range(100)]}})
    assert len(store.calls("a")) == 4
    assert store.calls("a")[-1]["result"]["data"]["items"][1] == {"id": 1}
    huge = store.record("a", "custom", {"body": "x" * 20000}, {"data": ["a" * 1000] * 1000, "access_token": "secret"})
    assert not huge["arguments_complete"]
    assert "secret" not in store.prompt("a")
    assert len(store.prompt("a")) < 16000
    for index in range(50):
        store.record(str(index), "custom", {}, {"ok": True})
    assert len(store._scopes) == store.MAX_SCOPES
    assert store.calls("a") == []


def test_failures_multi_tool_turns_and_already_undone_do_not_select_an_older_write():
    store = ToolFollowups()
    first = store.record("a", "write", {}, {"ok": True}, request_id=1)
    store.record("a", "read", {}, {"ok": False}, request_id=2)
    with pytest.raises(FollowupUnavailable, match="not complete"):
        store.select("a")
    assert store.select("a", first["id"])["tool"] == "write"
    store.record("a", "one", {}, {"ok": True}, request_id=3)
    last = store.record("a", "two", {}, {"ok": True}, request_id=3)
    with pytest.raises(FollowupUnavailable, match="several tools"):
        store.select("a")
    store.mark_undone("a", last["id"])
    with pytest.raises(FollowupUnavailable, match="already undone"):
        store.select("a", last["id"])


@pytest.mark.parametrize("app", ["google-calendar", "gmail", "calculator", "expense-tracker", "brand-new-plugin"])
def test_generic_repeat_uses_actual_arguments_and_exact_scoped_invocation(app):
    host = Harness()
    arguments = {"text": "Original", "date": "2026-10-23", "selected_id": "second"}
    host.call(app, "arbitrary-operation", arguments)
    arguments["text"] = "changed externally"
    host.begin(request_id=2)
    repeated = host.resolve("repeat", "do that again")
    assert repeated["app_result"]["result"]["ok"]
    assert len(host.app_host.calls) == 2
    assert host.app_host.calls[-1][2]["text"] == "Original"
    assert host.app_host.calls[-1][3] == scope_source("ui_text", "browser-a")
    assert host.receipts()[-1]["undo_ref"] == "job-2"


def test_undo_uses_exact_job_and_source_and_never_repeats_the_original_write():
    host = Harness()
    host.call("expense-tracker", "add", {"amount": "9.99"})
    host.begin(request_id=2)
    assert host.resolve("undo")["speak"] == "Undone."
    job, options = host.app_host.undos[0]
    assert job == "job-1" and options["source"] == scope_source("ui_text", "browser-a")
    assert options["cancellation"]() is False and options["deadline"] > time.monotonic()
    assert len(host.app_host.calls) == 1
    assert "already undone" in host.resolve("undo")["speak"]


def test_context_is_private_to_telegram_chat_and_browser_session():
    host = Harness()
    host.begin("telegram_text", "chat-a", 2)
    host.app_host.next_result = {"ok": True, "text": "PRIVATE-EMAIL"}
    host.call("gmail", "read", {"message_id": "private-id"})
    host._remember_followup_turn("read my mail", "telegram_text", {"speak": "PRIVATE-EMAIL"}, 2)
    for source, session in (("telegram_text", "chat-b"), ("ui_text", "chat-a"), ("ui_text", "browser-a")):
        scope = host.begin(source, session, 3)
        assert "PRIVATE-EMAIL" not in host._tool_followup_prompt(source, 3)
        assert host._tool_followup_store().messages(scope) == []
        assert "no recent" in host.resolve("repeat")["speak"]
    host.begin("telegram_text", "chat-a", 4)
    assert "PRIVATE-EMAIL" in host._tool_followup_prompt("telegram_text", 4)
    assert host._tool_source_for_request(4) == "telegram_text"
    assert host._tool_source_for_request(999) is None


@pytest.mark.parametrize("revocation", ["permissions", "disabled", "cancel"])
def test_revocation_removes_receipts_and_spoken_results_without_reimporting_history(revocation):
    host = Harness()
    host.call()
    host._remember_followup_turn("question", host.source, {"speak": "PRIVATE"}, 1)
    if revocation == "permissions":
        host.app_host.granted = False
    elif revocation == "disabled":
        host.app_host.active = False
    else:
        host.cancel_actions()
    scope = host.begin(request_id=2)
    assert "PRIVATE" not in host._tool_followup_prompt(host.source, 2)
    assert host._tool_followup_store().messages(scope) == []
    assert "no recent" in host.resolve("undo")["speak"]
    host.app_host.granted = host.app_host.active = True
    host.begin(request_id=3)
    assert "no recent" in host.resolve("undo")["speak"]


def test_a_late_cancelled_result_cannot_overwrite_a_newer_turn():
    host = Harness()
    host.call()
    host.cancel_actions()
    host.begin(request_id=2)
    host.call("calculator", "calculate", {"expression": "2+2"})
    host._remember_tool_call("app_tool", {"app": "gmail"}, {"ok": True, "text": "LATE PRIVATE"}, source="ui_text", request_id=1)
    assert host.receipts()[-1]["arguments"]["app"] == "calculator"
    assert "LATE PRIVATE" not in host._tool_followup_prompt("ui_text", 2)


def test_dashboard_completion_is_imported_once_and_older_jobs_do_not_override_core_calls():
    host = Harness()
    scope = scope_source("ui_text", "browser-a")
    host.app_host.call("bookmarks", "search", {"query": "travel"}, source=scope, cancellation=lambda: False, deadline=time.monotonic() + 20)
    host.begin(request_id=2)
    assert host.receipts()[-1]["arguments"]["arguments"] == {"query": "travel"}
    host.begin(request_id=3)
    assert len(host.receipts()) == 1
    host._remember_tool_call("robot_action", {"drive": "stop"}, {"ok": True}, source="ui_text", request_id=3)
    host.begin(request_id=4)
    assert host.receipts()[-1]["tool"] == "robot_action"
    assert "does not provide undo" in host.resolve("undo")["speak"]
    assert host.app_host.undos == []


def test_model_alone_cannot_replay_and_denied_source_cannot_undo():
    host = Harness()
    host.call()
    host.begin(request_id=2)
    assert "explicitly ask" in host.resolve("repeat", "What did you find?")["speak"]
    host.conversation_service.shortcuts_allowed = lambda source: False
    assert "disabled" in host.resolve("undo")["speak"]
    assert len(host.app_host.calls) == 1 and host.app_host.undos == []


def test_failed_call_is_the_latest_barrier_even_with_invalid_arguments():
    host = Harness()
    host.call()
    host.begin(request_id=2)
    host._execute_app_tool(None, source=host.source, request_id=2)
    host.begin(request_id=3)
    assert "not complete" in host.resolve("undo")["speak"]
    assert host.app_host.undos == []


def test_synthesis_cannot_add_new_writes_or_replay_tools():
    action = {"speak": "Done", "tool_followup": {"operation": "repeat"}, "create_note": "again",
              "memory_ops": {"upsert": ["secret"]}, "sequence": [{"add_todo": "again", "drive": "forward", "speak": "Hello"}]}
    assert ToolsDomain._strip_followup_tools(action, block_apps=True) == {"speak": "Done", "sequence": [{"speak": "Hello"}]}


def test_core_write_captures_its_exact_journal_receipt_and_repeats_only_once():
    host = Harness()
    recorded, scopes = [], []

    @contextmanager
    def change_scope(source):
        scopes.append(source)
        captured = []
        host.captured = captured
        yield captured

    def create(spec, source):
        recorded.append(spec)
        host.captured.append({"id": "actual-" + str(len(recorded))})
        return {"id": len(recorded), "title": spec["title"]}

    host.productivity_service = SimpleNamespace(change_scope=change_scope, last_change=lambda source: {"id": "unrelated-global"})
    host._tool_create_note = create
    host._apply_immediate_runtime_tools({"create_note": {"title": "Note"}}, host.source, 1)
    assert host.receipts()[-1]["undo_ref"] == "actual-1"
    host.begin(request_id=2)
    action = host.resolve("repeat")
    host._apply_immediate_runtime_tools(action, host.source, 2)
    assert len(recorded) == 2 and "create_note" not in action
    assert scopes == [scope_source("ui_text", "browser-a")] * 2
    assert host.receipts()[-1]["undo_ref"] == "actual-2"


def test_followup_action_validation_rejects_mixed_or_nested_effects():
    validate_action({"tool_followup": {"operation": "repeat", "call_id": "abc-123"}})
    for action in ({"tool_followup": {"operation": "redo"}},
                   {"tool_followup": {"operation": "repeat"}, "create_note": "bad"},
                   {"sequence": [{"tool_followup": {"operation": "undo"}}]}):
        with pytest.raises(ValidationError):
            validate_action(action)


def make_conversation():
    class Conversation(ConversationDomain, Harness):
        pass

    host = Conversation()
    host._soul_text = "robot"
    host.apps = SimpleNamespace(prompt_catalog=lambda: "custom-tool schemas")
    host.behavior = SimpleNamespace(note_interaction=lambda: None, snapshot=lambda: SimpleNamespace(current_face="neutral"))
    host.assets = SimpleNamespace(get=lambda category, name, **kwargs: Path(name + ".png"))
    host.parse_debug_command = lambda text: None
    host._maybe_run_special_app_request = lambda *args, **kwargs: None
    host._touch_activity = lambda: None
    host._pick_thinking_sound = lambda: None
    host._looks_like_daily_request = lambda text: False
    host._source_extra_instructions = lambda **kwargs: ""
    host._tools_prompt_context = lambda query="": "custom-tool schemas"
    host._store_pipeline_metrics = host._update_pipeline_metrics = lambda *args, **kwargs: None
    host._sanitize_llm_motion = lambda action, **kwargs: action
    host._parse_take_picture_spec = lambda action: None
    host._should_force_take_picture_followup = lambda *args: False
    host._stop_transient_thinking_audio = lambda: None
    host.execute_action = lambda action, **kwargs: action
    host._remember_interaction = host._queue_memory_extraction = lambda *args: None
    host._set_presentation = lambda **kwargs: None
    host.memory = SimpleNamespace(add_user_message=lambda *a, **k: None, add_assistant_message=lambda *a, **k: None)
    host.conversation_service = ConversationService(
        memory=SimpleNamespace(recent_messages=lambda: [{"role": "assistant", "content": "OTHER SESSION PRIVATE"}],
                               prepare_prompt_context=lambda *a, **k: {"prompt_text": "shared approved memories"}),
        capture_image=lambda **k: None, provider=None, scheduler=None, options=lambda: ConversationOptions(telegram_actions=True),
        daily_state=lambda: {}, publish_event=lambda *a: None)
    return host


@pytest.mark.parametrize("phrase", ["make that tomorrow", "use the second result", "save this"])
def test_generic_reference_reaches_model_with_actual_arguments_and_results(phrase):
    host = make_conversation()
    prompts = []
    responses = iter((
        {"app_tool": {"app": "brand-new-plugin", "tool": "find", "arguments": {"date": "2026-10-23", "query": "Bücher"}}},
        {"speak": "Two results."},
        {"app_tool": {"app": "brand-new-plugin", "tool": "save", "arguments": {"date": "2026-10-24", "selected_id": "second", "query": "Bücher"}}},
        {"speak": "Saved second result."},
    ))
    host._ask_llm_with_deadline = lambda text, **kwargs: prompts.append((text, kwargs)) or next(responses)
    host.think_and_respond("find books", conversation_id="browser-a")
    answer = host.think_and_respond(phrase, conversation_id="browser-a")
    second_prompt = prompts[2][1]
    assert '"id": "second"' in second_prompt["memory_text"]
    assert '"query": "Bücher"' in second_prompt["memory_text"]
    assert "one-based" in second_prompt["memory_text"]
    assert "OTHER SESSION PRIVATE" not in json.dumps(second_prompt)
    assert len(host.app_host.calls) == 2 and host.app_host.calls[-1][2]["selected_id"] == "second"
    assert answer["speak"] == "Saved second result."


def test_deterministic_shortcuts_work_without_model_or_global_recent_messages():
    host = make_conversation()
    host.call()
    host._ask_llm_with_deadline = lambda *a, **k: pytest.fail("explicit repeat/undo should not need the model")
    answer = host.think_and_respond("nochmal", conversation_id="browser-a")
    assert answer["app_result"]["result"]["ok"] and len(host.app_host.calls) == 2
    assert host.think_and_respond("undo", conversation_id="browser-b")["speak"].startswith("Which action")


def test_scoped_prompt_preparation_does_not_leak_global_daily_messages_or_events():
    host = make_conversation()
    host.conversation_service._daily_state = lambda: {"recent_messages": [{"content": "PRIVATE"}],
        "recent_failures": [{"text": "PRIVATE"}], "pending_memories": [{"content": "PRIVATE"}], "open_todos": []}
    prepared = host.conversation_service.prepare("today", source="ui_text", include_photo=False,
                                                include_daily_state=True, recent_messages=[])
    assert prepared.recent_messages == []
    assert "PRIVATE" not in prepared.memory_text


def test_running_dashboard_write_blocks_older_undo_until_its_actual_result_arrives():
    host = Harness()
    host.call()
    pending = {"id": "dashboard-pending", "app": "bookmarks", "tool": "save", "source": scope_source("ui_text", "browser-a"),
               "state": "running", "kind": "tool", "created_at": time.time(), "completed_at": None,
               "arguments": {"title": "New"}, "result": None}
    host.app_host.jobs.append(pending)
    host.begin(request_id=2)
    assert "no recent" in host.resolve("undo")["speak"]
    assert host.app_host.undos == []
    time.sleep(.01)
    pending.update(state="done", completed_at=time.time(), result={"ok": True, "text": "Saved new bookmark"})
    host.begin(request_id=3)
    host.resolve("undo")
    assert host.app_host.undos[0][0] == "dashboard-pending"


def test_real_app_host_receipt_can_be_undone_from_same_conversation(tmp_path):
    from robbi.app_runtime.host import AppHost

    subject = Harness()
    host = AppHost(Path(__file__).resolve().parents[1], tmp_path, SimpleNamespace(capture_token=lambda: None), permissions_enabled=False)
    host.scan()
    subject.app_host = host
    try:
        created = subject.call("bookmarks", "save", {"title": "Guide", "url": "https://example.com/guide", "tags": ["reading"]})
        assert created["ok"], created
        assert created["invocation"]["undo"]["available"]
        subject.begin(request_id=2)
        assert "Undid" in subject.resolve("undo")["speak"]
        found = host.call("bookmarks", "search", {"query": "Guide"}, source="test-inspection")
        assert found["data"]["bookmarks"] == []
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def test_real_productivity_write_and_undo_use_same_exact_scope(tmp_path):
    from robot.domains.productivity import ProductivityDomain
    from robot.productivity import ProductivityStore
    from robbi.services.productivity import ProductivityService

    class Subject(ProductivityDomain, Harness):
        pass

    subject = Subject()
    repository = ProductivityStore(tmp_path / "daily.db", tmp_path / "missing.json")
    subject.productivity_service = ProductivityService(repository, search=None, scheduler=None, publish_event=lambda *a: None)
    subject._apply_immediate_runtime_tools({"create_note": {"title": "Trip", "raw_text": "Book train"}}, subject.source, 1)
    assert len(repository.list_notes()) == 1
    receipt = subject.receipts()[-1]
    assert receipt["undo_ref"] == repository.change_history(scope_source("ui_text", "browser-a"))[0]["id"]
    subject.begin(request_id=2)
    assert subject.resolve("undo")["speak"]
    assert repository.list_notes() == []


def test_actual_memory_change_blocks_undo_of_previous_app_call():
    subject = Harness()
    subject.call()
    subject.begin(request_id=2)
    empty = {"added": [], "updated": [], "deleted": [], "search_results": []}
    subject.memory = SimpleNamespace(
        add_user_message=lambda *a, **k: None, add_assistant_message=lambda *a, **k: None,
        remember=lambda payload, **k: {**empty, "added": ["memory-id"]} if payload else dict(empty),
        apply_memory_ops=lambda *a, **k: dict(empty),
    )
    subject._maybe_extract_todo_from_text = lambda *a, **k: None
    subject._remember_interaction("remember this", subject.source, {"remember": "Project is Robbi", "speak": "Saved."})
    subject.begin(request_id=3)
    assert subject.receipts()[-1]["tool"] == "remember"
    assert "does not provide undo" in subject.resolve("undo")["speak"]
    assert subject.app_host.undos == []


def test_executor_motion_receipt_blocks_older_app_undo_and_repeats_once():
    from robot.action_executor import ActionExecutorMixin

    class Subject(ActionExecutorMixin, Harness):
        pass

    subject = Subject()
    subject.animations = SimpleNamespace(play=lambda *a: None, play_filming_sequence=lambda *a: None)
    subject._defer_focus_timer_for_action = lambda *a: None
    motions = []
    subject._execute_drive_action = lambda spec: motions.append(spec)
    subject.call()
    subject.begin(request_id=2)
    subject.execute_action({"drive": {"direction": "left", "duration": .2}}, request_id=2)
    assert subject.receipts()[-1]["tool"] == "robot_action"
    subject.begin(request_id=3)
    assert "does not provide undo" in subject.resolve("undo")["speak"]
    subject.begin(request_id=4)
    assert "Repeated" in subject.resolve("repeat")["speak"]
    assert len(motions) == 2 and subject.app_host.undos == []


def test_executor_exception_blocks_replay_of_the_uncertain_action():
    from robot.action_executor import ActionExecutorMixin

    class Subject(ActionExecutorMixin, Harness):
        pass

    subject = Subject()
    subject._defer_focus_timer_for_action = lambda *a: None
    subject.animations = SimpleNamespace(play=lambda *a: None, play_filming_sequence=lambda *a: None)
    subject._execute_drive_action = lambda spec: (_ for _ in ()).throw(RuntimeError("drive unavailable"))
    with pytest.raises(RuntimeError, match="drive unavailable"):
        subject.execute_action({"drive": {"direction": "left", "duration": .2}}, request_id=1)
    subject.begin(request_id=2)
    assert "not complete" in subject.resolve("repeat")["speak"]


def test_legacy_shortcut_without_exact_app_arguments_cannot_claim_a_replay():
    from robot.action_executor import ActionExecutorMixin

    class Subject(ActionExecutorMixin, Harness):
        pass

    subject = Subject()
    subject._remember_shortcut_receipt("legacy_app", {"request": "start a timer"}, {"speak": "Started"}, source=subject.source, request_id=1)
    subject.begin(request_id=2)
    subject.execute_action = lambda *a, **k: pytest.fail("no exact app arguments were recorded")
    assert "full request" in subject.resolve("repeat")["speak"]
