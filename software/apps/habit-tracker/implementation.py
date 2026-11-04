"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from decimal import localcontext

from functools import wraps

import json

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

def clean(value, label, maximum):
    value = value.strip()
    if not value or len(value) > maximum:
        raise InputError(f"Provide {label} of 1 to {maximum} characters.")
    return value

def collection(context, key, kind, limit):
    state = context.read_state()
    records = state.get(key, kind())
    if not isinstance(records, kind) or len(records) > limit:
        raise InputError("Saved app data is invalid; restore its state.json from a backup.")
    return state, records

def save(context, state, key, records):
    state[key] = records
    # Leave headroom for result metadata and reject before replacing valid state.
    if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > 55000:
        raise InputError("This app's storage is full. Remove an older entry and try again.")
    context.checkpoint()
    context.write_state(state)

def validate_habits(habits):
    if any(not isinstance(name, str) or not name.strip() or len(name) > 60 or not isinstance(days, list)
           or len(days) > 120 or any(not isinstance(item, str) for item in days)
           or len(days) != len(set(days)) for name, days in habits.items()):
        raise InputError("Saved habits are invalid; restore a state backup.")
    for dates in habits.values():
        for item in dates:
            day(item)

def habit_summary(name, dates, reference):
    logged = {day(item) for item in dates if day(item) <= reference}
    cursor = reference if reference in logged or reference == date.min else reference - timedelta(days=1)
    streak = 0
    while cursor in logged:
        streak += 1
        if cursor == date.min:
            break
        cursor -= timedelta(days=1)
    best = run = 0
    previous = None
    for marked in sorted(logged):
        run = run + 1 if previous and (marked - previous).days == 1 else 1
        best = max(best, run)
        previous = marked
    return {"name": name, "completed_today": reference in logged, "current_streak": streak,
            "best_streak": best, "completed_days": len(logged), "dates": sorted(dates)}

@handler
def habit_tracker(context, tool, args):
    state, habits = collection(context, "habits", dict, 12)
    validate_habits(habits)
    action, reference = args["action"], today(context)
    if action == "list":
        summaries = [habit_summary(name, dates, reference) for name, dates in habits.items()]
        return result("; ".join(f"{item['name']}: {item['current_streak']} day streak" for item in summaries)
                      or "No habits yet.", date=reference.isoformat(), habits=summaries)
    name = clean(args["name"], "a habit name", 60)
    if action == "create":
        if name in habits:
            raise InputError("That habit already exists.")
        if len(habits) >= 12:
            raise InputError("Twelve habits are saved. Remove one before adding another.")
        habits[name] = []
    else:
        if name not in habits:
            raise InputError("That habit does not exist. List habits to see their exact names.")
        if action == "remove":
            del habits[name]
        else:
            marked = day(args["date"], reference)
            if marked > reference:
                raise InputError("A habit cannot be completed on a future date.")
            value = marked.isoformat()
            if action == "mark" and value not in habits[name]:
                if len(habits[name]) >= 120:
                    raise InputError("This habit has 120 saved dates. Unmark an old date before adding another.")
                habits[name].append(value)
                habits[name].sort()
            elif action == "unmark" and value in habits[name]:
                habits[name].remove(value)
    save(context, state, "habits", habits)
    return result("Habit removed." if action == "remove" else f"Saved {name}.",
                  habit=habit_summary(name, habits[name], reference) if name in habits else None)
