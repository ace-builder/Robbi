import asyncio
from collections import deque
from dataclasses import replace
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.conversation_lab.engine import Conversation, Speech
from robbi.conversation_lab.models import Budget, BudgetExceeded, ChatModels
from robbi.conversation_lab.settings import ContinuousSettings
from robbi.conversation_lab.service import ConversationLab
from robbi.errors import BusyError
from robbi.conversation_lab.provider import Continuous


def decision(mode, text='', task=''):
    return {'content': json.dumps(dict(mode=mode, text=text, task=task))}


def call(name='robot_state', arguments='{}', ident='a'):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


class FakeExecutor:
    schemas = {name: {'name': name, 'parameters': {'type': 'object', 'properties': {}}}
               for name in ('robot_state', 'move', 'web_search', 'stop')}
    armed = False

    def __init__(self):
        self.calls = []
        self.cancels = 0

    async def call(self, ident, name, args):
        self.calls.append((ident, name, args))
        if name == 'move':
            return {'ok': False, 'error': 'motors disarmed'}
        return {'ok': True, 'result': 'source reports rain'}

    def cancel(self):
        self.cancels += 1


class FakeModel:
    def __init__(self, front, worker=()):
        self.front, self.worker = deque(front), deque(worker)
        self.requests = []
        self.gate = None

    async def complete(self, messages, worker=False, **kwargs):
        self.requests.append((worker, list(messages)))
        if worker and self.gate:
            await self.gate.wait()
        result = (self.worker if worker else self.front).popleft()
        if isinstance(result, Exception):
            raise result
        return result


def test_background_job_does_not_block_or_cancel_followup():
    async def run():
        model = FakeModel([decision('delegate', 'Checking the news.', 'Look up current news'), decision('reply', 'Hello again.')],
                          [{'tool_calls': [call('web_search', '{"query":"news today"}')]}, {'content': 'The source reports rain.'}])
        model.gate = asyncio.Event()
        engine = Conversation(model, FakeExecutor(), ContinuousSettings())
        engine.submit('News today?'); await engine.foreground
        job = next(iter(engine.jobs.values()))
        await asyncio.sleep(0)
        engine.submit('Hello!'); await engine.foreground
        assert job.state == 'running' and not job.task.done()
        assert any(x['content'] == 'Hello again.' for x in engine.history)
        model.gate.set(); await job.task
        assert job.state == 'done' and job.calls == 1
        last = model.requests[-1][1]
        assert last[1]['content'] == 'Look up current news'
        assert last[-1]['role'] == 'tool' and 'source reports rain' in last[-1]['content']
        assert engine.valid(Speech(job.result, 1, job.id, 'result'))
        assert not engine.valid(Speech('Checking', 1, job.id, 'ack'))
        await engine.close()
    asyncio.run(run())


def test_cancellation_stops_worker_and_suppresses_completed_results():
    async def run():
        model = FakeModel([decision('delegate', 'Checking.', 'Read state'), decision('cancel', 'Stopped.')], [{'content': 'Done'}])
        model.gate = asyncio.Event()
        executor = FakeExecutor()
        engine = Conversation(model, executor, ContinuousSettings())
        engine.submit('Read state'); await engine.foreground
        job = next(iter(engine.jobs.values()))
        engine.submit('Cancel everything'); await engine.foreground
        assert job.state == 'cancelled' and job.task.done() and executor.cancels == 1
        assert not engine.valid(Speech('Done', 1, job.id, 'result'))
        await engine.close()
    asyncio.run(run())


def test_duplicate_effect_is_not_repeated_and_denial_stays_in_evidence():
    async def run():
        model = FakeModel([decision('delegate', '', 'Move')], [
            {'tool_calls': [call('move', '{"centimeters":10}')]},
            {'tool_calls': [call('move', '{"centimeters":10}', 'b')]}, {'content': 'Motors were disarmed.'}])
        executor = FakeExecutor(); engine = Conversation(model, executor, ContinuousSettings())
        engine.submit('Move'); await engine.foreground
        job = next(iter(engine.jobs.values())); await job.task
        assert len(executor.calls) == 1 and job.calls == 2
        assert 'disarmed' in job.evidence[0]['result']
        assert 'Duplicate effect' in job.evidence[1]['result']
        await engine.close()
    asyncio.run(run())


def test_call_limit_rejects_oversized_batch_before_any_effect():
    async def run():
        model = FakeModel([decision('delegate', '', 'Read state')], [{'tool_calls': [call(), call(ident='b')]}])
        executor = FakeExecutor(); engine = Conversation(model, executor, replace(ContinuousSettings(), max_calls=1))
        engine.submit('Read state'); await engine.foreground
        job = next(iter(engine.jobs.values())); await job.task
        assert job.state == 'failed' and not executor.calls
        await engine.close()
    asyncio.run(run())


def test_stop_call_prevents_later_movement_in_same_batch():
    async def run():
        model = FakeModel([decision('delegate', '', 'Stop')], [{'tool_calls': [call('stop'), call('move', '{"centimeters":10}', 'b')]}])
        executor = FakeExecutor(); engine = Conversation(model, executor, ContinuousSettings())
        engine.submit('Stop now'); await engine.foreground
        job = next(iter(engine.jobs.values())); await job.task
        assert [c[1] for c in executor.calls] == ['stop']
        await engine.close()
    asyncio.run(run())


def test_partial_evidence_survives_model_failure():
    async def run():
        model = FakeModel([decision('delegate', '', 'Search')], [{'tool_calls': [call('web_search')]}, OSError('offline')])
        engine = Conversation(model, FakeExecutor(), ContinuousSettings())
        engine.submit('Search'); await engine.foreground
        job = next(iter(engine.jobs.values())); await job.task
        assert job.state == 'failed' and len(job.evidence) == 1
        assert '1 tool calls' in job.result
        await engine.close()
    asyncio.run(run())


