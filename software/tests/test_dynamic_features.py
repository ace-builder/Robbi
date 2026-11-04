"""Offline acceptance of the new games, motion envelope and LCD playback."""
from concurrent.futures import Future
from contextlib import contextmanager
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.robot_port import RuntimeRobotPort
from robbi.dynamics import DynamicBehavior, GESTURES, PickupDetector, advance, inside_envelope, plan, wheel_distances
from robbi.face_animation import FaceAnimations
from robbi.mini_games import local_game_request, position
from app_project_testing import source_module

best_move = source_module("tic-tac-toe").best_move
winner = source_module("tic-tac-toe").winner
from robbi.orchestration import Orchestrator
from robbi.errors import ValidationError
from robot.reminder import ReminderManager

ROOT=Path(__file__).resolve().parents[1]


class Robot:
    def __init__(self):
        self.images=[]
        self.sounds=[]
    def capture_token(self): return None
    def show_image(self,path,*args,**kwargs):
        with Image.open(path) as image:
            assert image.size==(280,240)
        self.images.append(path)
        return {'displayed':True}
    def play_sound(self,name,*args):
        self.sounds.append(name)
        return {'ok':True}
    @contextmanager
    def session(self,*args,**kwargs): yield


@pytest.fixture
def host(tmp_path):
    result=AppHost(ROOT,tmp_path,Robot(),permissions_enabled=False)
    result.scan()
    yield result
    result.stop()
    result._executor.shutdown(wait=True,cancel_futures=True)


def state(host,app_id,**fields):
    path=host.directory/'data'/app_id/'state.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    data=json.loads(path.read_text()) if path.exists() else {}
    data.update(fields)
    path.write_text(json.dumps(data))
    return data


def test_all_new_apps_are_discoverable_and_in_model_catalog(host):
    assert not host.packages.errors
    advertised = {json.loads(line)['app'] for line in host.prompt_catalog().splitlines()
                  if line.startswith('{')}
    for game in ('tic-tac-toe','memory-colours','higher-lower','expressions','checklists','decision-helper','kitchen-timers'):
        assert host.enabled(game)
        assert game in advertised


@pytest.mark.parametrize('spoken,index',[('top right',2),('Put it in the center!',4),('unten links',6),('nine',8),('top center',1),('middle right',5)])
def test_spoken_square(spoken,index):
    assert position(spoken)==index


def test_tictactoe_rejects_taken_square_and_celebrates_user_win(host):
    assert host.call('tic-tac-toe','play',{'action':'start'})['ok']
    state(host,'tic-tac-toe',board=['X','X','','O','O','','','',''],moves=2)
    before=state(host,'tic-tac-toe')['board']
    bad=host.call('tic-tac-toe','play',{'answer':'top left'})
    assert not bad['data']['accepted']
    assert state(host,'tic-tac-toe')['board']==before
    win=host.call('tic-tac-toe','play',{'answer':'top right'})
    assert win['ok'] and win['data']['winner']=='X' and not win['data']['active']
    assert win['data']['displayed'] and win['artifacts']
    assert 'game_victory' in host.robot.sounds


def test_robot_wins_and_draws_are_different(host):
    host.call('tic-tac-toe','play',{'action':'start'})
    state(host,'tic-tac-toe',board=['O','O','','X','','','','X',''],moves=2)
    win=host.call('tic-tac-toe','play',{'answer':'bottom right'})
    assert win['data']['winner']=='O'
    state(host,'tic-tac-toe',active=True,board=['X','O','X','X','O','O','O','X',''],moves=4)
    draw=host.call('tic-tac-toe','play',{'answer':'bottom right'})
    assert draw['data']['winner']=='draw'


def test_perfect_robot_never_loses_across_all_human_branches():
    # Explore all human moves against the robot policy at every reachable turn.
    def walk(board):
        if winner(board):
            assert winner(board)!='X'
            return
        for index,value in enumerate(board):
            if not value:
                human=board[:]
                human[index]='X'
                if winner(human):
                    assert winner(human)=='draw'
                    continue
                robot=best_move(human)
                human[robot]='O'
                walk(human)
    walk(['']*9)


def test_number_game_hides_secret_and_uses_true_bounds(host):
    start=host.call('higher-lower','play',{'action':'start'})
    assert 'secret' not in start['data']
    state(host,'higher-lower',secret=12)
    assert host.call('higher-lower','play',{'answer':'five'})['text']=='Higher!'
    assert host.call('higher-lower','play',{'answer':'sixteen'})['text']=='Lower!'
    assert not host.call('higher-lower','play',{'answer':'two'})['data']['accepted']
    assert host.call('higher-lower','play',{'answer':'twelve'})['data']['winner']=='user'


def test_memory_partial_sequence_incorrect_move_and_win(host):
    state(host,'memory-colours',active=True,game='memory-colours',moves=0,sequence=['red','blue'],index=0)
    host.configure('memory-colours',{'rounds':2})
    partial=host.call('memory-colours','play',{'answer':'red'})
    assert partial['ok'] and partial['data']['index']==1 and 'sequence' not in partial['data']
    won=host.call('memory-colours','play',{'answer':'blue'})
    assert won['data']['winner']=='user'
    state(host,'memory-colours',active=True,game='memory-colours',sequence=['green'],index=0)
    assert not host.call('memory-colours','play',{'answer':'blue'})['data']['active']


