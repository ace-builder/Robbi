"""Physical demonstration contracts using cached fake sensors, never hardware."""
import copy
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from flask import Flask
import pytest

from robbi.errors import BusyError, UnavailableError, ValidationError
from robbi.motion_capture import PoseTracker, to_project
from robbi.motion_studio import MotionStudio, DEFAULTS, compile_project
from robbi.motion_studio_web import blueprint
from robbi.orchestration import Intent, Orchestrator
from robbi.web import install_error_handlers

ROOT = Path(__file__).resolve().parents[1]


def imu(yaw=0, roll=0, **fields):
    y, r = math.radians(yaw)/2, math.radians(roll)/2
    return dict(fresh=True, calibration='complete', sample_count=100, sample_gaps=0,
                quaternion=[math.cos(y)*math.cos(r), math.cos(y)*math.sin(r), math.sin(y)*math.sin(r), math.sin(y)*math.cos(r)]) | fields


def motion(left=0, right=None, **fields):
    return dict(available=True, encoder_enabled=True, counts=dict(left=left, right=left if right is None else right),
                config=dict(wheel_base_cm=10.2, wheel_diameter_cm=6.2, encoder_counts_per_rev=math.pi*6.2*100)) | fields


def test_relative_pose_ignores_initial_heading_and_handles_reverse_and_yaw_wrap():
    tracker = PoseTracker(imu(170), motion(100), 'wheels')
    p = tracker.update(imu(-170), motion(200), .1)
    assert p['heading'] == pytest.approx(20)
    assert p['x'] == pytest.approx(math.sin(math.radians(20))/math.radians(20))
    assert p['y'] > 0
    p = tracker.update(imu(-170), motion(100), .2)
    assert p['v'] == pytest.approx(-10)
    assert p['left'] == pytest.approx(0)


def test_pcb_mounting_does_not_change_relative_world_heading():
    tracker = PoseTracker(imu(47, 90), {}, 'imu')
    p = tracker.update(imu(57, 90), {}, .1)
    assert p['heading'] == pytest.approx(10)
    assert p['roll'] == pytest.approx(0, abs=1e-6)
    assert p['x'] == p['y'] == 0


@pytest.mark.parametrize('reading,counts,elapsed,reason', [
    (imu(fresh=False), motion(), .1, 'Fresh'),
    (imu(sample_gaps=1), motion(), .1, 'missed'),
    (imu(sample_count=1), motion(), .1, 'restarted'),
    (imu(roll=25), motion(), .1, 'tilted'),
    (imu(), motion(10000), .1, 'jumped'),
    (imu(), motion(encoder_enabled=False), .1, 'unavailable'),
    (imu(), motion(), .4, 'interrupted'),
    (imu(100), motion(), .05, 'jumped'),
])
def test_tracking_stops_on_untrustworthy_samples(reading, counts, elapsed, reason):
    tracker = PoseTracker(imu(), motion(), 'wheels')
    with pytest.raises(UnavailableError, match=reason):
        tracker.update(reading, counts, elapsed)


def sample(t, x=0, y=0, heading=0, distance=0):
    return dict(t=t, x=x, y=y, heading=heading, left=distance, right=distance, roll=0, pitch=0)


def take(samples):
    return dict(name='Taught movement', settings=DEFAULTS, samples=samples)


def test_conversion_keeps_reverse_pauses_turns_and_original_take():
    samples = [sample(i/20) for i in range(21)]
    samples += [sample(1+i/20, x=i/2, distance=i/2) for i in range(1, 41)]
    samples += [sample(3+i/20, x=20, distance=20) for i in range(1, 21)]
    samples += [sample(4+i/20, x=20-i/2, distance=20-i/2) for i in range(1, 21)]
    samples += [sample(5+i/20, x=10, distance=10, heading=90*i/40) for i in range(1, 41)]
    raw = take(samples)
    before = copy.deepcopy(raw)
    project = to_project(raw)
    points = project['waypoints']
    assert any(p['direction'] == 'reverse' for p in points)
    assert sum(p['hold'] for p in points) >= 1.5
    assert points[-1]['heading'] == pytest.approx(90)
    assert points[-1]['x'] == pytest.approx(10, abs=1)
    assert any(p['speed'] == pytest.approx(10) for p in points[1:])
    assert not compile_project(project)['warnings']
    assert raw == before


def test_full_rotation_does_not_collapse_to_zero_or_shortest_turn():
    project = to_project(take([sample(i/20, heading=360*i/100) for i in range(101)]))
    plan = compile_project(project)
    assert sum(s['duration'] for s in plan['segments'] if s['kind'] == 'turn') >= 4
    assert len(project['waypoints']) >= 5
    assert not plan['warnings']


def test_conversion_handles_windows_crossing_from_driving_to_turning():
    samples = []
    for i in range(201):
        t = i/20
        if t < 1.13:
            x, h, d = t*10, 0, t*10
        elif t < 2.17:
            x, h, d = 11.3, (t-1.13)*40, 11.3
        else:
            x, h, d = 11.3, 41.6, 11.3
        samples.append(sample(t, x=x, heading=h, distance=d))
    project = to_project(take(samples))
    assert len(project['waypoints']) <= 5
    assert not compile_project(project)['warnings']
    assert project['waypoints'][-1]['heading'] == pytest.approx(41.6)


def test_long_still_take_is_split_into_supported_pauses():
    p = to_project(take([sample(i/20) for i in range(1200)]))
    assert len(p['waypoints']) == 2
    assert sum(w['hold'] for w in p['waypoints']) == pytest.approx(59.95)
    assert max(w['hold'] for w in p['waypoints']) <= 30


