"""Deterministic interruption/deadline reproductions, with no device or API I/O."""
from concurrent.futures import Future, wait
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.jobs import BoundedExecutor, JobRegistry
from robbi.orchestration import Intent, intent_scope
from robbi.runtime import Runtime
from robbi.services.background_work import TaskContext
from robbi.services.conversation import ConversationOptions, ConversationService
from robot.action_executor import ActionExecutorMixin
from robot.lifecycle import LifecycleMixin
from robot.presentation_pipeline import PresentationPipelineMixin
from robot import presentation_waits


class Clock:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now


class ClockSignal:
    def __init__(self, clock):
        self.clock, self.event = clock, threading.Event()

    def is_set(self):
        return self.event.is_set()

    def wait(self, seconds):
        self.clock.now += max(0, seconds)
        return self.is_set()


class CompletingFuture(Future):
    """A result becomes available while its caller is waiting at the deadline."""
    def __init__(self, clock, elapsed=.2):
        super().__init__()
        self.clock, self.elapsed = clock, elapsed
        self.set_running_or_notify_cancel()

    def result(self, timeout=None):
        if not self.done():
            self.clock.now += self.elapsed
            self.set_result({'speak': 'late answer'})
        return super().result(timeout)


def test_cancelled_queue_entries_free_capacity_before_running_provider_returns():
    entered, release = threading.Event(), threading.Event()
    executor = BoundedExecutor(1, 1)
    called = []
    try:
        running = executor.submit(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(1)
        # Churn must not leave tombstones or require the blocked worker to drain
        # cancelled jobs before it can accept a replacement.
        for _ in range(20):
            queued = executor.submit(lambda: called.append('cancelled'))
            assert queued.cancel()
        replacement = executor.submit(lambda: called.append('replacement'))
        release.set()
        running.result(1)
        replacement.result(1)
        assert called == ['replacement']
    finally:
        release.set()
        executor.shutdown(cancel_futures=True)
    assert executor._queue.unfinished_tasks == 0


def test_foreground_result_arriving_after_deadline_is_not_accepted():
    clock, events = Clock(), []
    future = CompletingFuture(clock)
    service = ConversationService(
        memory=None, capture_image=lambda **kwargs: None, provider=None,
        scheduler=SimpleNamespace(submit=lambda callback: future),
        options=ConversationOptions, daily_state=lambda: {},
        publish_event=lambda *args: events.append(args), clock=clock.monotonic,
    )
    result = service.ask('hello', timeout_s=.1)
    assert result.get('error') == 'timeout'
    assert [kind for kind, _ in events] == ['llm.timeout']


def test_foreground_completion_racing_stop_is_cancelled_without_timeout_event():
    clock, events, cancelled = Clock(), [], threading.Event()
    future = CompletingFuture(clock)
    future.add_done_callback(lambda _: cancelled.set())
    service = ConversationService(
        memory=None, capture_image=lambda **kwargs: None, provider=None,
        scheduler=SimpleNamespace(submit=lambda callback: future),
        options=ConversationOptions, daily_state=lambda: {},
        publish_event=lambda *args: events.append(args), clock=clock.monotonic,
    )
    assert service.ask('hello', timeout_s=.1, cancelled=cancelled) == {'cancelled': True}
    assert events == []


@pytest.mark.parametrize('mode', ['foreground', 'background', 'adopted-background'])
def test_provider_completion_before_deadline_is_still_accepted(monkeypatch, mode):
    from robbi.services import background_work

    clock = Clock()
    future = CompletingFuture(clock, elapsed=.09)
    if mode == 'foreground':
        service = ConversationService(
            memory=None, capture_image=lambda **kwargs: None, provider=None,
            scheduler=SimpleNamespace(submit=lambda callback: future),
            options=ConversationOptions, daily_state=lambda: {}, clock=clock.monotonic,
            publish_event=lambda *args: None,
        )
        result = service.ask('hello', timeout_s=.1)
    else:
        monkeypatch.setattr(background_work, 'time', SimpleNamespace(monotonic=clock.monotonic))
        context = TaskContext('example', clock.now + (10 if mode == 'adopted-background' else .1), ())
        result = context.wait_future(future, deadline=clock.now + .1)
    assert result == {'speak': 'late answer'}


@pytest.mark.parametrize('adopted_deadline', [False, True])
def test_background_wait_rejects_late_results_at_both_deadlines(monkeypatch, adopted_deadline):
    from robbi.services import background_work

    clock = Clock()
    monkeypatch.setattr(background_work, 'time', SimpleNamespace(monotonic=clock.monotonic))
    context = TaskContext('example', clock.now + (10 if adopted_deadline else .1), ())
    deadline = clock.now + .1 if adopted_deadline else None
    with pytest.raises(TimeoutError):
        context.wait_future(CompletingFuture(clock), deadline=deadline)


class ActionHost(ActionExecutorMixin, LifecycleMixin):
    def __init__(self):
        self._init_lifecycle()
        self._log = logging.getLogger(__name__)
        self.events, self.drives = [], []
        self._publish_event = lambda *args: self.events.append(args)
        self._set_presentation = lambda **kwargs: None
        self._defer_focus_timer_for_action = lambda action: None
        self._collect_spoken_text = lambda action: action.get('speak', '')
        self._execute_drive_action = lambda drive: self.drives.append(drive)
        self.animations = SimpleNamespace(play=lambda *a: None, play_filming_sequence=lambda *a: None)


def test_sequence_prefetch_timeout_aborts_without_retry_or_later_steps(monkeypatch):
    from robot import action_executor, presentation_waits

    clock, host, steps, submitted = Clock(), ActionHost(), [], []
    monkeypatch.setattr(action_executor, 'time', SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(presentation_waits, 'time', SimpleNamespace(monotonic=clock.monotonic))
    host._action_cancel = ClockSignal(clock)
    host._tts_prefetch_executor = object()
    host._prepare_tts_audio = lambda text: pytest.fail('No synchronous retry')
    host._run_sequence_step = lambda *args, **kwargs: steps.append(args)

    def submit(*args):
        future = Future()
        if not submitted:
            future.set_running_or_notify_cancel()
        submitted.append(future)
        return future

    host._submit_guarded = submit
    with pytest.raises(TimeoutError):
        host._run_sequence([{'speak': 'first'}, {'speak': 'next'}, {'drive': {'direction': 'forward'}}])
    assert steps == []
    assert submitted[1].cancelled()


def test_presentation_wait_releases_promptly_when_its_owner_is_interrupted(monkeypatch):
    from robot import action_executor

    clock, host = Clock(), ActionHost()
    monkeypatch.setattr(action_executor, 'time', SimpleNamespace(monotonic=clock.monotonic))
    signal = ClockSignal(clock)
    future = Future()
    future.set_running_or_notify_cancel()
    owner = Intent('old-presentation')
    owner.cancel('Replaced by another activity')
    with intent_scope(owner):
        assert host._wait_action_future(future, signal) is None
    assert clock.now < 10.1
    assert not signal.is_set()  # Other activities keep their own cancellation.


def test_replacement_during_a_sequence_pause_prevents_the_next_motion(monkeypatch):
    from robot import action_executor

    clock, host, owner = Clock(), ActionHost(), Intent('old-sequence')
    for module in (action_executor, presentation_waits):
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock.monotonic))

    class ReplacingSignal(ClockSignal):
        def wait(self, seconds):
            owner.cancel('Replaced during the pause')
            return super().wait(seconds)

    host._action_cancel = ReplacingSignal(clock)
    with intent_scope(owner):
        host._run_sequence([{'wait_ms': 1000}, {'drive': {'direction': 'forward'}}])
    assert host.drives == []
    assert clock.now <= 10.03
    assert not host._action_cancel.is_set()


