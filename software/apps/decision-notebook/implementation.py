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

def names(values, label, maximum, minimum, limit):
    if not isinstance(values, list) or not minimum <= len(values) <= limit:
        raise InputError(f"Supply {minimum}–{limit} {label}.")
    cleaned = [text(value, label, maximum) for value in values]
    if len({value.casefold() for value in cleaned}) != len(cleaned):
        raise InputError(f"Use distinct {label}; capitalization alone does not distinguish them.")
    return cleaned

def _decision(row):
    fields(row, "id title options criteria weights ratings notes updated outcome")
    saved_text(row["title"], 100)
    saved_text(row["notes"], 160, True)
    saved_date(row["updated"])
    valid(row["options"] == names(row["options"], "option names", 60, 2, 5))
    valid(row["criteria"] == names(row["criteria"], "criterion names", 50, 1, 5))
    valid(isinstance(row["weights"], list) and len(row["weights"]) == len(row["criteria"]))
    for weight in row["weights"]:
        integer(weight, 1, 10)
    valid(isinstance(row["ratings"], list) and len(row["ratings"]) <= 25)
    seen = set()
    for rating in row["ratings"]:
        fields(rating, "option criterion score reason")
        integer(rating["option"], 0, len(row["options"]) - 1)
        integer(rating["criterion"], 0, len(row["criteria"]) - 1)
        integer(rating["score"], 0, 10)
        saved_text(rating["reason"], 120, True)
        pair = rating["option"], rating["criterion"]
        valid(pair not in seen)
        seen.add(pair)
    if row["outcome"] is not None:
        outcome = row["outcome"]
        fields(outcome, "choice notes date")
        valid(outcome["choice"] in row["options"])
        saved_text(outcome["notes"], 240)
        saved_date(outcome["date"])

def named_index(values, name, label):
    match = next((index for index, value in enumerate(values) if value.casefold() == name.strip().casefold()), None)
    if match is None:
        raise InputError(f"Choose an existing {label}: {', '.join(values)}.")
    return match

def comparison(row):
    total_weight = sum(row["weights"])
    options, missing = [], []
    for index, name in enumerate(row["options"]):
        ratings = {rating["criterion"]: rating for rating in row["ratings"] if rating["option"] == index}
        omitted = [criterion for number, criterion in enumerate(row["criteria"]) if number not in ratings]
        missing.extend({"option": name, "criterion": criterion} for criterion in omitted)
        options.append({"name": name, "complete": not omitted,
                        "weighted_score": None if omitted else round(sum(ratings[number]["score"] * weight for number, weight in enumerate(row["weights"])) / total_weight, 4),
                        "reasons": [{"criterion": row["criteria"][number], "weight": row["weights"][number],
                                     "score": rating["score"], "reason": rating["reason"]} for number, rating in sorted(ratings.items())]})
    ranking = sorted(options, key=lambda option: -option["weighted_score"]) if not missing else []
    best = [option["name"] for option in ranking if option["weighted_score"] == ranking[0]["weighted_score"]] if ranking else []
    return {"id": row["id"], "title": row["title"], "options": options, "ranking": [option["name"] for option in ranking],
            "best_scored": best, "missing": missing, "outcome": row["outcome"], "notes": row["notes"]}

@handler
def decision_notebook(context, tool, arguments):
    local_day = today(context)
    state = load(context, {"decisions": (8, _decision)})
    rows = state["decisions"]
    if tool == "save":
        existing = selected(rows, arguments["id"]) if arguments.get("id") else None
        if existing is None:
            room_for(rows, 8)
        options = names(arguments["options"], "option names", 60, 2, 5)
        criteria = names(arguments["criteria"], "criterion names", 50, 1, 5)
        weights = arguments.get("weights", existing["weights"] if existing and criteria == existing["criteria"] else []) or [1] * len(criteria)
        if len(weights) != len(criteria):
            raise InputError("Supply one weight per criterion, in the same order, or leave all weights empty for equal importance.")
        changed = existing and (options != existing["options"] or criteria != existing["criteria"])
        if changed and (existing["ratings"] or existing["outcome"]) and not arguments.get("reset_scores", False):
            raise InputError("Changing options or criteria would discard scores and the recorded outcome. Set reset_scores=true only if you want that reset.")
        row = {"id": existing["id"] if existing else new_id(rows), "title": text(arguments["title"], "a decision title", 100),
               "options": options, "criteria": criteria, "weights": weights,
               "ratings": existing["ratings"] if existing and not changed else [],
               "notes": text(arguments.get("notes", existing["notes"] if existing else ""), "notes", 160, optional=True), "updated": local_day.isoformat(),
               "outcome": existing["outcome"] if existing and not changed else None}
        if existing is None:
            rows.append(row)
        else:
            rows[rows.index(existing)] = row
        commit(context, state)
        return result(f"Saved {row['title']}. Score each option on each criterion from 0 to 10; higher always means better.", decision=row)
    if tool == "review" and arguments.get("action", "list") == "list":
        query = arguments.get("query", "").strip().casefold()
        summaries = [{"id": row["id"], "title": row["title"], "updated": row["updated"], "has_outcome": row["outcome"] is not None,
                      "ratings_done": len(row["ratings"]), "ratings_needed": len(row["options"]) * len(row["criteria"])}
                     for row in rows if query in (row["title"] + " " + row["notes"]).casefold()]
        return result(f"{len(summaries)} decision(s) in your notebook.", decisions=summaries)
    row = selected(rows, arguments.get("id", ""))
    if tool == "score":
        option = named_index(row["options"], arguments["option"], "option")
        criterion = named_index(row["criteria"], arguments["criterion"], "criterion")
        rating = {"option": option, "criterion": criterion, "score": arguments["score"],
                  "reason": text(arguments.get("reason", ""), "a reason", 120, optional=True)}
        previous = next((value for value in row["ratings"] if (value["option"], value["criterion"]) == (option, criterion)), None)
        if previous is None:
            row["ratings"].append(rating)
        else:
            row["ratings"][row["ratings"].index(previous)] = rating
        row["updated"] = local_day.isoformat()
        commit(context, state)
        return result("Saved that score and reason.", comparison=comparison(row))
    action = arguments.get("action", "list")
    if action == "compare":
        compared = comparison(row)
        return result(f"Still missing {len(compared['missing'])} rating(s); no ranking yet." if compared["missing"] else
                      "Weighted comparison complete. Scores reflect your preferences, not a prediction of the outcome.", comparison=compared)
    if action == "remove":
        rows.remove(row)
        commit(context, state)
        return result("Removed the decision from your notebook.", id=row["id"])
    reviewed = day(arguments["reviewed_on"]) if arguments.get("reviewed_on") else local_day
    if reviewed > local_day:
        raise InputError("An outcome review date cannot be in the future.")
    choice = row["options"][named_index(row["options"], arguments.get("choice", ""), "chosen option")]
    row["outcome"] = {"choice": choice, "notes": text(arguments.get("outcome", ""), "an outcome review", 240), "date": reviewed.isoformat()}
    row["updated"] = local_day.isoformat()
    commit(context, state)
    return result("Saved your choice and outcome review.", comparison=comparison(row))
