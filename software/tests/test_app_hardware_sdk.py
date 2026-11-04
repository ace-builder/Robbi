from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest
import yaml

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.robot_port import RuntimeRobotPort
from robbi.app_runtime.sdk import AppContext, AppCancelled, Artifacts, Cancellation
from robbi.errors import ValidationError
from robbi.orchestration import BACKGROUND, MANUAL, Intent, Orchestrator, ResourceBusy, intent_scope
from robot.imu import IMUService
from robot.media import Display
from robot.tof import ToFService


def make_robot(tmp_path):
    stop = threading.Event()
    coordinator = Orchestrator()
    writes = []
    display = Display(coordinator)
    display._disp = SimpleNamespace(display=lambda frame: writes.append(frame.copy()))
    controller = SimpleNamespace(orchestrator=coordinator, _action_token=lambda: (0, stop),
        _action_current=lambda generation: not stop.is_set(), _action_local=threading.local(),
        _set_presentation=lambda **kw: None, tof=ToFService(), imu=IMUService(),
        media=SimpleNamespace(display=display, health=lambda: {
            'display': {'enabled': True, 'available': True}, 'audio': {'available': False}}))
    runtime = SimpleNamespace(controller=lambda: controller, snapshot=lambda: {}, health_snapshot=lambda: {})
    return controller, RuntimeRobotPort(runtime), writes


def context(tmp_path, port, permissions=(), name='test-app', duration=10):
    cancel = Cancellation(threading.Event(), None, time.monotonic() + duration)
    return AppContext(app_id=name, config={}, permissions=permissions, directory=tmp_path,
        artifacts=Artifacts(tmp_path / name / 'images', name), cancelled=cancel, deadline=cancel.deadline,
        robot=port, robot_token=port.capture_token(), services=lambda _: None, project_root=tmp_path)


@pytest.mark.parametrize('call', [lambda api: api.tof(), lambda api: api.imu(),
    lambda api: api.camera_snapshot(), lambda api: api.assets('sounds'),
    lambda api: api.play_sound('hello'), lambda api: api.play_sound_file('sound.wav'),
    lambda api: api.show_face('happy'), lambda api: api.display_text('Hi'), lambda api: api.play_video('dance')])
def test_new_capabilities_are_denied_before_hardware_access(tmp_path, call):
    port = SimpleNamespace(capture_token=lambda: None)
    with pytest.raises(ValidationError, match='permission'):
        call(context(tmp_path, port).robot)


def test_sensor_reads_recompute_freshness_and_cannot_mutate_service(tmp_path, monkeypatch):
    from robot.config import settings
    monkeypatch.setattr(settings, 'tof_enabled', True)
    monkeypatch.setattr(settings, 'tof_h2_enabled', True)
    monkeypatch.setattr(settings, 'imu_enabled', True)
    controller, port, _ = make_robot(tmp_path)
    now = time.monotonic()
    controller.tof._status['h1'].update(available=True, distance_cm=25, last_read_ts=now)
    controller.tof._status['h2'].update(available=True, distance_cm=40, last_read_ts=now - 5)
    controller.imu._data.update(available=True, last_sample_monotonic=now, acceleration_m_s2=[0, 0, 9.8],
                               quaternion=[1, 0, 0, 0], orientation_deg={'yaw': 12})
    api = context(tmp_path, port, ['robot.tof', 'robot.imu']).robot
    assert api.tof()['distance_cm'] == 25
    stale = api.tof('h2')
    assert stale['state'] == 'stale' and stale['distance_cm'] is None
    reading = api.imu()
    assert reading['fresh'] and reading['heading_reference'] == 'relative_gyro_no_magnetometer'
    reading['acceleration_m_s2'][0] = 99
    assert controller.imu._data['acceleration_m_s2'][0] == 0
    controller.imu._data['last_sample_monotonic'] = now - 5
    stale = api.imu()
    assert stale['quaternion'] is None and stale['orientation_deg'] is None and not stale['ok']
    assert controller.tof._thread is None and controller.imu._thread is None


def test_missing_sensor_component_stays_a_readable_snapshot(tmp_path):
    from robot.lifecycle import UnavailableComponent
    controller, port, _ = make_robot(tmp_path)
    controller.tof = UnavailableComponent('tof', 'cable disconnected')
    controller.imu = UnavailableComponent('imu', 'dependency missing')
    api = context(tmp_path, port, ['robot.tof', 'robot.imu']).robot
    assert api.tof()['state'] == 'unavailable' and api.tof()['distance_cm'] is None
    assert api.imu()['state'] == 'unavailable' and 'dependency missing' in api.imu()['reason']


def test_app_display_hold_blocks_ambient_but_next_app_can_replace_idle_image(tmp_path):
    controller, port, writes = make_robot(tmp_path)
    first = context(tmp_path, port, ['robot.display'], 'first')
    second = context(tmp_path, port, ['robot.display'], 'second')
    result = first.robot.display_text('First image', hold_s=10)
    assert result['displayed'] and writes[-1].size == (280, 240)
    with intent_scope(Intent('ambient', BACKGROUND)):
        with pytest.raises(ResourceBusy):
            controller.media.display.show_image(first._artifacts.path(result['artifact']['id']))
    assert len(writes) == 1
    assert second.robot.display_text('Second image')['displayed']
    with pytest.raises(AppCancelled):
        first.robot.display_text('Late first image')
    assert len(writes) == 2