def test_stop_at_runtime_command_handoff_cannot_adopt_the_new_generation():
    entered, release = threading.Event(), threading.Event()
    controller = ActionHost()
    controller.motion = SimpleNamespace(stop=lambda: None)
    controller.slam = SimpleNamespace(stop=lambda: None)
    controller.media = SimpleNamespace(stop_audio=lambda: None)

    def respond(text, **kwargs):
        if text == 'old':
            entered.set()
            assert release.wait(3)
        return controller.execute_action({'drive': {'direction': 'forward', 'label': text}})

    controller.think_and_respond = respond
    runtime = Runtime.__new__(Runtime)
    runtime._generation, runtime._controller = 0, controller
    runtime._stop, runtime._ready = threading.Event(), threading.Event()
    runtime._ready.set()
    runtime.app_host = SimpleNamespace(cancel_all=lambda: None)
    runtime.jobs = JobRegistry()

    def finished(job):
        deadline = time.monotonic() + 2
        while runtime.jobs.get(job['id'])['state'] in {'queued', 'running'}:
            assert time.monotonic() < deadline
            threading.Event().wait(.005)

    try:
        old = runtime.submit('text', {'text': 'old'})
        assert entered.wait(1)
        assert runtime.emergency_stop()['ok']
        release.set()
        finished(old)
        assert controller.drives == []
        new = runtime.submit('text', {'text': 'new'})
        finished(new)
        assert [drive['label'] for drive in controller.drives] == ['new']
    finally:
        release.set()
        runtime.jobs.stop()
        runtime.jobs.executor.shutdown(cancel_futures=True)


