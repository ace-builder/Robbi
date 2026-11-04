"""Validated configuration before any feature/hardware import."""

from __future__ import annotations

import copy
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from robbi.errors import ValidationError
from robbi.storage import atomic_write, read_text_limited


CONFIG_WRITE_LOCK = threading.RLock()


# Last-resort boot profile if the editable defaults are missing or corrupt.
# This keeps diagnostics available without inheriting any physical output.
SAFE_DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 8082,
        "hot_reload": False,
        "auth_enabled": False,
    },
    "runtime": {
        "worker_threads": 4,
        "command_queue_size": 2,
        "status_interval_s": 2,
        "summary_interval_s": 15,
        "catalog_interval_s": 60,
        "feature_task_limit": 16,
        "allow_host_management": False,
        "calibration_confirmed": False,
    },
    "hardware": {"enable_motors": False, "enable_lcd": False, "enable_servos": False},
    "camera": {"enabled": False},
    "voice": {"enabled": False},
    "audio_studio": {"max_storage_mb": 128, "max_clip_mb": 16, "history_limit": 500},
    "tof": {"enabled": False},
    "imu": {"enabled": False},
    "apps": {"permissions_enabled": True},
    "app_hub": {"url": "https://ai-tier.com/apphub"},
    "autonomy": {"enabled": False},
    "heartbeat": {"enabled": False},
    "startup_warmup": {"enabled": False, "chat_enabled": False},
    "telegram": {
        "enabled": False,
        "allow_robot_actions": False,
        "allow_photos": False,
        "allowed_chat_ids": "",
    },
    "daily_loop": {"enabled": False, "telegram_brief_enabled": False},
}


def read_defaults(root):
    path = root / "config" / "desktop.yaml"
    try:
        return validate(
            merge(SAFE_DEFAULTS, parse_yaml(read_text_limited(path, 256 * 1024, label="Configuration")))
        ), []
    except (OSError, ValueError) as error:
        return copy.deepcopy(SAFE_DEFAULTS), [f"Default configuration {path}: {error}"]


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def parse_yaml(text: str) -> dict:
    if len(text.encode("utf-8")) > 256 * 1024:
        raise ValidationError("Configuration exceeds 256 KiB")
    try:
        loaded = yaml.safe_load(text)
    except RecursionError:
        raise ValidationError("Configuration is too deeply nested") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}" if mark else ""
        raise ValidationError(
            f"Invalid YAML{location}; check indentation and values"
        ) from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValidationError("Configuration must be a YAML mapping")
    count = 0

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if depth > 10 or count > 3000:
            raise ValidationError("Configuration is too deeply nested or too large")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValidationError("Configuration keys must be text")
                if any(
                    part in key.lower()
                    for part in ("api_key", "bot_token", "password", "secret")
                ):
                    raise ValidationError("Store credentials in .env.secrets, not YAML")
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValidationError("Configuration cannot contain NaN or infinity")
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValidationError("Configuration values must be basic YAML values")

    visit(loaded)
    return loaded