def test_budget_is_persistent_and_reserves_unconfirmed_cost(tmp_path):
    ledger = tmp_path / 'budget.json'
    budget = Budget(.5, ledger)
    budget.reserve(.3)
    budget.settle(.3, {})
    assert Budget(.5, ledger).used == .3
    with pytest.raises(BudgetExceeded):
        budget.reserve(.21)
    budget.settle(.3, {'cost': .01})
    assert budget.used == pytest.approx(.01)


def test_cloud_secret_never_goes_to_local_endpoint():
    async def run():
        model = ChatModels(replace(ContinuousSettings(), model_url='http://localhost:1234/v1'), Budget(.25), key='private-cloud-key')
        assert model.http.headers.get('Authorization') != 'Bearer private-cloud-key'
        await model.close()
    asyncio.run(run())


@pytest.mark.parametrize('values', [{'speech_url':'file:///private'}, {'model_url':'http://user:password@localhost'},
    {'budget_usd':float('nan')}, {'budget_usd':True}, {'max_calls':0}, {'unknown':1}])
def test_options_reject_invalid_inputs(values):
    with pytest.raises(ValueError): ContinuousSettings.parse(values)


def test_settings_and_service_are_opt_in(tmp_path):
    service=ConversationLab(SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path)))
    assert service.settings()['budget_usd']==.25
    assert service.thread is None and service.status()['state']=='idle'
    service.configure({'voice':'am_adam'})
    assert (tmp_path/'conversation-lab/continuous.local.json').is_file()
    service.thread=SimpleNamespace(is_alive=lambda:True)
    with pytest.raises(BusyError):service.configure({})


def test_typed_followup_does_not_cancel_continuous_jobs(tmp_path):
    service=ConversationLab(SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path)))
    service.state='live';service.executor=FakeExecutor()
    service.text('How are you?')
    assert service.executor.cancels==0 and service.inputs.get_nowait()=='How are you?'


def test_job_timeout_is_bounded_and_keeps_foreground_available():
    async def run():
        model = FakeModel([decision('delegate', '', 'Read state'), decision('reply', 'Still listening')], [])
        model.gate = asyncio.Event()
        engine = Conversation(model, FakeExecutor(), replace(ContinuousSettings(), job_seconds=.02))
        engine.submit('Read state'); await engine.foreground
        job = next(iter(engine.jobs.values())); await job.task
        assert job.state == 'failed' and 'TimeoutError' in job.result
        engine.submit('Hello'); await engine.foreground
        assert engine.history[-1]['content'] == 'Still listening'
        await engine.close()
    asyncio.run(run())


def test_two_job_bound_keeps_chat_responsive():
    async def run():
        model = FakeModel([decision('delegate', '', 'Research')]*3, [])
        model.gate = asyncio.Event()
        engine = Conversation(model, FakeExecutor(), ContinuousSettings())
        for _ in range(3):
            engine.submit('Research'); await engine.foreground
        assert len(engine.jobs) == 2
        assert 'two tasks' in engine.history[-1]['content']
        await engine.close()
    asyncio.run(run())


class FakeAudio:
    def __init__(self):
        self.output = queue.Queue()
        self.chunks = []
        self.interrupts = 0
    def interrupt(self): self.interrupts += 1
    def enqueue(self, chunk): self.chunks.append(chunk)
    def end_turn(self): pass


def test_speech_waits_if_user_started_talking_during_synthesis(tmp_path):
    import httpx
    async def run():
        audio = FakeAudio(); events=[]
        runtime = SimpleNamespace(config=SimpleNamespace(root=tmp_path), cancelled=threading.Event(),
            hardware=SimpleNamespace(audio=audio), event=lambda *e: events.append(e))
        provider = Continuous(runtime)
        provider.engine = Conversation(FakeModel([]), FakeExecutor(), ContinuousSettings())
        provider.speech_http = httpx.AsyncClient(base_url='http://speech/',
            transport=httpx.MockTransport(lambda _: httpx.Response(200,content=b'\x00'*4800)))
        provider.transcription = asyncio.Future()
        item = Speech('Hello',0)
        task = asyncio.create_task(provider._render(item))
        await asyncio.sleep(.03)
        assert not audio.chunks and not item.emitted
        provider.transcription.set_result(None)
        provider.last_activity = time.monotonic()-1
        await task
        assert item.emitted and audio.chunks
        await provider.speech_http.aclose()
        await provider.engine.close()
    asyncio.run(run())


def test_new_input_cancels_inflight_tts_without_cancelling_jobs(tmp_path):
    async def run():
        audio = FakeAudio()
        runtime = SimpleNamespace(config=SimpleNamespace(root=tmp_path), hardware=SimpleNamespace(audio=audio))
        provider = Continuous(runtime)
        executor=FakeExecutor()
        provider.engine=Conversation(FakeModel([decision('reply','Hello')]),executor,ContinuousSettings())
        provider.synthesis=asyncio.create_task(asyncio.Event().wait())
        await provider._accept('Hello')
        await asyncio.gather(provider.synthesis, return_exceptions=True)
        await provider.engine.foreground
        assert provider.synthesis.cancelled() and executor.cancels==0 and audio.interrupts==1
        await provider.engine.close()
    asyncio.run(run())


def test_stale_result_is_not_spoken_minutes_later():
    engine=Conversation(FakeModel([]),FakeExecutor(),ContinuousSettings())
    assert not engine.valid(Speech('An old reply',0,created=time.monotonic()-60))