@pytest.mark.parametrize('interruption', ['stop', 'replacement'])
def test_cancelled_conversation_cannot_run_mutating_shortcuts(interruption):
    from robot.domains.conversation import ConversationDomain

    class ConversationHost(ConversationDomain, ActionHost):
        pass

    host, parsed = ConversationHost(), []
    host._app_active_display_request, host._app_display_until = 42, 1000
    host.conversation_service = SimpleNamespace(
        shortcuts_allowed=lambda source: True,
        executable_action=lambda action, source: action,
    )
    host.parse_debug_command = lambda text: (parsed.append(text) or {'speak': 'Volume changed'})
    owner = Intent('old-conversation')
    if interruption == 'stop':
        host._action_local.token = host._action_token()
        host.cancel_actions()
    else:
        owner.cancel('Replaced')
    # Test the stale conversation's behavior after interruption. Global Stop
    # itself intentionally clears prior image holds; use a new hold as sentinel.
    host._app_display_until=1000
    with intent_scope(owner):
        result = host.think_and_respond('/volume 80')
    assert parsed == []
    assert result == {'cancelled': True}
    assert host._app_active_display_request == 42
    assert host._app_display_until == 1000


class StuckPlayback:
    def __init__(self, clock):
        self.clock, self.calls = clock, 0

    def wait(self, timeout):
        self.calls += 1
        # Bound even the broken reproduction: never hang the offline suite.
        assert self.calls < 1300, 'Playback wait has no effective deadline'
        self.clock.now += timeout
        return None


@pytest.mark.parametrize('path', ['speech', 'speech-overlay', 'sound-sequence'])
def test_playback_deadline_fails_and_cancels_only_its_owner(monkeypatch, path):
    from pathlib import Path
    from robot import action_executor, presentation_pipeline, presentation_waits

    class SpeechHost(PresentationPipelineMixin, ActionHost):
        pass

    host, clock = SpeechHost(), Clock()
    for module in (action_executor, presentation_pipeline, presentation_waits):
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock.monotonic, perf_counter=clock.monotonic))
    host._action_cancel = ClockSignal(clock)
    handle = StuckPlayback(clock)
    host.media = SimpleNamespace(speak_wav_bytes=lambda *args, **kwargs: handle,
                                 play_sound=lambda *args, **kwargs: handle)
    host._start_speaking_face_overlay = lambda *args, **kwargs: None
    host._update_pipeline_metrics = lambda *args, **kwargs: None
    host._resolve_asset = lambda *args, **kwargs: Path('sound.wav')
    owner, other = Intent('stuck-playback'), Intent('other-activity')
    with intent_scope(owner), pytest.raises(TimeoutError):
        if path == 'sound-sequence':
            host._run_sequence([{'sound': 'beep', 'wait_for_audio': True},
                                {'drive': {'direction': 'forward'}}])
        else:
            play = host._play_prepared_tts if path == 'speech' else host._play_prepared_tts_with_overlay
            play({'wav': b'fake wav'}, wait=True)
    assert host.drives == []
    assert owner.is_set()
    assert not other.is_set() and not host._action_cancel.is_set()
    assert 129.9 <= clock.now <= 130.1


@pytest.mark.parametrize('completion', ['on-time', 'late', 'interrupted'])
def test_playback_completion_honors_deadline_and_interruption(monkeypatch, completion):
    clock, owner, signal = Clock(), Intent('playback'), threading.Event()
    monkeypatch.setattr(presentation_waits, 'time', SimpleNamespace(monotonic=clock.monotonic))

    def complete(timeout):
        clock.now += .09 if completion == 'on-time' else .2
        if completion == 'interrupted':
            owner.cancel('New activity')
        return 0

    handle = SimpleNamespace(wait=complete)
    with intent_scope(owner):
        if completion == 'late':
            with pytest.raises(TimeoutError):
                presentation_waits.wait_for_playback(handle, signal, timeout_s=.1)
            assert owner.is_set()
        else:
            result = presentation_waits.wait_for_playback(handle, signal, timeout_s=.1)
            assert result == (0 if completion == 'on-time' else None)
            assert owner.is_set() == (completion == 'interrupted')
    assert not signal.is_set()


