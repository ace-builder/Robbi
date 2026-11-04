"""Sound-enabled expression routing, interruption and matching runtime assets."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.sdk import AppCancelled
from robbi.expression_catalog import FACE_EXPRESSIONS, SCREEN_GAGS
from robbi.motion_media import MotionMedia

ROOT=Path(__file__).resolve().parents[1]
NAMES={**FACE_EXPRESSIONS,**SCREEN_GAGS}

@pytest.fixture
def host(tmp_path):
    calls=[]
    def video(name,*a,**kw):
        calls.append(('video',name,kw));return {'ok':True,'state':'completed','frames_displayed':30,'audio_state':'completed'}
    def face(name,*a,**kw):
        calls.append(('face',name,kw));return {'ok':True,'state':'completed','displayed':True}
    h=AppHost(ROOT,tmp_path,SimpleNamespace(capture_token=lambda:None,play_video=video,face_animation=face),permissions_enabled=False)
    h.scan()
    yield h,calls
    h.stop();h._executor.shutdown(wait=True,cancel_futures=True)

@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('with_audio',[True,False])
def test_all_missing_sound_animations_route_in_real_app(host,name,with_audio):
    h,calls=host;tool='gag' if name in SCREEN_GAGS else 'animate'
    result=h.call('expressions',tool,{'name':name,'with_audio':with_audio,'loops':2,'fps':30})
    assert result['ok'],result
    if with_audio:
        assert [(kind,n) for kind,n,_ in calls]==[('video',name)]*2
    else:
        assert len(calls)==1 and calls[0][0]=='face'
        assert calls[0][2]['fps']==30 and calls[0][2]['loops']==2

@pytest.mark.parametrize('tool,name',[('animate','skeptical-look'),('gag','gag-bonk')])
def test_sound_is_default_for_explicit_app_playback(host,tool,name):
    h,calls=host;assert h.call('expressions',tool,{'name':name})['ok']
    assert calls[0][0]=='video'

def app():
    spec=importlib.util.spec_from_file_location('sound_test_expressions',ROOT/'apps/expressions/main.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

@pytest.mark.parametrize('audio_state',['unavailable','interrupted','cancelled'])
def test_incomplete_audio_stops_repeats_and_reports_partial(audio_state):
    calls=[]
    def play(name):
        calls.append(name);return {'ok':True,'frames_displayed':12,'audio_state':audio_state}
    context=SimpleNamespace(checkpoint=lambda:None,robot=SimpleNamespace(play_video=play))
    result=app().run(context,'gag',{'name':'gag-bonk','loops':3})
    assert not result['ok'] and result['data']['state']=='partial'
    assert result['data']['loops_completed']==0 and len(calls)==1

def test_cancellation_between_repeats_never_starts_next_sound():
    calls=[]
    def checkpoint():
        if calls:raise AppCancelled('Stopped')
    def play(name):
        calls.append(name);return {'ok':True,'frames_displayed':30,'audio_state':'completed'}
    context=SimpleNamespace(checkpoint=checkpoint,robot=SimpleNamespace(play_video=play))
    with pytest.raises(AppCancelled):app().run(context,'animate',{'name':'happy-blink','loops':3})
    assert calls==['happy-blink']

def test_all_sound_files_match_motion_studio_and_provider_records(tmp_path):
    clips=json.loads((catalog_group(ROOT, 'animation_sound')).read_text(encoding='utf-8'))['clips']
    assert {c['id'] for c in clips}==set(NAMES)
    media=MotionMedia(ROOT,tmp_path);library={c['name']:c for c in media.catalog()}
    for c in clips:
        video=library[c['id']+'.mp4'];sound=media.resolve(video['soundtrack'],'sound')['_path']
        assert sound.read_bytes()==(ROOT/c['sound']).read_bytes()
        with wave.open(str(sound)) as w:assert w.getnframes()/w.getframerate()==pytest.approx(c['duration_s'],abs=1/22050)

def test_no_sound_output_before_audio_permission(tmp_path):
    robot=SimpleNamespace(capture_token=lambda:None,play_video=lambda *a,**k:pytest.fail('Output before permission'))
    h=AppHost(ROOT,tmp_path,robot);h.scan()
    try:
        result=h.call('expressions','gag',{'name':'gag-bonk'})
        assert result['permission_required']
    finally:h.stop();h._executor.shutdown(wait=True,cancel_futures=True)
