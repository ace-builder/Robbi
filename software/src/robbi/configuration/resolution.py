"""Resolve the shared schema once; hot control loops keep direct attributes."""

from copy import deepcopy
import math

from .schema import (
    BY_ATTR,
    SPECS,
    Settings,
    lookup,
    put,
    validated_number,
    NUMERIC_FIELDS,
)

# These switches have always belonged to the selected validated robot profile.
# Inherited shell variables must not silently enable capture, messaging or motors.
PROFILE_FLAGS = {
    "hardware.enable_motors",
    "hardware.enable_lcd",
    "hardware.enable_servos",
    "camera.enabled",
    "voice.enabled",
    "tof.enabled",
    "imu.enabled",
    "autonomy.enabled",
    "heartbeat.enabled",
    "startup_warmup.enabled",
    "startup_warmup.chat_enabled",
    "telegram.enabled",
    "telegram.allow_robot_actions",
    "hardware.right_encoder_b_pin",
}
_MISSING = object()


def resolve_settings(document, environment, *, errors=None, protect_flags=False):
    """Keep old YAML/env aliases and flat settings attributes, without I/O."""
    errors = errors if errors is not None else []
    cache = {}

    def read(meta):
        path = meta["path"]
        if meta["secret"]:
            return str(environment.get(meta["env"], meta["default"])).strip()
        if path in cache:
            return cache[path]
        env = {} if protect_flags and path in PROFILE_FLAGS else environment
        value = env.get(meta["env"], _MISSING)
        if value is _MISSING:
            value = lookup(document, path, _MISSING)
        if value is _MISSING:
            for old_path, old_env in meta["legacy"]:
                value = env.get(old_env, lookup(document, old_path, _MISSING))
                if value is not _MISSING:
                    break
        if value is _MISSING and meta["fallback"]:
            value = read(SPECS[meta["fallback"]])
        if value is _MISSING:
            value = meta["default"]
        try:
            cast = meta["cast"]
            if path in NUMERIC_FIELDS:
                value = validated_number(path, value)
            if cast is bool:
                value = (
                    value
                    if isinstance(value, bool)
                    else str(value).strip().lower() in {"1", "true", "yes", "on"}
                )
            elif cast is str:
                value = "" if value is None else str(value).strip()
            elif cast in (int, float):
                number = float(value)
                if not math.isfinite(number) or cast is int and not number.is_integer():
                    raise ValueError("Expected a finite number")
                value = cast(number)
            if meta["normalize"]:
                value = meta["normalize"](value)
        except (TypeError, ValueError, OverflowError):
            # Report the field, never a possibly sensitive environment value.
            errors.append(f"{path}: invalid value; using the schema default")
            value = meta["default"]
        cache[path] = value
        return value

    return Settings(**{attr: read(meta) for attr, meta in BY_ATTR.items()})


def resolve_document(document, environment, *, errors=None):
    """Materialize a complete non-secret snapshot while retaining extension keys."""
    result = deepcopy(document)
    settings = resolve_settings(
        document, environment, errors=errors, protect_flags=True
    )
    for path, meta in SPECS.items():
        put(result, path, getattr(settings, meta["attr"]))
    return result