def test_cancel_callback_can_submit_replacement_without_waiting_for_busy_worker():
    executor = BoundedExecutor(1, 2)
    entered, release = threading.Event(), threading.Event()
    called, replacement = [], []
    try:
        executor.submit(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(1)
        queued = executor.submit(lambda: pytest.fail('Cancelled callback ran'))
        executor.submit(lambda: called.append('earlier'))
        queued.add_done_callback(lambda _: replacement.append(executor.submit(lambda: called.append('replacement'))))
        assert queued.cancel()
        assert len(replacement) == 1
        assert wait([queued], timeout=.01).done == {queued}
        release.set()
        replacement[0].result(1)
        assert called == ['earlier', 'replacement']
    finally:
        release.set()
        executor.shutdown(cancel_futures=True)
    assert executor._queue.unfinished_tasks == 0


def test_shutdown_notifies_waiters_before_blocked_worker_returns():
    executor = BoundedExecutor(1, 1)
    entered, release = threading.Event(), threading.Event()
    try:
        running = executor.submit(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(1)
        queued = executor.submit(lambda: pytest.fail('Cancelled callback ran'))
        executor.shutdown(wait=False, cancel_futures=True)
        assert wait([queued], timeout=.01).done == {queued}
        assert not running.done()
    finally:
        release.set()
        executor.shutdown()


@pytest.mark.parametrize("failing_service", ["apps", "briefing", "filming"])
def test_app_cancellation_failure_cannot_prevent_emergency_hardware_stops(failing_service):
    stopped = []
    runtime = Runtime.__new__(Runtime)
    runtime._generation = 0
    runtime._controller = SimpleNamespace(
        cancel_actions=lambda: stopped.append('actions'),
        motion=SimpleNamespace(stop=lambda: stopped.append('motion')),
        slam=SimpleNamespace(stop=lambda: stopped.append('mapping')),
        media=SimpleNamespace(stop_audio=lambda: stopped.append('audio')),
    )

    def broken_cancel():
        raise RuntimeError('App cancellation failed')

    runtime.app_host = SimpleNamespace(cancel_all=broken_cancel if failing_service == "apps" else lambda: None)
    if failing_service == "briefing":
        runtime.morning_briefing = SimpleNamespace(cancel=broken_cancel)
    if failing_service == "filming":
        runtime.filming = SimpleNamespace(stop=broken_cancel)
    result = runtime.emergency_stop()
    assert stopped == ['actions', 'motion', 'mapping', 'audio']
    assert not result['ok'] and result['stopped']
    assert any('App cancellation failed' in issue for issue in result['issues'])
    if failing_service == "filming":
        assert result['issues'] == ['filming: App cancellation failed']


def test_emergency_stop_preserves_filming_cancellation_and_continues_to_hardware():
    stopped = []
    runtime = Runtime.__new__(Runtime)
    runtime._generation = 4
    runtime.filming = SimpleNamespace(stop=lambda: stopped.append('filming'))
    runtime.app_host = SimpleNamespace(cancel_all=lambda: stopped.append('apps'))
    runtime._controller = SimpleNamespace(
        cancel_actions=lambda: stopped.append('actions'),
        motion=SimpleNamespace(stop=lambda: stopped.append('motion')),
        slam=SimpleNamespace(stop=lambda: stopped.append('mapping')),
        media=SimpleNamespace(stop_audio=lambda: stopped.append('audio')),
    )
    result = runtime.emergency_stop()
    assert runtime._generation == 5
    assert stopped == ['filming', 'apps', 'actions', 'motion', 'mapping', 'audio']
    assert result == {'ok': True, 'stopped': True, 'issues': []}


def test_cancelled_command_is_reported_as_cancelled_in_job_status():
    jobs = JobRegistry()
    try:
        job = jobs.submit('text', lambda: {'cancelled': True})
        # Join the bounded worker rather than sleeping until a status changes.
        jobs.executor.shutdown()
        result = jobs.get(job['id'])
        assert result['state'] == 'cancelled'
        assert result['result'] == {'cancelled': True}
        assert result['error']
    finally:
        jobs.stop()
