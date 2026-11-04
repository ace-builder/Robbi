"""New kindness cartoons: playable assets, continuity and actual app integration."""
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
from PIL import Image
import pytest
from scipy.ndimage import label as components

from robbi.app_runtime.host import AppHost
from robbi.expression_catalog import SWEET_ANIMATIONS, local_expression_request
from robbi.errors import ValidationError
from robbi.face_animation import FaceAnimations
from robbi.motion_media import MotionMedia
from test_expression_polish import clock, player

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'test-results/little-kindness-preview'
CLIPS = {c['id']: c for c in json.loads((catalog_group(ROOT, 'little_kindness')).read_text(encoding='utf-8'))['clips']}


@pytest.mark.parametrize('name', SWEET_ANIMATIONS)
def test_audio_assets_and_cues_match_the_video_and_studio(name, tmp_path):
    c = CLIPS[name]
    assert c['frames'] == 90 and c['fps'] == 20 and c['duration_s'] == 4.5
    for field in ('video', 'audio', 'sheet'):
        assert hashlib.sha256((ROOT/c[field]).read_bytes()).hexdigest() == c[field+'_sha256']
    assert (ROOT/c['audio']).read_bytes() == (ROOT/c['sidecar']).read_bytes()
    with wave.open(str(ROOT/c['audio']), 'rb') as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 22050, 99225)
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float)/32768
    assert np.max(np.abs(pcm)) < .36 and abs(pcm.mean()) < .0001
    assert not np.any(pcm[:2205]) and not np.any(pcm[-2205:])
    assert c['audio_metrics']['clipped_samples'] == 0
    assert -32 < c['audio_metrics']['lufs'] <= -24.5
    for cue in c['cues']:
        assert cue['time_s']*20 == pytest.approx(cue['video_frame'])
        start = round(cue['time_s']*22050)
        assert np.sqrt(np.mean(pcm[start:start+2205]**2)) > .001
    media = MotionMedia(ROOT, tmp_path)
    entry = next(v for v in media.catalog() if v['name'] == name+'.mp4')
    assert media.resolve(entry['soundtrack'], 'sound')['_path'] == ROOT/c['sidecar']


@pytest.mark.parametrize('name', SWEET_ANIMATIONS)
def test_sprite_and_gif_timing_stable_headings_and_exact_neutral_endpoints(name):
    faces = FaceAnimations(ROOT/'assets/face_animations', None)
    clip = faces.clip(name)
    assert len(clip.frames) == 20
    assert clip.duration(10) == clip.duration(30) == pytest.approx(4.5)
    assert clip.frames[0].tobytes() == clip.frames[-1].tobytes() == faces.frames('natural-blink')[0].tobytes()
    heading = clip.frames[2].crop((0, 0, 280, 32)).tobytes()
    assert all(f.crop((0, 0, 280, 32)).tobytes() == heading for f in clip.frames[3:17])
    with optional_gif(OUT/CLIPS[name]['gif']) as gif:
        total = 0
        # GIF merges identical holds (the apology deliberately rests on its
        # repaired heart). Verify the 20 fps motion and complete timeline.
        assert gif.info['loop'] == 0 and len(clip.frames) < gif.n_frames <= 90
        moving_frames = 0
        for n in range(gif.n_frames):
            gif.seek(n)
            assert gif.size == (280, 240) and gif.info['duration'] % 50 == 0
            total += gif.info['duration']
            moving_frames += gif.info['duration'] == 50
        assert total == 4500 and moving_frames >= 20


@pytest.mark.parametrize('name', SWEET_ANIMATIONS)
def test_silent_fallback_completes_and_can_be_interrupted(clock, name):
    animation, writes, cancel = player(clock)
    result = animation.play(name, fps=10, cancel_event=cancel)
    assert result['ok'] and result['elapsed_s'] == 4.5 and len(writes) == 45
    animation, writes, cancel = player(clock, cancel_after=12)
    result = animation.play(name, fps=10, cancel_event=cancel)
    assert result['state'] == 'cancelled' and len(writes) == 12
    assert animation.display.orchestrator.snapshot()['active_leases'] == 0