@pytest.mark.parametrize('tolerance', [True, None, float('nan'), 0, 11])
def test_conversion_validates_detail(tolerance):
    with pytest.raises(ValidationError):
        to_project(take([sample(0), sample(1)]), tolerance)


class Sensors:
    def __init__(self):
        self.reading = imu()
        self.position = motion()
        self.stops = 0
        self.controller = SimpleNamespace(
            imu=SimpleNamespace(status=lambda: copy.deepcopy(self.reading), calibrate=lambda: dict(ok=True)),
            motion=SimpleNamespace(status=lambda: copy.deepcopy(self.position), stop=self.stop, is_running=lambda: False),
            orchestrator=Orchestrator())
        self.runtime = SimpleNamespace(controller=lambda: self.controller,
                                       config=SimpleNamespace(values={'runtime': {'calibration_confirmed': True}}))

    def stop(self):
        self.stops += 1


def wait_until(check):
    deadline = time.monotonic()+3
    while not check():
        assert time.monotonic() < deadline, 'Recording did not settle'
        threading.Event().wait(.01)


def test_recording_holds_motor_lock_without_driving_saves_converts_and_recovers(tmp_path):
    sensors = Sensors()
    studio = MotionStudio(ROOT, tmp_path, sensors.runtime)
    capture = studio.capture
    started = capture.start({'name': 'My demo'})
    try:
        assert sensors.stops == 1
        with pytest.raises(BusyError):
            sensors.controller.orchestrator.acquire(['motion'], intent=Intent('manual', 80))
        with pytest.raises(BusyError):
            capture.start({})
        with pytest.raises(BusyError):
            capture.convert(started['id'], 1.5)
        with pytest.raises(ValidationError):
            capture.stop('0'*32)
        wait_until(lambda: capture.status()['count'] >= 3)
        sensors.position = motion(50)
        wait_until(lambda: capture.status()['pose']['x'] > .4)
    finally:
        capture.stop(started['id'])
    assert capture.status()['state'] == 'completed'
    with sensors.controller.orchestrator.acquire(['motion'], intent=Intent('manual', 80)):
        pass
    assert len(capture.library()) == 1
    saved = capture.convert(started['id'], 1.5)
    assert studio.get(saved['id']) == saved['project']
    recovered = MotionStudio(ROOT, tmp_path, sensors.runtime).capture
    assert recovered.get(started['id']) == capture.get(started['id'])
    recovered.delete(started['id'])
    assert recovered.library() == []


@pytest.mark.parametrize('failure', ['disconnect', 'sensor', 'stop'])
def test_interruption_keeps_partial_take_and_releases_ownership(tmp_path, failure):
    sensors = Sensors()
    capture = MotionStudio(ROOT, tmp_path, sensors.runtime).capture
    started = capture.start({'mode': 'imu'})
    try:
        wait_until(lambda: capture.status()['count'] >= 2)
        if failure == 'disconnect':
            capture.heartbeat = time.monotonic()-6
        elif failure == 'sensor':
            sensors.reading['fresh'] = False
        else:
            sensors.controller.orchestrator.cancel_all('Operator stop')
        wait_until(lambda: not capture.active())
        assert capture.get(started['id'])['samples']
        assert len(capture.library()) == 1
        assert capture.status()['state'] != 'recording'
        with sensors.controller.orchestrator.acquire(['motion'], intent=Intent('manual', 80)):
            pass
    finally:
        capture.stop()


def test_http_pc_unavailable_and_robot_recording_roundtrip(tmp_path):
    sensors = Sensors()
    app = Flask(__name__)
    install_error_handlers(app)
    app.register_blueprint(blueprint(ROOT, tmp_path, sensors.runtime))
    client = app.test_client()
    assert client.get('/api/motion-studio/capture').json['capabilities']['wheels']
    response = client.post('/api/motion-studio/capture', json={'mode': 'wheels', 'name': 'Demo'})
    assert response.status_code == 201
    take_id = response.json['id']
    assert client.post('/api/motion-studio/capture/poll', json={'id': take_id}).json['state'] == 'recording'
    assert client.post('/api/motion-studio/capture/calibrate', json={}).status_code == 400
    threading.Event().wait(.12)
    assert client.post('/api/motion-studio/capture/stop', json={'id': take_id}).json['state'] == 'completed'
    raw = client.get('/api/motion-studio/capture/takes/'+take_id)
    assert 'attachment' in raw.headers['Content-Disposition']
    assert len(raw.json['samples']) >= 2
    result = client.post('/api/motion-studio/capture/takes/'+take_id+'/convert', json={'tolerance': 1.5})
    assert result.status_code == 201
    assert client.get('/api/motion-studio/library/'+result.json['id']).json['project'] == result.json['project']
    assert client.delete('/api/motion-studio/capture/takes/'+take_id).status_code == 200
    with pytest.raises(UnavailableError):
        MotionStudio(ROOT, tmp_path/'pc').capture.start({})


def test_disk_failure_keeps_take_downloadable_and_prevents_overwrite(tmp_path, monkeypatch):
    sensors = Sensors()
    capture = MotionStudio(ROOT, tmp_path, sensors.runtime).capture
    def fail_write(*args):
        raise OSError('Disk full')
    monkeypatch.setattr('robbi.motion_capture.atomic_write', fail_write)
    started = capture.start({'mode': 'imu'})
    capture.stop(started['id'])
    assert capture.status()['state'] == 'unsaved'
    assert capture.get(started['id'])['samples']
    with pytest.raises(ValidationError, match='unsaved'):
        capture.start({'mode': 'imu'})
    capture.delete(started['id'])
    assert capture.status()['state'] == 'idle'
