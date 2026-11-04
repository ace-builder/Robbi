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

def _stock(row):
    fields(row, "id name quantity expires storage")
    saved_text(row["name"], 80)
    saved_text(row["quantity"], 40, True)
    saved_date(row["expires"], True)
    valid(row["storage"] in {"fridge", "freezer", "pantry"})

def _shopping(row):
    fields(row, "id name bought")
    saved_text(row["name"], 80)
    valid(type(row["bought"]) is bool)

ALIASES = {
    "tomatoes": "tomato", "tomaten": "tomato", "onions": "onion", "zwiebel": "onion", "zwiebeln": "onion",
    "eggs": "egg", "ei": "egg", "eier": "egg", "potatoes": "potato", "kartoffeln": "potato",
    "carrots": "carrot", "karotten": "carrot", "möhren": "carrot", "spinach": "spinach", "spinat": "spinach",
    "reis": "rice", "nudeln": "pasta", "spaghetti": "pasta", "brot": "bread", "milch": "milk",
    "käse": "cheese", "kichererbsen": "chickpeas", "chickpea": "chickpeas", "flour": "flour", "mehl": "flour",
    "courgette": "zucchini", "bananas": "banana", "banane": "banana", "bananen": "banana", "oats": "oats", "haferflocken": "oats",
}

def ingredient(value):
    normalized = " ".join(value.casefold().split())
    return ALIASES.get(normalized, normalized)

RECIPES = (
    ("tomato-pasta", "Tomato pasta", ("pasta", "tomato", "onion"),
     ("Cook the pasta following its package instructions.", "Chop and soften the onion and tomatoes in a pan with a splash of water; combine with the cooked pasta.")),
    ("vegetable-rice", "Vegetable rice", ("rice", "carrot", "onion"),
     ("Cook rice following its package instructions.", "Finely chop the carrot and onion, cook in a covered pan with a little water until tender, then combine with rice.")),
    ("spinach-omelette", "Spinach omelette", ("egg", "spinach", "cheese"),
     ("Wilt the spinach and grate the cheese.", "Beat the eggs, add spinach and cheese, and cook in a suitable pan until the egg is set throughout.")),
    ("potato-hash", "Potato and onion hash", ("potato", "onion"),
     ("Dice and boil the potatoes until tender, then drain.", "Cook chopped onion in a suitable pan, add the potatoes and turn until browned as desired.")),
    ("chickpea-stew", "Tomato chickpea stew", ("chickpeas", "tomato", "onion"),
     ("Use cooked or canned chickpeas; prepare dried chickpeas according to their package first.", "Soften chopped onion and tomato with a little water; add the cooked chickpeas and simmer until hot.")),
    ("tomato-toast", "Tomato cheese toast", ("bread", "tomato", "cheese"),
     ("Top bread with sliced tomato and cheese.", "Toast or grill until the bread is toasted and cheese melts, watching it closely.")),
    ("banana-oats", "Banana oats", ("oats", "banana", "milk"),
     ("Cook oats with milk following the oat package instructions.", "Slice or mash the banana and stir it into the oats.")),
    ("courgette-pasta", "Courgette pasta", ("pasta", "zucchini", "cheese"),
     ("Cook pasta following its package instructions.", "Cook thinly sliced courgette in a suitable pan with a splash of pasta water; combine with pasta and grated cheese.")),
)

def recipe(identifier):
    value = next((row for row in RECIPES if row[0] == identifier), None)
    if value is None:
        raise InputError("Choose a recipe ID returned by Suggest recipes.")
    return value

def available_stock(rows, local_day):
    return {ingredient(row["name"]) for row in rows if not row["expires"] or day(row["expires"]) >= local_day}

