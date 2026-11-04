"""Authoring/export parity and playback contracts; no GPIO or robot connection."""
import copy
from concurrent.futures import Future
import json
import math
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

from flask import Flask
import pytest

from robbi.errors import UnavailableError, ValidationError
from robbi.motion_studio import MotionStudio, DEFAULTS, FORMAT, compile_project, validate_project
from robbi.motion_studio_web import blueprint
from robbi.orchestration import Orchestrator
from robbi.web import install_error_handlers
from robot.curve_path import CurveFollower, CurvePath

ROOT = Path(__file__).resolve().parents[1]


def point(x=0, y=0, heading=0, **values):
    return dict(x=x, y=y, heading=heading, direction='forward', path='curve', speed=12, hold=0, tension=.38, face='happy', **{}) | values


def clip(*points):
    return dict(format=FORMAT, name='Test clip', settings=dict(DEFAULTS), waypoints=list(points or [point(), point(30)]))


@pytest.mark.parametrize('edit', [lambda p: p.update(format='bad'), lambda p: p.update(settings=[]),
    lambda p: p.update(waypoints=[]), lambda p: p['settings'].update(wheelBase=True),
    lambda p: p['waypoints'][0].update(x=float('nan')), lambda p: p['waypoints'][0].update(hold=-1),
    lambda p: p['waypoints'][0].update(heading=181), lambda p: p['waypoints'][0].update(face='../unsafe')])
def test_invalid_files_are_rejected(edit):
    project = clip()
    edit(project)
    with pytest.raises(ValidationError):
        validate_project(project)


def test_format_is_declarative_and_strips_actuator_overrides():
    project = clip()
    project['drive'] = {'ignore_obstacles': True}
    project['waypoints'][0]['pwm'] = 100
    clean = validate_project(project)
    assert 'drive' not in clean and 'pwm' not in clean['waypoints'][0]


