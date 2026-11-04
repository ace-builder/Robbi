"""Power requests are simulated: these tests never control the host OS."""
import subprocess
import threading
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError, UnavailableError
from robbi.services.host_management import HostManagement
from robbi.setup.host import power_policy
from test_core import configuration
from robbi.runtime import Runtime
from robbi.web import create_app


def runtime_stub():
    events = []
    return SimpleNamespace(config=SimpleNamespace(values={'runtime': {'allow_host_management': True}}),
        emergency_stop=lambda **kw: events.append(('stop', kw)),
        health=SimpleNamespace(set=lambda *args: events.append(('health', args))), events=events)


@pytest.mark.parametrize('operation,tail', [('shutdown',['poweroff']), ('restart',['restart','robbi-v2.service'])])
def test_acceptance_stops_actions_and_submits_only_fixed_nonblocking_commands(operation, tail):
    runtime = runtime_stub()
    def run(command, **kwargs):
        assert runtime.events[0] == ('stop', {'operator_stop': False})
        assert command == ['/usr/bin/systemctl','--no-ask-password','--no-block','--no-wall',*tail]
        assert kwargs == {'check':True,'timeout':4,'capture_output':True,'text':True}
    service = HostManagement(runtime, run=run, platform=lambda:'linux')
    result = service.request(operation)
    assert result['requested'] and 'accepted' in result['message']
    with pytest.raises(BusyError):service.request(operation)


@pytest.mark.parametrize('error', [FileNotFoundError(), subprocess.TimeoutExpired('systemctl',4),
    subprocess.CalledProcessError(1,'systemctl',stderr='Interactive authentication required')])
def test_failure_is_not_accepted_and_can_be_retried(error):
    runtime = runtime_stub()
    def fail(*args, **kwargs):raise error
    service = HostManagement(runtime,run=fail,platform=lambda:'linux')
    for _ in range(2):
        with pytest.raises(UnavailableError):service.request('shutdown')
    assert runtime.events[-1][1][1] == 'failed'


def test_no_host_command_on_unsupported_or_disabled_runtime():
    runtime = runtime_stub()
    service = HostManagement(runtime, run=lambda *a,**kw:pytest.fail('No command allowed'),platform=lambda:'win32')
    with pytest.raises(UnavailableError):service.request('shutdown')
    service.platform=lambda:'linux'
    runtime.config.values['runtime']['allow_host_management']=False
    with pytest.raises(UnavailableError):service.request('shutdown')
    with pytest.raises(ValueError):service.request('restart arbitrary.service')
    assert runtime.events == []


def test_concurrent_power_request_is_rejected_without_waiting():
    runtime=runtime_stub();entered=threading.Event();release=threading.Event()
    service=HostManagement(runtime,run=lambda *a,**kw:(entered.set(),release.wait(2)),platform=lambda:'linux')
    worker=threading.Thread(target=lambda:service.request('shutdown'));worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(BusyError):service.request('shutdown')
    finally:release.set();worker.join(2)


def test_http_surfaces_permission_error_and_persists_explicit_enable(tmp_path,monkeypatch):
    import robbi.services.host_management as module
    runtime=Runtime(configuration(tmp_path))
    def denied(*a,**kw):raise subprocess.CalledProcessError(1,'systemctl')
    monkeypatch.setattr(module,'HostManagement',lambda runtime:HostManagement(runtime,run=denied,platform=lambda:'linux'))
    client=create_app(runtime).test_client()
    try:
        assert client.post('/shutdown',json={}).status_code==403
        assert client.patch('/api/host-management',json={'enabled':'true'}).status_code==400
        response=client.patch('/api/host-management',json={'enabled':True})
        assert response.json=={'enabled':True,'supported':True}
        assert 'allow_host_management: true' in runtime.config.source.read_text()
        response=client.post('/shutdown',json={})
        assert response.status_code==503 and 'rejected' in response.json['error']
        assert client.post('/api/stop',json={}).status_code==200
        assert client.post('/shutdown',json={},headers={'Origin':'https://other.invalid'}).status_code==403
        assert client.patch('/api/host-management',json={'enabled':False}).status_code==200
        assert client.post('/restart_service',json={}).status_code==403
    finally:runtime.jobs.stop()


@pytest.mark.parametrize('user',['bad\nuser','maker"; return true; //','',None,'two users'])
def test_policy_refuses_injected_users(user):
    with pytest.raises(ValueError):power_policy(user)


def test_policy_only_grants_poweroff_and_exact_robot_service_restart():
    policy=power_policy('maker')
    assert 'subject.user !== "maker"' in policy
    assert 'action.lookup("unit") === "robbi-v2.service"' in policy
    assert 'action.lookup("verb") === "restart"' in policy
    assert 'power-off-multiple-sessions' in policy
    assert 'ignore-inhibit' not in policy and 'reboot' not in policy