@handler
def fridge_rescue(context, tool, arguments):
    local_day = today(context)
    state = load(context, {"stock": (60, _stock), "shopping": (60, _shopping)})
    rows = state["stock"]
    if tool == "stock":
        action = arguments.get("action", "list")
        if action == "list":
            view = arguments.get("view", "all")
            until = shifted(local_day, arguments.get("days", 3))
            matching = [row for row in rows if view == "all" or (row["expires"] and
                (day(row["expires"]) < local_day if view == "past_date" else local_day <= day(row["expires"]) <= until))]
            matching.sort(key=lambda row: (row["expires"] or "9999-12-31", row["name"].casefold()))
            items = [{**row, "days_until_date": (day(row["expires"]) - local_day).days if row["expires"] else None} for row in matching]
            return result(f"{len(items)} stock item(s). Dates are your labels, not a food-safety assessment.", items=items, today=local_day.isoformat())
        existing = selected(rows, arguments["id"]) if arguments.get("id") else None
        if action == "remove":
            if existing is None:
                raise InputError("Supply the exact stock ID to remove.")
            rows.remove(existing)
            commit(context, state)
            return result("Removed that stock record.", item=existing)
        if existing is None:
            room_for(rows, 60)
        expires = arguments.get("expires", existing["expires"] if existing else "")
        if expires:
            day(expires)
        row = {"id": existing["id"] if existing else new_id(rows), "name": text(arguments.get("name", existing["name"] if existing else ""), "an ingredient name", 80),
               "quantity": text(arguments.get("quantity", existing["quantity"] if existing else ""), "a quantity", 40, optional=True),
               "expires": expires, "storage": arguments.get("storage", existing["storage"] if existing else "fridge")}
        if existing is None:
            rows.append(row)
        else:
            rows[rows.index(existing)] = row
        commit(context, state)
        return result(f"Saved {row['name']} in your stock.", item=row)
    available = available_stock(rows, local_day)
    if tool == "suggest":
        exclude = {ingredient(text(name, "an excluded ingredient", 80)) for name in arguments.get("exclude", [])}
        soon = {ingredient(row["name"]) for row in rows if row["expires"] and 0 <= (day(row["expires"]) - local_day).days <= 3}
        suggestions = []
        for rid, name, ingredients, steps in RECIPES:
            required = set(ingredients)
            matched = [item for item in ingredients if item in available]
            if not matched or required & exclude:
                continue
            missing = [item for item in ingredients if item not in available]
            suggestions.append({"id": rid, "name": name, "matched": matched, "missing": missing,
                                "use_soon": sorted(required & soon), "steps": list(steps)})
        suggestions.sort(key=lambda row: (len(row["missing"]), -len(row["use_soon"]), -len(row["matched"]), row["id"]))
        suggestions = suggestions[:arguments.get("count", 5)]
        return result(f"{len(suggestions)} offline recipe idea(s). Past-date stock is excluded; check ingredients and quantities yourself. Nothing was added to shopping.",
                      recipes=suggestions, past_date_excluded=sum(bool(row["expires"]) and day(row["expires"]) < local_day for row in rows))
    shopping = state["shopping"]
    action = arguments.get("action", "list")
    if action == "list":
        return result(f"{sum(not row['bought'] for row in shopping)} pending item(s) in Fridge Rescue's own shopping list.", items=shopping)
    if action in {"bought", "needed", "remove"}:
        row = selected(shopping, arguments.get("id", ""))
        if action == "remove":
            shopping.remove(row)
        else:
            row["bought"] = action == "bought"
        commit(context, state)
        return result("Updated Fridge Rescue's own shopping list.", item=row)
    if action == "add_missing":
        chosen = recipe(arguments.get("recipe_id", ""))
        names = [name for name in chosen[2] if name not in available]
    else:
        names = [text(name, "a shopping item", 80) for name in arguments.get("items", [])]
        if not names:
            raise InputError("Supply at least one shopping item.")
    changed = []
    for name in names:
        row = next((row for row in shopping if ingredient(row["name"]) == ingredient(name)), None)
        if row is None:
            room_for(shopping, 60)
            row = {"id": new_id(shopping), "name": name, "bought": False}
            shopping.append(row)
            changed.append(row)
        elif row["bought"]:
            row["bought"] = False
            changed.append(row)
    if changed:
        commit(context, state)
    return result(f"Added or reopened {len(changed)} item(s) in Fridge Rescue's own shopping list.", items=changed, list="fridge-rescue")

def names(values, label, maximum, minimum, limit):
    if not isinstance(values, list) or not minimum <= len(values) <= limit:
        raise InputError(f"Supply {minimum}–{limit} {label}.")
    cleaned = [text(value, label, maximum) for value in values]
    if len({value.casefold() for value in cleaned}) != len(cleaned):
        raise InputError(f"Use distinct {label}; capitalization alone does not distinguish them.")
    return cleaned
