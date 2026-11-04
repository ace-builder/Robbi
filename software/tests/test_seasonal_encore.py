"""End-to-end contracts for the twelve additional seasonal object stories."""
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
import yaml

from robbi.app_runtime.host import AppHost
from robbi.errors import ValidationError
from robbi.expression_catalog import (
    ADVENT_ANIMATIONS, ALL_SEASONAL_ANIMATIONS, FACE_EXPRESSIONS,
    MEME_ANIMATIONS, POSTCARD_ANIMATIONS, SCREEN_GAGS, SEASONAL_ANIMATIONS,
    SEASONAL_ENCORE_ANIMATIONS, SWEET_ANIMATIONS, local_expression_request,
)
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia
from test_expression_polish import clock, player

ROOT=Path(__file__).resolve().parents[1]
NEW_NAMES=tuple(SEASONAL_ENCORE_ANIMATIONS)
LEGACY_NAMES=(set(FACE_EXPRESSIONS)|set(SCREEN_GAGS)|set(MEME_ANIMATIONS)
              |set(SWEET_ANIMATIONS)|set(POSTCARD_ANIMATIONS)
              |set(SEASONAL_ANIMATIONS)|set(ADVENT_ANIMATIONS))


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'tools'/'assets'))
    return importlib.import_module('prepare_seasonal_encore')


@pytest.fixture(scope='module')
def catalog():
    payload=json.loads((catalog_group(ROOT, 'seasonal_encore')).read_text(encoding='utf-8'))
    return {clip['id']:clip for clip in payload['clips']}


def _sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def _gif_color_tables(data):
    """Read actual GIF colour tables, including any per-frame overrides."""
    assert data[:6] in (b'GIF87a',b'GIF89a')
    packed=data[10];position=13;tables=[]
    if packed&128:
        count=3*(2**((packed&7)+1));tables.append(data[position:position+count]);position+=count

    def skip_blocks(position):
        while data[position]:position+=1+data[position]
        return position+1

    while position<len(data):
        marker=data[position]
        if marker==0x3b:return tables
        if marker==0x21:
            position=skip_blocks(position+2)
        elif marker==0x2c:
            packed=data[position+9];position+=10
            if packed&128:
                count=3*(2**((packed&7)+1));tables.append(data[position:position+count]);position+=count
            position=skip_blocks(position+1)  # LZW minimum code size, then blocks.
        else:raise AssertionError(f'Unexpected GIF block marker {marker:#x}')
    raise AssertionError('GIF has no trailer')


