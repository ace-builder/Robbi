"""Recorded voice failures reproduced with the real output coordinator."""
from concurrent.futures import Future
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS

import pytest

from test_action_backpressure import Host
from test_autonomous_runtime import autonomy_host
from test_live_reactions import ready
from robbi.engine.domains.motion import MotionDomain
from robbi.engine.domains.voice import VoiceDomain
from robbi.engine.domains.vision import VisionDomain
from robbi.engine.vision_skills import classify_vision_skill,person_facing_request,skill_prompt
from robbi.orchestration import APP,BACKGROUND,INTERACTIVE,MANUAL,Intent,Orchestrator,ResourceBusy,intent_scope
from robbi.services.attention import Attention


class Queue:
    def __init__(self):self.pending=[]
    def submit(self,fn,*args,**kwargs):
        future=Future();self.pending.append((future,fn,args,kwargs));return future
    def run(self):
        for future,fn,args,kwargs in self.pending:
            if future.set_running_or_notify_cancel():
                try:future.set_result(fn(*args,**kwargs))
                except Exception as exc:future.set_exception(exc);raise
        self.pending=[]


class SceneHost(VoiceDomain,Host):
    def __init__(self):
        super().__init__();self.orchestrator=Orchestrator()
        self._audio_executor=Queue();self._effect_executor=Queue()
        self.displayed=[];self.speakers=[];self.motor_handles=[]
        self.behavior.set_current_face=lambda _:None
        self.media=NS(show_face=self.show_face,stop_audio=lambda **kw:None)
        self._execute_drive_action=self.drive
        self._speak_text_with_overlay=self.speak
        self._speak_text=self.speak
        self.presentation_state=lambda:self.presentation
    def show_face(self,path):
        with self.orchestrator.acquire('display'):
            self.displayed.append(path)
    def drive(self,spec):
        self.drives.append(spec)
        lease=self.orchestrator.acquire('motion');future=Future()
        self.motor_handles.append((future,lease));return future
    def speak(self,*args):
        with self.orchestrator.acquire(('audio','display')):pass


def ambient(coordinator):
    lease=coordinator.acquire('display',intent=Intent('ambient',BACKGROUND))
    def cleanup():
        while not lease.is_set():time.sleep(.002)
        lease.release()
    thread=threading.Thread(target=cleanup,daemon=True);thread.start()
    return lease,thread


def test_ambient_face_yields_to_complete_reply_and_cannot_steal_display_before_queued_speech():
    host=SceneHost();screen,worker=ambient(host.orchestrator)
    owner=Intent('conversation',INTERACTIVE)
    action={'face':'mischievous','drive':{'mode':'turn','angle_deg':360},'speak':'One full turn.'}
    with intent_scope(owner):host.execute_action(action)
    worker.join(1);assert not worker.is_alive() and screen.is_set()
    assert len(host.drives)==1 and host.drives[0]['angle_deg']==360
    assert set(host.orchestrator.snapshot()['resources'])=={'display','audio','motion'}
    with pytest.raises(ResourceBusy):
        host.orchestrator.acquire('display',intent=Intent('ambient',BACKGROUND),wait_s=0)
    # Speech starts after execute_action has returned; it must inherit the scene.
    host._audio_executor.run()
    future,lease=host.motor_handles[0];future.set_result(None);lease.release()
    assert host.orchestrator.snapshot()['resources']=={}


def test_scene_reservation_is_released_when_queued_presentation_is_cancelled():
    host=SceneHost()
    with intent_scope(Intent('conversation',INTERACTIVE)):
        host.execute_action({'face':'happy','speak':'Hello.'})
    assert host.orchestrator.snapshot()['resources']
    future=host._audio_executor.pending[0][0];future.cancel()
    assert host.orchestrator.snapshot()['resources']=={}


def test_cancelled_owner_cannot_admit_a_new_scene_or_start_hardware():
    host=SceneHost();owner=Intent('conversation',INTERACTIVE);owner.cancel('interrupted')
    with intent_scope(owner):host._run_sequence_step({'drive':{'mode':'turn','angle_deg':360}})
    assert not host.drives and not host.orchestrator.snapshot()['resources']


def test_scene_releases_outputs_after_a_queued_speech_failure():
    host=SceneHost()
    def failed(*args):raise OSError('speaker disconnected')
    host._speak_text_with_overlay=failed
    host.execute_action({'face':'happy','speak':'Hello.'})
    with pytest.raises(OSError,match='speaker disconnected'):host._audio_executor.run()
    assert host.orchestrator.snapshot()['resources']=={}


def test_scene_preserves_held_app_image_and_rejects_stronger_owner_before_driving():
    host=SceneHost();image=host.orchestrator.acquire('display',intent=Intent('app:image',APP))
    image.release(hold_s=10)
    host._app_preserves_display=lambda request_id=None:True
    with intent_scope(Intent('conversation',INTERACTIVE)):
        host.execute_action({'face':'happy','drive':{'mode':'turn','angle_deg':20}})
    assert not host.displayed
    assert host.orchestrator.snapshot()['resources']['display']['owner']=='app:image'
    future,lease=host.motor_handles[0];future.set_result(None);lease.release()
    motion=host.orchestrator.acquire('motion',intent=Intent('manual:dashboard',MANUAL))
    with intent_scope(Intent('conversation',INTERACTIVE)),pytest.raises(ResourceBusy):
        host.execute_action({'drive':{'mode':'turn','angle_deg':360}})
    assert len(host.drives)==1
    motion.release()


