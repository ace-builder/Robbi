"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date, datetime

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

def _place(row):
    fields(row, "id name location notes status updated photo_id")
    saved_text(row["name"], 80)
    saved_text(row["location"], 180)
    saved_text(row["notes"], 160, True)
    valid(row["status"] in {"stored", "found"})
    saved_date(row["updated"])
    identifier(row["photo_id"], 32, True)

def place_view(row):
    return {**row, "photo_url": f"/api/apps/where-did-i-put-it/artifacts/{row['photo_id']}" if row["photo_id"] else ""}

@handler
def where_did_i_put_it(context, tool, arguments):
    local_day = today(context)
    state = load(context, {"items": (40, _place)})
    rows = state["items"]
    if tool == "find":
        query = arguments.get("query", "").strip().casefold()
        status = arguments.get("status", "all")
        matches = [row for row in rows if (status == "all" or row["status"] == status)
                   and query in " ".join((row["name"], row["location"], row["notes"])).casefold()]
        matches.sort(key=lambda row: row["updated"], reverse=True)
        return result(f"Found {len(matches)} saved location record(s). Locations were supplied by you.", items=[place_view(row) for row in matches])
    if tool == "remember":
        existing = selected(rows, arguments["id"]) if arguments.get("id") else None
        if existing is None:
            room_for(rows, 40)
        row = {"id": existing["id"] if existing else new_id(rows),
               "name": text(arguments["name"], "an item name", 80),
               "location": text(arguments["location"], "a location", 180),
               "notes": text(arguments.get("notes", existing["notes"] if existing else ""), "notes", 160, optional=True),
               "status": "stored", "updated": local_day.isoformat(),
               "photo_id": existing["photo_id"] if existing else ""}
        capture = arguments.get("photo", False)
        if capture:
            context.request_permissions("state", "robot.camera", "robot.display")
            row["photo_id"] = "0" * 32  # Reserve metadata space before touching the camera.
        if existing is None:
            rows.append(row)
        else:
            rows[rows.index(existing)] = row
        check_size(state)
        artifacts = []
        if capture:
            artifact = context.save_image(context.robot.capture_image(), "Location: " + row["name"])
            row["photo_id"] = artifact["id"]
            artifacts.append(artifact)
        commit(context, state)
        response = result(f"Remembered {row['name']}: {row['location']}." + (" Photo attached without interpreting its contents." if capture else ""), item=place_view(row))
        if artifacts:
            response["artifacts"] = artifacts
        return response
    row = selected(rows, arguments["id"])
    action = arguments["action"]
    if action == "remove":
        rows.remove(row)
    else:
        row["status"] = "found" if action == "found" else "stored"
        if arguments.get("location"):
            row["location"] = text(arguments["location"], "a location", 180)
        row["updated"] = local_day.isoformat()
    commit(context, state)
    return result(f"{row['name']}: {'record removed' if action == 'remove' else row['status']}.", item=place_view(row))

def names(values, label, maximum, minimum, limit):
    if not isinstance(values, list) or not minimum <= len(values) <= limit:
        raise InputError(f"Supply {minimum}–{limit} {label}.")
    cleaned = [text(value, label, maximum) for value in values]
    if len({value.casefold() for value in cleaned}) != len(cleaned):
        raise InputError(f"Use distinct {label}; capitalization alone does not distinguish them.")
    return cleaned
