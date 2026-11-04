"""Pure settings schema for day rhythm and responsive autonomous work."""

from dataclasses import dataclass, field, fields
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def option(default, path, cast, low=None, high=None):
    return field(
        default=default, metadata={"path": path, "cast": cast, "low": low, "high": high}
    )


@dataclass(slots=True)
class AutonomousSettings:
    autonomy_deliberation_enabled: bool = option(
        True, "autonomy.deliberation_enabled", bool
    )
    autonomy_decide_interval_s: float = option(
        60.0, "autonomy.decide_interval_s", float, 10, 3600
    )
    autonomy_attention_poll_s: float = option(
        10.0, "autonomy.attention_poll_s", float, 2, 60
    )
    background_work_enabled: bool = option(True, "background_work.enabled", bool)
    background_work_foreground_budget_s: float = option(
        2.0, "background_work.foreground_budget_s", float, 0.2, 10
    )
    background_work_timeout_s: float = option(
        120.0, "background_work.timeout_s", float, 5, 300
    )
    background_work_provider: str = option("", "background_work.provider", str)
    background_work_model: str = option("", "background_work.model", str)
    background_work_max_pending: int = option(
        2, "background_work.max_pending", int, 0, 2
    )
    background_work_max_history: int = option(
        32, "background_work.max_history", int, 4, 32
    )
    background_work_notify_on_complete: bool = option(
        False, "background_work.notify_on_complete", bool
    )
    background_work_acknowledgement: str = option(
        "I'll work on this and let you know when it's ready. You can keep talking to me.",
        "background_work.acknowledgement",
        str,
    )
    day_cycle_enabled: bool = option(True, "day_cycle.enabled", bool)
    day_cycle_timezone: str = option("Europe/Berlin", "day_cycle.timezone", str)
    day_cycle_weekday_wake_hour: int = option(
        7, "day_cycle.weekday_wake_hour", int, 0, 23
    )
    day_cycle_weekend_wake_hour: int = option(
        9, "day_cycle.weekend_wake_hour", int, 0, 23
    )
    day_cycle_day_hour: int = option(10, "day_cycle.day_hour", int, 0, 23)
    day_cycle_evening_hour: int = option(18, "day_cycle.evening_hour", int, 0, 23)
    day_cycle_sleep_hour: int = option(22, "day_cycle.sleep_hour", int, 0, 23)
    autonomy_observe_interval_s: float = option(
        900.0, "autonomy.observe_interval_s", float, 60, 86400
    )
    autonomy_explore_interval_s: float = option(
        7200.0, "autonomy.explore_interval_s", float, 300, 86400
    )
    autonomy_max_attempts_per_day: int = option(
        24, "autonomy.max_attempts_per_day", int, 0, 100
    )
    autonomy_max_model_calls_per_day: int = option(
        8, "autonomy.max_model_calls_per_day", int, 0, 50
    )
    autonomy_max_explorations_per_day: int = option(
        2, "autonomy.max_explorations_per_day", int, 0, 20
    )
    autonomy_allow_exploration: bool = option(False, "autonomy.allow_exploration", bool)
    autonomy_explore_max_steps: int = option(1, "autonomy.explore_max_steps", int, 1, 5)
    autonomy_explore_max_distance_cm: float = option(
        30.0, "autonomy.explore_max_distance_cm", float, 5, 100
    )
    autonomy_explore_timeout_s: float = option(
        90.0, "autonomy.explore_timeout_s", float, 5, 180
    )
    autonomy_model_timeout_s: float = option(
        25.0, "autonomy.model_timeout_s", float, 2, 60
    )


SPECS = {
    item.metadata["path"]: {**item.metadata, "attr": item.name, "default": item.default}
    for item in fields(AutonomousSettings)
}
NUMERIC = {
    path: {
        "min": spec["low"],
        "max": spec["high"],
        "default": spec["default"],
        "integer": spec["cast"] is int,
        "step": 1 if spec["cast"] is int else "any",
    }
    for path, spec in SPECS.items()
    if spec["low"] is not None
}


def load_settings(settings, readers):
    for path, spec in SPECS.items():
        value = readers[spec["cast"]](
            path, "ROBBI_" + path.upper().replace(".", "_"), spec["default"]
        )
        setattr(settings, spec["attr"], value)


def editable_fields():
    return {
        path: {"attr": spec["attr"], "cast": spec["cast"], "normalize": spec["cast"]}
        for path, spec in SPECS.items()
    }


def group_values(settings, group):
    return {
        path.split(".", 1)[1]: getattr(settings, spec["attr"])
        for path, spec in SPECS.items()
        if path.startswith(group + ".")
    }


def validate_extended(config):
    """Cross-field/string checks shared by full YAML and live settings writes."""
    for path, spec in SPECS.items():
        group, key = path.split(".", 1)
        value = config.get(group, {}).get(key, spec["default"])
        if spec["cast"] is bool and not isinstance(value, bool):
            raise ValueError(f"{path} must be true or false")
        if spec["cast"] is str and (
            not isinstance(value, str)
            or len(value) > (500 if key == "acknowledgement" else 200)
        ):
            raise ValueError(f"Invalid text for {path}")
        if key == "acknowledgement" and not value.strip():
            raise ValueError("Background acknowledgement must not be empty")
    cycle = {
        **group_values(AutonomousSettings(), "day_cycle"),
        **config.get("day_cycle", {}),
    }
    try:
        ZoneInfo(cycle["timezone"])
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError(
            "day_cycle.timezone must be an installed IANA timezone, e.g. Europe/Berlin"
        ) from None
    if not (
        max(cycle["weekday_wake_hour"], cycle["weekend_wake_hour"])
        < cycle["day_hour"]
        < cycle["evening_hour"]
        < cycle["sleep_hour"]
    ):
        raise ValueError("Day cycle hours must follow wake < day < evening < sleep")
