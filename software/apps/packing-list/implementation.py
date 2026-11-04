"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date

from decimal import localcontext

from functools import wraps

import json

import re

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

def validate_trips(trips):
    for name, trip in trips.items():
        if (not isinstance(name, str) or not name.strip() or len(name) > 60 or not isinstance(trip, dict)
                or not isinstance(trip.get("items"), list) or len(trip["items"]) > 40
                or not isinstance(trip.get("departure"), str)):
            raise InputError("Saved packing lists are invalid; restore a state backup.")
        if trip["departure"]:
            day(trip["departure"])
        for item in trip["items"]:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip() or len(item["name"]) > 80
                    or not isinstance(item.get("category"), str) or not item["category"].strip() or len(item["category"]) > 40
                    or type(item.get("quantity")) is not int or not 1 <= item["quantity"] <= 100
                    or type(item.get("packed")) is not int or not 0 <= item["packed"] <= item["quantity"]):
                raise InputError("Saved packing items are invalid; restore a state backup.")

@handler
def packing_list(context, tool, args):
    state, trips = collection(context, "trips", dict, 5)
    validate_trips(trips)
    action = args["action"]
    if action == "list":
        return result(", ".join(trips) or "No trips yet.", trips=[{"name": name, "departure": trip["departure"],
                      "items": len(trip["items"])} for name, trip in trips.items()])
    name = clean(args["trip"], "a trip name", 60)
    if action == "create":
        if name in trips:
            raise InputError("That trip already exists.")
        if len(trips) >= 5:
            raise InputError("Five trips are saved. Delete an old trip before creating another.")
        departure = day(args["departure"]).isoformat() if args["departure"] else ""
        trips[name] = {"departure": departure, "items": []}
    elif name not in trips:
        raise InputError("That trip does not exist. List trips to see their exact names.")
    trip = trips[name]
    if action == "add":
        if len(trip["items"]) >= 40:
            raise InputError("This trip has 40 items. Remove one before adding another.")
        trip["items"].append({"name": clean(args["item_name"], "an item name", 80),
                              "category": clean(args["category"], "a category", 40),
                              "quantity": args["quantity"], "packed": 0})
    elif action in {"pack", "remove"}:
        index = args["item_number"] - 1
        if not 0 <= index < len(trip["items"]):
            raise InputError("Choose an item number from this trip's packing list.")
        if action == "remove":
            del trip["items"][index]
        else:
            packed = args["packed"]
            if packed > trip["items"][index]["quantity"]:
                raise InputError("Packed quantity cannot exceed the quantity needed.")
            trip["items"][index]["packed"] = packed
    elif action == "delete":
        del trips[name]
        save(context, state, "trips", trips)
        return result("Trip deleted.")
    if action != "show":
        save(context, state, "trips", trips)
    items = [{"number": index + 1, **item, "remaining": item["quantity"] - item["packed"]}
             for index, item in enumerate(trip["items"])]
    remaining = sum(item["remaining"] for item in items)
    return result(f"{name}: {remaining} individual items still to pack.", trip=name,
                  departure=trip["departure"], items=items, remaining=remaining)
