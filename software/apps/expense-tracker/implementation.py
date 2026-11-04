"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date, datetime

from decimal import Decimal, ROUND_HALF_UP, localcontext

from functools import wraps

import json

import re

import secrets

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

def decimal(value):
    return Decimal(str(value))

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

def currency(value):
    value = value.upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise InputError("Use a three-letter currency label such as EUR, USD or GBP.")
    return value

def validate_expenses(entries):
    identifiers = set()
    for entry in entries:
        if (not isinstance(entry, dict) or not all(isinstance(entry.get(field), str)
                for field in ("id", "amount", "currency", "date", "category", "description"))
                or not re.fullmatch(r"[0-9a-f]{12}", entry["id"])
                or not re.fullmatch(r"\d{1,10}\.\d{2}", entry["amount"])
                or not re.fullmatch(r"[A-Z]{3}", entry["currency"])
                or len(entry["description"]) > 120 or not entry["category"].strip() or len(entry["category"]) > 40
                or entry["id"] in identifiers or not Decimal(0) < decimal(entry["amount"]) <= Decimal("1000000000")):
            raise InputError("Saved expenses are invalid; restore a state backup.")
        identifiers.add(entry["id"])
        day(entry["date"])

@handler
def expense_tracker(context, tool, args):
    state, entries = collection(context, "entries", list, 120)
    validate_expenses(entries)
    if tool == "add":
        if len(entries) >= 120:
            raise InputError("120 expenses are saved. Remove an older expense before adding another.")
        amount = decimal(args["amount"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if amount <= 0:
            raise InputError("Expense amount must round to at least 0.01.")
        record = {"id": secrets.token_hex(6), "amount": format(amount, ".2f"),
                  "currency": currency(args["currency"]), "date": day(args["date"], today(context)).isoformat(),
                  "category": clean(args["category"], "a category", 40), "description": args["description"].strip()}
        entries.append(record)
        save(context, state, "entries", entries)
        return result(f"Saved {record['amount']} {record['currency']} for {record['category']}.", expense=record)
    if tool == "remove":
        remaining = [entry for entry in entries if entry["id"] != args["id"]]
        if len(remaining) == len(entries):
            raise InputError("That expense ID was not found. List expenses to copy its ID.")
        save(context, state, "entries", remaining)
        return result("Expense removed.")
    start = day(args["start"], date.min)
    end = day(args["end"], date.max)
    if start > end:
        raise InputError("Start date must be no later than end date.")
    code = currency(args["currency"]) if args["currency"].strip() else ""
    selected = [entry for entry in entries if start <= day(entry["date"]) <= end
                and (not code or entry["currency"] == code)]
    totals = {}
    for entry in selected:
        totals[entry["currency"]] = totals.get(entry["currency"], Decimal(0)) + decimal(entry["amount"])
    totals = {key: format(value, ".2f") for key, value in sorted(totals.items())}
    return result("; ".join(f"{value} {key}" for key, value in totals.items()) or "No matching expenses.",
                  entries=sorted(selected, key=lambda item: item["date"], reverse=True), totals=totals,
                  note="Totals are separate for each currency. No exchange rates or accounting/tax calculations.")