def test_scene_is_atomic_and_checks_its_deadline_on_exit(tmp_path):
    controller, port, _ = make_robot(tmp_path)
    ctx = context(tmp_path, port, ['robot.audio', 'robot.display'])
    lease = controller.orchestrator.acquire(('audio',), intent=Intent('manual sound', MANUAL))
    try:
        with pytest.raises(ResourceBusy):
            with ctx.robot.session('display', 'audio'):
                pytest.fail('Conflicting scene started')
        assert 'display' not in controller.orchestrator.snapshot()['resources']
    finally:
        lease.release()
    with pytest.raises(AppCancelled, match='expired'):
        with ctx.robot.session('display', duration_s=.05):
            threading.Event().wait(.065)
    assert not controller.orchestrator.snapshot()['resources']


def test_sound_permission_cannot_be_inferred_from_speech_permission(tmp_path):
    controller, port, _ = make_robot(tmp_path)
    ctx = context(tmp_path, port, ['robot.speak'])
    with ctx.robot.session('audio'):
        with pytest.raises(ValidationError, match='permission'):
            ctx.robot.play_sound('anything')
    assert not controller.orchestrator.snapshot()['resources']


@pytest.mark.parametrize('spec', [{'direction': 'forward', 'duration': 30}, {'direction': 'forward', 'speed': 101},
                                  {'direction': 'stop'}, {'direction': 'forward', 'speed': float('nan')}])
def test_sdk_motion_cannot_bypass_limits(tmp_path, spec):
    _, port, _ = make_robot(tmp_path)
    with pytest.raises(ValidationError):
        context(tmp_path, port, ['robot.motion']).robot.move(**spec)


def test_sdk_motion_defaults_to_full_power(tmp_path):
    calls=[]
    def move(spec,*args):calls.append(spec);return {'ok':True}
    port=SimpleNamespace(capture_token=lambda:None,move=move)
    api=context(tmp_path,port,['robot.motion']).robot
    api.move(direction='forward')
    api.move(direction='left',speed=35)
    assert [spec['speed'] for spec in calls]==[100,35]


def test_cancelling_app_motion_stops_only_that_owner(tmp_path):
    controller, port, _ = make_robot(tmp_path)
    started, stopped = threading.Event(), []
    future = Future()
    def drive(*args, **kwargs):
        started.set()
        return future
    controller.motion = SimpleNamespace(status=lambda: {'available': True}, drive=drive,
                                        stop=lambda *, owner: stopped.append(owner))
    ctx = context(tmp_path, port, ['robot.motion'])
    with ThreadPoolExecutor(1) as worker:
        result = worker.submit(ctx.robot.move, direction='forward')
        assert started.wait(1)
        ctx.cancelled.event.set()
        with pytest.raises(AppCancelled):
            result.result(timeout=1)
    assert stopped == [ctx.robot._intent]


def test_speech_preparation_holds_no_output_and_reports_provider_failure(tmp_path):
    controller, port, _ = make_robot(tmp_path)
    controller.media.health = lambda: {'audio': {'available': True}}
    def prepare(text):
        assert not controller.orchestrator.snapshot()['resources']
        return {'tts_error': 'offline'}
    controller._prepare_tts_audio = prepare
    result = context(tmp_path, port, ['robot.speak']).robot.speak('Hello')
    assert result['state'] == 'unavailable' and not result['ok']


def test_project_sound_path_is_confined(tmp_path):
    _, port, _ = make_robot(tmp_path)
    with pytest.raises(ValidationError, match='escapes'):
        context(tmp_path, port, ['robot.audio']).robot.play_sound_file('../outside.wav')


def test_screen_text_fits_wide_glyphs_and_reports_truncation():
    from PIL import ImageChops
    from robbi.app_runtime.screen import text_image
    data, truncated = text_image('W' * 500)
    image = Image.open(io.BytesIO(data))
    bounds = ImageChops.difference(image, Image.new('RGB', image.size, 'white')).getbbox()
    assert truncated and image.size == (280, 240)
    assert bounds[0] >= 10 and bounds[2] <= 270 and bounds[3] <= 235


def test_app_busy_result_preserves_owner_without_marking_app_broken(tmp_path):
    source = tmp_path / 'source'
    project = source / 'apps' / 'test-app'
    project.mkdir(parents=True)
    (project / 'main.py').write_text('def run(context, tool, arguments):\n    return {}\n', encoding='utf-8')
    (project / 'app.yaml').write_text(yaml.safe_dump({'api_version': 2, 'id': 'test-app',
        'tools': [{'name': 'test', 'description': 'Test hardware'}]}), encoding='utf-8')
    _, port, _ = make_robot(tmp_path)
    host = AppHost(source, tmp_path / 'data', port, permissions_enabled=False)
    host.scan()
    def busy(*args):
        raise ResourceBusy('display is in use', ['display'], {'display': {'owner': 'other-app'}})
    host._handler = lambda project: busy
    try:
        result = host.call('test-app', 'test', {})
        assert result['busy'] and result['owners']['display']['owner'] == 'other-app'
        assert host.detail('test-app')['app']['health']['state'] == 'ready'
    finally:
        host.stop()