@pytest.mark.parametrize('name,anchor', [
    ('sweet-flower',(79,124,102)), ('sweet-pocket-hug',(140,111,116)),
    ('sweet-sorry',(75,126,93)), ('sweet-cookie',(140,83,78)),
    ('sweet-rain',(140,143,91)), ('sweet-rest',(140,123,122)),
])
def test_eye_anchors_do_not_snap_at_expression_endpoints(name,anchor):
    faces=FaceAnimations(ROOT/'assets/face_animations',None)
    cx,cy,width=anchor;scale=width/280
    left=cx-width/2;top=cy-round(width*240/280)/2
    for x,y,w,h in [(32,56,80,80),(176,54,82,82)]:
        centers=[]
        box=(round(left+x*scale),round(top+y*scale),round(left+(x+w)*scale),round(top+(y+h)*scale))
        for frame in faces.clip(name).frames[2:18]:
            patch=np.asarray(frame.crop(box))
            labels,count=components(patch.max(axis=2)<90)
            assert count>0
            # Select the eye, excluding disconnected nearby prop outlines.
            sizes=np.bincount(labels.ravel());sizes[0]=0
            yy,xx=np.where(labels==np.argmax(sizes))
            assert len(xx)>0
            centers.append(((xx.min()+xx.max())/2,(yy.min()+yy.max())/2))
        # Native rasterization can move a silhouette bound by half a pixel;
        # the old raw blink endpoints jumped several complete LCD pixels.
        assert np.max(np.ptp(np.asarray(centers),axis=0))<=1.5


def test_real_app_all_six_with_sound_and_silent_and_narrow_voice_shortcuts(tmp_path):
    calls = []
    robot = SimpleNamespace(capture_token=lambda: None,
        play_video=lambda name, *a, **kw: calls.append(('video', name)) or dict(ok=True, state='completed', frames_displayed=90, audio_state='completed'),
        face_animation=lambda name, *a, **kw: calls.append(('sprite', name)) or dict(ok=True, state='completed', displayed=True))
    host = AppHost(ROOT, tmp_path, robot, permissions_enabled=False)
    host.scan()
    try:
        for name in SWEET_ANIMATIONS:
            assert host.call('expressions', 'kindness', {'name': name})['ok']
            assert host.call('expressions', 'kindness', {'name': name, 'with_audio': False})['ok']
        assert calls == [(kind, name) for name in SWEET_ANIMATIONS for kind in ('video', 'sprite')]
        for phrase, name in [('Robbi, show a flower gift please!', 'sweet-flower'),
                             ('Show the pocket hug', 'sweet-pocket-hug'), ('Show the little apology', 'sweet-sorry'),
                             ('Zeig den letzten Keks', 'sweet-cookie'), ('Show the rainy day umbrella', 'sweet-rain'),
                             ('Show the bedtime tuck-in', 'sweet-rest')]:
            assert local_expression_request(host, phrase) == {'app': 'expressions', 'tool': 'kindness',
                'arguments': {'name': name, 'with_audio': True}}
        for phrase in ('I am sorry', 'I need rest', 'I feel lonely', 'It is raining', 'You hurt my feelings'):
            assert local_expression_request(host, phrase) is None
        with pytest.raises(ValidationError, match='declared choices'):
            host.call('expressions', 'kindness', {'name': 'meme-support-brick'})
        host.set_enabled('expressions', False)
        assert local_expression_request(host, 'Show a flower gift') is None
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('audio_state', ['not_requested', 'cancelled', 'unavailable'])
def test_kindness_does_not_report_success_with_an_incomplete_soundtrack(tmp_path, audio_state):
    robot = SimpleNamespace(capture_token=lambda: None, play_video=lambda *a, **kw:
        dict(ok=True, state='completed', frames_displayed=90, audio_state=audio_state))
    host = AppHost(ROOT, tmp_path, robot, permissions_enabled=False)
    host.scan()
    try:
        result = host.call('expressions', 'kindness', {'name': 'sweet-flower'})
        assert not result['ok'] and result['data']['state'] == 'partial'
        assert 'soundtrack did not complete' in result['text']
        assert not result['data'].get('display_hold_s')
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def optional_gif(path):
    if not path.exists():
        pytest.skip('Optional authoring preview absent; preceding runtime sprite checks passed')
    return Image.open(path)
