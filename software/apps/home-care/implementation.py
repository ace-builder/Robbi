"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from functools import wraps

import re

import secrets

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robbi.app_runtime.schema import bounded_json

from robbi.errors import ValidationError

class InputError(ValueError):
    pass

def handler(function):
    @wraps(function)
    def run(context, tool, arguments):
        context.checkpoint()
        try:
            return function(context, tool, arguments)
        except InputError as error:
            return {"ok": False, "text": str(error)}
    return run

def result(text, **data):
    return {"ok": True, "text": text, "data": data}

def today(context):
    try:
        zone = ZoneInfo(context.config.get("timezone", "Europe/Berlin"))
        return datetime.now(zone).date()
    except (ValueError, ZoneInfoNotFoundError):
        raise InputError("Choose an IANA timezone such as Europe/Berlin or UTC in app settings.") from None

def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise InputError("Use a date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputError("That calendar date does not exist.") from None

def shifted(value, days):
    try:
        return value + timedelta(days=days)
    except OverflowError:
        raise InputError("The resulting date would be outside years 0001–9999.") from None

def text(value, name, maximum, *, optional=False):
    if not isinstance(value, str) or len(value.strip()) > maximum or (not optional and not value.strip()):
        raise InputError(f"Provide {name} of {'0' if optional else '1'} to {maximum} characters.")
    return value.strip()

def valid(condition):
    if not condition:
        raise ValueError("Invalid saved record")

def fields(row, names):
    valid(isinstance(row, dict) and set(row) == set(names.split()))

def saved_text(value, maximum, optional=False):
    valid(isinstance(value, str) and len(value) <= maximum and (optional or bool(value.strip())))

def saved_date(value, optional=False):
    if optional and value == "":
        return
    day(value)

def identifier(value, length=12, optional=False):
    valid(isinstance(value, str) and ((optional and value == "") or bool(re.fullmatch(r"[0-9a-f]{%d}" % length, value))))

def integer(value, minimum, maximum):
    valid(type(value) is int and minimum <= value <= maximum)

def load(context, collections):
    """Validate every row before a mutation; never drop unfamiliar saved data."""
    state = context.read_state()
    if state == {}:
        return {"version": 1, **{name: [] for name in collections}}
    try:
        valid(set(state) == {"version", *collections})
        valid(type(state["version"]) is int and state["version"] == 1)
        for name, (limit, validate) in collections.items():
            rows = state[name]
            valid(isinstance(rows, list) and len(rows) <= limit)
            seen = set()
            for row in rows:
                validate(row)
                identifier(row["id"])
                valid(row["id"] not in seen)
                seen.add(row["id"])
    except (InputError, ValueError, TypeError, KeyError):
        raise InputError("Saved app data is invalid or has an unsupported version. Restore its state.json from a backup; no records were changed.") from None
    return state

def check_size(state):
    try:
        bounded_json(state, 48000)
    except ValidationError:
        raise InputError("The app's saved-data limit is reached. Shorten notes or remove older records before saving; existing data is unchanged.") from None

def commit(context, state):
    check_size(state)
    context.write_state(state)

def new_id(rows):
    used = {row["id"] for row in rows}
    for _ in range(10):
        value = secrets.token_hex(6)
        if value not in used:
            return value
    raise InputError("Could not allocate a record ID; please try again.")

def selected(rows, value):
    row = next((row for row in rows if row["id"] == value), None)
    if row is None:
        raise InputError("That record ID was not found. List or search the app and use the exact returned ID.")
    return row

def room_for(rows, limit):
    if len(rows) >= limit:
        raise InputError(f"This collection holds at most {limit} records. Remove an older record first.")

def _chore(row):
    fields(row, "id name every_days next_due last_done completions notes")
    saved_text(row["name"], 80)
    integer(row["every_days"], 1, 3650)
    saved_date(row["next_due"])
    saved_date(row["last_done"], True)
    integer(row["completions"], 0, 1000000)
    saved_text(row["notes"], 160, True)
    valid((row["completions"] == 0) == (row["last_done"] == ""))

@handler
def home_care(context, tool, arguments):
    local_day = today(context)
    state = load(context, {"chores": (40, _chore)})
    rows = state["chores"]
    if tool == "agenda":
        all_chores = arguments.get("include_all", False)
        horizon = date.max if all_chores else shifted(local_day, arguments.get("days_ahead", 7))
        matches = [row for row in rows if day(row["next_due"]) <= horizon]
        matches.sort(key=lambda row: (row["next_due"], row["name"].casefold()))
        summary = f"All {len(matches)} saved chore(s)." if all_chores else f"{len(matches)} chore(s) due by {horizon.isoformat()}."
        return result(summary + " Due dates do not trigger automatic alerts.",
                      today=local_day.isoformat(), chores=[{**row, "days_until_due": (day(row["next_due"]) - local_day).days} for row in matches])
    if tool == "manage":
        existing = selected(rows, arguments["id"]) if arguments.get("id") else None
        if arguments.get("action", "save") == "remove":
            if existing is None:
                raise InputError("Supply the exact chore ID to remove.")
            rows.remove(existing)
            commit(context, state)
            return result("Removed the chore. Any separately started timer remains independent.", chore=existing)
        if existing is None:
            room_for(rows, 40)
        interval = arguments.get("every_days", existing["every_days"] if existing else 7)
        due = day(arguments["first_due"]) if arguments.get("first_due") else (
            shifted(day(existing["last_done"]), interval) if existing and existing["last_done"] and interval != existing["every_days"] else
            day(existing["next_due"]) if existing else local_day)
        row = {"id": existing["id"] if existing else new_id(rows), "name": text(arguments.get("name", existing["name"] if existing else ""), "a chore name", 80),
               "every_days": interval, "next_due": due.isoformat(), "last_done": existing["last_done"] if existing else "",
               "completions": existing["completions"] if existing else 0, "notes": text(arguments.get("notes", existing["notes"] if existing else ""), "notes", 160, optional=True)}
        if existing is None:
            rows.append(row)
        else:
            rows[rows.index(existing)] = row
        commit(context, state)
        return result(f"Saved {row['name']}; next due {row['next_due']}.", chore=row)
    row = selected(rows, arguments["id"])
    if arguments.get("action", "complete") == "remind":
        minutes = arguments.get("minutes", 0)
        if minutes < 1:
            raise InputError("Choose 1–1440 minutes for a one-shot audible reminder.")
        context.request_permissions("state", "robot.reminders")
        timer = context.robot.timer("start", seconds=minutes * 60, label=("Home care: " + row["name"])[:80])
        if not timer.get("ok"):
            return timer
        return result(f"Started a one-shot reminder for {row['name']} in {minutes} minute(s). Its timer is independent of the recurring due date.",
                      chore_id=row["id"], timer=timer.get("data", {}))
    completed = day(arguments["completed_on"]) if arguments.get("completed_on") else local_day
    if completed > local_day:
        raise InputError("A completion date cannot be in the future.")
    if row["last_done"] and completed < day(row["last_done"]):
        raise InputError("Completion predates the latest recorded completion. Undo that completion first if it needs correction.")
    if row["last_done"] == completed.isoformat():
        return result("This chore is already completed on that date; its next due date is unchanged.", chore=row)
    if row["completions"] >= 1000000:
        raise InputError("This chore has reached its completion-count limit.")
    row["next_due"] = shifted(completed, row["every_days"]).isoformat()
    row["last_done"] = completed.isoformat()
    row["completions"] += 1
    commit(context, state)
    return result(f"Completed {row['name']}; next due {row['next_due']}, measured from the actual completion date.", chore=row)

def names(values, label, maximum, minimum, limit):
    if not isinstance(values, list) or not minimum <= len(values) <= limit:
        raise InputError(f"Supply {minimum}–{limit} {label}.")
    cleaned = [text(value, label, maximum) for value in values]
    if len({value.casefold() for value in cleaned}) != len(cleaned):
        raise InputError(f"Use distinct {label}; capitalization alone does not distinguish them.")
    return cleaned
