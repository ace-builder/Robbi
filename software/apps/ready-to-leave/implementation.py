"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from functools import wraps

import math

import re

import uuid

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robbi.app_runtime.schema import bounded_json

class PlanningInput(ValueError):
    pass

def handler(function):
    @wraps(function)
    def run(context, tool, arguments):
        context.checkpoint()
        try:
            return function(context, tool, arguments)
        except PlanningInput as error:
            return {"ok": False, "text": str(error)}
    return run

def now():
    return datetime.now(timezone.utc)

def text(value, label, limit, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise PlanningInput(f"{label} must contain {'0' if empty else '1'} to {limit} characters.")
    return value.strip()

def integer(value, label, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise PlanningInput(f"{label} must be a whole number from 0 to {maximum}.")
    return value

def items(value, label, count, length):
    if not isinstance(value, list) or len(value) > count:
        raise PlanningInput(f"{label} must contain at most {count} items.")
    return list(dict.fromkeys(text(item, label, length, empty=False) for item in value))

def moment(value, label):
    value = text(value, label, 64, empty=False)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        result.astimezone(timezone.utc)
        return result
    except (ValueError, OverflowError):
        raise PlanningInput(f"{label} needs an ISO date/time with an explicit UTC offset.") from None

def zone(value):
    try:
        return ZoneInfo(value)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise PlanningInput("Use an IANA timezone such as Europe/Berlin or UTC.") from None

def reply(message, **data):
    return {"ok": True, "text": message, "data": data}

def dependency(context, app, tool, arguments):
    context.checkpoint()
    result = context.call_app(app, tool, arguments)
    context.checkpoint()
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise PlanningInput(f"{app} returned an invalid receipt. Check that app before planning again.")
    if result.get("cancelled"):
        context.checkpoint()
        raise PlanningInput(f"{app} was cancelled; no plan was saved.")
    return result

def event_record(value):
    if not isinstance(value, dict):
        raise PlanningInput("Calendar returned a malformed event.")
    identifier = text(value.get("id"), "Calendar event ID", 1024, empty=False)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise PlanningInput("Calendar returned an invalid event ID.")
    event = {key: text(value.get(key, ""), key, limit) for key, limit in
             (("title", 200), ("location", 200), ("start", 64), ("end", 64), ("url", 1000))}
    if type(value.get("all_day", False)) is not bool:
        raise PlanningInput("Calendar returned an invalid all-day marker.")
    event.update(id=identifier, all_day=value.get("all_day", False))
    try:
        if event["all_day"]:
            start, end = date.fromisoformat(event["start"]), date.fromisoformat(event["end"])
        else:
            start = moment(event["start"], "Appointment start").astimezone(timezone.utc)
            end = moment(event["end"], "Appointment end").astimezone(timezone.utc)
        if end <= start:
            raise ValueError
    except ValueError:
        raise PlanningInput("Calendar returned an invalid event time range.") from None
    return event

def select_event(context, args):
    response = dependency(context, "google-calendar", "events", {key: args.get(key, "") for key in ("query", "time_min", "time_max")} | {"limit": 10})
    if not response["ok"]:
        return None, {"ok": False, "text": "Calendar is unavailable: " + str(response.get("text", "Check Google Calendar configuration."))[:500]}
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) or len(data["events"]) > 10:
        raise PlanningInput("Calendar returned an invalid event list.")
    events = [event_record(item) for item in data["events"]]
    if len({item["id"] for item in events}) != len(events):
        raise PlanningInput("Calendar returned duplicate event IDs; narrow the search and retry.")
    selected = args.get("event_id", "")
    matches = [item for item in events if item["id"] == selected] if selected else events
    if len(matches) != 1 or (not selected and data.get("has_more")):
        return None, {"ok": False, "text": "Choose an event ID from these results, or narrow the calendar query/date range. No event was selected.",
                      "data": {"events": events, "has_more": bool(data.get("has_more")), "selection_required": True}}
    return matches[0], None

def ready_state(context):
    state = context.read_state()
    if not state:
        return {"version": 1, "preferences": {"origin": "", "weather_location": "", "mode": "driving", "buffer_minutes": 10, "travel_minutes": None, "carry_items": []}, "plans": []}
    if set(state) != {"version", "preferences", "plans"} or type(state.get("version")) is not int or state.get("version") != 1 or not isinstance(state.get("preferences"), dict):
        raise PlanningInput("Saved departure data is invalid. Restore or repair ready-to-leave/state.json before editing it.")
    prefs = state["preferences"]
    if set(prefs) != {"origin", "weather_location", "mode", "buffer_minutes", "travel_minutes", "carry_items"}:
        raise PlanningInput("Saved departure preferences are invalid; restore the state file before editing.")
    validate_preferences(prefs)
    plans = state.get("plans")
    if not isinstance(plans, list) or len(plans) > 3:
        raise PlanningInput("Saved departure plans are invalid; restore the state file before editing.")
    seen = set()
    for plan in plans:
        required = {"id", "created_at", "event", "departure", "travel_seconds", "buffer_minutes", "origin", "destination", "mode", "carry_items", "reminder"}
        if not isinstance(plan, dict) or set(plan) != required or not re.fullmatch(r"[a-f0-9]{12}", str(plan.get("id", ""))) or plan["id"] in seen:
            raise PlanningInput("Saved departure plan IDs or fields are invalid; restore the state file before editing.")
        seen.add(plan["id"])
        moment(plan["created_at"], "Saved plan timestamp")
        event_record(plan["event"])
        if plan["departure"] is not None:
            moment(plan["departure"], "Saved departure")
        if plan["travel_seconds"] is not None:
            integer(plan["travel_seconds"], "Saved travel seconds", 86400)
        integer(plan["buffer_minutes"], "Buffer minutes", 180)
        text(plan["origin"], "Origin", 300)
        text(plan["destination"], "Destination", 300)
        if plan["mode"] not in {"driving", "walking", "bicycling", "transit"}:
            raise PlanningInput("Saved travel mode is invalid.")
        items(plan["carry_items"], "Carry items", 10, 60)
        reminder = plan["reminder"]
        if not isinstance(reminder, dict) or set(reminder) != {"status", "id"} or reminder["status"] not in {"none", "requested", "scheduled", "cancelled"}:
            raise PlanningInput("Saved reminder receipt is invalid.")
        text(reminder["id"], "Timer ID", 80, empty=reminder["status"] in {"none", "requested"})
    return state

def validate_preferences(prefs):
    text(prefs["origin"], "Origin", 300)
    text(prefs["weather_location"], "Weather city", 120)
    if prefs["mode"] not in {"driving", "walking", "bicycling", "transit"}:
        raise PlanningInput("Choose driving, walking, bicycling or transit.")
    integer(prefs["buffer_minutes"], "Buffer minutes", 180)
    if prefs["travel_minutes"] is not None:
        integer(prefs["travel_minutes"], "Travel minutes", 1440)
    items(prefs["carry_items"], "Carry items", 10, 60)

@handler
def ready_to_leave(context, tool, args):
    context.request_permissions("state", *(["apps.read"] if tool == "plan" else []), *(["robot.reminders"] if tool == "reminder" else []))
    state = ready_state(context)
    if tool == "preferences":
        if args.get("operation", "show") == "save":
            updates = {key: value for key, value in args.items() if key != "operation"}
            prefs = {**state["preferences"], **updates}
            validate_preferences(prefs)
            prefs["origin"] = prefs["origin"].strip()
            prefs["weather_location"] = prefs["weather_location"].strip()
            prefs["carry_items"] = items(prefs["carry_items"], "Carry items", 10, 60)
            state["preferences"] = prefs
            context.write_state(state)
        return reply("Departure preferences and the last three plans.", preferences=state["preferences"], plans=state["plans"])
    if tool == "reminder":
        return departure_reminder(context, state, args)
    event, failure = select_event(context, args)
    if failure:
        return failure
    if event["all_day"]:
        raise PlanningInput("This is an all-day event. Choose an appointment with an exact start time before calculating departure.")
    start = moment(event["start"], "Appointment start").astimezone(timezone.utc)
    if start <= now():
        raise PlanningInput("This appointment has already started. Choose a future appointment.")
    prefs = state["preferences"]
    selected_zone = zone(context.config.get("timezone", "UTC"))
    origin = text(args.get("origin", ""), "Origin", 300) or prefs["origin"]
    destination = text(args.get("destination", ""), "Destination", 300) or event["location"]
    if not destination:
        raise PlanningInput("This event has no destination. Supply its physical address; online meetings do not need a departure plan.")
    mode = args.get("mode", prefs["mode"])
    buffer = args.get("buffer_minutes", prefs["buffer_minutes"])
    carry = items(args.get("carry_items", prefs["carry_items"]), "Carry items", 10, 60)
    minutes = args.get("travel_minutes", prefs["travel_minutes"])
    duration = minutes * 60 if minutes is not None else None
    warnings, route = [], {}
    route_args = {"origin": origin, "destination": destination, "mode": mode}
    link = dependency(context, "google-maps", "directions", route_args)
    if link["ok"] and isinstance(link.get("data"), dict):
        route = {"url": str(link["data"].get("url", ""))[:1500], "link_only": True}
    else:
        warnings.append("Directions link unavailable: " + str(link.get("text", "Check Google Maps."))[:300])
    if args.get("route_estimate", False):
        if not origin:
            raise PlanningInput("Supply an origin address before requesting a route estimate.")
        estimate = dependency(context, "google-maps", "route", route_args)
        details = estimate.get("data", {})
        seconds = details.get("duration_seconds") if isinstance(details, dict) else None
        if estimate["ok"] and type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 86400:
            duration = math.ceil(seconds)
            route = bounded_json(details, 12000)
        else:
            warnings.append("Route estimate unavailable: " + str(estimate.get("text", "Supply travel minutes instead."))[:400])
    if duration is None:
        warnings.append("Supply travel_minutes, save a usual travel time, or request a configured Google route estimate. A directions link contains no travel-time estimate.")
    elif not args.get("route_estimate", False) or route.get("link_only", True):
        route["duration_source"] = "user supplied travel time"
    local_start = start.astimezone(selected_zone)
    forecast = dependency(context, "weather", "forecast", {"location": text(args.get("weather_location", ""), "Weather city", 120) or prefs["weather_location"], "day": local_start.date().isoformat()})
    weather = bounded_json(forecast.get("data", {}), 12000)
    if not isinstance(weather, dict):
        raise PlanningInput("Weather returned invalid forecast details.")
    if not forecast["ok"]:
        warnings.append("Weather unavailable: " + str(forecast.get("text", "Configure Weather or choose a city."))[:400])
    if route.get("traffic_aware") is False:
        warnings.append("The route estimate does not include live traffic. Check the route before leaving.")
    route_warnings = route.get("warnings", [])
    if not isinstance(route_warnings, list) or len(route_warnings) > 6:
        raise PlanningInput("Maps returned invalid route warnings.")
    warnings.extend(text(item, "Route warning", 400) for item in route_warnings)
    if weather.get("stale"):
        warnings.append("The forecast is cached and stale; check current conditions before leaving.")
    departure = (start - timedelta(seconds=duration, minutes=buffer)).astimezone(selected_zone) if duration is not None else None
    if departure and departure.astimezone(timezone.utc) <= now():
        warnings.append("The calculated departure time has passed. The appointment may require immediate departure or a revised plan.")
    plan = {"id": uuid.uuid4().hex[:12], "created_at": now().isoformat(), "event": event,
            "departure": departure.isoformat() if departure else None, "travel_seconds": duration, "buffer_minutes": buffer,
            "origin": origin, "destination": destination, "mode": mode, "carry_items": carry, "reminder": {"status": "none", "id": ""}}
    state["plans"] = (state["plans"] + [plan])[-3:]
    context.checkpoint()
    context.write_state(state)
    summary = f"Leave at {departure.isoformat(timespec='minutes')} for {event['title']} ({buffer}-minute buffer)." if departure else f"Selected {event['title']}; travel time is still needed to calculate departure."
    if carry:
        summary += " Carry: " + ", ".join(carry) + "."
    if forecast["ok"]:
        summary += " " + str(forecast.get("text", ""))[:600]
    if warnings:
        summary += " " + " ".join(warnings)
    return reply(summary, plan=plan, complete=departure is not None, route=route, weather=weather, warnings=warnings,
                 reminder_scheduled=False, external_content_untrusted=True)

def departure_reminder(context, state, args):
    plan = next((item for item in state["plans"] if item["id"] == args["plan_id"]), None)
    if plan is None:
        raise PlanningInput("That plan is no longer in the last three saved plans. Show preferences/plans or calculate it again.")
    reminder = plan["reminder"]
    if args.get("operation", "start") == "cancel":
        if reminder["status"] != "scheduled" or not reminder["id"]:
            raise PlanningInput("This plan has no confirmed active timer ID. Check Kitchen timers if a previous request was uncertain.")
        receipt = context.robot.timer("cancel", id=reminder["id"])
        if receipt.get("ok"):
            reminder["status"] = "cancelled"
            context.write_state(state)
        return receipt
    if reminder["status"] != "none":
        raise PlanningInput("A reminder was already requested for this plan. Check Kitchen timers; this tool will not create a duplicate automatically.")
    if plan["departure"] is None:
        raise PlanningInput("This plan has no calculated departure. Supply travel time and calculate a new plan first.")
    seconds = math.ceil((moment(plan["departure"], "Departure").astimezone(timezone.utc) - now()).total_seconds())
    if not 1 <= seconds <= 86400:
        raise PlanningInput("The departure reminder must be in the future and within 24 hours. Calculate a fresh plan closer to departure.")
    reminder["status"] = "requested"
    context.write_state(state)  # Reserve before the side effect: an uncertain retry cannot duplicate it.
    receipt = context.robot.timer("start", label=("Leave: " + " ".join(plan["event"]["title"].split()))[:80], seconds=seconds)
    identifier = receipt.get("data", {}).get("id") if isinstance(receipt.get("data"), dict) else None
    if receipt.get("ok") and isinstance(identifier, str) and 1 <= len(identifier) <= 80:
        reminder.update(status="scheduled", id=identifier)
        context.write_state(state)
    else:
        return {"ok": False, "text": "The departure timer was not confirmed with an exact ID. Check Kitchen timers before making another request.", "data": {"receipt": receipt}}
    return reply("Departure reminder scheduled for " + plan["departure"] + ".", plan_id=plan["id"], departure=plan["departure"], timer=receipt)
