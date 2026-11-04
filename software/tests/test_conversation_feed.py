"""Live typed/voice transcript and deferred replies without audio dependencies."""
from types import SimpleNamespace
from pathlib import Path
import pytest

from robbi.services.conversation_feed import ConversationFeed, publish_conversation
from robbi.config import load
from robbi.runtime import Runtime
from robbi.web import create_app


def test_voice_shared_typed_scoped_and_telegram_excluded():
    feed=ConversationFeed()
    first=feed.begin('Typed in A','ui_text','tab-a')
    feed.finish(first,{'speak':'Hello A'})
    second=feed.begin('Typed in B','ui_text','tab-b')
    feed.finish(second,{'speak':'Hello B'})
    voice=feed.begin('Heard through microphone','speech_input')
    feed.finish(voice,{'speak':'Voice answer'})
    assert feed.begin('Private Telegram text','telegram_text') is None
    assert [r['text'] for r in feed.snapshot('tab-a')['turns']]==['Typed in A','Heard through microphone']
    assert [r['text'] for r in feed.snapshot('tab-b')['turns']]==['Typed in B','Heard through microphone']
    assert all('conversation_id' not in row for row in feed.snapshot('tab-a')['turns'])


@pytest.mark.parametrize('status,result,error',[
    ('completed',{'speak':'Actual research answer'},''),
    ('failed',None,'Provider failed'),('cancelled',None,'Task cancelled')])
def test_background_ack_replaced_by_final_result_even_without_speaker(status,result,error):
    feed=ConversationFeed()
    turn=feed.begin('Research it','ui_text','tab')
    feed.finish(turn,{'speak':'I will work on it','background_task':{'id':'task'}})
    assert feed.snapshot('tab')['turns'][0]['state']=='working'
    task={'status':status,'result':result,'error':error}
    final=feed.snapshot('tab',read_task=lambda _:task)['turns'][0]
    assert final['state']==status
    assert final['reply']==(result['speak'] if result else error)
    assert 'I will work' not in final['reply']


def test_feed_is_bounded_and_provider_errors_do_not_leak():
    feed=ConversationFeed(limit=3)
    for index in range(7):
        turn=feed.begin(str(index)*12000,'speech_input')
        feed.finish(turn,{'speak':'answer'*2000})
    rows=feed.snapshot()['turns']
    assert len(rows)<=3 and sum(len(r['text'])+len(r['reply']) for r in rows)<=48000
    class Fake:
        conversation_feed=feed
        @publish_conversation
        def think(self,text,**kwargs):
            raise RuntimeError('private API key')
    with pytest.raises(RuntimeError):Fake().think('failure',source='speech_input')
    final=feed.snapshot()['turns'][-1]
    assert final['state']=='failed' and 'private' not in final['reply']


def test_transcript_exposes_input_before_reply_and_captures_followup():
    feed=ConversationFeed()
    class Fake:
        conversation_feed=feed
        @publish_conversation
        def think(self,text,**kwargs):
            turn=feed.snapshot()['turns'][-1]
            assert turn['text']==text and turn['state']=='thinking'
            return {'speak':'First answer','next':{'speak':'Tool result'}}
    answer=Fake().think('A spoken question',source='speech_input')
    assert answer['speak']=='First answer'
    assert feed.snapshot()['turns'][0]['reply']=='First answer\nTool result'


def test_http_transcript_scopes_use_existing_dashboard_boundary(tmp_path):
    root=Path(__file__).resolve().parents[1]
    source=tmp_path/'robot.yaml';source.write_text('camera: {enabled: false}\n')
    runtime=Runtime(load(root,source,tmp_path/'var'))
    feed=ConversationFeed()
    turn=feed.begin('private tab','ui_text','browser-a');feed.finish(turn,{'speak':'Reply'})
    turn=feed.begin('spoken','speech_input');feed.finish(turn,{'speak':'Heard you'})
    runtime._controller=SimpleNamespace(conversation_feed=feed)
    runtime._ready.set()
    client=create_app(runtime).test_client()
    try:
        a=client.get('/api/conversation',headers={'X-Robbi-Conversation':'browser-a'})
        b=client.get('/api/conversation',headers={'X-Robbi-Conversation':'browser-b'})
        assert a.status_code==b.status_code==200
        assert len(a.get_json()['turns'])==2 and len(b.get_json()['turns'])==1
        assert client.get('/api/conversation',headers={'X-Robbi-Conversation':'../bad'}).status_code==400
    finally:
        runtime._controller=None
        runtime.stop()