def test_local_voice_routes_only_latest_game_and_respects_disabled_apps(host):
    assert local_game_request(host,'play tic tac toe')['arguments']['action']=='start'
    host.call('tic-tac-toe','play',{'action':'start'})
    assert local_game_request(host,'top right')['app']=='tic-tac-toe'
    assert local_game_request(host,'What is the weather?') is None
    host.call('higher-lower','play',{'action':'start'})
    assert local_game_request(host,'my guess is twelve')['arguments']['answer']=='12'
    host.call('higher-lower','play',{'action':'stop'})
    assert local_game_request(host,'top right') is None
    host.set_enabled('tic-tac-toe',False)
    assert local_game_request(host,'play tic tac toe') is None


def test_permissions_precede_game_state_and_screen(tmp_path):
    host=AppHost(ROOT,tmp_path,Robot())
    try:
        host.scan()
        denied=host.call('tic-tac-toe','play',{'action':'start'})
        assert denied['permission_required']
        assert not host.robot.images
        assert not (host.directory/'data/tic-tac-toe/state.json').exists()
    finally:
        host.stop()


def test_checklists_persist_and_reject_accidental_overwrite(host):
    create={'action':'create','name':'Travel','items':['Passport','Charger']}
    assert host.call('checklists','manage',create)['ok']
    assert not host.call('checklists','manage',create)['ok']
    checked=host.call('checklists','manage',{'action':'check','name':'Travel','item':1})
    assert checked['data']['next']=='Charger'
    host.scan()
    assert host.call('checklists','manage',{'action':'show','name':'Travel'})['data']['items'][0]['done']
    assert not host.call('checklists','manage',{'action':'check','name':'Travel','item':9})['ok']


def test_decisions_stay_within_requested_choices(host):
    dice=host.call('decision-helper','decide',{'action':'dice','count':10,'sides':6})
    assert len(dice['data']['values'])==10 and all(1<=n<=6 for n in dice['data']['values'])
    choices=['Pizza','Pasta','Soup']
    result=host.call('decision-helper','decide',{'action':'shuffle','choices':choices})
    assert sorted(result['data']['values'])==sorted(choices)
    assert not host.call('decision-helper','decide',{'action':'choose'})['ok']


@pytest.mark.parametrize('name',GESTURES)
def test_gestures_return_to_origin_inside_requested_radius(name):
    for base in (8,10.2,14):
        pose=(0,0,0)
        steps=plan(name)
        assert inside_envelope(pose,steps,base,16.01)
        for step in steps:
            pose=advance(pose,*wheel_distances(step,base),base)
        assert math.hypot(*pose[:2])<1e-7
        assert abs(math.remainder(pose[2],2*math.pi))<1e-7
    assert not inside_envelope((19,0,0),[{'mode':'distance','distance_cm':3}],10.2)


def sample(acceleration=(0,0,9.80665),fresh=True,gyro=(0,0,0)):
    return {'fresh':fresh,'acceleration_m_s2':acceleration,'gyro_deg_s':gyro}


def test_pickup_requires_settling_debounces_and_latches_until_put_down():
    detector=PickupDetector()
    detector.update(sample(),now=0)
    detector.update(sample(),now=2.1)
    assert detector.baseline is not None
    tilt=sample((7,0,7))
    assert detector.update(tilt,now=3) is None
    assert detector.update(tilt,now=3.2)=='picked_up'
    assert detector.update(tilt,now=3.4) is None
    detector.update(sample(fresh=False),now=4)
    assert detector.picked_up
    detector.update(sample(),now=5)
    assert detector.update(sample(),now=7.1)=='put_down'


def test_moving_acceleration_does_not_trigger_pickup():
    detector=PickupDetector()
    detector.update(sample(),now=0)
    detector.update(sample(),now=2.1)
    for now in (3,3.2,3.4):
        assert detector.update(sample((0,0,15),gyro=(0,0,50)),moving=True,now=now) is None
    assert not detector.picked_up


def test_sprite_cache_exact_frames_and_real_cancellation():
    from robot.media import Display
    screen=Display(Orchestrator())
    cancel=threading.Event()
    writes=[]
    def write(image):
        writes.append(image)
        if len(writes)==3:
            cancel.set()
    screen._disp=SimpleNamespace(display=write)
    player=FaceAnimations(ROOT/'assets/face_animations',screen)
    frames=player.frames('happy-blink')
    assert len(frames)==10 and all(image.size==(280,240) for image in frames)
    assert player.frames('happy-blink') is frames
    result=player.play('happy-blink',fps=30,cancel_event=cancel)
    assert result['state']=='cancelled' and len(writes)==3
    assert screen.orchestrator.snapshot()['active_leases']==0
    with pytest.raises(ValidationError): player.frames('../escape')


