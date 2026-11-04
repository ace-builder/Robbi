"""Asset-level checks for registration, fixed pixels and smooth emotion loops."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest
from scipy.ndimage import label

from robbi.app_runtime.host import AppHost
from robbi.expression_catalog import EMOTION_EXPRESSIONS, local_expression_request
from robbi.face_animation import FaceAnimations

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT/'assets/face_animations'
REGIONS = {'left_eye': (32, 56, 80, 80), 'right_eye': (176, 54, 82, 82),
           'mouth': (90, 132, 108, 64)}
ANCHORS = {'left_eye': (71.5, 98.5), 'right_eye': (216.5, 97.5), 'mouth': (143.5, 165)}


@pytest.fixture(scope='module')
def neutral():
    with Image.open(DIRECTORY/'expression-atlas-v2.png') as image:
        return np.asarray(image.convert('RGB'))[:240, :280]


@pytest.fixture(scope='module')
def fixed():
    result = np.ones((240, 280), dtype=bool)
    for x, y, width, height in REGIONS.values():
        result[y:y+height, x:x+width] = False
    assert result.sum() == 47340
    return result


def frames_for(name):
    return FaceAnimations(DIRECTORY, None).clip(name)


@pytest.mark.parametrize('name', EMOTION_EXPRESSIONS)
def test_ten_registered_frames_have_identical_cheeks_background_and_loop_boundary(name, neutral, fixed):
    clip = frames_for(name)
    frames = [np.asarray(frame) for frame in clip.frames]
    assert len(frames) == 10
    assert np.array_equal(frames[0], neutral) and np.array_equal(frames[-1], neutral)
    assert not np.array_equal(frames[4], neutral)
    # Cropped cheek fragments must never appear elsewhere on the face.
    def orange(pixels):
        data = pixels.astype(int)
        return (data[:, :, 0] > data[:, :, 1]+25) & (data[:, :, 1] > data[:, :, 2]+25)
    for index, frame in enumerate(frames):
        assert np.array_equal(frame[fixed], neutral[fixed]), (name, index)
        assert np.array_equal(orange(frame), orange(neutral)), (name, index)
        assert np.array_equal(frame, frames[9-index]), (name, index)


@pytest.mark.parametrize('name', EMOTION_EXPRESSIONS)
def test_eye_and_mouth_anchors_stay_fixed_without_duplicate_silhouettes(name):
    frames = [np.asarray(frame) for frame in frames_for(name).frames]
    for feature, (x, y, width, height) in REGIONS.items():
        centers = []
        for frame in frames:
            ink = frame[y:y+height, x:x+width].max(axis=2) < 100
            yy, xx = np.where(ink)
            center = np.array([x+(xx.min()+xx.max()+1)/2, y+(yy.min()+yy.max()+1)/2])
            assert np.max(np.abs(center-ANCHORS[feature])) <= .5, (name, feature, center)
            centers.append(center)
            components, count = label(ink)
            sizes = np.bincount(components.ravel())[1:]
            assert np.count_nonzero(sizes > 4) == 1, (name, feature, sizes.tolist())
        assert np.abs(np.diff(centers, axis=0)).max() <= 1


@pytest.mark.parametrize('fps', [10, 30])
def test_runtime_blending_keeps_static_pixels_and_authored_duration(fps, neutral, fixed):
    for name in EMOTION_EXPRESSIONS:
        clip = frames_for(name)
        assert clip.duration(10) == clip.duration(30)
        assert 1.5 < clip.duration(fps) < 3
        for seconds in np.arange(0, clip.duration(fps), 1/fps):
            image = np.asarray(clip.frame_at(float(seconds), fps))
            assert np.array_equal(image[fixed], neutral[fixed]), (name, fps, seconds)
            for feature, (x, y, width, height) in REGIONS.items():
                yy, xx = np.where(image[y:y+height, x:x+width].max(axis=2) < 100)
                center = np.array([x+(xx.min()+xx.max()+1)/2, y+(yy.min()+yy.max()+1)/2])
                assert np.max(np.abs(center-ANCHORS[feature])) <= .5, (name, fps, seconds, feature)


def test_only_intentional_gazes_move_eye_highlights():
    report = json.loads((ROOT/'assets/dynamics_sources/emotion-registration-v3.json').read_text())
    for name, spec in report['emotions'].items():
        if name in {'shy-smile', 'thoughtful-look'}:
            continue
        for eye, peak in spec['highlights_peak'].items():
            assert peak['center'] == spec['highlights_start'][eye]['center']


@pytest.mark.parametrize('name', ['proud-smile', 'excited-grin'])
def test_closed_eyes_leave_no_floating_highlight(name):
    peak = np.asarray(frames_for(name).frames[4])
    for feature in ('left_eye', 'right_eye'):
        x, y, width, height = REGIONS[feature]
        assert not np.any(peak[y:y+height, x:x+width].min(axis=2) > 220)


def test_new_emotions_are_available_through_real_app_host(tmp_path):
    played = []
    robot = SimpleNamespace(capture_token=lambda: None,
        face_animation=lambda name, *args, **kwargs: played.append(name) or {
            'ok': True, 'state': 'completed', 'displayed': True})
    host = AppHost(ROOT, tmp_path, robot, permissions_enabled=False)
    host.scan()
    try:
        for name in EMOTION_EXPRESSIONS:
            result = host.call('expressions', 'animate', {'name': name, 'with_audio': False})
            assert result['ok'], result
        assert played == list(EMOTION_EXPRESSIONS)
        assert local_expression_request(host, 'Robbi, schau nachdenklich, bitte!')['arguments']['name'] == 'thoughtful-look'
        assert local_expression_request(host, 'Look proud')['arguments']['name'] == 'proud-smile'
        # A user's own feelings are conversation, not a command to perform them.
        assert local_expression_request(host, 'I feel sad today') is None
        host.set_enabled('expressions', False)
        assert local_expression_request(host, 'Look proud') is None
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)
