import json
from types import SimpleNamespace

import pytest

from robbi.actions import validate_action
from robbi.errors import ValidationError
from robbi.services.prompt_context import prompt_sections,recent_context,tool_evidence,daily_context
from robbi.services.capabilities import catalog,select
from robot.llm import LLMClient


def test_context_flood_is_bounded_without_dropping_recent_user_turn():
    parts=prompt_sections(*(['x'*100000]*5))
    assert sum(map(len,parts.values()))<=40500
    recent=recent_context([{'role':'assistant','content':'a'*8000}]*99+[{'role':'user','content':'latest'}])
    assert sum(len(row['content']) for row in recent)<=6000
    assert recent[-1]['content']=='latest'
    assert len(recent)<=10


def test_tool_evidence_remains_json_and_retains_failure_truth():
    huge=[{'tool':'web_search','result':{'ok':False,'error':'network unavailable',
         'results':[{'text':'x'*50000,'url':'https://example.com'}]*100}}]
    encoded=tool_evidence(huge)
    assert len(encoded)<=14000
    assert json.loads(encoded)[0]['result']['ok'] is False
    assert 'network unavailable' in encoded


def test_daily_summary_does_not_duplicate_transcript_or_raw_events():
    text=daily_context({'open_todos':[{'title':'Check fit','secret':'hidden'}],
                        'recent_messages':['raw transcript'],'recent_events':['raw camera event']})
    assert 'Check fit' in text
    assert not any(word in text for word in ('secret','transcript','camera'))


def test_every_app_remains_discoverable_even_with_large_schemas():
    rows=[{'app':f'app-{i:02}','tool':'run','description':'arbitrary task '*20,
           'input_schema':{'type':'object','description':'x'*10000},'examples':[]} for i in range(48)]
    text=catalog(rows,'app-47')
    assert len(text)<16000
    for i in range(48):assert f'app-{i:02}' in text
    assert not select(rows,'app-47') # Too-large schemas are never cut into invalid JSON.
    rows[-1]['input_schema']={'type':'object','properties':{'location':{'type':'string'}}}
    assert select(rows,'app-47')[0]['app']=='app-47'


def test_initiative_prompt_has_no_action_catalog_or_old_examples():
    llm=LLMClient.__new__(LLMClient)
    llm._behavior=SimpleNamespace(llm_context=lambda: {})
    prompt=llm._system_prompt('some evidence',soul_text='A helpful robot',tools_text='quiet or speak',
                             extra_instructions='Choose one',purpose='initiative')
    assert 'some evidence' in prompt and 'quiet or speak' in prompt
    assert 'reminder' not in prompt and len(prompt)<1000


@pytest.mark.parametrize('action',[
    {'capability_search':{'query':''}}, {'capability_search':{'query':'x'*201}},
    {'capability_search':{'query':'weather'},'drive':{'direction':'forward'}},
    {'sequence':[{'capability_search':{'query':'weather'}}]}])
def test_discovery_cannot_smuggle_effects(action):
    with pytest.raises(ValidationError):validate_action(action)


def test_schema_discovery_does_not_execute_and_is_bounded_to_one_lookup():
    from test_app_conversation import Harness, CALL
    host=Harness(answer=lambda *a,**k:{'app_tool':CALL})
    lookups=[]
    host.app_host.discover_capabilities=lambda query:lookups.append(query) or {'tools':[{'app':'weather','tool':'forecast','input_schema':{}}]}
    result=host._resolve_capability_action({'capability_search':{'query':'weather'}},'weather tomorrow','ui_text',42,'fake','small',[],'')
    assert result['app_tool']==CALL and not host.calls and lookups==['weather']
    host._ask_llm_with_deadline=lambda *a,**k:{'capability_search':{'query':'again'}}
    result=host._resolve_capability_action({'capability_search':{'query':'weather'}},'weather','ui_text',42,'fake','small',[],'')
    assert 'capability_search' not in result and not host.calls


@pytest.mark.parametrize('provider,model,luna',[('openai','gpt-6-luna',True),('local','owner-model',False)])
def test_provider_adapter_bounds_context_and_keeps_other_models_compatible(monkeypatch,provider,model,luna):
    client=LLMClient(SimpleNamespace(names=lambda _:[]),SimpleNamespace(llm_context=lambda:{}))
    requests=[]
    def complete(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"initiative":{"kind":"quiet"}}'))])
    monkeypatch.setattr(client,'_ensure_chat_client',lambda _:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete))))
    monkeypatch.setattr(client,'_trace_openai_request_async',lambda *a:None)
    try:
        result=client.ask('Choose a behavior',provider=provider,model=model,purpose='initiative',
                         memory_text='x'*100000,soul_text='s'*10000,tools_text='quiet or observe',
                         recent_messages=[{'role':'assistant','content':'old'*10000}]*100)
        assert result['initiative']['kind']=='quiet'
        request=requests[0]
        assert len(request['messages'][0]['content'])<10000
        assert sum(len(row['content']) for row in request['messages'][1:-1])<=6000
        if luna:
            assert request['reasoning_effort']=='none' and request['max_completion_tokens']==1800
        else:
            assert 'reasoning_effort' not in request and 'max_completion_tokens' not in request
    finally:client.shutdown()

def test_spoken_answers_do_not_read_citation_footers_unless_requested():
    client = LLMClient(SimpleNamespace(names=lambda _: []), SimpleNamespace(llm_context=lambda: {}))
    prompt = client._system_prompt('', app_catalog_text='Weather: keep provider attribution in result metadata.')
    assert 'In speak, omit source names, URLs and citation footers unless explicitly requested' in prompt
    assert 'retain evidence in tool results' in prompt
    assert 'distinguish reported claims from verified facts' in prompt
    assert 'include source names/URLs' not in prompt

