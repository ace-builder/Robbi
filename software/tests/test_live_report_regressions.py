"""Regressions for actual news, motion and asynchronous photo failures."""
from contextvars import copy_context
import math
from unittest.mock import Mock

import pytest

from robbi.engine import camera
from robbi.engine.heading import HeadingFeedback
from robbi.engine.motion import MotionManager
from robbi.engine.turn_control import run_turn
from robbi.orchestration import Intent, intent_scope
from robbi.services.conversation_feed import ConversationFeed, publish_conversation, finish_followup
from test_app_conversation import Harness
from test_heading import SlipRig, sample


def test_news_citations_are_not_an_empty_refinement():
    answer = {'speak': 'Here is the news summary.', 'web_search': {
        'sources': [{'name': 'Source', 'url': 'https://example.com/news'}]}}
    host = Harness(answer=lambda *a, **kw: answer)
    host._execute_web_search_tool = Mock(return_value={'ok': True, 'results': [{'title': 'News'}]})
    result = host._maybe_run_tool_followup({'web_search': {'query': 'today news'}},
        'What are the news today?', 'speech_input', 'fake', 'model', [], '', None, 42)
    assert result['speak'] == 'Here is the news summary.'
    assert 'web_search' not in result
    assert host._execute_web_search_tool.call_count == 1
    assert len(host.provider_calls) == 1


@pytest.mark.parametrize('owner,priority,expected', [
    ('conversation',60,True), ('robot-action',60,True), ('manual:dashboard',80,True),
    ('navigation',60,False), ('conversation',20,False), ('app:test',50,False)])
def test_interactive_motion_uses_manual_recovery_but_navigation_does_not(owner,priority,expected):
    with intent_scope(Intent(owner, priority)):
        assert MotionManager._manual_command() is expected


def test_full_rotation_finishes_with_fresh_but_flat_gyro():
    rig = SlipRig(1)
    feedback = HeadingFeedback(lambda: sample(time=rig.now), math.radians(.05), 1,
                               now=0, recover=True)
    result = run_turn(rig, rig, (-3600,3600), cap=45, start_duty=38, tolerance=20,
                      feedback=feedback, clock=lambda:rig.now, timeout=30, best_effort=True)
    assert result['ok'], result
    assert abs(feedback.angle-360) <= 2.1
    assert feedback.status()['source'] == 'encoders'
    assert 'imu_heading_unresponsive' in feedback.status()['warnings']
    assert rig.duties == (0,0)


def test_photo_capture_retries_one_fresh_frame_without_preview(monkeypatch):
    service = camera.CameraService()
    service._running = True
    monkeypatch.setattr(camera, 'cv2', object())
    service.get_llm_image_bytes = Mock(side_effect=[None,b'fresh-jpeg'])
    service.get_frame = Mock(return_value=object())
    assert service.capture_llm_image_bytes(640,75) == b'fresh-jpeg'
    service.get_frame.assert_called_once_with(fresh=True,max_age_s=1.5)
    assert service.get_llm_image_bytes.call_count == 2


@pytest.mark.parametrize('fast', [True,False])
def test_photo_followup_replaces_ack_in_correct_interact_turn(fast):
    feed = ConversationFeed()
    pending=[]
    class Host:
        conversation_feed=feed
        @publish_conversation
        def ask(self,text,**kwargs):
            context=copy_context()
            callback=lambda: context.run(finish_followup, {'speak':'There is a chair.'})
            if fast: callback()
            else: pending.append(callback)
            return {'speak':'Taking a photo now.'}
    Host().ask('Photo please',source='ui_text',conversation_id='first')
    for callback in pending:callback()
    assert feed.snapshot('first')['turns'][0]['reply']=='There is a chair.'
    assert feed.snapshot('second')['turns']==[]
    assert not any(k.startswith('_') for k in feed.snapshot('first')['turns'][0])