def test_browser_and_robot_geometry_and_timing_agree():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for cross-language format validation')
    script = "import {compile,example,CUE_DEFAULTS} from './web/motion-studio/motion.js'; const clips=['hello','wiggle','reverse','square'].map(n=>example(n)); const media=example('reverse'); media.waypoints[0].cue={...CUE_DEFAULTS,speech:'Hi 🤖!',sound:'media-sound',duration:2}; media.waypoints[2].cue={...CUE_DEFAULTS,screen:'video',media:'media-movie',duration:3,wait:false}; clips.push(media); console.log(JSON.stringify(clips.map(compile)));"
    result = subprocess.run([node, '--input-type=module', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True, encoding='utf-8')
    for browser in json.loads(result.stdout):
        robot = compile_project(browser['project'])
        assert robot['warnings'] == browser['warnings'] == []
        for key in ('duration', 'distance', 'left', 'right'):
            assert robot[key] == pytest.approx(browser[key], abs=1e-8)
        assert len(robot['segments']) == len(browser['segments'])
        assert len(robot['cues']) == len(browser['cues'])
        assert len(robot['beats']) == len(browser['beats'])
        for a, b in zip(robot['segments'], browser['segments']):
            for sample_a, sample_b in zip(a['samples'], b['samples']):
                for key in ('x', 'y', 'heading', 'left', 'right', 's', 't', 'v'):
                    assert sample_a[key] == pytest.approx(sample_b[key], abs=1e-8)


@pytest.mark.parametrize('reverse', [False, True])
def test_authored_curves_follow_body_headings_and_signed_wheels(reverse):
    project = clip(point(), point(-30 if reverse else 30, 15, -45 if reverse else 45,
                                  direction='reverse' if reverse else 'forward'))
    plan = compile_project(project)
    assert not plan['warnings']
    samples = plan['segments'][0]['samples']
    values = [[p[k] for k in ('s', 'x', 'y', 'heading', 'curvature')] for p in samples]
    path = CurvePath.authored(values, -1 if reverse else 1, DEFAULTS['wheelBase'])
    assert path.points[-1].x == pytest.approx(project['waypoints'][-1]['x'])
    assert path.wheel_cm(path.length_cm) == pytest.approx((plan['left'], plan['right']))
    follower = CurveFollower(path, .01)
    left, right = follower.command((0, 0), 35, 15, 35, .1, 0, 0, 100, .02, 200)
    assert left*path.direction > 0 and right*path.direction > 0
    assert abs(left) <= 4 and abs(right) <= 4


@pytest.mark.parametrize('values,direction', [([[0,0,0,0,0],[10,10,0,0,0]],0),
    ([[0,0,0,0,0],[0,1,0,0,0]],1), ([[0,0,0,0,0],[501,501,0,0,0]],1),
    ([[0,0,0,0,0],[1,1,0,math.pi,0]],1), ([[1,0,0,0,0],[2,1,0,0,0]],1),
    ([[0,0,0,0,0],[10,10,0,0,float('nan')]],1)])
def test_native_path_rejects_unbounded_or_inner_wheel_reversal(values, direction):
    with pytest.raises(ValueError):
        CurvePath.authored(values, direction, 10.2)


def test_library_roundtrip_and_paths_are_bounded(tmp_path):
    studio = MotionStudio(ROOT, tmp_path)
    saved = studio.save(clip())
    assert studio.get(saved['id']) == validate_project(clip())
    assert studio.library()[0]['name'] == 'Test clip'
    assert studio.save(clip(point(5), point(30)), saved['id'])['id'] == saved['id']
    assert len(studio.library()) == 1
    with pytest.raises(ValidationError):
        studio.get('../config')
    assert not studio.status()['ready']
    with pytest.raises(UnavailableError):
        studio.play(saved['id'])
    studio.delete(saved['id'])
    assert studio.library() == []


class MeasuredMotion:
    def __init__(self, *, error=0, fail=False, missing_odometry=False):
        self.specs, self.error, self.fail, self.missing_odometry = [], error, fail, missing_odometry
        self.stops = []
    def status(self):
        return dict(available=True, encoder_enabled=True, obstacle=dict(enabled=True, blocked=False, distance_cm=100),
                    config=dict(wheel_base_cm=10.2, wheel_diameter_cm=6.2, pwm_min_duty=28))
    def drive(self, spec, **kwargs):
        self.specs.append(spec)
        if spec['mode'] == 'studio_path':
            last = spec['path'][-1]
            distance, angle = spec['path_direction']*last[0], last[3]
            result = dict(ok=not self.fail, final=dict(left=(distance-angle*5.1)*100, right=(distance+angle*5.1)*100),
                          odometry=dict(x_cm=last[1]+self.error, y_cm=last[2], heading_rad=angle), reason='encoder_stall')
            if self.missing_odometry:
                result.pop('odometry')
        else:
            angle = math.radians(spec['angle_deg'])
            result = dict(ok=True, final=dict(left=-angle*510, right=angle*510))
        future = Future()
        future.set_result(result)
        return future
    def cm_per_count(self):
        return .01
    def is_running(self):
        return False
    def stop(self, **kwargs):
        self.stops.append(kwargs)


def runtime_for(motion):
    controller = SimpleNamespace(motion=motion, media=SimpleNamespace(show_face=lambda *a, **kw: None), orchestrator=Orchestrator())
    return SimpleNamespace(config=SimpleNamespace(values={'runtime': {'calibration_confirmed': True}}), controller=lambda: controller)


def test_robot_player_uses_signed_paths_and_settled_turns(tmp_path):
    motion = MeasuredMotion()
    studio = MotionStudio(ROOT, tmp_path, runtime_for(motion))
    saved = studio.save(clip(point(), point(-30, direction='reverse'), point(-30, heading=45)))
    studio.play(saved['id'])
    studio._thread.join(2)
    assert studio.status()['playback']['state'] == 'completed'
    assert motion.specs[0]['mode'] == 'studio_path' and motion.specs[0]['path_direction'] == -1
    assert motion.specs[1]['mode'] == 'turn'
    assert all(spec['require_encoders'] for spec in motion.specs)
    assert all(28 <= spec['speed'] <= DEFAULTS['motorPower'] for spec in motion.specs)
    assert all('ignore_obstacles' not in spec for spec in motion.specs)
    assert motion.stops


@pytest.mark.parametrize('options,reason', [({'error':8}, 'drift'), ({'fail':True}, 'encoder_stall'), ({'missing_odometry':True}, 'odometry')])
def test_robot_player_stops_on_drift_missing_receipts_or_motion_failure(tmp_path, options, reason):
    motion = MeasuredMotion(**options)
    studio = MotionStudio(ROOT, tmp_path, runtime_for(motion))
    saved = studio.save(clip(point(), point(30), point(60)))
    studio.play(saved['id'])
    studio._thread.join(2)
    status = studio.status()['playback']
    assert status['state'] == 'failed' and reason in status['reason']
    assert len(motion.specs) == 1 and motion.stops


def test_stop_during_pause_prevents_all_subsequent_motion(tmp_path):
    motion = MeasuredMotion()
    studio = MotionStudio(ROOT, tmp_path, runtime_for(motion))
    saved = studio.save(clip(point(hold=3), point(30)))
    studio.play(saved['id'])
    studio.stop()
    studio._thread.join(2)
    assert studio.status()['playback']['state'] == 'cancelled'
    assert motion.specs == []


def test_different_geometry_and_unachievable_paths_cannot_play(tmp_path):
    studio = MotionStudio(ROOT, tmp_path, runtime_for(MeasuredMotion()))
    project = clip()
    project['settings']['wheelBase'] = 9
    saved = studio.save(project)
    with pytest.raises(ValidationError, match='geometry'):
        studio.play(saved['id'])
    saved = studio.save(clip(point(), point(2, 2, 180)))
    with pytest.raises(ValidationError, match='tight|folds'):
        studio.play(saved['id'])


def test_http_library_validation_and_preview_only_playback(tmp_path):
    app = Flask(__name__)
    install_error_handlers(app)
    app.register_blueprint(blueprint(ROOT, tmp_path))
    client = app.test_client()
    assert client.get('/motion-studio').status_code == 200
    saved = client.post('/api/motion-studio/library', json={'project':clip()})
    assert saved.status_code == 201
    clip_id = saved.json['id']
    assert client.get('/api/motion-studio/library/'+clip_id).json['project'] == validate_project(clip())
    assert client.post('/api/motion-studio/validate', json={'project':clip()}).json['warnings'] == []
    assert client.post('/api/motion-studio/play', json={'id':clip_id}).status_code == 503
    assert not client.get('/api/motion-studio/status').json['ready']
    assert client.delete('/api/motion-studio/library/'+clip_id).status_code == 200
    assert client.get('/api/motion-studio/library/'+clip_id).status_code == 404
    assert client.post('/api/motion-studio/library', json={'project':{'name':'bad'}}).status_code == 400