def read_yaml(path, *, strict=False, errors=None):
    """Shared bounded parser for startup and live settings edits."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return parse_yaml(read_text_limited(path, 256 * 1024, label="Configuration"))
    except (OSError, ValueError) as error:
        if strict:
            raise
        message = f"Invalid configuration {path}: {error}; defaults in use"
        if errors is not None:
            errors.append(message)
        import logging

        logging.getLogger(__name__).warning(message)
        return {}


def persist_settings_patch(source, effective, document, updates):
    """Persist the restart source and derived cache before publishing live values.

    Both callers use CONFIG_WRITE_LOCK. The restart source is authoritative; a
    failed source write rolls back the cache and never changes the live view.
    """
    from .schema import put

    source, effective = Path(source), Path(effective)
    with CONFIG_WRITE_LOCK:
        data = read_yaml(source, strict=True) if source != effective else document
        for key, value in updates.items():
            put(data, key, value)
        serialized = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
        if source == effective:
            atomic_write(source, serialized)
            return
        before = effective.read_bytes() if effective.exists() else None
        atomic_write(
            effective, yaml.safe_dump(document, sort_keys=False, allow_unicode=False)
        )
        try:
            atomic_write(source, serialized)
        except BaseException:
            if before is None:
                effective.unlink(missing_ok=True)
            else:
                atomic_write(effective, before)
            raise


def validate(config: dict):
    for group, values in config.items():
        if not isinstance(values, dict):
            raise ValidationError(f"{group} must be a mapping")
    server = config.get("server", {})
    hosts = server.get("allowed_hosts", [])
    if (
        not isinstance(hosts, list)
        or len(hosts) > 32
        or any(
            not isinstance(h, str)
            or not h
            or len(h) > 253
            or any(c in h for c in "/*\\ @\r\n\t")
            for h in hosts
        )
    ):
        raise ValidationError(
            "server.allowed_hosts must contain up to 32 exact hostnames, without wildcards or paths"
        )
    pin = config.get("hardware", {}).get("right_encoder_b_pin", 16)
    if type(pin) is not int or pin not in (7, 16):
        raise ValidationError(
            "hardware.right_encoder_b_pin must be 16 (standard board) or 7 (alternate wiring)"
        )
    port = server.get("port", 8082)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValidationError("server.port must be an integer from 1024 to 65535")
    if not isinstance(server.get("host", "127.0.0.1"), str):
        raise ValidationError("server.host must be text")
    for key in ("width", "height"):
        if key in config.get("camera", {}) and type(config["camera"][key]) is not int:
            raise ValidationError(f"camera.{key} must be an integer")
    for group, values in config.items():
        for key, value in values.items():
            if group == "plugins" and key == "enabled":
                if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value
                ):
                    raise ValidationError("plugins.enabled must be a list of app IDs")
                continue
            if (
                key.startswith("enable_")
                or key.endswith("_enabled")
                or key
                in {
                    "enabled",
                    "hot_reload",
                    "calibration_confirmed",
                    "allow_host_management",
                }
            ):
                if not isinstance(value, bool):
                    raise ValidationError(f"{group}.{key} must be true or false")
    from .schema import RUNTIME_BOUNDS

    bounds = RUNTIME_BOUNDS
    for path, (low, high) in bounds.items():
        group, key = path.split(".")
        value = config.get(group, {}).get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not low <= value <= high
        ):
            raise ValidationError(f"{path} must be between {low} and {high}")
        if (
            path
            in {
                "camera.width",
                "camera.height",
                "runtime.worker_threads",
                "runtime.command_queue_size",
                "runtime.feature_task_limit",
                "audio_studio.history_limit",
            }
            and value is not None
            and not isinstance(value, int)
        ):
            raise ValidationError(f"{path} must be an integer")
    if "url" in config.get("app_hub", {}):
        from robbi.app_runtime.store_client import store_url

        store_url(config["app_hub"]["url"], allow_http=True)
    studio = config.get("audio_studio", {})
    if studio.get("max_clip_mb", 16) > studio.get("max_storage_mb", 128):
        raise ValidationError("audio_studio.max_clip_mb cannot exceed max_storage_mb")
    # Pure schema import: no robot.config, devices, state, or provider imports.
    from .schema import NUMERIC_FIELDS, validated_number

    for path in NUMERIC_FIELDS:
        group, key = path.split(".", 1)
        if key in config.get(group, {}):
            try:
                validated_number(path, config[group][key])
            except ValueError:
                raise ValidationError(f"Invalid value for {path}") from None
    if config.get("hardware", {}).get("enable_motors") and not config.get(
        "runtime", {}
    ).get("calibration_confirmed"):
        raise ValidationError(
            "Set runtime.calibration_confirmed after the bench checks before enabling motors"
        )
    from .autonomy import validate_extended

    try:
        validate_extended(config)
    except ValueError as error:
        raise ValidationError(str(error)) from None
    return config


@dataclass
class RuntimeConfig:
    root: Path
    data_dir: Path
    source: Path
    values: dict
    issues: list[str] = field(default_factory=list)

    @property
    def host(self):
        return self.values["server"]["host"]

    @property
    def port(self):
        return int(self.values["server"]["port"])

    def install_environment(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        effective = self.data_dir / "effective.yaml"
        atomic_write(effective, yaml.safe_dump(self.values, sort_keys=False))
        os.environ["ROBBI_ROOT"] = str(self.root)
        os.environ["ROBBI_DATA_DIR"] = str(self.data_dir)
        os.environ["ROBBI_CONFIG"] = str(effective)
        os.environ["ROBBI_CONFIG_SOURCE"] = str(self.source)
        # Preserve existing installations without this field; new setup writes 16.
        os.environ["ROBBI_RIGHT_ENCODER_B_PIN"] = str(
            self.values.get("hardware", {}).get("right_encoder_b_pin", 16)
        )
        # A malformed source must never re-enable physical output through defaults.
        armed = (
            not self.issues
            and self.values.get("hardware", {}).get("enable_motors", False)
            and self.values.get("runtime", {}).get("calibration_confirmed", False)
        )
        os.environ["ROBBI_ENABLE_MOTORS"] = "true" if armed else "false"
        # Feature flags come from the selected validated profile. An unrelated
        # shell/service environment must not silently start capture or messaging.
        for variable, group, key in (
            ("ROBBI_ENABLE_LCD", "hardware", "enable_lcd"),
            ("ROBBI_ENABLE_SERVOS", "hardware", "enable_servos"),
            ("ROBBI_CAMERA_ENABLED", "camera", "enabled"),
            ("ROBBI_VOICE_ENABLED", "voice", "enabled"),
            ("ROBBI_TOF_ENABLED", "tof", "enabled"),
            ("ROBBI_IMU_ENABLED", "imu", "enabled"),
            ("ROBBI_AUTONOMY_ENABLED", "autonomy", "enabled"),
            ("ROBBI_HEARTBEAT_ENABLED", "heartbeat", "enabled"),
            ("ROBBI_STARTUP_WARMUP_ENABLED", "startup_warmup", "enabled"),
            ("ROBBI_STARTUP_WARMUP_CHAT_ENABLED", "startup_warmup", "chat_enabled"),
            ("ROBBI_TELEGRAM_ENABLED", "telegram", "enabled"),
            ("ROBBI_TELEGRAM_ALLOW_ROBOT_ACTIONS", "telegram", "allow_robot_actions"),
        ):
            os.environ[variable] = (
                "true" if self.values.get(group, {}).get(key, False) else "false"
            )

    def save(self, text):
        with CONFIG_WRITE_LOCK:
            defaults, _ = read_defaults(self.root)
            validate(merge(defaults, parse_yaml(text)))
            atomic_write(self.source, text)


def load(root=None, source=None, data_dir=None):
    root = Path(
        root or os.getenv("ROBBI_ROOT") or Path(__file__).resolve().parents[3]
    ).resolve()
    # Installed editable layout: src/robbi/configuration/store.py -> project root.
    defaults, issues = read_defaults(root)
    source = Path(source or root / "config" / "robot.yaml").resolve()
    try:
        overlay = (
            parse_yaml(read_text_limited(source, 256 * 1024, label="Configuration")) if source.exists() else {}
        )
        values = validate(merge(defaults, overlay))
    except (OSError, ValueError) as error:
        issues.append(str(error))
        values = defaults
    from .resolution import resolve_document

    values = resolve_document(values, os.environ, errors=issues)
    return RuntimeConfig(
        root, Path(data_dir or root / "var").resolve(), source, values, issues
    )
