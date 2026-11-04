import io
import json
import stat
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from robot import apps, config
from robot.focus_timer import FocusTimerManager
from robot.lifecycle import LifecycleMixin, UnavailableComponent
from robot.llm import LLMClient
from robot.presentation_pipeline import PresentationPipelineMixin
from robot.action_executor import ActionExecutorMixin
from robbi.jobs import BoundedExecutor


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', 'C:/outside.txt', '..\\outside.txt'])
def test_app_archive_cannot_escape(tmp_path, monkeypatch, name):
    monkeypatch.setattr(apps, 'APPS_DIR', tmp_path / 'apps')
    registry = apps.AppRegistry()
    with pytest.raises(ValueError):
        registry.install_zip('bad.zip', archive({'app.yaml': 'id: example', name: 'bad'}))
    assert not registry.list_apps()
    assert not (tmp_path/'outside.txt').exists()


def test_app_failed_update_keeps_previous(tmp_path, monkeypatch):
    monkeypatch.setattr(apps, 'APPS_DIR', tmp_path/'apps')
    registry=apps.AppRegistry()
    registry.install_zip('good.zip', archive({'app.yaml':'id: example\nname: Original\nsequence:\n  - speak: Hello'}))
    with pytest.raises(ValueError):
        registry.install_zip('bad.zip', archive({'app.yaml':'id: example\nprompt_file: ../outside'}))
    assert registry.get('example').name == 'Original'
    with pytest.raises(ValueError):
        registry.remove('../apps')
    assert registry.get('example') is not None


def test_app_rejects_symlink_and_multiple_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(apps, 'APPS_DIR', tmp_path/'apps')
    registry=apps.AppRegistry()
    with pytest.raises(ValueError):
        registry.install_zip('ambiguous.zip', archive({'one/app.yaml':'id: one','two/app.yaml':'id: two'}))
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as bundle:
        entry=zipfile.ZipInfo('link')
        entry.create_system=3
        entry.external_attr=(stat.S_IFLNK | 0o777)<<16
        bundle.writestr(entry, '../outside')
        bundle.writestr('app.yaml','id: example')
    with pytest.raises(ValueError):
        registry.install_zip('link.zip',output.getvalue())


def test_focus_session_survives_restart(tmp_path):
    path=tmp_path/'focus.json'
    first=FocusTimerManager(lambda *_:None,lambda:None,lambda:False,state_file=path)
    first.start(180)
    deadline=first.status()['ends_at']
    first.shutdown()
    second=FocusTimerManager(lambda *_:None,lambda:None,lambda:False,state_file=path)
    try:
        assert second.status()['active']
        assert second.status()['ends_at']==deadline
        second.stop()
        assert json.loads(path.read_text())['active'] is False
    finally:
        second.shutdown()


def test_corrupt_focus_state_is_reported_not_fatal(tmp_path):
    path=tmp_path/'focus.json';path.write_text('{broken')
    timer=FocusTimerManager(lambda *_:None,lambda:None,lambda:False,state_file=path)
    try:
        assert not timer.status()['active']
        assert timer.status()['error']
        assert path.read_text()=='{broken'
        with pytest.raises(ValueError):timer.start(float('inf'))
    finally:timer.shutdown()


def test_bad_settings_do_not_modify_files(tmp_path, monkeypatch):
    effective=tmp_path/'effective.yaml';source=tmp_path/'local.yaml'
    effective.write_text('voice:\n  enabled: false\n');source.write_text('voice:\n  enabled: false\n')
    monkeypatch.setattr(config,'CONFIG_FILE',effective);monkeypatch.setattr(config,'CONFIG_SOURCE',source)
    with pytest.raises(ValueError):config.update_settings_from_payload({'voice':{'command_wait_s':float('nan')}})
    with pytest.raises(ValueError):config.update_settings_from_payload({'telegram':{'enabled':'false'}})
    assert 'nan' not in effective.read_text().lower()
    source.write_text('invalid: [')
    with pytest.raises(ValueError):config.update_settings_from_payload({'voice':{'command_wait_s':10}})
    assert source.read_text()=='invalid: ['


def test_settings_persist_to_source_and_effective(tmp_path,monkeypatch):
    effective=tmp_path/'effective.yaml';source=tmp_path/'local.yaml'
    effective.write_text('voice:\n  command_wait_s: 8\n');source.write_text('ui:\n  debug_mode: false\n')
    monkeypatch.setattr(config,'CONFIG_FILE',effective);monkeypatch.setattr(config,'CONFIG_SOURCE',source)
    monkeypatch.setattr(config.settings,'voice_command_wait_s',8)
    config.update_settings_from_payload({'voice':{'command_wait_s':11}})
    assert 'command_wait_s: 11' in source.read_text()
    assert 'debug_mode: false' in source.read_text()
    assert 'command_wait_s: 11' in effective.read_text()


