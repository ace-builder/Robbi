"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from decimal import localcontext

from functools import wraps

import json

import re

import secrets

from urllib.parse import urlsplit

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

def valid_url(value):
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 or char in "<>\\" for char in value):
        raise InputError("Use an HTTP or HTTPS link without spaces or control characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or port == 0):
            raise ValueError
    except ValueError:
        raise InputError("Use a complete HTTP or HTTPS URL without embedded credentials.") from None
    return value

def validate_bookmarks(entries):
    identifiers, urls = set(), set()
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("title"), str) or not entry["title"].strip() or len(entry["title"]) > 100
                or not isinstance(entry.get("url"), str) or len(entry["url"]) > 1000
                or not isinstance(entry.get("tags"), list) or len(entry["tags"]) > 5
                or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 30 for tag in entry["tags"])
                or not isinstance(entry.get("id"), str) or not re.fullmatch(r"[0-9a-f]{12}", entry["id"])
                or entry["id"] in identifiers or entry["url"] in urls):
            raise InputError("Saved bookmarks are invalid; restore a state backup.")
        identifiers.add(entry["id"])
        urls.add(entry["url"])
        valid_url(entry["url"])

@handler
def bookmarks(context, tool, args):
    state, entries = collection(context, "bookmarks", list, 40)
    validate_bookmarks(entries)
    if tool == "save":
        url = valid_url(args["url"])
        title = clean(args["title"], "a bookmark title", 100)
        tags = []
        for item in args["tags"]:
            tag = clean(item, "a tag", 30).casefold()
            if len(tag) > 30:
                raise InputError("A tag exceeds 30 characters after case normalization. Use a shorter tag.")
            if tag not in tags:
                tags.append(tag)
        existing = next((entry for entry in entries if entry["url"] == url), None)
        if existing is None and len(entries) >= 40:
            raise InputError("Forty bookmarks are saved. Remove one before adding another.")
        record = {"id": existing["id"] if existing else secrets.token_hex(6), "title": title, "url": url, "tags": tags}
        if existing is not None:
            entries[entries.index(existing)] = record
        else:
            entries.append(record)
        save(context, state, "bookmarks", entries)
        return result(f"Saved {title}.", bookmark=record)
    if tool == "remove":
        remaining = [entry for entry in entries if entry["id"] != args["id"]]
        if len(remaining) == len(entries):
            raise InputError("That bookmark ID was not found. Search bookmarks to copy its ID.")
        save(context, state, "bookmarks", remaining)
        return result("Bookmark removed.")
    query = args["query"].strip().casefold()
    selected = [entry for entry in entries if query in " ".join([entry["title"], entry["url"], *entry["tags"]]).casefold()]
    summary = "; ".join(entry["title"] for entry in selected[:8]) or "No matching bookmarks."
    if len(selected) > 8:
        summary += f"; and {len(selected) - 8} more. All {len(selected)} links are included in the results."
    return result(summary,
                  bookmarks=selected, note="Links are stored locally; this tool does not fetch or open websites.")
