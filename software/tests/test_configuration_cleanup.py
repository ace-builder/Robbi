"""Migration contracts: one module identity, one schema, preserved saved state."""

import dataclasses
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from robbi.configuration import schema, store
from robbi.configuration.resolution import resolve_document, resolve_settings


@pytest.mark.parametrize(
    "old,new",
    [
        ("robot.config", "robbi.configuration.settings"),
        ("robot.settings_schema", "robbi.configuration.schema"),
        ("robot.autonomous_settings", "robbi.configuration.autonomy"),
        ("robot.motion", "robbi.engine.motion"),
        ("robot.controller", "robbi.engine.controller"),
        ("robot.domains.motion", "robbi.engine.domains.motion"),
        ("robbi.config", "robbi.configuration.store"),
    ],
)
def test_compatibility_imports_share_identity(old, new):
    legacy = importlib.import_module(old)
    canonical = importlib.import_module(new)
    assert legacy is canonical
    assert canonical.__spec__.name == new


def test_validation_import_does_not_initialize_engine_or_read_private_files(tmp_path):
    script = """import sys
from robbi.config import validate
validate({'camera': {'fps': 3}, 'motion': {'wheel_base_cm': 10.2}})
assert 'robot' not in sys.modules
assert 'robbi.configuration.settings' not in sys.modules
assert not any(n.startswith('robbi.engine') for n in sys.modules)
assert 'cv2' not in sys.modules and 'numpy' not in sys.modules
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "ROBBI_ROOT": str(tmp_path),
    }
    subprocess.run(
        [sys.executable, "-c", script], env=environment, check=True, timeout=15
    )


def test_numeric_defaults_and_editor_bindings_come_from_settings_schema():
    defaults = schema.Settings()
    for key, constraint in schema.NUMERIC_FIELDS.items():
        meta = schema.SPECS[key]
        assert constraint["default"] == getattr(defaults, meta["attr"])
        schema.validated_number(key, constraint["default"])
    for key, meta in schema.editable_fields().items():
        assert meta["attr"] == schema.SPECS[key]["attr"]
        assert hasattr(defaults, meta["attr"])


def test_runtime_document_matches_engine_view_and_excludes_credentials():
    raw = {
        "llm": {"provider": "lm-studio", "model": "my-model"},
        "motion": {"wheel_base_cm": 10.7},
        "voice": {"enabled": False},
    }
    env = {
        "OPENAI_API_KEY": "test-only-private-value",
        "ROBBI_CAMERA_FPS": "4",
        "ROBBI_VOICE_ENABLED": "true",
    }
    effective = resolve_document(raw, env)
    loaded = resolve_settings(effective, {"OPENAI_API_KEY": env["OPENAI_API_KEY"]})
    assert effective["camera"]["fps"] == loaded.camera_fps == 4
    assert effective["voice"]["enabled"] is loaded.voice_enabled is False
    assert (
        loaded.chat_provider
        == loaded.llm_provider
        == loaded.ui_text_provider
        == "local"
    )
    assert (
        loaded.chat_model
        == loaded.ui_text_model
        == loaded.speech_input_model
        == "my-model"
    )
    assert loaded.openai_api_key == env["OPENAI_API_KEY"]
    assert "test-only-private-value" not in json.dumps(effective)
    assert "api_key" not in json.dumps(effective)


@pytest.mark.parametrize(
    "patch",
    [
        {"tof": {"distance_mode": 2, "timing_budget_ms": 40}},
        {"tof": {"timing_budget_ms": 50, "intermeasurement_ms": 20}},
        {"motion": {"pwm_min_duty": 60, "pwm_max_duty": 40}},
        {"slam": {"min_range_cm": 100, "max_range_cm": 80}},
    ],
)
def test_live_edits_keep_existing_pair_validation(patch, monkeypatch, tmp_path):
    from robbi.configuration import settings

    target = tmp_path / "settings.yaml"
    target.write_text("{}")
    monkeypatch.setattr(settings, "CONFIG_FILE", target)
    monkeypatch.setattr(settings, "CONFIG_SOURCE", target)
    with pytest.raises(ValueError):
        settings.update_settings_from_payload(patch)
    assert target.read_text() == "{}"


def test_failed_source_write_rolls_back_cache_and_keeps_live_settings(
    tmp_path, monkeypatch
):
    from robbi.configuration import settings

    source, cache = tmp_path / "robot.yaml", tmp_path / "effective.yaml"
    source.write_text("voice: {command_wait_s: 8}\ncustom: {keep: true}\n")
    cache.write_bytes(source.read_bytes())
    before = source.read_bytes()
    monkeypatch.setattr(settings, "CONFIG_SOURCE", source)
    monkeypatch.setattr(settings, "CONFIG_FILE", cache)
    monkeypatch.setattr(settings.settings, "voice_command_wait_s", 8)
    write = store.atomic_write

    def fail_source(path, content):
        if Path(path) == source:
            raise OSError("simulated read-only source")
        write(path, content)

    monkeypatch.setattr(store, "atomic_write", fail_source)
    with pytest.raises(OSError):
        settings.update_settings_from_payload({"voice": {"command_wait_s": 11}})
    assert source.read_bytes() == cache.read_bytes() == before
    assert settings.settings.voice_command_wait_s == 8


def test_live_save_and_restart_resolve_identically(tmp_path, monkeypatch):
    from robbi.configuration import settings

    source, cache = tmp_path / "robot.yaml", tmp_path / "effective.yaml"
    source.write_text("custom: {preserved: true}\n")
    cache.write_text(yaml.safe_dump(resolve_document({}, {})))
    monkeypatch.setattr(settings, "CONFIG_SOURCE", source)
    monkeypatch.setattr(settings, "CONFIG_FILE", cache)
    for name in [
        "voice_command_wait_s",
        "motion_curve_trim_gain",
        "telegram_allow_photos",
    ]:
        monkeypatch.setattr(settings.settings, name, getattr(settings.settings, name))
    settings.update_settings_from_payload(
        {
            "voice": {"command_wait_s": 11},
            "motion": {"curve_trim_gain": 0.7},
            "telegram": {"allow_photos": True},
        }
    )
    restarted = resolve_settings(store.read_yaml(cache, strict=True), {})
    for name in [
        "voice_command_wait_s",
        "motion_curve_trim_gain",
        "telegram_allow_photos",
    ]:
        assert getattr(settings.settings, name) == getattr(restarted, name)
    assert yaml.safe_load(source.read_text())["custom"]["preserved"] is True


def test_environment_parser_is_shared_with_setup(tmp_path, monkeypatch):
    from robbi.configuration.secrets import read_env, load_into_environment
    from robbi.setup.configuration import Configuration

    secret = tmp_path / ".env.secrets"
    secret.write_text(
        'export OPENAI_API_KEY="test-only-key"\n# keep\nEXTRA_OPTION=custom\n'
    )
    assert Configuration(tmp_path).keys()["OPENAI_API_KEY"] == "test-only-key"
    assert read_env(secret)["EXTRA_OPTION"] == "custom"
    monkeypatch.setenv("OPENAI_API_KEY", "external-override")
    monkeypatch.setattr(os, "environ", os.environ.copy())
    load_into_environment(secret)
    assert os.environ["OPENAI_API_KEY"] == "external-override"


def test_engine_implementations_do_not_depend_on_compatibility_package():
    import ast

    root = Path(__file__).resolve().parents[1] / "src/robbi"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("robot"), str(path)
            elif isinstance(node, ast.Import):
                assert not any(
                    n.name == "robot" or n.name.startswith("robot.") for n in node.names
                ), str(path)


def test_all_legacy_fields_are_still_dataclass_fields():
    assert {
        "motion_curve_trim_gain",
        "imu_poll_hz",
        "tof_poll_hz",
        "voice_wake_engine",
        "chat_provider",
        "telegram_allowed_chat_ids",
        "background_work_enabled",
        "day_cycle_timezone",
    } <= {f.name for f in dataclasses.fields(schema.Settings)}


def test_invalid_environment_reports_field_without_echoing_value(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/desktop.yaml").write_text("server: {host: 127.0.0.1, port: 8082}\n")
    monkeypatch.setenv("ROBBI_PORT", "99999")
    monkeypatch.setenv("ROBBI_CAMERA_FPS", "private-mistaken-input")
    config = store.load(tmp_path)
    store.validate(config.values)
    assert config.port == schema.Settings().port
    assert any("server.port" in issue for issue in config.issues)
    assert any("camera.fps" in issue for issue in config.issues)
    assert "private-mistaken-input" not in str(config.issues)
    assert "99999" not in str(config.issues)
