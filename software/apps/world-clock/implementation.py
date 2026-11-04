"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import datetime, timezone

from decimal import localcontext

from functools import wraps

import re

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

class InputError(ValueError):
    """An expected, actionable input problem rather than a broken app."""

def handler(function):
    @wraps(function)
    def run(context, tool_name, arguments):
        context.checkpoint()
        try:
            # Keep intermediate conversions accurate before display rounding;
            # never change another app worker's decimal context.
            with localcontext() as settings:
                settings.prec = 60
                return function(context, tool_name, arguments)
        except InputError as error:
            return {"ok": False, "text": str(error)}
    return run

def result(message, **data):
    return {"ok": True, "text": message, "data": data}

def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputError("Use an IANA timezone such as Europe/Berlin, America/New_York or UTC.") from None

@handler
def world_clock(context, tool, args):
    targets = [(name, zone(name)) for name in args["timezones"]]
    if tool == "now":
        instant = datetime.now(timezone.utc)
    else:
        source = zone(args["source_timezone"])
        try:
            naive = datetime.fromisoformat(args["local_time"])
        except ValueError:
            raise InputError("Use local date and time in YYYY-MM-DDTHH:MM[:SS] format.") from None
        if naive.tzinfo is not None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", args["local_time"]):
            raise InputError("Supply a local date and time without a UTC offset, plus its source timezone.")
        candidates = []
        for fold in (0, 1):
            try:
                candidate = naive.replace(tzinfo=source, fold=fold).astimezone(timezone.utc)
            except (ValueError, OverflowError):
                raise InputError("Converted times must remain between years 1 and 9999.") from None
            if candidate.astimezone(source).replace(tzinfo=None) == naive and candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            raise InputError("That local time does not exist because the clocks move forward. Choose another time.")
        if len(candidates) == 2 and args["occurrence"] == "reject":
            raise InputError("That local time occurs twice when clocks move back. Choose earlier or later occurrence.")
        instant = sorted(candidates)[-1 if args["occurrence"] == "later" else 0]
    try:
        times = [{"timezone": name, "datetime": instant.astimezone(target).isoformat(timespec="seconds"),
                  "abbreviation": instant.astimezone(target).tzname()} for name, target in targets]
    except (ValueError, OverflowError):
        raise InputError("Converted times must remain between years 1 and 9999.") from None
    return result("; ".join(f"{item['timezone']}: {item['datetime']}" for item in times),
                  utc=instant.isoformat(timespec="seconds"), times=times)
