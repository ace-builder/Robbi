"""Real LCD gags: timing, stable artwork, app discovery and cancellation."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from robbi.app_runtime.host import AppHost
from robbi.expression_catalog import FACE_EXPRESSIONS, SCREEN_GAGS, ALL_MEME_ANIMATIONS, SWEET_ANIMATIONS, POSTCARD_ANIMATIONS, ALL_SEASONAL_ANIMATIONS, ADVENT_ANIMATIONS, local_expression_request
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia
from test_expression_polish import clock, player

ROOT=Path(__file__).resolve().parents[1]
SHEETS=ROOT/'assets/face_animations'


def test_catalog_crosses_old_32_entry_limit_without_hiding_clips():
    names=FaceAnimations(SHEETS,None).catalog()
    assert set(names)>=set(FACE_EXPRESSIONS)|set(SCREEN_GAGS)|set(ALL_MEME_ANIMATIONS)|set(SWEET_ANIMATIONS)|set(POSTCARD_ANIMATIONS) | set(ALL_SEASONAL_ANIMATIONS) | set(ADVENT_ANIMATIONS)
    assert 'thoughtful-look' in names and 'sleepy-yawn' in names


@pytest.mark.parametrize('name',SCREEN_GAGS)
def test_gag_frames_return_to_identical_neutral_and_hold_caption(name):
    animation=FaceAnimations(SHEETS,None)
    clip=animation.clip(name)
    neutral=animation.clip('natural-blink').frames[0]
    assert len(clip.frames)==20
    assert clip.duration(10)==pytest.approx(3)
    assert clip.duration(30)==pytest.approx(3)
    assert clip.frames[0].tobytes()==clip.frames[-1].tobytes()==neutral.tobytes()
    assert all(f.size==(280,240) and f.mode=='RGB' for f in clip.frames)
    # The task-failed punchline intentionally holds still so it can be read.
    assert len({f.tobytes() for f in clip.frames})>=8
    # Re-decoding cannot change the artwork or position of any pixel.
    assert FaceAnimations(SHEETS,None).frames(name)[12].tobytes()==clip.frames[12].tobytes()
    if name not in {'gag-deal-with-it','gag-dramatic-zoom'}:
        header=clip.frames[2].crop((16,10,264,40)).tobytes()
        assert all(f.crop((16,10,264,40)).tobytes()==header for f in clip.frames[3:18])
    with optional_gif(ROOT/'test-results/screen-gags-preview/gifs'/(name+'.gif')) as gif:
        elapsed=0
        for i in range(gif.n_frames):
            gif.seek(i);elapsed+=gif.info['duration']
            assert gif.size==(280,240)
        assert elapsed==3000


@pytest.mark.parametrize('name',SCREEN_GAGS)
@pytest.mark.parametrize('fps',[10,30])
def test_real_player_finishes_each_gag_at_neutral(clock,name,fps):
    animation,writes,cancel=player(clock)
    result=animation.play(name,fps=fps,cancel_event=cancel)
    assert result['ok'] and result['displayed']
    assert result['duration_s']==result['elapsed_s']==3
    assert len(writes)==3*fps
    assert writes[-1][1].tobytes()==animation.frames('natural-blink')[0].tobytes()
    assert animation.display.orchestrator.snapshot()['active_leases']==0


def test_gag_interrupts_without_overwriting_the_next_screen(clock):
    animation,writes,cancel=player(clock,cancel_after=7)
    result=animation.play('gag-hamster-brain',cancel_event=cancel)
    assert result['state']=='cancelled'
    assert len(writes)==7
    assert writes[-1][1].tobytes()!=animation.frames('natural-blink')[0].tobytes()
    assert animation.display.orchestrator.snapshot()['active_leases']==0


def test_real_app_exposes_gags_and_narrow_voice_routes(tmp_path):
    calls=[]
    robot=SimpleNamespace(capture_token=lambda:None,
        face_animation=lambda name,*a,**kw:calls.append(name) or {'ok':True,'state':'completed','displayed':True})
    host=AppHost(ROOT,tmp_path,robot,permissions_enabled=False)
    host.scan()
    try:
        for name in SCREEN_GAGS:
            result=host.call('expressions','gag',{'name':name,'with_audio':False})
            assert result['ok'],result
        assert calls==list(SCREEN_GAGS)
        assert local_expression_request(host,'Robbi, play the tiny violin please!')['arguments']['name']=='gag-tiny-violin'
        assert local_expression_request(host,'Zeig den Hamster')['tool']=='gag'
        assert local_expression_request(host,'This is fine') is None
        assert local_expression_request(host,'I feel like my social battery is empty') is None
        host.set_enabled('expressions',False)
        assert local_expression_request(host,'Show buffering') is None
    finally:
        host.stop();host._executor.shutdown(wait=True,cancel_futures=True)


def test_video_exports_and_sound_suggestions_are_discoverable(tmp_path):
    catalog=json.loads((catalog_group(ROOT, 'screen_gags')).read_text(encoding='utf-8'))['clips']
    media=MotionMedia(ROOT,tmp_path)
    available={entry['name']:entry for entry in media.catalog()}
    assert len(catalog)==12
    for record in catalog:
        assert record['id'] in SCREEN_GAGS
        assert record['sound_default'] is True
        assert (ROOT/'assets/sounds'/(record['suggested_sound']+'.wav')).is_file()
        sheet=ROOT/record['sheet'];video=ROOT/record['video']
        assert hashlib.sha256(sheet.read_bytes()).hexdigest()==record['sheet_sha256']
        assert hashlib.sha256(video.read_bytes()).hexdigest()==record['video_sha256']
        assert media.resolve(available[video.name]['id'],'video')['_path']==video


def optional_gif(path):
    if not path.exists():
        pytest.skip('Optional authoring preview absent; preceding runtime sprite checks passed')
    return Image.open(path)