@pytest.mark.parametrize('name',NEW_NAMES)
def test_all_ninety_frames_are_face_free_with_fixed_headings_and_exact_bookends(name,renderer,catalog,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Object stories must not access Robbi face artwork')
    for helper in ('face','face_badge','head'):monkeypatch.setattr(renderer.c.art,helper,forbidden)
    monkeypatch.setattr(renderer.c.art.PLAYER,'frames',forbidden)
    card=renderer.cover(name)
    heading=card.crop((0,0,840,38*3)).tobytes()
    action_hashes=set()
    for index in range(90):
        frame=renderer.render(name,index/20)
        assert frame.size==(840,720) and frame.mode=='RGB'
        assert frame.crop((0,0,840,38*3)).tobytes()==heading
        if 7<=index<=80:action_hashes.add(hashlib.sha256(frame.tobytes()).hexdigest())
        if index in (0,1,87,88,89):assert frame.tobytes()==card.tobytes()
    assert len(action_hashes)>20, 'The story should contain real authored motion'
    assert catalog[name]['contains_robbi_face'] is False
    assert catalog[name]['bookend']=='typographic-postcard'


@pytest.mark.parametrize('name',NEW_NAMES)
def test_native_sprite_and_gif_preserve_duration_heading_palette_and_loop_endpoints(name,renderer,catalog):
    record=catalog[name]
    clip=FaceAnimations(ROOT/'assets/face_animations',None).clip(name)
    card=renderer.cover(name).resize((280,240),Image.Resampling.LANCZOS).convert('RGB')
    assert len(clip.frames)==20
    assert clip.duration(10)==clip.duration(30)==pytest.approx(4.5)
    assert clip.frames[0].tobytes()==clip.frames[-1].tobytes()==card.tobytes()
    heading=card.crop((0,0,280,38)).tobytes()
    assert all(frame.crop((0,0,280,38)).tobytes()==heading for frame in clip.frames)
    path=renderer.OUT/record['gif']
    if not path.exists():
        pytest.skip('Optional authoring preview absent; runtime sprite checks passed')
    tables=_gif_color_tables(path.read_bytes())
    assert tables and all(table==tables[0] for table in tables), 'A moving palette causes colour flicker'
    with Image.open(path) as gif:
        assert gif.info['loop']==0 and 20<gif.n_frames<=90
        durations=[];headings=[];endpoints=[]
        for index in range(gif.n_frames):
            gif.seek(index)
            assert gif.size==(280,240)
            durations.append(gif.info['duration'])
            headings.append(gif.convert('RGB').crop((0,0,280,38)).tobytes())
            if index in (0,gif.n_frames-1):endpoints.append(gif.convert('RGB').tobytes())
        assert sum(durations)==4500 and all(ms>0 and ms%50==0 for ms in durations)
        assert durations.count(50)>=20
        assert len(set(headings))==1 and endpoints[0]==endpoints[-1]
    assert record['gif_palette']['worst_mean_error']<3


@pytest.mark.parametrize('name',NEW_NAMES)
def test_exported_audio_is_current_bounded_and_synchronized_with_motion_media(name,renderer,catalog,tmp_path):
    record=catalog[name]
    assert (record['frames'],record['fps'],record['duration_s'],record['native_size'])==(90,20,4.5,[280,240])
    for field in ('video','audio','sheet'):assert _sha(ROOT/record[field])==record[field+'_sha256']
    data=(ROOT/record['audio']).read_bytes()
    assert data==(ROOT/record['sidecar']).read_bytes()==renderer.soundtrack(name)[0]
    with wave.open(io.BytesIO(data),'rb') as wav:
        assert (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes())==(1,2,22050,99225)
        pcm=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)/32768
    assert np.max(np.abs(pcm))<.36 and abs(pcm.mean())<.0001
    assert not np.any(pcm[:2205]) and not np.any(pcm[-2205:])
    assert record['audio_metrics']['clipped_samples']==0
    assert record['audio_metrics']['true_peak_dbfs']<=-9
    assert -33<record['audio_metrics']['lufs']<=-24.5
    assert record['cues']
    for cue in record['cues']:
        assert .35<=cue['time_s']<3.96
        assert abs(cue['time_s']-cue['video_frame']/20)<=.025+1e-9
        start=round(cue['time_s']*22050)
        assert np.sqrt(np.mean(pcm[start:start+2205]**2))>.001,(name,cue['label'])
    media=MotionMedia(ROOT,tmp_path)
    entry=next(item for item in media.catalog() if item['name']==name+'.mp4')
    assert media.resolve(entry['id'],'video')['_path']==ROOT/record['video']
    assert media.resolve(entry['soundtrack'],'sound')['_path']==ROOT/record['sidecar']


@pytest.mark.parametrize('name',NEW_NAMES)
def test_native_silent_playback_uses_authored_duration_and_releases_cancelled_lease(name,clock):
    animation,writes,cancel=player(clock)
    result=animation.play(name,fps=10,cancel_event=cancel)
    assert result['ok'] and result['elapsed_s']==4.5 and len(writes)==45
    assert writes[-1][1].tobytes()==animation.frames(name)[-1].tobytes()
    animation,writes,cancel=player(clock,cancel_after=12)
    result=animation.play(name,fps=10,cancel_event=cancel)
    assert result['state']=='cancelled' and len(writes)==12
    assert animation.display.orchestrator.snapshot()['active_leases']==0


def test_the_new_catalog_adds_twelve_distinct_entries_and_preserves_all_eighty_seven(renderer,catalog):
    assert len(NEW_NAMES)==12 and set(catalog)==set(NEW_NAMES)==set(renderer.definitions()[0])
    assert len(LEGACY_NAMES)==87 and not LEGACY_NAMES&set(NEW_NAMES)
    visible=set(FaceAnimations(ROOT/'assets/face_animations',None).catalog())
    assert LEGACY_NAMES<=visible and set(NEW_NAMES)<=visible
    assert len(ALL_SEASONAL_ANIMATIONS)==21
    assert set(ALL_SEASONAL_ANIMATIONS)==set(SEASONAL_ANIMATIONS)|set(NEW_NAMES)
    for field in ('title','video_sha256','audio_sha256','sheet_sha256'):
        assert len({record[field] for record in catalog.values()})==12,field


