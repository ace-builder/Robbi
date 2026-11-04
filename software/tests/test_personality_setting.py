import pytest
from robot import config


def test_sarcasm_setting_is_bounded_and_persisted(tmp_path, monkeypatch):
    source = tmp_path / 'local.yaml'
    effective = tmp_path / 'effective.yaml'
    source.write_text('llm:\n  sarcasm_level: 4\n', encoding='utf-8')
    effective.write_text(source.read_text(), encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_SOURCE', source)
    monkeypatch.setattr(config, 'CONFIG_FILE', effective)
    monkeypatch.setattr(config.settings, 'sarcasm_level', 4)
    for bad in (0, 6, True, 2.5, 'sarcastic'):
        with pytest.raises(ValueError):
            config.update_settings_from_payload({'llm': {'sarcasm_level': bad}})
        assert config.settings.sarcasm_level == 4
        assert 'sarcasm_level: 4' in source.read_text()
    config.update_settings_from_payload({'llm': {'sarcasm_level': 5}})
    assert config.settings.sarcasm_level == 5
    assert 'sarcasm_level: 5' in source.read_text()
    assert 'sarcasm_level: 5' in effective.read_text()
    snapshot = config.settings_ui_snapshot()
    assert snapshot['values']['llm']['sarcasm_level'] == 5
    assert snapshot['constraints']['llm.sarcasm_level']['max'] == 5
