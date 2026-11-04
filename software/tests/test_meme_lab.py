"""Audiovisual memes: asset pairing, real app routes and silent cancellation."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import numpy as np
from PIL import Image
import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.robot_port import RuntimeRobotPort
from robbi.app_runtime.sdk import AppCancelled
from robbi.expression_catalog import MEME_ANIMATIONS, local_expression_request
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia
from test_expression_polish import clock, player

ROOT=Path(__file__).resolve().parents[1]
CATALOG=json.loads((catalog_group(ROOT, 'meme_lab')).read_text(encoding='utf-8'))
CLIPS={c['id']:c for c in CATALOG['clips']}


@pytest.mark.parametrize('name',MEME_ANIMATIONS)
def test_audio_pairing_timing_and_actual_pcm(name,tmp_path):
    record=CLIPS[name]
    assert record['sound_default'] is True
    data=(ROOT/record['audio']).read_bytes()
    assert hashlib.sha256(data).hexdigest()==record['audio_sha256']
    assert data==(ROOT/record['sidecar']).read_bytes()
    with wave.open(str(ROOT/record['audio']),'rb') as w:
        assert (w.getnchannels(),w.getsampwidth(),w.getframerate())==(1,2,22050)
        assert w.getnframes()==99225
        pcm=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float)/32768
    assert np.max(np.abs(pcm))<.40
    assert abs(pcm.mean())<.0001
    assert np.max(np.abs(pcm[:2205]))==0
    assert np.max(np.abs(pcm[-2205:]))==0
    for cue in record['cues']:
        assert cue['time_s']*20==pytest.approx(cue['video_frame'])
        start=round(cue['time_s']*22050)
        assert np.sqrt(np.mean(pcm[start:start+2205]**2))>.001
    media=MotionMedia(ROOT,tmp_path)
    catalog={item['name']:item for item in media.catalog()}
    entry=catalog[name+'.mp4']
    assert media.resolve(entry['soundtrack'],'sound')['_path']==ROOT/record['sidecar']


@pytest.mark.parametrize('name',MEME_ANIMATIONS)
def test_silent_atlas_has_neutral_endpoints_and_fixed_heading(name):
    faces=FaceAnimations(ROOT/'assets/face_animations',None)
    clip=faces.clip(name)
    assert len(clip.frames)==20
    assert clip.duration(10)==pytest.approx(4.5)
    assert clip.duration(30)==pytest.approx(4.5)
    neutral=faces.frames('natural-blink')[0].tobytes()
    assert clip.frames[0].tobytes()==clip.frames[-1].tobytes()==neutral
    # Test the actual caption region; the enlarging face intentionally moves
    # below it, including the Lanczos fringe at row 34.
    header=clip.frames[2].crop((0,0,280,32)).tobytes()
    assert all(f.crop((0,0,280,32)).tobytes()==header for f in clip.frames[3:17])
    with optional_gif(ROOT/'test-results/meme-lab-preview'/CLIPS[name]['gif']) as gif:
        duration=0
        for n in range(gif.n_frames):
            gif.seek(n);duration+=gif.info['duration']
            assert gif.size==(280,240)
        assert duration==4500


@pytest.mark.parametrize('fps',[10,30])
def test_silent_player_preserves_duration_and_cancels(clock,fps):
    animation,writes,cancel=player(clock)
    result=animation.play('meme-support-brick',fps=fps,cancel_event=cancel)
    assert result['ok'] and result['elapsed_s']==4.5
    assert len(writes)==round(4.5*fps)
    assert writes[-1][1].tobytes()==animation.frames('natural-blink')[0].tobytes()
    animation,writes,cancel=player(clock,cancel_after=12)
    result=animation.play('meme-support-brick',fps=fps,cancel_event=cancel)
    assert result['state']=='cancelled' and len(writes)==12
    assert animation.display.orchestrator.snapshot()['active_leases']==0


def test_real_app_all_seven_with_sound_and_without(tmp_path):
    calls=[]
    robot=SimpleNamespace(capture_token=lambda:None,
        play_video=lambda name,*a,**kw:calls.append(('video',name)) or dict(ok=True,state='completed',frames_displayed=90,audio_state='completed'),
        face_animation=lambda name,*a,**kw:calls.append(('sprite',name)) or dict(ok=True,state='completed',displayed=True))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        for name in MEME_ANIMATIONS:
            assert host.call('expressions','meme',{'name':name})['ok']
            assert host.call('expressions','meme',{'name':name,'with_audio':False})['ok']
        assert calls==[(kind,name) for name in MEME_ANIMATIONS for kind in ('video','sprite')]
        for text,name in [('Show the support brick','meme-support-brick'),('Robbi, play per my last beep please!','meme-last-beep'),('Zeig die Überreaktion','meme-overreaction')]:
            result=local_expression_request(host,text)
            assert result['tool']=='meme' and result['arguments']=={'name':name,'with_audio':True}
        for text in ('I need emotional support','I am having a crisis','Per my last email','That is audacity'):
            assert local_expression_request(host,text) is None
        host.set_enabled('expressions',False)
        assert local_expression_request(host,'Show the support brick') is None
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


@pytest.mark.parametrize('name',MEME_ANIMATIONS)
def test_real_video_port_uses_the_matching_wav_and_cancellation(name):
    calls=[]
    def asset(kind,value,extensions):
        path=ROOT/'assets'/kind/(value+extensions[0])
        return path if path.is_file() else None
    def play(video,audio,**options):
        calls.append((video,audio,options['cancel_event']))
        return dict(ok=True,state='completed',frames_displayed=90,audio_state='completed')
    controller=SimpleNamespace(assets=SimpleNamespace(get=asset),media=SimpleNamespace(
        health=lambda:{'display':{'available':True}},play_video=play))
    port=RuntimeRobotPort(SimpleNamespace(controller=lambda:controller))
    cancelled=threading.Event()
    result=port.play_video(name,None,cancelled.is_set)
    assert result['ok']
    assert calls[0][0]==ROOT/CLIPS[name]['video']
    assert calls[0][1]==ROOT/CLIPS[name]['audio']
    cancelled.set()
    with pytest.raises(AppCancelled):port.play_video(name,None,cancelled.is_set)
    assert len(calls)==1


@pytest.mark.parametrize('state',['partial','cancelled','unavailable'])
def test_meme_app_does_not_claim_a_failed_soundtrack_played(tmp_path,state):
    robot=SimpleNamespace(capture_token=lambda:None,play_video=lambda *a,**kw:
        dict(ok=False,state=state,frames_displayed=12,reason='Soundtrack '+state))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        result=host.call('expressions','meme',{'name':'meme-support-brick'})
        assert not result['ok']
        assert 'Soundtrack '+state in result['text']
        assert not result['data'].get('display_hold_s')
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


def test_requested_sound_is_not_silently_omitted_when_wav_is_missing(tmp_path):
    robot=SimpleNamespace(capture_token=lambda:None,play_video=lambda *a,**kw:
        dict(ok=True,state='completed',frames_displayed=90,audio_state='not_requested'))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        result=host.call('expressions','meme',{'name':'meme-support-brick'})
        assert not result['ok'] and result['data']['state']=='partial'
        assert 'soundtrack did not complete' in result['text']
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


def optional_gif(path):
    if not path.exists():
        pytest.skip('Optional authoring preview absent; preceding runtime sprite checks passed')
    return Image.open(path)
