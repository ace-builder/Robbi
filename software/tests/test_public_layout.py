from pathlib import Path
from types import SimpleNamespace

import pytest

from robbi.config import load, validate, merge
from robbi.errors import ValidationError
from robbi.web import create_app
from robot.domains.engagement import EngagementDomain
from test_app_http_api import runtime

ROOT = Path(__file__).resolve().parents[1]


def test_hub_default_is_configurable_without_contacting_the_server(runtime):
    client = create_app(runtime).test_client()
    status = client.get('/api/app-store').get_json()
    assert status['hub_url'] == 'https://ai-tier.com/apphub'
    assert not status['configured']
    runtime.config.values['app_hub']['url'] = 'https://example.org/custom-hub'
    assert client.get('/api/app-store').get_json()['hub_url'] == 'https://example.org/custom-hub'
    assert not runtime.app_manager.store.status()['configured']
    for url in ('https://user:password@example.org', 'javascript:alert(1)', 'https://example.org/../private'):
        with pytest.raises(ValidationError):
            validate(merge(runtime.config.values, {'app_hub': {'url': url}}))


def test_public_defaults_and_app_layout(tmp_path):
    config = load(ROOT, tmp_path/'robot.yaml', tmp_path/'data')
    assert config.values['conversation']['followup_enabled'] is True
    assert config.values['conversation']['followup_timeout_s'] == 5
    assert config.values['background_work']['notify_on_complete'] is False
    assert len(list((ROOT/'apps').glob('*/app.yaml'))) == 40
    assert not (ROOT/'app_projects').exists()
    assert not (ROOT/'filming_sequences').exists()
    assert not (ROOT/'src/robbi_store').exists()


def test_owner_can_keep_wake_word_only_conversations(tmp_path):
    owner = tmp_path/'robot.yaml'
    owner.write_text('conversation:\n  followup_enabled: false\n  followup_timeout_s: 4\n')
    config = load(ROOT, owner, tmp_path/'data')
    assert config.values['conversation']['followup_enabled'] is False
    assert config.values['conversation']['followup_timeout_s'] == 4


def test_prompt_overrides_are_local_and_empty_files_fall_back_to_templates(tmp_path, monkeypatch):
    import robot.config as config
    monkeypatch.setattr(config, 'ROOT', ROOT)
    reader = SimpleNamespace(_record_health=lambda *args: None)
    for name in ('SOUL', 'HEARTBEAT', 'TOOLS'):
        path = tmp_path/(name+'.md')
        expected = (ROOT/'templates'/(name+'.md')).read_text(encoding='utf-8').strip()
        assert expected
        assert EngagementDomain._ensure_prompt_file(reader, path, 'fallback') == expected
        path.write_text(' \n')
        assert EngagementDomain._ensure_prompt_file(reader, path, 'fallback') == expected
        path.write_text('My local customization')
        assert EngagementDomain._ensure_prompt_file(reader, path, 'fallback') == 'My local customization'


def test_builtin_core_workflows_survive_the_single_apps_folder(tmp_path, monkeypatch):
    from robot import apps
    monkeypatch.setattr(apps, 'APPS_DIR', tmp_path/'workflow-apps')
    registry = apps.AppRegistry(bundled_root=ROOT/'templates/workflows')
    assert {app['id'] for app in registry.list_apps()} == {'focus-timer', 'reminder', 'kids'}
    assert not registry.errors
