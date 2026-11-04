"""Small timezone-aware daily rhythm; no threads, files or hardware imports."""
from datetime import datetime, timedelta
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DayCycle:
    def __init__(self, options=lambda: {}, *, clock=time.time):
        self.options, self.clock = options, clock

    def snapshot(self, now=None):
        options = self.options()
        now = self.clock() if now is None else now
        requested = str(options.get("timezone", "Europe/Berlin"))
        error = ""
        try:
            zone = ZoneInfo(requested)
        except (ZoneInfoNotFoundError, ValueError):
            zone, error = ZoneInfo("UTC"), "Configured timezone unavailable; using UTC"
        local = datetime.fromtimestamp(now, zone)
        weekend = local.weekday() >= 5
        wake = int(options.get("weekend_wake_hour" if weekend else "weekday_wake_hour", 9 if weekend else 7))
        day, evening, sleep = (int(options.get(key, default)) for key, default in
                               (("day_hour", 10), ("evening_hour", 18), ("sleep_hour", 22)))
        if not 0 <= wake < day < evening < sleep <= 23:
            wake, day, evening, sleep = (9 if weekend else 7), 10, 18, 22
            error = error or "Invalid phase hours; using the default daily rhythm"
        enabled = bool(options.get("enabled", True))
        hour = local.hour + local.minute / 60
        phase = "night" if hour < wake or hour >= sleep else "morning" if hour < day else "evening" if hour >= evening else "day"
        tomorrow = local + timedelta(days=1)
        tomorrow_wake = int(options.get("weekend_wake_hour" if tomorrow.weekday() >= 5 else "weekday_wake_hour", 9 if tomorrow.weekday() >= 5 else 7))
        transitions = [local.replace(hour=value, minute=0, second=0, microsecond=0).timestamp()
                       for value in (wake, day, evening, sleep)]
        transitions.append(tomorrow.replace(hour=max(0, min(23, tomorrow_wake)), minute=0, second=0, microsecond=0).timestamp())
        return {"enabled": enabled, "timezone": str(zone), "local_time": local.isoformat(timespec="seconds"),
                "date": local.date().isoformat(), "weekday": local.strftime("%A"), "weekend": weekend,
                "phase": phase if enabled else "day", "quiet": enabled and phase == "night",
                "wake_hour": wake, "sleep_hour": sleep,
                "next_transition_at": min(value for value in transitions if value > now), "error": error}