def test_real_app_accepts_all_twelve_with_sound_and_silent_native_routes(tmp_path):
    calls=[]
    def video(name,*args,**kwargs):
        calls.append(('video',name,kwargs))
        return dict(ok=True,state='completed',frames_displayed=90,audio_state='completed')
    def sprite(name,*args,**kwargs):
        calls.append(('sprite',name,kwargs))
        return dict(ok=True,state='completed',displayed=True)
    robot=SimpleNamespace(capture_token=lambda:None,play_video=video,face_animation=sprite)
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False);host.scan()
    try:
        for name in NEW_NAMES:
            for with_audio in (True,False):
                result=host.call('expressions','seasonal',{'name':name,'with_audio':with_audio})
                assert result['ok'] and result['data']['displayed']
        assert [(kind,name) for kind,name,_ in calls]==[(kind,name) for name in NEW_NAMES for kind in ('video','sprite')]
        assert all(args=={'fps':10,'loops':1} for kind,_,args in calls if kind=='sprite')
        with pytest.raises(ValidationError):host.call('expressions','seasonal',{'name':'post-sunshine'})
        host.set_enabled('expressions',False)
        assert local_expression_request(host,'Show the Diwali animation') is None
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)
    package=yaml.safe_load((ROOT/'apps/expressions/app.yaml').read_text(encoding='utf-8'))
    assert tuple(map(int,package['version'].split('.'))) >= (1,8,0)
    seasonal=next(tool for tool in package['tools'] if tool['name']=='seasonal')
    assert set(seasonal['input_schema']['properties']['name']['enum'])==set(ALL_SEASONAL_ANIMATIONS)


@pytest.fixture
def voice_host():
    return SimpleNamespace(packages=SimpleNamespace(projects={'expressions':True}),enabled=lambda _:True)


@pytest.mark.parametrize('name',NEW_NAMES)
def test_each_new_occasion_has_an_explicit_canonical_english_voice_route(name,voice_host):
    theme=name.removeprefix('season-').replace('-',' ')
    assert local_expression_request(voice_host,f'Robbi, show the {theme} animation please!')=={
        'app':'expressions','tool':'seasonal','arguments':{'name':name,'with_audio':True}}


@pytest.mark.parametrize('phrase,name',[
    ('Zeig die Valentinstags-Animation','season-valentine'),
    ('Zeig die Mondneujahrs-Animation','season-lunar-new-year'),
    ('Zeig die Sommerpicknick-Animation','season-summer-picnic'),
    ('Zeig die Wintersonnenwende-Animation','season-winter-solstice'),
    ('Zeig die Chanukka-Animation','season-hanukkah'),
])
def test_new_german_voice_aliases_stay_narrow(phrase,name,voice_host):
    assert local_expression_request(voice_host,phrase)=={
        'app':'expressions','tool':'seasonal','arguments':{'name':name,'with_audio':True}}


@pytest.mark.parametrize('phrase',[
    'Happy Diwali!', 'Happy Hanukkah!', 'Happy Lunar New Year!', 'Happy Valentines Day!',
    'It is the winter solstice', 'We are going on a summer picnic', 'When is Earth Day?',
    'Show the Diwali animation and drive forward',
    'Show the lunar new year animation then turn left',
    'Zeig die Chanukka-Animation und fahre vorwärts',
    'Do not show the carnival animation',
])
def test_greetings_context_and_mixed_commands_do_not_start_playback(phrase,voice_host):
    assert local_expression_request(voice_host,phrase) is None


def test_lunar_new_year_and_new_year_select_different_stories(voice_host):
    lunar=local_expression_request(voice_host,'Play the lunar new year animation')
    solar=local_expression_request(voice_host,'Play the new year animation')
    assert lunar['arguments']['name']=='season-lunar-new-year'
    assert solar['arguments']['name']=='season-new-year'
    assert lunar!=solar
