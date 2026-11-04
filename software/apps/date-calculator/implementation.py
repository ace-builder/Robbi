"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date, datetime, timedelta

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

def today(context):
    return datetime.now(zone(context.config.get("timezone", "Europe/Berlin"))).date()

def day(value, fallback=None):
    if not value and fallback is not None:
        return fallback
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise InputError("Use a date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputError("That calendar date does not exist.") from None

@handler
def date_calculator(context, tool, args):
    current = today(context)
    if tool == "shift":
        start = day(args["date"], current)
        try:
            shifted = start + timedelta(days=args["days"])
        except OverflowError:
            raise InputError("The resulting date must be between years 1 and 9999.") from None
        return result(f"{shifted.isoformat()} ({shifted.strftime('%A')}).", date=shifted.isoformat(),
                      weekday=shifted.strftime("%A"), days=args["days"])
    start = day(args.get("start", ""), current)
    end = day(args["end"])
    low, high = sorted((start, end))
    span = (high - low).days
    # Full weeks plus at most six days; no loop over arbitrary date spans.
    business = (span // 7) * 5 + sum((low.weekday() + i) % 7 < 5 for i in range(span % 7))
    holidays = {day(item) for item in args.get("holidays", [])}
    business -= sum(low <= item < high and item.weekday() < 5 for item in holidays)
    if end < start:
        business = -business
    days = (end - start).days
    return result(f"{days} calendar days; {business} weekdays excluding supplied holidays.",
                  start=start.isoformat(), end=end.isoformat(), calendar_days=days,
                  business_days=business, convention="Earlier date included, later date excluded; reverse ranges have a negative sign.")
