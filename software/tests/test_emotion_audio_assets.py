"""Verify the real delivered audio, its rights trail, and robot discovery."""
import hashlib
import json
from pathlib import Path
import wave
import zipfile

import numpy as np
import pytest
from scipy import signal

from robbi.expression_catalog import FACE_EXPRESSIONS
from robbi.motion_media import MotionMedia
from robot.assets import AssetRegistry

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT/'assets/emotion_audio'
MANIFEST = json.loads((LIBRARY/'manifest.json').read_text(encoding='utf-8'))
EMOTIONS = MANIFEST['emotions']


def read(clip):
    path = ROOT/clip['file']
    with wave.open(str(path), 'rb') as wav:
        assert (wav.getframerate(),wav.getnchannels(),wav.getsampwidth()) == (22050,1,2)
        assert wav.getcomptype() == 'NONE'
        samples = np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2')
        assert len(samples) == wav.getnframes()
    return samples.astype(float)/32768


def test_complete_library_and_matching_faces():
    assert len(EMOTIONS) == 24
    assert len({e['id'] for e in EMOTIONS}) == 24
    assert MANIFEST['selected_variants'] == {}
    hashes = []
    for emotion in EMOTIONS:
        assert emotion['face'] in FACE_EXPRESSIONS
        assert {v['variant'] for v in emotion['variants']} == {'A','B','C'}
        assert len(emotion['variants']) == 3
        for clip in emotion['variants']:
            assert clip['license'] == 'CC0-1.0'
            digest = hashlib.sha256((ROOT/clip['file']).read_bytes()).hexdigest()
            assert digest == clip['sha256']
            hashes.append(digest)
    assert len(set(hashes)) == 72


@pytest.mark.parametrize('emotion',EMOTIONS,ids=lambda e:e['id'])
def test_actual_audio_is_clean_and_variants_are_distinct(emotion):
    signals=[]
    levels=[]
    for clip in emotion['variants']:
        x=read(clip)
        assert .6 <= len(x)/22050 <= 3.5
        assert np.isfinite(x).all()
        assert .04 < abs(x).max() < 10**(-9/20)
        assert abs(x.mean()) < .0001
        assert np.max(abs(x[:330])) == 0
        assert np.max(abs(x[-330:])) == 0
        assert clip['clipped_samples'] == 0
        assert clip['true_peak_dbfs'] <= -9
        assert abs(clip['lufs']-clip['target_lufs']) <= .4
        levels.append(clip['lufs'])
        assert 20 <= clip['leading_silence_ms'] < 100
        assert 40 <= clip['trailing_silence_ms'] < 350
        assert clip['sub_150hz_energy'] < .04
        assert clip['above_6khz_energy'] < .01
        # A normalized time comparison catches accidental duplicate variants,
        # including duplicates exported at a different duration or gain.
        stretched=signal.resample(x,4096)
        signals.append(stretched/np.linalg.norm(stretched))
    assert max(levels)-min(levels) <= .5
    for i in range(3):
        for j in range(i):
            assert abs(np.dot(signals[i],signals[j])) < .9


def test_cc0_source_archives_and_member_provenance():
    folder=ROOT/'assets/dynamics_sources/emotion-audio-v1'
    sources=json.loads((folder/'sources.json').read_text())
    for source in sources:
        archive=folder/source['archive']
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == source['sha256']
        assert source['license'] == 'CC0-1.0'
        assert 'CC0' in (folder/(source['id']+'-LICENSE.txt')).read_text()
    source_map={s['id']:s for s in sources}
    for emotion in EMOTIONS:
        a,b,c=emotion['variants']
        assert not a['sources'] and not b['sources']
        assert len(c['sources']) == 1
        source=c['sources'][0]
        with zipfile.ZipFile(folder/source_map[source['pack']]['archive']) as archive:
            assert source['member'] in archive.namelist()


def test_all_sounds_are_available_to_runtime_and_motion_studio(tmp_path):
    assets=AssetRegistry()
    assets.scan()
    media=MotionMedia(ROOT,tmp_path)
    catalog={item['name']:item for item in media.catalog()}
    for emotion in EMOTIONS:
        for clip in emotion['variants']:
            path=ROOT/clip['file']
            assert assets.get('sounds',clip['id'],extensions=('.wav',)) == path
            assert path.name in catalog
            entry=media.resolve(catalog[path.name]['id'],'sound')
            assert entry['_path'] == path
            checked=media._check(path.name,path.read_bytes())
            assert checked['duration'] == pytest.approx(clip['duration_s'],abs=.0001)


@pytest.mark.parametrize('name',['sad','disappointed','relieved','sleepy'])
def test_falling_reactions_actually_fall_in_pitch(name):
    clip=next(e for e in EMOTIONS if e['id']==name)['variants'][1]
    x=read(clip)
    def dominant(segment):
        spec=abs(np.fft.rfft(segment*np.hanning(len(segment))))
        freqs=np.fft.rfftfreq(len(segment),1/22050)
        return freqs[np.argmax(spec)]
    early=x[round(.15*22050):round(.3*22050)]
    late=x[-round(.35*22050):-round(.2*22050)]
    assert dominant(late) < dominant(early)*.9