def test_thinking_sound_is_a_background_child_not_a_partial_foreground_reservation():
    host=SceneHost();held=[];host.presentation['thinking']=True
    def play(*args,**kwargs):
        lease=host.orchestrator.acquire('audio');held.append(lease)
    host.media.play_sound=play
    parent=Intent('conversation',INTERACTIVE)
    with intent_scope(parent):
        host._play_delayed_thinking_sound(Path('thinking.wav'),None,delay_s=0)
    host._effect_executor.run();lease=held[0]
    assert lease.intent is not parent and lease.intent.priority==BACKGROUND
    with intent_scope(parent):host._stop_transient_thinking_audio()
    assert lease.is_set() and not parent.is_set()
    lease.release();assert not host.orchestrator.snapshot()['resources']


@pytest.mark.parametrize('pose',[None,{},'unavailable',[],{'x_cm':float('nan')}])
def test_unknown_map_pose_is_usable_unknown_attention_evidence(pose):
    value=Attention.evidence({'relative_pose':pose},{},{},[])
    assert value['relative_area']==[None,None]


def test_live_reaction_runs_with_an_unlocalized_map(autonomy_host,tmp_path):
    host,_=ready(autonomy_host,tmp_path)
    host.slam.status=lambda:{'pose':None}
    host._run_heartbeat_turn()
    assert host.outputs and host.outputs[0]['speak']=='Hi there!'


@pytest.mark.parametrize('text',[
    'Can you do a 360, Robbi?','Turn around 360 degrees.',
    'Can you perform a full 360?','Give me a three-sixty, please.',
])
def test_full_rotation_shorthand_keeps_the_entire_angle(text):
    host=MotionDomain();host._optional_float=lambda v:float(v)
    value=host._sanitize_llm_motion({'drive':{'mode':'turn','angle_deg':360}},text)
    assert value['drive']['angle_deg']==360


@pytest.mark.parametrize('text',[
    'Can, can, can you turn towards me?','Can you turn toward me?',
    'Face me, please.','Look at me.',
])
def test_person_facing_request_uses_fresh_vision_and_allows_a_grounded_turn(text):
    assert person_facing_request(text) and VisionDomain()._looks_like_vision_question(text)
    assert classify_vision_skill(text)=='face_person'
    prompt=skill_prompt('face_person',text)
    assert 'fresh' in prompt and 'without driving' in prompt
    host=MotionDomain();host._optional_float=lambda v:float(v)
    result=host._sanitize_llm_motion({'drive':{'mode':'turn','angle_deg':25}},text)
    assert result['drive']['angle_deg']==25


def test_negated_orientation_does_not_trigger_the_camera_shortcut():
    assert not person_facing_request("Don't turn towards me.")


def test_person_facing_camera_followup_uses_fresh_image_and_executes_one_turn():
    class CameraHost(VisionDomain,MotionDomain,SceneHost):pass
    host=CameraHost();host._research_executor=Queue();host._soul_text=''
    calls=[];remembered=[]
    host._register_request(1,host._action_token()[0])
    host.camera=NS(get_llm_image_bytes=lambda **kw:pytest.fail('cached image used'),
                   capture_llm_image_bytes=lambda **kw:b'fresh-image')
    host._update_pipeline_metrics=lambda *args,**kw:None
    host._tools_prompt_context=lambda:''
    host.apps=NS(prompt_catalog=lambda:'')
    host._source_extra_instructions=lambda **kw:''
    host._action_for_source_execution=lambda action,source:action
    host._remember_followup_result=lambda *args:remembered.append(args)
    def ask(prompt,**kw):
        calls.append((prompt,kw));return {'speak':'Turning towards you.','drive':{'mode':'turn','angle_deg':20}}
    host._ask_llm_with_deadline=ask
    text='Can, can, can you turn towards me?'
    spec=host._parse_take_picture_spec({'take_picture':{'prompt':text,'vision_skill':classify_vision_skill(text)}})
    with intent_scope(Intent('conversation',INTERACTIVE)):
        host._start_take_picture_followup(1,text,'speech_input','example','model',[],
                                          '',{},spec)
    host._research_executor.run()
    assert len(calls)==1 and calls[0][1]['image_bytes']==b'fresh-image'
    assert 'face_person' in calls[0][0] and len(host.drives)==1
    assert host.drives[0]['mode']=='turn' and host.drives[0]['angle_deg']==20
    assert remembered[0][1]['drive']['angle_deg']==20
    host._audio_executor.run()
    future,lease=host.motor_handles[0];future.set_result(None);lease.release()
    assert not host.orchestrator.snapshot()['resources']