def test_stuck_component_does_not_block_other_start(monkeypatch):
    class Host(LifecycleMixin):pass
    host=Host();host._init_lifecycle();host._running=True
    release=threading.Event();ready=threading.Event()
    host.camera=SimpleNamespace(start=lambda:release.wait(3),stop=lambda:None)
    host.events=SimpleNamespace(start=ready.set,stop=lambda:None)
    monkeypatch.setattr(config.settings,'camera_enabled',True)
    monkeypatch.setattr(config.settings,'events_enabled',True)
    started=time.monotonic();host._start_component('camera');host._start_component('events')
    try:
        assert time.monotonic()-started<0.2
        assert ready.wait(1)
        assert not host.retry_component('camera')
    finally:
        host._running=False;release.set()


def test_failed_component_construction_has_independent_health():
    class Host(LifecycleMixin):pass
    host=Host();host._init_lifecycle()
    host.camera=host._component('camera',lambda:(_ for _ in ()).throw(ImportError('picamera2 missing')))
    host.memory=host._component('memory',lambda:SimpleNamespace(ready=True))
    assert isinstance(host.camera,UnavailableComponent)
    assert host.memory.ready
    assert {r['name']:r['state'] for r in host.health_snapshot()}=={'camera':'failed','memory':'ready'}


def test_wifi_failure_circuit_and_new_request_recovery(monkeypatch):
    monkeypatch.setattr(config.settings,'network_cooldown_s',30)
    monkeypatch.setattr(config.settings,'local_llm_base_url','http://127.0.0.1:9/v1')
    client=LLMClient(SimpleNamespace(),SimpleNamespace())
    calls=[]
    def complete(**kwargs):
        calls.append(kwargs)
        if len(calls)==1:raise TimeoutError('Wi-Fi link interrupted')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"speak":"Reconnected"}'))])
    monkeypatch.setattr(client,'_ensure_chat_client',lambda _:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete))))
    monkeypatch.setattr(client,'_system_prompt',lambda **_:'test')
    monkeypatch.setattr(client,'_openai_timeout',lambda *_:15)
    try:
        client.ask('one',provider='local')
        assert not client.available('local')
        assert 'Wi-Fi' in client.status()['last_error']
        client.ask('two',provider='local')
        assert len(calls)==1
        client._provider_states['local:chat']['retry_at']=0
        assert client.ask('new request',provider='local')['speak']=='Reconnected'
        assert len(calls)==2
        assert client.available('local')
        assert not client.status()['last_error']
    finally:client.shutdown()


def test_late_tts_after_stop_is_not_played():
    class Host(LifecycleMixin,PresentationPipelineMixin):pass
    host=Host();host._init_lifecycle()
    started=threading.Event();release=threading.Event();played=[]
    def synthesize(_):started.set();release.wait(2);return b'audio'
    host.llm=SimpleNamespace(synthesize=synthesize)
    host.media=SimpleNamespace(speak_wav_bytes=lambda *_,**__:played.append(True))
    host._update_pipeline_metrics=lambda *_args,**_kwargs:None
    host._log=SimpleNamespace(warning=lambda *_:None)
    executor=BoundedExecutor(max_workers=1,max_pending=1)
    future=host._submit_guarded(executor,host._speak_text,'hello')
    assert started.wait(1)
    host.cancel_actions();release.set();future.result(timeout=2)
    executor.shutdown(wait=False,cancel_futures=True)
    assert not played


def test_stop_is_immediate_while_face_device_is_stuck():
    class Host(LifecycleMixin,ActionExecutorMixin):pass
    host=Host();host._init_lifecycle()
    entered=threading.Event();release=threading.Event();drives=[]
    host._resolve_asset=lambda *_a,**_k:Path('face.png')
    def show(_):entered.set();release.wait(2)
    host.media=SimpleNamespace(show_face=show)
    host.behavior=SimpleNamespace(set_current_face=lambda _:None)
    host._set_presentation=lambda **_:None
    host._execute_drive_action=lambda spec:drives.append(spec)
    executor=BoundedExecutor(max_workers=1,max_pending=0)
    future=host._submit_guarded(executor,host._run_sequence_step,{'face':'happy','drive':{'direction':'forward','speed':20,'duration':0.1}})
    assert entered.wait(1)
    started=time.monotonic();host.cancel_actions()
    assert time.monotonic()-started<0.1
    release.set();future.result(timeout=2);executor.shutdown(wait=False,cancel_futures=True)
    assert not drives


def test_evicted_request_ids_fail_closed():
    class Host(LifecycleMixin):pass
    host=Host();host._init_lifecycle()
    host._register_request(1,0)
    host.cancel_actions()
    for number in range(2,260):host._register_request(number,1)
    assert not host._request_current(1)
    assert not host._request_current(1000)
    assert host._request_current(259)
