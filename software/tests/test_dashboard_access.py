"""Optional dashboard sign-in, persistence and owner access without devices."""
import base64
from pathlib import Path

import pytest
import yaml

from robbi.config import load, validate
from robbi.errors import ValidationError
from robbi.runtime import Runtime
from robbi.web import create_app


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "test-dashboard-password")
    source = tmp_path / "robot.yaml"
    source.write_text("camera: {fps: 2}\napps: {permissions_enabled: true}\n")
    runtime = Runtime(load(ROOT, source, tmp_path / "var"))
    client = create_app(runtime).test_client()
    client.environ_base["REMOTE_ADDR"] = "192.168.1.50"
    yield runtime, client
    runtime.stop()


def test_default_trusted_lan_access_ignores_saved_token(dashboard):
    runtime, client = dashboard
    assert runtime.config.values["server"]["auth_enabled"] is False
    assert client.get("/healthz").status_code == 200
    result = client.get("/api/dashboard-access").get_json()
    assert result == {"auth_enabled": False, "password_configured": True, "restart_required": False}
    assert "test-dashboard-password" not in str(result)


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_auth_flag_requires_boolean(value):
    with pytest.raises(ValidationError, match="true or false"):
        validate({"server": {"auth_enabled": value}})


def test_live_switch_persists_and_requires_login_until_authenticated_disable(dashboard):
    runtime, client = dashboard
    result = client.post("/api/dashboard-access", json={"auth_enabled": True})
    assert result.status_code == 200 and result.get_json()["restart_required"] is False
    assert client.get("/healthz").status_code == 401
    assert client.get("/healthz", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/dashboard-access", json={"auth_enabled": False}).status_code == 401
    saved = yaml.safe_load(runtime.config.source.read_text())
    assert saved == {"camera": {"fps": 2}, "apps": {"permissions_enabled": True}, "server": {"auth_enabled": True}}
    restarted = load(ROOT, runtime.config.source, runtime.config.data_dir)
    assert restarted.values["server"]["auth_enabled"] is True
    password = base64.b64encode(b"robbi:test-dashboard-password").decode()
    headers = {"Authorization": "Basic " + password}
    assert client.get("/healthz", headers=headers).status_code == 200
    result = client.post("/api/dashboard-access", json={"auth_enabled": False}, headers=headers)
    assert result.status_code == 200
    assert client.get("/healthz").status_code == 200
    assert yaml.safe_load(runtime.config.source.read_text())["server"]["auth_enabled"] is False
    assert runtime.config.values["apps"]["permissions_enabled"] is True


def test_no_password_or_failed_save_cannot_enable_sign_in(dashboard, monkeypatch):
    runtime, client = dashboard
    before = runtime.config.source.read_bytes()
    monkeypatch.delenv("ROBBI_WEB_TOKEN")
    assert client.post("/api/dashboard-access", json={"auth_enabled": True}).status_code == 400
    assert client.post("/api/config", json={"yaml": "server: {auth_enabled: true}"}).status_code == 400
    assert runtime.config.source.read_bytes() == before
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "test-dashboard-password")
    def failed_save(_):
        raise OSError("disk full")
    monkeypatch.setattr(runtime.config, "save", failed_save)
    assert client.post("/api/dashboard-access", json={"auth_enabled": True}).status_code == 503
    assert runtime.config.source.read_bytes() == before
    assert client.get("/healthz").status_code == 200


def test_missing_runtime_password_fails_closed(dashboard, monkeypatch):
    runtime, client = dashboard
    runtime.config.values["server"]["auth_enabled"] = True
    monkeypatch.delenv("ROBBI_WEB_TOKEN")
    assert client.get("/healthz").status_code == 503
    assert client.post("/api/dashboard-access", json={"auth_enabled": False}).status_code == 503


def test_access_switch_rejects_cross_origin_malformed_and_wrong_settings_api(dashboard):
    runtime, client = dashboard
    for headers in ({"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}):
        assert client.post("/api/dashboard-access", json={"auth_enabled": True}, headers=headers).status_code == 403
    for payload in ({}, {"auth_enabled": "false"}, {"auth_enabled": True, "password": "new"}):
        assert client.post("/api/dashboard-access", json=payload).status_code == 400
    assert client.post("/settings_data", json={"server": {"auth_enabled": True}}).status_code == 400
    assert runtime.config.values["server"]["auth_enabled"] is False


def test_full_configuration_auth_change_waits_for_restart(dashboard):
    runtime, client = dashboard
    result = client.post("/api/config", json={"yaml": "server: {auth_enabled: true}\n"})
    assert result.status_code == 200 and result.get_json()["restart_required"] is True
    assert client.get("/healthz").status_code == 200
    assert load(ROOT, runtime.config.source, runtime.config.data_dir).values["server"]["auth_enabled"] is True


def test_password_can_be_set_and_rotated_without_restart(dashboard, tmp_path, monkeypatch):
    runtime, client = dashboard
    monkeypatch.setattr(runtime.config, 'root', tmp_path)
    monkeypatch.delenv('ROBBI_WEB_TOKEN')
    result = client.post('/api/dashboard-access', json={'auth_enabled': True, 'password': 'new-dashboard-password'})
    assert result.status_code == 200
    assert 'new-dashboard-password' not in result.get_data(as_text=True)
    assert client.get('/healthz').status_code == 401
    old = {'Authorization': 'Bearer new-dashboard-password'}
    assert client.get('/healthz', headers=old).status_code == 200
    assert client.post('/api/dashboard-access', json={'auth_enabled': True, 'password': 'rotated-dashboard-password'}, headers=old).status_code == 200
    assert client.get('/healthz', headers=old).status_code == 401
    assert client.get('/healthz', headers={'Authorization': 'Bearer rotated-dashboard-password'}).status_code == 200
    assert 'rotated-dashboard-password' in (tmp_path/'.env.secrets').read_text()


def test_rebinding_is_blocked_before_auth_and_origin_comparison(dashboard):
    runtime, client = dashboard
    for method, path in [('GET', '/healthz'), ('POST', '/api/stop')]:
        result = client.open(path, method=method, base_url='http://attacker.example:8082',
                             headers={'Origin': 'http://attacker.example:8082'}, json={} if method=='POST' else None)
        assert result.status_code == 403
    runtime.config.values['server']['allowed_hosts'] = ['proxy.example']
    assert client.get('/healthz', base_url='http://proxy.example').status_code == 200
