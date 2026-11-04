from __future__ import annotations

import os
import logging
from pathlib import Path


from .schema import (
    Settings as Settings,
    NUMERIC_FIELDS,
    validated_number,
    editable_fields,
    validate_combinations,
)
from .schema import lookup as _yaml_lookup, put as _yaml_set
from .autonomy import validate_extended
from .resolution import resolve_settings
from .store import CONFIG_WRITE_LOCK, read_yaml, persist_settings_patch


ROOT = Path(os.environ.get("ROBBI_ROOT", Path(__file__).resolve().parents[3])).resolve()
DATA_DIR = Path(os.environ.get("ROBBI_DATA_DIR", ROOT / "var")).resolve()
CONFIG_ERRORS = []
CONFIG_FILE = Path(
    os.environ.get("ROBBI_CONFIG", ROOT / "config" / "desktop.yaml")
).resolve()
CONFIG_SOURCE = Path(os.environ.get("ROBBI_CONFIG_SOURCE", CONFIG_FILE)).resolve()
SECRETS_ENV_FILE = ROOT / ".env.secrets"
SOUL_FILE = DATA_DIR / "personality" / "SOUL.md"
HEARTBEAT_FILE = DATA_DIR / "personality" / "HEARTBEAT.md"
TOOLS_FILE = DATA_DIR / "personality" / "TOOLS.md"
APPS_DIR = DATA_DIR / "workflow-apps"
ASSETS_DIR = ROOT / "assets"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = DATA_DIR / "logs"
FULL_LOG_FILE = LOG_DIR / "robot_full.log"
TRACE_LOG_FILE = LOG_DIR / "robot_trace.jsonl"
RUNTIME_DIR = DATA_DIR / "runtime"
PHOTOS_DIR = RUNTIME_DIR / "photos"
ANIMATIONS_DIR = ROOT / "animations"
WAKE_REFERENCES_DIR = STATE_DIR / "wake_references"
WAKE_NEGATIVE_DIR = STATE_DIR / "wake_negative"
WAKE_NOISE_DIR = STATE_DIR / "wake_noise"
WAKE_MODELS_DIR = STATE_DIR / "wake_models"
WAKE_EXPORTS_DIR = STATE_DIR / "wake_exports"
MAPS_DIR = STATE_DIR / "maps"
TEMPLATES_DIR = ROOT / "web" / "templates"
MEMORY_FILE = STATE_DIR / "MEMORY.md"
MEMORY_DB_FILE = STATE_DIR / "memory.db"
EVENTS_DB_FILE = STATE_DIR / "events.db"
RECENT_CONTEXT_FILE = STATE_DIR / "recent_context.json"
REMINDERS_FILE = STATE_DIR / "reminders.json"
TELEGRAM_STATE_FILE = STATE_DIR / "telegram.json"
TODO_STATE_FILE = STATE_DIR / "todos.json"
DAILY_LOOP_STATE_FILE = STATE_DIR / "daily_loop.json"
PRODUCTIVITY_DB_FILE = STATE_DIR / "productivity.db"
ONBOARDING_DRAFT_FILE = STATE_DIR / "onboarding_draft.json"
PROFILE_STATE_FILE = STATE_DIR / "profile.json"


def ensure_directories():
    for directory in (
        APPS_DIR,
        ASSETS_DIR,
        STATE_DIR,
        LOG_DIR,
        RUNTIME_DIR,
        PHOTOS_DIR,
        ANIMATIONS_DIR,
        WAKE_REFERENCES_DIR,
        WAKE_NEGATIVE_DIR,
        WAKE_NOISE_DIR,
        WAKE_MODELS_DIR,
        WAKE_EXPORTS_DIR,
        MAPS_DIR,
        TEMPLATES_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _load_env_file(path):
    from .secrets import load_into_environment

    try:
        load_into_environment(path)
    except (OSError, ValueError):
        CONFIG_ERRORS.append("Private environment file could not be read")
        logging.getLogger(__name__).warning(CONFIG_ERRORS[-1])


def _load_yaml_file(path, strict=False):
    return read_yaml(path, strict=strict, errors=CONFIG_ERRORS)


_load_env_file(SECRETS_ENV_FILE)
_yaml_config = _load_yaml_file(CONFIG_FILE)


settings = resolve_settings(_yaml_config, os.environ, errors=CONFIG_ERRORS)

_EDITABLE_SETTINGS = editable_fields()
_settings_write_lock = CONFIG_WRITE_LOCK


def settings_ui_snapshot():
    from .view import snapshot

    return snapshot(settings, CONFIG_FILE, SECRETS_ENV_FILE)


def _validate_hardware_settings(data, updates):
    validate_combinations(
        data, updates, lambda key: getattr(settings, _EDITABLE_SETTINGS[key]["attr"])
    )


def update_settings_from_payload(payload: dict) -> dict:
    with _settings_write_lock:
        return _update_settings_from_payload_locked(payload)


def _update_settings_from_payload_locked(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("settings payload must be an object")
    data = _load_yaml_file(CONFIG_FILE, strict=True)
    updates = {}
    for dotted_key, meta in _EDITABLE_SETTINGS.items():
        value = _yaml_lookup(payload, dotted_key, default=None)
        if value is None:
            continue
        try:
            if meta["cast"] is bool and not isinstance(value, bool):
                raise ValueError("must be true or false")
            if dotted_key in NUMERIC_FIELDS:
                value = validated_number(dotted_key, value)
            cast_value = meta["cast"](value)
            normalized = meta["normalize"](cast_value)
        except Exception as exc:
            raise ValueError(f"invalid value for {dotted_key}: {exc}") from exc
        _yaml_set(data, dotted_key, normalized)
        updates[dotted_key] = normalized
    _validate_hardware_settings(data, updates)
    validate_extended(data)
    persist_settings_patch(CONFIG_SOURCE, CONFIG_FILE, data, updates)
    for dotted_key, normalized in updates.items():
        setattr(settings, _EDITABLE_SETTINGS[dotted_key]["attr"], normalized)
    return settings_ui_snapshot()
