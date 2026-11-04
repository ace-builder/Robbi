"""Postcards have no robot-face frames, including their loop endpoints."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
from PIL import Image
import pytest

from robbi.app_runtime.host import AppHost
from robbi.errors import ValidationError
from robbi.expression_catalog import POSTCARD_ANIMATIONS, local_expression_request
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia
from test_expression_polish import clock, player

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'test-results/pocket-postcards-preview'
CLIPS={c['id']:c for c in json.loads((catalog_group(ROOT, 'pocket_postcards')).read_text(encoding='utf-8'))['clips']}


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'tools'/'assets'))
    return importlib.import_module('prepare_pocket_postcards')


@pytest.mark.parametrize('name',POSTCARD_ANIMATIONS)
def test_all_ninety_frames_render_without_access_to_robot_face_art(name,renderer,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('A postcard must not request any Robbi face artwork')
    for helper in ('face','face_badge','head'):
        monkeypatch.setattr(renderer.art,helper,forbidden)
    monkeypatch.setattr(renderer.art.PLAYER,'frames',forbidden)
    for n in range(90):
        frame=renderer.render(name,n/20)
        assert frame.size==(840,720)
    first=renderer.render(name,0).tobytes()
    assert first==renderer.render(name,4.45).tobytes()
    assert CLIPS[name]['contains_robbi_face'] is False
    assert CLIPS[name]['bookend']=='typographic-postcard'


@pytest.mark.parametrize('name',POSTCARD_ANIMATIONS)
def test_audio_video_and_motion_studio_pairing(name,tmp_path):
    c=CLIPS[name]
    assert (c['frames'],c['fps'],c['duration_s'])==(90,20,4.5)
    for field in ('video','audio','sheet'):
        assert hashlib.sha256((ROOT/c[field]).read_bytes()).hexdigest()==c[field+'_sha256']
    assert (ROOT/c['audio']).read_bytes()==(ROOT/c['sidecar']).read_bytes()
    with wave.open(str(ROOT/c['audio']),'rb') as w:
        assert (w.getnchannels(),w.getsampwidth(),w.getframerate(),w.getnframes())==(1,2,22050,99225)
        pcm=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float)/32768
    assert np.max(np.abs(pcm))<.36 and abs(pcm.mean())<.0001
    assert not np.any(pcm[:2205]) and not np.any(pcm[-2205:])
    assert c['audio_metrics']['clipped_samples']==0
    assert -32<c['audio_metrics']['lufs']<=-24.5
    for cue in c['cues']:
        assert cue['time_s']*20==pytest.approx(cue['video_frame'])
        start=round(cue['time_s']*22050)
        assert np.sqrt(np.mean(pcm[start:start+2205]**2))>.001
    media=MotionMedia(ROOT,tmp_path)
    entry=next(v for v in media.catalog() if v['name']==name+'.mp4')
    assert media.resolve(entry['soundtrack'],'sound')['_path']==ROOT/c['sidecar']


@pytest.mark.parametrize('name',POSTCARD_ANIMATIONS)
def test_text_card_endpoints_fixed_headings_and_gif_timing(name,renderer):
    faces=FaceAnimations(ROOT/'assets/face_animations',None)
    clip=faces.clip(name)
    assert len(clip.frames)==20
    assert clip.duration(10)==clip.duration(30)==pytest.approx(4.5)
    card=renderer.cover(name).resize((280,240),Image.Resampling.LANCZOS).convert('RGB')
    assert clip.frames[0].tobytes()==clip.frames[-1].tobytes()==card.tobytes()
    assert clip.frames[0].tobytes()!=faces.frames('natural-blink')[0].tobytes()
    heading=clip.frames[0].crop((0,0,280,38)).tobytes()
    assert all(f.crop((0,0,280,38)).tobytes()==heading for f in clip.frames)
    with optional_gif(OUT/CLIPS[name]['gif']) as gif:
        total=fast_frames=0
        assert gif.info['loop']==0 and 20<gif.n_frames<=90
        for n in range(gif.n_frames):
            gif.seek(n)
            assert gif.size==(280,240) and gif.info['duration']%50==0
            total+=gif.info['duration'];fast_frames+=gif.info['duration']==50
        assert total==4500 and fast_frames>=20


@pytest.mark.parametrize('name',POSTCARD_ANIMATIONS)
def test_silent_playback_preserves_text_card_and_cancels(clock,name):
    animation,writes,cancel=player(clock)
    result=animation.play(name,fps=10,cancel_event=cancel)
    assert result['ok'] and result['elapsed_s']==4.5 and len(writes)==45
    assert writes[-1][1].tobytes()==animation.frames(name)[-1].tobytes()
    animation,writes,cancel=player(clock,cancel_after=12)
    result=animation.play(name,fps=10,cancel_event=cancel)
    assert result['state']=='cancelled' and len(writes)==12
    assert animation.display.orchestrator.snapshot()['active_leases']==0


def test_real_postcard_app_all_six_and_narrow_voice_routes(tmp_path):
    calls=[]
    robot=SimpleNamespace(capture_token=lambda:None,
        play_video=lambda name,*a,**kw:calls.append(('video',name)) or dict(ok=True,state='completed',frames_displayed=90,audio_state='completed'),
        face_animation=lambda name,*a,**kw:calls.append(('sprite',name)) or dict(ok=True,state='completed',displayed=True))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        for name in POSTCARD_ANIMATIONS:
            assert host.call('expressions','postcard',{'name':name})['ok']
            assert host.call('expressions','postcard',{'name':name,'with_audio':False})['ok']
        assert calls==[(kind,name) for name in POSTCARD_ANIMATIONS for kind in ('video','sprite')]
        for phrase,name in [('Robbi, show pocket sunshine please!','post-sunshine'),
                           ('Show a cup of sorry','post-peace-tea'),('Zeig die kleine Schnecke','post-slow-club'),
                           ('Show the heart mail','post-heart-mail'),('Show return to sender','post-return-guilt'),
                           ('Show the soft landing','post-soft-landing')]:
            assert local_expression_request(host,phrase)=={'app':'expressions','tool':'postcard','arguments':{'name':name,'with_audio':True}}
        for phrase in ('I am sorry','I feel guilty','Return my package to sender','Make me tea','I am having a rough day'):
            assert local_expression_request(host,phrase) is None
        with pytest.raises(ValidationError,match='declared choices'):
            host.call('expressions','postcard',{'name':'sweet-flower'})
        host.set_enabled('expressions',False)
        assert local_expression_request(host,'Show pocket sunshine') is None
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


@pytest.mark.parametrize('audio_state',['not_requested','cancelled','unavailable'])
def test_postcard_reports_incomplete_soundtrack(tmp_path,audio_state):
    robot=SimpleNamespace(capture_token=lambda:None,play_video=lambda *a,**kw:
        dict(ok=True,state='completed',frames_displayed=90,audio_state=audio_state))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        result=host.call('expressions','postcard',{'name':'post-sunshine'})
        assert not result['ok'] and result['data']['state']=='partial'
        assert 'soundtrack did not complete' in result['text']
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


def optional_gif(path):
    if not path.exists():
        pytest.skip('Optional authoring preview absent; preceding runtime sprite checks passed')
    return Image.open(path)
