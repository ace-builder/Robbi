"""After-hours asset continuity, sound pairing, app integration and capacity."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import ast
import hashlib
import importlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import wave

import numpy as np
import pytest
import yaml

from robbi.app_runtime.host import AppHost
from robbi.expression_catalog import AFTER_HOURS_ANIMATIONS,ALL_MEME_ANIMATIONS,local_expression_request
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia

ROOT=Path(__file__).resolve().parents[1]
NAMES=tuple(AFTER_HOURS_ANIMATIONS)

@pytest.fixture(scope='module')
def renderer():
    import sys
    sys.path.insert(0,str(ROOT/'tools'/'assets'))
    return importlib.import_module('prepare_after_hours')

@pytest.fixture(scope='module')
def catalog():
    return {c['id']:c for c in json.loads((catalog_group(ROOT, 'after_hours')).read_text(encoding='utf-8'))['clips']}

@pytest.mark.parametrize('name',NAMES)
def test_every_frame_is_stable_and_face_free(name,renderer,monkeypatch):
    monkeypatch.setattr(renderer.art,'face',lambda *a,**k:pytest.fail('No Robbi face in this pack'))
    card=renderer.cover(name);header=card.crop((0,0,840,132)).tobytes()
    assert card.size==(840,720)
    unique=set()
    for i in range(120):
        frame=renderer.render(name,i/20)
        assert frame.crop((0,0,840,132)).tobytes()==header
        unique.add(hashlib.sha256(frame.tobytes()).digest())
        if i in (0,1,116,117,118,119):assert frame.tobytes()==card.tobytes()
    assert len(unique)>30  # Readable held punchlines are intentional.

@pytest.mark.parametrize('name',NAMES)
def test_native_assets_audio_and_motion_studio_pair(name,catalog,tmp_path):
    c=catalog[name]
    for field in ('video','audio','sheet'):assert hashlib.sha256((ROOT/c[field]).read_bytes()).hexdigest()==c[field+'_sha256']
    assert (ROOT/c['audio']).read_bytes()==(ROOT/c['sidecar']).read_bytes()
    clip=FaceAnimations(ROOT/'assets/face_animations',None).clip(name)
    assert len(clip.frames)==20 and clip.duration()==pytest.approx(6)
    assert clip.frames[0].tobytes()==clip.frames[-1].tobytes()
    with wave.open(str(ROOT/c['audio'])) as w:
        assert (w.getframerate(),w.getnchannels(),w.getsampwidth(),w.getnframes())==(22050,1,2,132300)
        x=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float)/32768
    assert max(abs(x))<.57 and abs(x.mean())<.0003
    assert not np.any(x[:2205]) and not np.any(x[-4410:])
    assert np.sqrt(np.mean(x[int(3.05*22050):int(4.4*22050)]**2))>.015
    assert 3.02<c['audio_metrics']['voice_end_s']<5.7
    media=MotionMedia(ROOT,tmp_path);entries={r['name']:r for r in media.catalog()}
    assert media.resolve(entries[name+'.mp4']['soundtrack'],'sound')['_path']==ROOT/c['sidecar']

def test_ten_unique_concepts_and_sources(catalog):
    assert len(NAMES)==10 and set(catalog)==set(NAMES)
    assert len({c['audio_sha256'] for c in catalog.values()})==10
    assert len({c['title'] for c in catalog.values()})==10
    assert 'meme-boundary-cactus' not in FaceAnimations(ROOT/'assets/face_animations',None).catalog()
    assert 'meme-urgent-paperboat' not in FaceAnimations(ROOT/'assets/face_animations',None).catalog()
    for c in catalog.values():
        assert not c['contains_robbi_face']
        assert c['cues'][-1]['time_s']==3.02

def test_real_app_and_manifest_agree(tmp_path):
    calls=[]
    robot=SimpleNamespace(capture_token=lambda:None,
        play_video=lambda name,*a,**kw:calls.append(('video',name)) or dict(ok=True,frames_displayed=120,audio_state='completed'),
        face_animation=lambda name,*a,**kw:calls.append(('sprite',name)) or dict(ok=True,displayed=True))
    h=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);h.scan()
    try:
        for name in NAMES:
            for sound in (True,False):assert h.call('expressions','meme',{'name':name,'with_audio':sound})['ok']
        assert calls==[(kind,name) for name in NAMES for kind in ('video','sprite')]
        h.set_enabled('expressions',False);assert local_expression_request(h,'Show the shrimp judge') is None
    finally:h.stop();h._executor.shutdown(wait=True,cancel_futures=True)
    package=yaml.safe_load((ROOT/'apps/expressions/app.yaml').read_text(encoding='utf-8'))
    actual=next(t for t in package['tools'] if t['name']=='meme')
    assert set(actual['input_schema']['properties']['name']['enum'])==set(ALL_MEME_ANIMATIONS)

@pytest.mark.parametrize('name',NAMES)
def test_explicit_voice_route(name):
    h=SimpleNamespace(packages=SimpleNamespace(projects={'expressions':True}),enabled=lambda _:True)
    phrase='Robbi, show the '+name.removeprefix('meme-').replace('-',' ')+' please!'
    assert local_expression_request(h,phrase)=={'app':'expressions','tool':'meme','arguments':{'name':name,'with_audio':True}}

@pytest.mark.parametrize('phrase',['I feel like a cactus','This is urgent','I need to log off','Do not show the shrimp judge','Show the control tea and drive forward'])
def test_context_does_not_trigger_cartoon(phrase):
    h=SimpleNamespace(packages=SimpleNamespace(projects={'expressions':True}),enabled=lambda _:True)
    assert local_expression_request(h,phrase) is None

def test_catalog_above_128_keeps_all_existing_and_new_names(tmp_path):
    for i in range(140):(tmp_path/f'entry-{i:03d}.json').write_text('{}')
    names=FaceAnimations(tmp_path,None).catalog()
    assert len(names)==140 and names[-1]=='entry-139'
    runtime=FaceAnimations(ROOT/'assets/face_animations',None).catalog()
    assert set(runtime)=={p.stem for p in (ROOT/'assets/face_animations').glob('*.json')}
    assert set(NAMES)<=set(runtime)

@pytest.mark.parametrize('name,times',[('meme-shrimp-authority',[2.0]),('meme-tomorrow-vending',[1.3,1.95,2.6]),('meme-hot-take-freezer',[2.85])])
def test_contact_sound_peak_is_on_authored_frame(name,times,renderer,catalog):
    if not (renderer.SOURCE/'audio'/(name+'-accent.mp3')).exists():
        pytest.skip('Optional isolated authoring source not in runtime pack')
    accent,peak=renderer.isolated_accent(name)
    assert peak==np.argmax(abs(accent))
    assert catalog[name]['audio_metrics']['isolated_contact_peaks_s']==times
    for t in times:assert abs(round(t*20)/20-t)<.001
