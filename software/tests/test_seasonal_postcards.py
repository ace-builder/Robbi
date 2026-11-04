"""Postcards have no robot-face frames, including their loop endpoints."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import hashlib
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
from PIL import Image
import pytest

from robbi.app_runtime.host import AppHost
from robbi.errors import ValidationError
from robbi.expression_catalog import SEASONAL_ANIMATIONS, ADVENT_ANIMATIONS, local_expression_request
NEW_ANIMATIONS = {**SEASONAL_ANIMATIONS, **ADVENT_ANIMATIONS}
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia
from test_expression_polish import clock, player

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'test-results/seasonal-postcards-preview'
CLIPS={c['id']:c for c in json.loads((catalog_group(ROOT, 'seasonal_postcards')).read_text(encoding='utf-8'))['clips']}


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'tools'/'assets'))
    return importlib.import_module('prepare_seasonal_postcards')


@pytest.mark.parametrize('name',NEW_ANIMATIONS)
def test_all_ninety_frames_render_without_access_to_robot_face_art(name,renderer,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('A postcard must not request any Robbi face artwork')
    for helper in ('face','face_badge','head'):
        monkeypatch.setattr(renderer.c.art,helper,forbidden)
    monkeypatch.setattr(renderer.c.art.PLAYER,'frames',forbidden)
    for n in range(90):
        frame=renderer.render(name,n/20)
        assert frame.size==(840,720)
    first=renderer.render(name,0).tobytes()
    assert first==renderer.render(name,4.45).tobytes()
    assert CLIPS[name]['contains_robbi_face'] is False
    assert CLIPS[name]['bookend']=='typographic-postcard'


@pytest.mark.parametrize('name',NEW_ANIMATIONS)
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
        # Audio cues use the sample clock; their annotated video frame is the
        # nearest 20 fps frame, at most 25 ms away (e.g. a 1.42 s puff).
        assert abs(cue['time_s']-cue['video_frame']/c['fps'])<=.5/c['fps']+1e-9
        start=round(cue['time_s']*22050)
        assert np.sqrt(np.mean(pcm[start:start+2205]**2))>.001
    media=MotionMedia(ROOT,tmp_path)
    entry=next(v for v in media.catalog() if v['name']==name+'.mp4')
    assert media.resolve(entry['soundtrack'],'sound')['_path']==ROOT/c['sidecar']


@pytest.mark.parametrize('name',NEW_ANIMATIONS)
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


@pytest.mark.parametrize('name',NEW_ANIMATIONS)
def test_silent_playback_preserves_text_card_and_cancels(clock,name):
    animation,writes,cancel=player(clock)
    result=animation.play(name,fps=10,cancel_event=cancel)
    assert result['ok'] and result['elapsed_s']==4.5 and len(writes)==45
    assert writes[-1][1].tobytes()==animation.frames(name)[-1].tobytes()
    animation,writes,cancel=player(clock,cancel_after=12)
    result=animation.play(name,fps=10,cancel_event=cancel)
    assert result['state']=='cancelled' and len(writes)==12
    assert animation.display.orchestrator.snapshot()['active_leases']==0


def test_all_requested_occasions_and_every_december_day_are_distinct():
    assert set(CLIPS)==set(NEW_ANIMATIONS)
    assert len(SEASONAL_ANIMATIONS)==9 and len(ADVENT_ANIMATIONS)==24
    assert {c['day'] for c in CLIPS.values() if c['group']=='advent'}==set(range(1,25))
    assert len({c['video_sha256'] for c in CLIPS.values()})==33
    assert len({c['audio_sha256'] for c in CLIPS.values()})==33
    assert len({c['title'] for c in CLIPS.values()})==33


def test_chord_keeps_all_notes_present_instead_of_gliding_between_them(renderer):
    notes=[440,554.37,659.25]
    data,_=renderer.c.synthesize('chord-regression',[
        dict(at=.5,kind='chord',hz=notes,duration=1.0,gain=.2)])
    with wave.open(io.BytesIO(data),'rb') as wav:
        rate=wav.getframerate()
        samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)
    # All three pitches must coexist near both the beginning and end of the
    # gesture. A frequency-list glide loses at least two notes in each window.
    for start in (.60,1.10):
        segment=samples[round(start*rate):round((start+.25)*rate)]
        spectrum=np.abs(np.fft.rfft(segment*np.hanning(len(segment))))
        frequencies=np.fft.rfftfreq(len(segment),1/rate)
        amplitudes=[spectrum[np.abs(frequencies-note)<6].max() for note in notes]
        assert min(amplitudes)>.35*max(amplitudes)


def test_catalog_discovers_entries_past_the_previous_64_limit(tmp_path):
    for n in range(87):
        (tmp_path/f'entry-{n:03d}.json').write_text('{}')
    catalog=FaceAnimations(tmp_path,None).catalog()
    assert len(catalog)==87 and catalog[-1]=='entry-086'


def test_all_33_run_through_real_app_with_audio_or_silent_sprites(tmp_path):
    calls=[]
    robot=SimpleNamespace(capture_token=lambda:None,
        play_video=lambda name,*a,**kw:calls.append(('video',name)) or dict(ok=True,state='completed',frames_displayed=90,audio_state='completed'),
        face_animation=lambda name,*a,**kw:calls.append(('sprite',name)) or dict(ok=True,state='completed',displayed=True))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        for name in NEW_ANIMATIONS:
            tool='advent' if name.startswith('advent-') else 'seasonal'
            args={'day':int(name[-2:])} if tool=='advent' else {'name':name}
            assert host.call('expressions',tool,args)['ok']
            assert host.call('expressions',tool,{**args,'with_audio':False})['ok']
        assert calls==[(kind,name) for name in NEW_ANIMATIONS for kind in ('video','sprite')]
        assert host.call('expressions','advent',{})['ok']
        assert calls[-1]==('video','advent-01')
        for bad in (0,25,True,1.5,'2'):
            with pytest.raises(ValidationError):host.call('expressions','advent',{'day':bad})
        with pytest.raises(ValidationError):host.call('expressions','seasonal',{'name':'post-sunshine'})
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


def test_seasonal_voice_routes_are_explicit_bilingual_and_date_independent():
    host=SimpleNamespace(packages=SimpleNamespace(projects={'expressions':True}),enabled=lambda _:True)
    for name in SEASONAL_ANIMATIONS:
        theme=name.removeprefix('season-').replace('-',' ')
        assert local_expression_request(host,f'Robbi, show the {theme} animation please!')=={
            'app':'expressions','tool':'seasonal','arguments':{'name':name,'with_audio':True}}
    for day in range(1,25):
        assert local_expression_request(host,f'Show Advent day {day}')=={
            'app':'expressions','tool':'advent','arguments':{'day':day,'with_audio':True}}
    for phrase,day in [('Play Advent day twenty-four',24),('Zeig Advent Türchen zwölf',12),
                       ('Öffne Advent Türchen vierundzwanzig',24)]:
        assert local_expression_request(host,phrase)['arguments']['day']==day
    assert local_expression_request(host,'Zeig die Weihnachts-Animation')['arguments']['name']=='season-christmas'
    for phrase in ('Happy birthday','Merry Christmas','It is winter','My birthday is tomorrow',
                   'Show Advent day 0','Show Advent day 25','Open Advent day -1','Show Advent day 1.5',
                   'Show Advent day +2','What is Advent day 3?',
                   'Show Advent day 1 then drive forward'):
        assert local_expression_request(host,phrase) is None
    host.enabled=lambda _:False
    assert local_expression_request(host,'Show Advent day 1') is None


@pytest.mark.parametrize('tool,args',[('seasonal',{'name':'season-birthday'}),('advent',{'day':24})])
@pytest.mark.parametrize('audio_state',['not_requested','cancelled','unavailable'])
def test_seasonal_app_reports_incomplete_soundtrack(tmp_path,tool,args,audio_state):
    robot=SimpleNamespace(capture_token=lambda:None,play_video=lambda *a,**kw:
        dict(ok=True,state='completed',frames_displayed=90,audio_state=audio_state))
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        result=host.call('expressions',tool,args)
        assert not result['ok'] and result['data']['state']=='partial'
        assert 'soundtrack did not complete' in result['text']
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)




def optional_gif(path):
    if not path.exists():
        pytest.skip('Optional authoring preview absent; preceding runtime sprite checks passed')
    return Image.open(path)