def test_sprite_slow_display_skips_frames_instead_of_queueing():
    def write(*args,**kwargs):
        time.sleep(.065)
        return True
    player=FaceAnimations(ROOT/'assets/face_animations',SimpleNamespace(orchestrator=None,show_pil_frame=write))
    result=player.play('happy-blink',fps=30)
    assert result['ok'] and result['skipped']>0 and result['actual_fps']<20
    assert result['elapsed_s'] < result['duration_s'] + .25


def test_timer_cancel_unknown_id_never_cancels_another_timer(tmp_path):
    manager=ReminderManager(tmp_path/'reminders.json',lambda *a:None,lambda:True)
    one=manager.add('[Kitchen] Tea timer is done.',60)
    two=manager.add('[Kitchen] Pasta timer is done.',600)
    assert one.id!=two.id
    assert manager.cancel('missing') is None
    assert manager.status()['active_count']==2
    reloaded=ReminderManager(tmp_path/'reminders.json',lambda *a:None,lambda:True)
    assert reloaded.status()['active_count']==2
    assert reloaded.cancel(one.id).id==one.id


def test_kitchen_sdk_never_cancels_non_kitchen_reminders(tmp_path):
    manager=ReminderManager(tmp_path/'reminders.json',lambda *a:None,lambda:True)
    ordinary=manager.add('Call someone',60)
    port=RuntimeRobotPort(SimpleNamespace(controller=lambda:SimpleNamespace(reminders=manager)))
    timer=port.timer('start',{'label':'Tea','seconds':60},None,lambda:False)
    assert timer['ok']
    assert not port.timer('cancel',{'id':ordinary.id},None,lambda:False)['ok']
    assert len(port.timer('list',{},None,lambda:False)['data']['timers'])==1


class MeasuredMotion:
    def __init__(self):
        self.commands=[]
        self.stops=[]
        self.last={}
        self.available=True
        self.on_move=lambda:None
    def status(self):
        return {'available':self.available,'encoder_enabled':True,'config':{'wheel_base_cm':10.2},
                'obstacle':{'enabled':True,'blocked':False},'last_result':self.last}
    def is_running(self): return False
    def cm_per_count(self): return .01
    def stop(self,**kwargs): self.stops.append(kwargs)
    def drive(self,step,**kwargs):
        self.commands.append(step)
        left,right=wheel_distances(step,10.2)
        self.last={'ok':True,'final':{'left':left/.01,'right':right/.01}}
        self.on_move()
        future=Future()
        future.set_result(None)  # The current motor driver reports through status.
        return future


def dynamic_controller():
    motion=MeasuredMotion()
    audio=[]
    screen=SimpleNamespace(orchestrator=None,show_pil_frame=lambda *a,**k:True)
    media=SimpleNamespace(display=screen,show_face=lambda *a,**k:True,
                          play_sound=lambda *a,**k:audio.append('play') or object(),
                          stop_audio=lambda **k:audio.append(('stop',k)))
    controller=SimpleNamespace(motion=motion,media=media,orchestrator=Orchestrator(),
                               imu=SimpleNamespace(status=lambda:{'fresh':True}))
    dynamics=DynamicBehavior(controller,ROOT/'assets')
    dynamics.pickup.baseline=[0,0,1]
    return dynamics,motion,audio


def test_gesture_uses_measured_receipts_and_releases_only_its_outputs():
    dynamics,motion,audio=dynamic_controller()
    result=dynamics.play('happy-dance')
    assert result['ok'] and result['completed_steps']==len(plan('happy-dance'))
    assert result['estimated_offset_cm']<.01
    assert motion.stops[-1]['owner'] is not None
    assert audio[0]=='play' and audio[-1][0]=='stop'
    assert dynamics.controller.orchestrator.snapshot()['active_leases']==0


def test_pickup_during_dance_cancels_remaining_motion_and_audio():
    dynamics,motion,audio=dynamic_controller()
    def lift():
        if len(motion.commands)==2:
            dynamics.pickup.picked_up=True
    motion.on_move=lift
    result=dynamics.play('happy-dance')
    assert not result['ok'] and result['state']=='cancelled'
    assert len(motion.commands)==2 and audio[-1][0]=='stop'
    assert motion.stops and dynamics.last_result==result


def test_disabled_motors_and_stale_imu_never_start_dance_outputs():
    dynamics,motion,audio=dynamic_controller()
    motion.available=False
    assert not dynamics.play('shuffle')['ok']
    assert not motion.commands and not audio
    motion.available=True
    dynamics.controller.imu.status=lambda:{'fresh':False}
    assert not dynamics.play('shuffle')['ok']
    assert not motion.commands and not audio


def test_missing_encoder_receipt_and_excess_drift_stop_choreography():
    dynamics,motion,audio=dynamic_controller()
    motion.on_move=lambda:motion.last.update(final=None)
    assert not dynamics.play('shuffle')['ok']
    assert len(motion.commands)==1
    dynamics,motion,audio=dynamic_controller()
    motion.on_move=lambda:motion.last.update(final={'left':1900,'right':1900})
    assert not dynamics.play('shuffle')['ok']
    assert len(motion.commands)==1
