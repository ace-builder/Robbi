import json
from types import SimpleNamespace

import pytest

from robbi.services.attention import Attention
from robot.config import settings
from test_autonomous_runtime import autonomy_host


def test_novelty_is_durable_and_ignores_sensor_jitter(tmp_path):
    clock=[1000.]
    journal=Attention(tmp_path/'attention.json',clock=lambda:clock[0])
    sensor={'tof':{'h1':{'fresh':True,'distance_cm':120}},'relative_pose':{'x_cm':5,'y_cm':3}}
    first=journal.evidence(sensor,{'phase':'day'},{},[])
    assert journal.claim(first)
    sensor['tof']['h1']['distance_cm']=121
    sensor['relative_pose']['x_cm']=6
    second=journal.evidence(sensor,{'phase':'day'},{},[])
    assert not journal.novel(second)
    restarted=Attention(journal.path,clock=lambda:clock[0])
    assert not restarted.novel(second)
    clock[0]+=86401
    assert restarted.novel(second)


def test_journal_bounded_and_corruption_never_replays(tmp_path):
    journal=Attention(tmp_path/'attention.json')
    for i in range(60):
        journal.claim({'situation':i});journal.record('quiet','reason','idle','focus')
    assert journal.status()['situations']==32
    assert len(journal.status()['episodes'])==12
    assert journal.path.stat().st_size<32768
    journal.path.write_text('{broken')
    restarted=Attention(journal.path)
    assert restarted.error and not restarted.claim({'situation':'new'})


def activate(host, clock, tmp_path, answer):
    host.attention=Attention(tmp_path/'attention.json',clock=lambda:clock[0])
    host.model_answer=answer
    return host


def test_real_loop_stays_quiet_for_same_evidence_including_restart(autonomy_host,tmp_path):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{'initiative':{'kind':'quiet','reason':'Nothing useful has changed'}})
    host._run_heartbeat_turn()
    assert len(host.model_calls)==1 and host.outputs==[]
    assert host.model_calls[0][1]['purpose']=='initiative'
    assert host.autonomy_policy.status()['model_calls']==1
    clock[0]+=61
    host._run_heartbeat_turn()
    assert len(host.model_calls)==1
    host.attention=Attention(host.attention.path,clock=lambda:clock[0])
    clock[0]+=61
    host._run_heartbeat_turn()
    assert len(host.model_calls)==1
    assert host._heartbeat_state['last_reason']=='no_meaningful_change'


def test_new_evidence_reconsiders_but_model_cannot_inject_movement_or_memory(autonomy_host,tmp_path):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{'initiative':{'kind':'speak','reason':'Requested project context'},
        'speak':'The next useful step is checking the fit.', 'drive':{'direction':'forward'},
        'remember':'Invented personal fact','sequence':[{'drive':{'direction':'forward'}}]})
    host._run_heartbeat_turn()
    assert host.outputs==[{'speak':'The next useful step is checking the fit.'}]
    assert not host.motion_calls and not host.memory_calls
    assert host.attention.status()['episodes'][-1]['outcome']=='action_requested'


def test_user_interrupt_during_inference_discards_all_outputs(autonomy_host,tmp_path):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{})
    def ask(*args,**kwargs):
        host._last_activity_ts=clock[0]
        return {'initiative':{'kind':'speak'},'speak':'Must not interrupt'}
    host.conversation_service.ask=ask
    host._run_heartbeat_turn()
    assert not host.outputs
    assert host.attention.status()['episodes'][-1]['outcome']=='cancelled'
    assert host._autonomy_intent is None


def test_disabled_exploration_cannot_be_enabled_by_model(autonomy_host,tmp_path):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{'initiative':{'kind':'explore','reason':'Curiosity'}})
    host._run_heartbeat_turn()
    assert not host.motion_calls
    assert host.attention.status()['episodes'][-1]['outcome']=='unavailable'


def test_observe_vision_uses_second_budgeted_call_no_memory_promotion(autonomy_host,tmp_path):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{})
    answers=iter(({'initiative':{'kind':'observe','reason':'Check changed surroundings'}}, {'speak':'A plain box.'}))
    host.conversation_service.ask=lambda *a,**k:host.model_calls.append((a,k)) or next(answers)
    host._run_heartbeat_turn()
    assert len(host.model_calls)==2
    assert host.autonomy_policy.status()['model_calls']==2
    assert host.autonomy_policy.status()['observations'][-1]['data']['visual_summary']=='A plain box.'
    assert not host.memory_calls and not host.outputs


def test_journal_write_failure_does_not_leave_active_owner(autonomy_host,tmp_path,monkeypatch):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{'initiative':{'kind':'quiet'}})
    monkeypatch.setattr(host.attention,'record',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):host._run_heartbeat_turn()
    assert host._autonomy_intent is None
    assert host.autonomy_policy.status()['active'] is None


def test_budget_exhaustion_does_not_call_provider(autonomy_host,tmp_path):
    host,clock=autonomy_host
    activate(host,clock,tmp_path,{'initiative':{'kind':'quiet'}})
    host.autonomy_policy.options=lambda:{'max_model_calls_per_day':0}
    host._run_heartbeat_turn()
    assert host.model_calls==[]
