"""Regression coverage for saved image reuse and model-led task delivery."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from robbi.services.background_work import TaskContext
from robbi.services.conversation import PreparedConversation
from robbi.services.conversation_feed import ConversationFeed
from robbi.services.tool_followups import shortcut
from test_background_work import Host, wait_until
from test_tool_followups import Harness


IMAGE = 'a' * 32


@pytest.mark.parametrize('text', ['again', 'display again', 'display it again', 'show it again',
                                'display the generated image of Switzerland again'])
def test_saved_image_repeat_uses_show_never_paid_generation(text):
    robot = Harness()
    robot.app_host.next_result = {'ok': True, 'text': 'Saved', 'artifacts': [{'id': IMAGE}]}
    robot.call('image-studio', 'generate', {'prompt': 'Switzerland', 'hold_s': 24})
    robot.app_host.next_result = {'ok': True, 'text': 'Displayed', 'data': {'displayed': True, 'display_hold_s': 24}}
    for request in range(2, 5):
        robot.begin(request_id=request)
        result = robot.resolve(shortcut(text), text)
        assert result['app_result']['result']['ok']
        assert robot.app_host.calls[-1][1:3] == ('show', {'artifact_id': IMAGE, 'hold_s': 24})
        assert robot._app_preserves_display(request)
    assert [call[1] for call in robot.app_host.calls].count('generate') == 1


def test_display_again_cannot_replay_an_unrelated_write():
    robot = Harness()
    robot.call('notes', 'save', {'text': 'hello'})
    count = len(robot.app_host.calls)
    assert 'no recent generated image' in robot.resolve('repeat', 'display again')['speak']
    assert len(robot.app_host.calls) == count


def test_saved_image_survives_worker_deadline_but_not_permission_revocation_or_stop():
    robot = Harness()
    robot.app_host.next_result = {'ok': True, 'artifacts': [{'id': IMAGE}]}
    robot.call('image-studio', 'generate', {'prompt': 'Switzerland'})
    call = robot.receipts()[-1]
    robot.app_host.invocation = lambda *_: {'context_valid': False, 'artifact_context_valid': True}
    assert robot._tool_receipt_allowed(call)
    robot.app_host.granted = False
    assert not robot._tool_receipt_allowed(call)
    robot.app_host.granted = True
    robot.app_host.invocation = lambda *_: {'context_valid': False, 'artifact_context_valid': False}
    assert not robot._tool_receipt_allowed(call)


def test_image_app_default_hold_and_explicit_preview():
    path = Path(__file__).parents[1] / 'apps/image-studio/main.py'
    spec = importlib.util.spec_from_file_location('image_studio_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    context = SimpleNamespace(request_permissions=lambda *args: None, config={}, cancelled=lambda: False,
        deadline=100, save_image=lambda *args: {'id': IMAGE},
        services=SimpleNamespace(images=SimpleNamespace(generate=lambda *args, **kw:
            SimpleNamespace(data=b'image', provider='fake', model='fake'))),
        robot=SimpleNamespace(show_image=lambda artifact, **kw:
            calls.append((artifact, kw)) or {'displayed': True, 'display_hold_s': kw['hold_s']}))
    assert module.run(context, 'generate', {'prompt': 'Switzerland'})['data']['displayed']
    assert calls == [(IMAGE, {'hold_s': 10})]
    module.run(context, 'show', {'artifact_id': IMAGE, 'hold_s': 35})
    assert calls[-1][1]['hold_s'] == 35
    module.run(context, 'generate', {'prompt': 'Switzerland', 'show_on_screen': False})
    assert len(calls) == 2


@pytest.mark.parametrize('preview', [False, True])
def test_background_generation_defers_display_and_carries_selected_hold(tmp_path, preview):
    robot = Host(tmp_path/'tasks.db', lambda *a, **k: {})
    robot.app_host = SimpleNamespace(project=lambda app: SimpleNamespace(bundled=True,
        manifest={'permissions': ['network', 'robot.display']}))
    observed = []
    def tool(**kwargs):
        observed.append(kwargs)
        return {'speak': 'Here is Switzerland.', 'app_result': {'app': 'image-studio', 'tool': 'generate',
            'result': {'ok': True, 'artifacts': [{'id': IMAGE}]}}}
    robot._maybe_run_tool_followup = tool
    try:
        action = {'app_tool': {'app': 'image-studio', 'tool': 'generate', 'arguments': {
            'prompt': 'Switzerland', 'show_on_screen': not preview, 'hold_s': 25}}}
        context = TaskContext('test', time.monotonic()+5, [])
        result = robot._finish_background_action(action, 'draw', 'ui_text', 1, {}, context)
        assert observed[0]['action']['app_tool']['arguments']['show_on_screen'] is False
        if preview:
            assert 'display_result' not in result
        else:
            assert result['display_result'] == {'artifact_id': IMAGE, 'hold_s': 25}
            assert observed[0]['deferred_display'] == {'hold_s': 25}
    finally:
        robot.close()


def test_model_acknowledgement_is_used_without_replanning(tmp_path):
    robot = Host(tmp_path/'tasks.db', lambda *a, **k: pytest.fail('Do not repeat planning'))
    release = threading.Event()
    def pending(**kwargs):
        assert release.wait(2)
        return kwargs['action']
    robot._maybe_run_tool_followup = pending
    try:
        assert robot._route_background_request('Create an image', 'ui_text', 1, False) is None
        result = robot._defer_background_action({'speak': 'Let me look up those headlines.', 'web_search': {'query': 'news'}},
            'news', 'ui_text', 1, PreparedConversation([], '', None, 0), 'fast', 'small')
        assert result['speak'] == 'Let me look up those headlines.'
        assert robot.actions == [{'speak': result['speak']}]
        release.set()
        wait_until(lambda: robot.background_work.get(result['background_task']['id'])['status'] == 'completed')
    finally:
        release.set()
        robot.close()


def test_progress_once_tool_free_and_no_late_update(tmp_path):
    entered, finish, progress_release = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def ask(text, **kwargs):
        calls.append(kwargs)
        entered.set()
        progress_release.wait(2)
        return {'speak': 'I am still drawing those mountains.', 'drive': {'direction': 'forward'}}
    robot = Host(tmp_path/'tasks.db', ask)
    robot._llm_executor = robot.live
    try:
        task = robot.background_work.submit(text='draw mountains', source='ui_text', provider='fast', model='small',
            work=lambda ctx: finish.wait(2) and {'speak': 'Done'}, reply_requested=True)
        robot._track_background_turn(task, 'ui_text', 1, overdue=True)
        robot._queue_background_progress()
        assert entered.wait(1)
        robot._queue_background_progress()
        finish.set()
        wait_until(lambda: robot.background_work.get(task['id'])['status'] == 'completed')
        progress_release.set()
        robot.live.shutdown()
        assert len(calls) == 1
        assert calls[0]['tools_text'] == '' and calls[0]['app_catalog_text'] == ''
        assert not robot.background_work.get(task['id']).get('progress')
        assert robot.actions == []
    finally:
        finish.set(); progress_release.set(); robot.close()


def test_progress_visible_in_correct_browser_and_final_result_replaces_it(tmp_path):
    release = threading.Event()
    robot = Host(tmp_path/'tasks.db', lambda *a, **kw: {})
    try:
        task = robot.background_work.submit(text='image', source='ui_text', provider='', model='',
            work=lambda ctx: release.wait(2) and {'speak': 'Here is the image.'}, reply_requested=True)
        feed = ConversationFeed()
        turn = feed.begin('image', 'ui_text', 'browser-a')
        feed.finish(turn, {'speak': 'Let me draw that.', 'background_task': task})
        assert robot.background_work.set_progress(task['id'], 'Still drawing it.')
        assert feed.snapshot('browser-a', robot.background_work.get)['turns'][0]['reply'] == 'Still drawing it.'
        assert feed.snapshot('browser-b', robot.background_work.get)['turns'] == []
        release.set()
        wait_until(lambda: robot.background_work.get(task['id'])['status'] == 'completed')
        assert feed.snapshot('browser-a', robot.background_work.get)['turns'][0]['reply'] == 'Here is the image.'
    finally:
        release.set(); robot.close()


def test_delivery_shows_before_speech_and_works_muted(tmp_path):
    robot = Host(tmp_path/'tasks.db', lambda *a, **kw: {})
    events = []
    robot._execute_app_tool = lambda call, **kw: events.append(('show', call['arguments'])) or {'ok': True}
    robot._remember_app_display = lambda *args: events.append(('hold',))
    robot._speak_text = lambda *args: events.append(('speak', args[0])) or True
    try:
        task = robot.background_work.submit(text='image', source='ui_text', provider='', model='', reply_requested=True,
            work=lambda ctx: {'speak': 'Here is the image.', 'display_result': {'artifact_id': IMAGE, 'hold_s': 10}})
        wait_until(lambda: robot.background_work.get(task['id'])['status'] == 'completed')
        task = robot.background_work.get(task['id'])
        assert robot._present_background_result(task, task['result']['speak'], 1, True)
        assert [e[0] for e in events] == ['show', 'hold', 'speak']
        events.clear()
        assert robot._present_background_result(task, task['result']['speak'], 1, False)
        assert [e[0] for e in events] == ['show', 'hold']
        robot.background_work.cancel(task['id'])
        events.clear()
        assert robot._present_background_result(task, '', 1, True) is None
        assert not events
    finally:
        robot.close()


def test_visible_image_is_not_replayed_when_speech_retries(tmp_path):
    robot = Host(tmp_path/'tasks.db', lambda *a, **kw: {})
    shown = []
    robot._execute_app_tool = lambda call, **kw: shown.append(call) or {'ok': True}
    robot._remember_app_display = lambda *args: None
    robot._speak_text = lambda *args: None
    try:
        task = robot.background_work.submit(text='image', source='ui_text', provider='', model='', reply_requested=True,
            work=lambda ctx: {'speak': 'Here is the image.', 'display_result': {'artifact_id': IMAGE, 'hold_s': 10}})
        robot._track_background_turn(task, 'ui_text', 1)
        wait_until(lambda: robot.background_work.get(task['id'])['status'] == 'completed')
        task = robot.background_work.get(task['id'])
        robot._present_background_result(task, 'Here is the image.', 1, True)
        robot._present_background_result(task, 'Here is the image.', 1, True)
        assert len(shown) == 1
    finally:
        robot.close()
