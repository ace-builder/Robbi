"""Contracts for ten six-second object memes with generated speech and effects."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import importlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave
import numpy as np
import pytest
import yaml
from PIL import Image
from robbi.app_runtime.host import AppHost
from robbi.expression_catalog import MEME_ANIMATIONS,MEME_ENCORE_ANIMATIONS,ALL_MEME_ANIMATIONS,local_expression_request
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia

ROOT=Path(__file__).resolve().parents[1]
NAMES=tuple(MEME_ENCORE_ANIMATIONS)


@pytest.fixture(scope='module')
def catalog():return {c['id']:c for c in json.loads((catalog_group(ROOT, 'meme_encore')).read_text())['clips']}


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'tools'/'assets'))
    return importlib.import_module('prepare_meme_encore')


@pytest.mark.parametrize('name',NAMES)
def test_every_frame_is_face_free_with_stable_title_and_readable_punchline(name,renderer,monkeypatch):
    def forbidden(*a,**kw):raise AssertionError('Meme encore may not load a robot face')
    for helper in ('face','face_badge','head'):monkeypatch.setattr(renderer.art,helper,forbidden)
    monkeypatch.setattr(renderer.art.PLAYER,'frames',forbidden)
    renderer.cover.cache_clear()
    card=renderer.cover(name);header=card.crop((0,0,840,120)).tobytes();motion=set()
    for n in range(180):
        frame=renderer.render(name,n/30)
        assert frame.size==(840,720) and frame.mode=='RGB'
        assert frame.crop((0,0,840,120)).tobytes()==header
        if n in (0,1,174,179):assert frame.tobytes()==card.tobytes()
        if 10<n<100:motion.add(hashlib.sha256(frame.tobytes()).hexdigest())
    assert len(motion)>40
    clip=FaceAnimations(ROOT/'assets/face_animations',None).clip(name)
    assert len(clip.frames)==20 and clip.duration(10)==clip.duration(30)==6
    assert clip.frames[0].tobytes()==clip.frames[-1].tobytes()


@pytest.mark.parametrize('name',NAMES)
def test_native_audio_voice_timing_and_runtime_pairing(name,catalog,tmp_path):
    clip=catalog[name];path=ROOT/clip['audio']
    assert path.read_bytes()==(ROOT/clip['sidecar']).read_bytes()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==clip['audio_sha256']
    with wave.open(str(path),'rb') as w:
        assert (w.getframerate(),w.getnchannels(),w.getsampwidth(),w.getnframes())==(22050,1,2,132300)
        pcm=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float)/32768
    assert abs(pcm).max()<.57 and abs(pcm.mean())<.0002
    assert not np.any(pcm[:2205]) and not np.any(pcm[-4410:])
    assert np.sqrt(np.mean(pcm[7000:60000]**2))>.01
    assert np.sqrt(np.mean(pcm[68000:100000]**2))>.015
    assert 3.02<clip['audio_metrics']['voice_end_s']<5.7
    media=MotionMedia(ROOT,tmp_path);entries={c['name']:c for c in media.catalog()}
    assert media.resolve(entries[name+'.mp4']['soundtrack'],'sound')['_path']==ROOT/clip['sidecar']


def test_app_accepts_every_new_meme_with_sound_or_sprites(tmp_path):
    calls=[]
    robot=SimpleNamespace(capture_token=lambda:None,
        play_video=lambda name,*a,**kw:calls.append(('video',name,kw)) or dict(ok=True,state='completed',frames_displayed=120,audio_state='completed'),
        face_animation=lambda name,*a,**kw:calls.append(('sprite',name,kw)) or dict(ok=True,state='completed',displayed=True))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        for name in NAMES:
            for sound in (True,False):assert host.call('expressions','meme',{'name':name,'with_audio':sound})['ok']
        assert [(kind,name) for kind,name,_ in calls]==[(kind,name) for name in NAMES for kind in ('video','sprite')]
        assert all(kw=={'fps':10,'loops':1} for kind,_,kw in calls if kind=='sprite')
    finally:host.stop();host._executor.shutdown(wait=True,cancel_futures=True)
    package=yaml.safe_load((ROOT/'apps/expressions/app.yaml').read_text())
    tool=next(t for t in package['tools'] if t['name']=='meme')
    assert set(tool['input_schema']['properties']['name']['enum'])==set(ALL_MEME_ANIMATIONS)
    assert set(MEME_ANIMATIONS)|set(NAMES) <= set(ALL_MEME_ANIMATIONS)
    assert len(NAMES)==10 and set(NAMES)<=set(FaceAnimations(ROOT/'assets/face_animations',None).catalog())


@pytest.mark.parametrize('name',NAMES)
def test_explicit_voice_shortcuts(name):
    host=SimpleNamespace(packages=SimpleNamespace(projects={'expressions':True}),enabled=lambda _:True)
    phrase='Robbi, show the '+name.removeprefix('meme-').replace('-',' ')+' please!'
    assert local_expression_request(host,phrase)=={'app':'expressions','tool':'meme','arguments':{'name':name,'with_audio':True}}


@pytest.mark.parametrize('phrase',['I am overthinking','I have a deadline','I cannot cope','Do not show the brain tabs','Show the couch quest and drive forward'])
def test_ordinary_context_or_mixed_requests_do_not_start_memes(phrase):
    host=SimpleNamespace(packages=SimpleNamespace(projects={'expressions':True}),enabled=lambda _:True)
    assert local_expression_request(host,phrase) is None
