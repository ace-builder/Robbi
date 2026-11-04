import time
from types import SimpleNamespace

import pytest

from robbi.configuration.settings import settings
from robbi.live_awareness.gemini import report, grounded_report
from robbi.live_awareness.session import options
from robbi.live_awareness.web import reaction_status
from robbi.orchestration import Intent, APP, BACKGROUND
from robbi.services.attention import Attention
from test_autonomous_runtime import autonomy_host


def social(kind='greeting', text='', **changes):
    data = {'summary': 'The person waved at Robbi.', 'changed': False, 'salience': 'low',
            'engagement': kind, 'directed_at_robot': True, 'utterance': text, **changes}
    return report({'name': 'report_observation', 'args': data})


def ready(autonomy_host, tmp_path, *, gestures=False):
    host, clock = autonomy_host
    host.attention = Attention(tmp_path/'live-attention.json', clock=lambda: clock[0])
    host.assets = SimpleNamespace(names=lambda _: ['happy.png'])
    host.model_answer = {'initiative': {'kind': 'speak'}, 'speak': 'Hi there!', 'face': 'happy.png'}
    host.attention.observe_live(social(), react=True, allow_gestures=gestures)
    return host, clock


def test_contradictory_low_unchanged_wave_reaches_actual_reaction_lane(autonomy_host, tmp_path):
    host, _ = ready(autonomy_host, tmp_path)
    assert social()['changed'] and social()['salience'] == 'high'
    host._run_heartbeat_turn()
    assert host.outputs == [{'speak': 'Hi there!', 'face': 'happy.png'}]
    assert host.attention.live_evidence() is None


def test_rephrased_same_wave_does_not_cancel_inflight_answer(autonomy_host, tmp_path):
    host, clock = ready(autonomy_host, tmp_path)
    def ask(*args, **kwargs):
        clock[0] += 3
        host.attention.observe_live(social(summary='A hand is waving in greeting.'), react=True)
        return host.model_answer
    host.conversation_service.ask = ask
    host._run_heartbeat_turn()
    assert host.outputs and host.outputs[0]['speak'] == 'Hi there!'


def test_claimed_interaction_survives_slow_response_but_not_stop(autonomy_host, tmp_path):
    host, clock = ready(autonomy_host, tmp_path)
    def ask(*args, **kwargs):
        clock[0] += 17
        return host.model_answer
    host.conversation_service.ask = ask
    host._run_heartbeat_turn()
    assert host.outputs and host.outputs[0]['speak'] == 'Hi there!'
    host.attention.clear_live()
    host.attention.observe_live(social('question', 'How are you?'), react=True)
    evidence = host.attention.live_evidence()
    assert host.attention.reserve_live(evidence, 30)
    host.attention.clear_live()
    assert host.attention.live_evidence() is None


def test_reservation_is_bounded_and_cannot_extend_an_expired_event(tmp_path):
    clock = [100.]
    attention = Attention(tmp_path/'attention.json', clock=lambda: clock[0])
    attention.observe_live(social(), react=True)
    evidence = attention.live_evidence()
    assert attention.reserve_live(evidence, 10000)
    clock[0] += 41
    assert attention.live_evidence() is None
    assert not attention.reserve_live(evidence, 30)


def test_continuous_wave_is_once_but_new_wave_after_neutral_can_be_acknowledged(autonomy_host, tmp_path):
    host, clock = ready(autonomy_host, tmp_path)
    host._run_heartbeat_turn()
    clock[0] += 9
    host.attention.observe_live(social(summary='Still waving.'), react=True)
    assert host.attention.live_evidence() is None
    host.attention.observe_live({'summary': 'Sitting quietly.', 'changed': False, 'salience': 'low'}, react=True)
    host.attention.observe_live(social(), react=True)
    host._run_heartbeat_turn()
    assert len(host.outputs) == 2


def test_question_has_actual_words_and_requires_audio(autonomy_host, tmp_path):
    host, _ = ready(autonomy_host, tmp_path)
    question = social('question', 'How are you doing?', summary='A person asked Robbi how it is doing.')
    camera = grounded_report(question, audio=False)
    assert not camera['changed'] and not camera['utterance']
    host.attention.clear_live()
    host.attention.observe_live(camera, react=True)
    assert host.attention.live_evidence() is None
    host.attention.observe_live(grounded_report(question, audio=True), react=True)
    host._run_heartbeat_turn()
    assert 'How are you doing?' in host.model_calls[-1][1]['memory_text']
    assert host.outputs


def test_background_or_uncertain_social_event_is_not_promoted(tmp_path):
    attention = Attention(tmp_path/'attention.json')
    value = social(directed_at_robot=False)
    assert value['changed'] is False
    attention.observe_live(value, react=True)
    assert attention.live_evidence() is None
    assert report({'name': 'report_observation', 'args': {**value, 'drive': 'forward'}}) is None


@pytest.mark.parametrize('priority,allowed', [(BACKGROUND, True), (APP, False)])
def test_ambient_display_does_not_starve_reactions_but_active_app_is_respected(autonomy_host, tmp_path, priority, allowed):
    host, _ = ready(autonomy_host, tmp_path)
    lease = host.orchestrator.acquire(('display',), intent=Intent('test-display', priority))
    try:
        host._run_heartbeat_turn()
        assert bool(host.outputs) is allowed
    finally:
        lease.release()


def test_speaking_preserves_owner_held_image(autonomy_host, tmp_path):
    host, _ = ready(autonomy_host, tmp_path)
    lease = host.orchestrator.acquire(('display',), intent=Intent('app:image', APP))
    lease.release(hold_s=10)
    host._app_display_until = time.monotonic()+10
    host._run_heartbeat_turn()
    assert host.outputs == [{'speak': 'Hi there!'}]
    assert host.orchestrator.snapshot()['resources']['display']['owner'] == 'app:image'


@pytest.mark.parametrize('consent,commissioned,gesture,expected', [
    (False, True, 'happy-wiggle', False), (True, False, 'happy-wiggle', False),
    (True, True, 'happy-wiggle', True), (True, True, 'happy-dance', False),
])
def test_gestures_require_session_consent_commissioning_and_whitelist(autonomy_host, tmp_path, monkeypatch, consent, commissioned, gesture, expected):
    host, _ = ready(autonomy_host, tmp_path, gestures=consent)
    monkeypatch.setattr(settings, 'enable_motors', commissioned)
    monkeypatch.setattr(settings, 'runtime_calibration_confirmed', commissioned)
    host.model_answer.update(gesture=gesture, drive={'distance_cm': 200}, app_tool={'app': 'anything'})
    host._run_heartbeat_turn()
    assert ('gesture' in host.outputs[0]) is expected
    assert 'drive' not in host.outputs[0] and 'app_tool' not in host.outputs[0]


def test_overview_explains_observation_only_and_blocked_output(autonomy_host, tmp_path):
    host, _ = ready(autonomy_host, tmp_path)
    assert reaction_status(host, {'running': True, 'options': {'react': False}})['state'] == 'observing'
    lease = host.orchestrator.acquire(('display',), intent=Intent('app:picture', APP))
    try:
        state = reaction_status(host, {'running': True, 'options': {'react': True}})
        assert 'outputs occupied' in state['reason']
    finally:
        lease.release()
    with pytest.raises(Exception, match='Enable reactions'):
        options({'gestures': True})
