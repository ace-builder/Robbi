import pytest
import yaml

from robot import config
from robot.settings_schema import NUMERIC_FIELDS, lookup, repair_numeric_settings, validated_number


@pytest.mark.parametrize("key,value", [
    ("pwm_max_duty", 101), ("control_interval_s", 0), ("stall_timeout_s", 0),
    ("pwm_max_duty", float("nan")), ("encoder_counts_per_rev", 0),
])
def test_invalid_motion_update_is_atomic(monkeypatch, tmp_path, key, value):
    path = tmp_path / "config.yaml"
    path.write_text("motion:\n  pwm_max_duty: 55\n")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_SOURCE", path)
    previous = config.settings.motion_pwm_max_duty
    with pytest.raises(ValueError):
        config.update_settings_from_payload({"motion": {key: value}})
    assert path.read_text() == "motion:\n  pwm_max_duty: 55\n"
    assert config.settings.motion_pwm_max_duty == previous


def test_tof_validates_discrete_timing_budget():
    with pytest.raises(ValueError, match="timing budget"):
        config._validate_hardware_settings({"tof": {"distance_mode": 2, "timing_budget_ms": 40}},
                                          {"tof.timing_budget_ms": 40})


def test_optional_tof_setting_is_editable_and_persisted(monkeypatch, tmp_path):
    path = tmp_path / "robot.yaml"
    path.write_text("tof:\n  enabled: true\n  h2_enabled: false\n")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_SOURCE", path)
    monkeypatch.setattr(config.settings, "tof_h2_enabled", False)
    result = config.update_settings_from_payload({"tof": {"h2_enabled": True}})
    assert config.settings.tof_h2_enabled is True
    assert yaml.safe_load(path.read_text())["tof"]["h2_enabled"] is True
    assert result["values"]["tof"]["h2_enabled"] is True


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_optional_tof_setting_requires_a_boolean(monkeypatch, tmp_path, value):
    from robbi.config import validate
    path = tmp_path / "robot.yaml"
    path.write_text("tof:\n  h2_enabled: false\n")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_SOURCE", path)
    with pytest.raises(ValueError):
        config.update_settings_from_payload({"tof": {"h2_enabled": value}})
    with pytest.raises(ValueError):
        validate({"tof": {"h2_enabled": value}})
    assert yaml.safe_load(path.read_text())["tof"]["h2_enabled"] is False


def test_valid_hardware_changes_preserve_other_config(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm:\n  model: user-model\nmotion:\n  pwm_max_duty: 55\n  pwm_min_duty: 30\n")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_SOURCE", path)
    monkeypatch.setattr(config, "settings_ui_snapshot", lambda: {})
    monkeypatch.setattr(config.settings, "motion_pwm_max_duty", 55)
    config.update_settings_from_payload({"motion": {"pwm_max_duty": 50}})
    assert config.settings.motion_pwm_max_duty == 50
    assert yaml.safe_load(path.read_text())["llm"]["model"] == "user-model"


def test_repair_keeps_valid_preferences_and_restores_corrupt_numeric_fields():
    data = {"telegram": {"poll_interval_s": 0, "allowed_chat_ids": "owner-id"},
            "voice": {"command_silence_ms": 0, "ready_tone_block_s": 0},
            "motion": {"pwm_max_duty": 100, "wheel_diameter_cm": 12},
            "llm": {"ui_text_model": "my-custom-model"}}
    fixed, changes = repair_numeric_settings(data)
    assert fixed["telegram"]["poll_interval_s"] == 1
    assert fixed["voice"]["command_silence_ms"] == 550
    assert fixed["voice"]["ready_tone_block_s"] == 0  # Zero is valid here.
    assert fixed["motion"]["pwm_max_duty"] == 100
    assert fixed["motion"]["wheel_diameter_cm"] == 12
    assert fixed["llm"]["ui_text_model"] == data["llm"]["ui_text_model"]
    assert fixed["llm"]["sarcasm_level"] == NUMERIC_FIELDS["llm.sarcasm_level"]["default"]
    assert fixed["telegram"]["allowed_chat_ids"] == "owner-id"
    assert data["telegram"]["poll_interval_s"] == 0  # No in-place mutation.
    for key in NUMERIC_FIELDS:
        validated_number(key, lookup(fixed, key))
    assert repair_numeric_settings(fixed)[1] == []


def test_partial_edit_does_not_validate_or_overwrite_untouched_field(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("telegram:\n  poll_interval_s: 0\nmotion:\n  pwm_max_duty: 55\n  pwm_min_duty: 30\n")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_SOURCE", path)
    monkeypatch.setattr(config, "settings_ui_snapshot", lambda: {})
    monkeypatch.setattr(config.settings, "motion_pwm_max_duty", 55)
    config.update_settings_from_payload({"motion": {"pwm_max_duty": 100}})
    saved = yaml.safe_load(path.read_text())
    assert saved["motion"]["pwm_max_duty"] == 100
    assert saved["telegram"]["poll_interval_s"] == 0


@pytest.mark.parametrize("value", [0, "", float("inf"), True, 1.5])
def test_invalid_integer_never_silently_truncates(value):
    with pytest.raises(ValueError):
        validated_number("voice.command_start_chunks", value)


def test_fast_control_and_wheel_feedback_tuning_roundtrip(monkeypatch,tmp_path):
    from robot import config
    target=tmp_path/'robot.yaml';target.write_text('motion: {}\n',encoding='utf-8')
    monkeypatch.setattr(config,'CONFIG_FILE',target)
    monkeypatch.setattr(config,'CONFIG_SOURCE',target)
    values={'control_interval_s':.01,'curve_trim_gain':.7,'curve_start_assist':False,'curve_cross_track_gain':.9}
    for name in values:
        attr='motion_'+name;monkeypatch.setattr(config.settings,attr,getattr(config.settings,attr))
    result=config.update_settings_from_payload({'motion':values})
    assert all(result['values']['motion'][k]==v for k,v in values.items())
    old=target.read_bytes()
    for changes in [{'control_interval_s':.001},{'curve_trim_gain':-1},{'curve_cross_track_gain':3}]:
        with pytest.raises(ValueError):config.update_settings_from_payload({'motion':changes})
        assert target.read_bytes()==old
