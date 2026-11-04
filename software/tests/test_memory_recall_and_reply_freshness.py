"""Small approved memory sets and stale conversational reply regressions."""
import threading

import pytest

from robbi.engine import memory as memory_module
from robbi.engine.lifecycle import LifecycleMixin
from robbi.engine.memory import MemoryStore
from test_background_work import Host, wait_until
from test_task_lifecycle_hardening import service_at


@pytest.fixture
def memories(tmp_path, monkeypatch):
    for name, filename in [('MEMORY_DB_FILE', 'memory.db'), ('MEMORY_FILE', 'MEMORY.md'),
                           ('RECENT_CONTEXT_FILE', 'recent.json')]:
        monkeypatch.setattr(memory_module, name, tmp_path / filename)
    monkeypatch.setattr(memory_module.settings, 'memory_export_markdown_enabled', False)
    store = MemoryStore()
    for item in [dict(kind='person', content='Mira', summary="User's name", tags=['name']),
                 dict(content='User is 34 years old.', tags=['age']),
                 dict(content='User lives in Italy.', tags=['location']),
                 dict(content='User avoids peanuts.', tags=['food'])]:
        store.remember(item, source='ui')
    store.remember(dict(content='No durable memories yet.', tags=['legacy']), source='legacy_markdown')
    store.remember('Unreviewed claim.', source='memory_extractor:test')
    return store


@pytest.mark.parametrize('question', ["What's my name?", 'Old am I?', 'And where do I live?',
                                     'What memories do you have of me?', 'What else?', 'How are you?'])
def test_small_approved_set_survives_question_wording(memories, question):
    bundle = memories.prepare_prompt_context(question, recent_messages=[
        {'role': 'assistant', 'content': 'I do not have any memories of you.'}])
    assert {m['content'] for m in bundle['matches']} == {
        'Mira', 'User is 34 years old.', 'User lives in Italy.', 'User avoids peanuts.'}
    assert 'No durable memories yet' not in bundle['prompt_text']
    assert 'Unreviewed claim' not in bundle['prompt_text']


def test_legacy_placeholder_not_imported_or_recalled(memories, tmp_path):
    assert not memories.search('durable memories')
    with memories._connect() as connection:
        connection.execute('DELETE FROM memories')
    (tmp_path / 'MEMORY.md').write_text('# Memory\n- No durable memories yet.\n- User likes soup.\n')
    memories._bootstrap_legacy_memory()
    assert [m['content'] for m in memories.list_entries()] == ['User likes soup.']


def test_large_store_prioritizes_question_over_old_chat(memories):
    for index in range(20):
        memories.remember(f'Project entry {index} about old robots.', source='ui')
    memories.remember('The umbrella is in the hallway.', source='ui')
    bundle = memories.prepare_prompt_context('Where is the umbrella?', recent_messages=[
        {'role': 'assistant', 'content': 'aardvark aa ab ac ad ae af ag ah ai'}])
    assert any('umbrella' in m['content'] for m in bundle['matches'])
    assert len(bundle['matches']) <= memory_module.settings.memory_search_limit


def test_new_turn_invalidates_only_same_conversation():
    host = LifecycleMixin()
    host._init_lifecycle()
    for request, source, session in [(1, 'speech_input', ''), (2, 'ui_text', 'a'),
                                     (3, 'ui_text', 'b'), (4, 'speech_input', '')]:
        host._register_request(request, 0)
        host._register_conversation_request(request, source, session)
    assert not host._request_current(1)
    assert all(host._request_current(request) for request in (2, 3, 4))
    host._register_request(5, 0)  # Explicit background work is independent.
    assert host._request_current(5)


def test_cancellation_after_completion_suppresses_delivery_but_retains_result(tmp_path):
    service, scheduler = service_at(tmp_path)
    cancelled = threading.Event()
    task = service.submit(text='test', source='speech_input', provider='', model='',
                          work=lambda _: {'speak': 'old answer'}, cancellation=cancelled,
                          reply_requested=True)
    scheduler.run()
    assert service.pending_delivery()
    cancelled.set()
    assert not service.pending_delivery()
    assert not service.delivery_current(task['id'])
    assert service.get(task['id'])['result']['speak'] == 'old answer'


def test_adopted_chat_expires_on_next_turn_but_research_remains(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def ask(*args, **kwargs):
        entered.set()
        release.wait(2)
        return {'speak': 'old answer'}
    host = Host(tmp_path / 'tasks.db', ask)
    try:
        host._register_conversation_request(1, 'speech_input')
        receipt = host._ask_responsive_conversation('old', source='speech_input', request_id=1)
        release.set()
        task_id = receipt['background_task']['id']
        wait_until(lambda: host.background_work.get(task_id)['status'] == 'completed')
        assert host.background_work.pending_delivery()
        host._register_request(2, 0)
        host._register_conversation_request(2, 'speech_input')
        assert not host.background_work.pending_delivery()
        assert not host.background_work.delivery_current(task_id)
        assert host.background_work.get(task_id)['result']['speak'] == 'old answer'
        research = host.planned_research('Research lunar geology in detail', 'speech_input', 2)
        research_id = research['background_task']['id']
        wait_until(lambda: host.background_work.get(research_id)['status'] == 'completed')
        host._register_request(3, 0)
        host._register_conversation_request(3, 'speech_input')
        assert host.background_work.delivery_current(research_id)
    finally:
        release.set()
        host.close()


def test_expired_delivery_owner_cannot_speak_completed_result(tmp_path):
    from robbi.orchestration import Intent
    service, scheduler = service_at(tmp_path)
    owner = Intent('casual-reply')
    task = service.submit(text='test', source='speech_input', provider='', model='',
                          work=lambda _: {'speak': 'old answer'}, cancellation=owner,
                          reply_requested=True)
    scheduler.run()
    owner.deadline = 0  # Provider finished, but no timely delivery was possible.
    assert not service.pending_delivery()
    assert not service.delivery_current(task['id'])
    assert service.get(task['id'])['result']['speak'] == 'old answer'
