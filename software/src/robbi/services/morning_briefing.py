"""Opt-in local morning briefings composed through the existing app permission gates.

The scheduler owns one thread and never occupies an AppHost worker. It durably
claims a scheduled date before work; an interrupted attempt is not replayed.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as wall_time, timedelta, timezone
import json
import math
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robbi.errors import BusyError, UnavailableError, ValidationError
from robbi.storage import write_json


DEFAULT_CONFIG = {"enabled": False, "time": "08:00", "timezone": "Europe/Berlin",
                  "sections": {"weather": True, "calendar": True, "habits": True, "mail": False}}
_SECTIONS = {"weather": ("Weather", "weather", "forecast"),
             "calendar": ("Calendar", "google-calendar", "events"),
             "habits": ("Habits", "habit-tracker", "manage"),
             "mail": ("Unread mail", "gmail", "search")}


class _Cancelled(Exception):
    pass


def _text(value, maximum):
    return " ".join(value.split())[:maximum] if isinstance(value, str) else ""


def _zone(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise ValidationError("Use an IANA timezone such as Europe/Berlin")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError("Unknown morning briefing timezone; use an installed IANA timezone") from None


def _config(current, patch):
    if not isinstance(patch, dict) or set(patch) - set(DEFAULT_CONFIG):
        raise ValidationError("Unknown morning briefing setting")
    result = copy.deepcopy(current)
    for name, value in patch.items():
        if name == "sections":
            if not isinstance(value, dict) or set(value) - set(_SECTIONS) or any(type(enabled) is not bool for enabled in value.values()):
                raise ValidationError("Sections must select weather, calendar, habits or mail with true/false")
            result["sections"].update(value)
        else:
            result[name] = value
    if type(result["enabled"]) is not bool:
        raise ValidationError("Morning briefing enabled must be true or false")
    if not isinstance(result["time"], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", result["time"]):
        raise ValidationError("Morning briefing time must use HH:MM, from 00:00 to 23:59")
    _zone(result["timezone"])
    if result["enabled"] and not any(result["sections"].values()):
        raise ValidationError("Choose at least one briefing section or turn the briefing off")
    return result


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Morning briefing clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _scheduled_at(day, config):
    hour, minute = map(int, config["time"].split(":"))
    zone = _zone(config["timezone"])
    local = datetime.combine(day, wall_time(hour, minute), zone).replace(fold=0)
    instant = local.astimezone(timezone.utc)
    # Spring-forward gaps do not have a scheduled instant. Fall-back times use
    # the first occurrence only; the persisted date also prevents repeats.
    if instant.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return instant


class MorningBriefingService:
    def __init__(self, path, app_host, *, deliver=lambda result, **kwargs: False,
                 can_run=lambda: True, capture_cancellation=lambda: (lambda: False),
                 now=None, monotonic=time.monotonic, poll_interval_s=15,
                 due_window_s=300, run_timeout_s=90):
        self.path, self.app_host = Path(path), app_host
        self._deliver, self._can_run, self._capture = deliver, can_run, capture_cancellation
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        for name, value, minimum, maximum in (("poll interval", poll_interval_s, .01, 60),
                                               ("due window", due_window_s, 1, 600),
                                               ("run timeout", run_timeout_s, 1, 120)):
            if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValidationError(f"Invalid morning briefing {name}")
        self._poll_interval, self._due_window, self._timeout = poll_interval_s, due_window_s, run_timeout_s
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False
        self._active = None
        self._revision = 0
        self._cancellation_epoch = 0
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        self._last_scheduled_date = ""
        self._last_result = None
        self._error = ""
        self._load()

    def _load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > 16384:
                raise ValueError("Oversized settings")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("config"), dict):
                raise ValueError("Invalid settings")
            config = _config(DEFAULT_CONFIG, value["config"])
            last = value.get("last_scheduled_date", "")
            if not isinstance(last, str) or (last and (not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", last) or date.fromisoformat(last).isoformat() != last)):
                raise ValueError("Invalid scheduled date")
            self._config, self._last_scheduled_date = config, last
        except (OSError, ValueError, TypeError, RecursionError):
            # A damaged checkpoint must never silently re-enable daily output.
            self._error = "Morning briefing settings could not be loaded. Scheduling is off; review and save the settings."

    def _save(self, config, last):
        write_json(self.path, {"version": 1, "config": config, "last_scheduled_date": last})

    def _available(self):
        try:
            return bool(self._can_run())
        except Exception:
            return False

    def configure(self, patch):
        with self._lock:
            if self._closed:
                raise UnavailableError("Morning briefing service is stopped")
            updated = _config(self._config, patch)
            self._save(updated, self._last_scheduled_date)
            self._config = updated
            self._revision += 1
            if self._active is not None:
                self._active["event"].set()
            self._last_result, self._error = None, ""
        self._wake.set()
        return self.snapshot()

    def _next_run(self, config, now, last):
        if not config["enabled"] or self._closed:
            return None
        day = now.astimezone(_zone(config["timezone"])).date()
        try:
            if last and last >= day.isoformat():
                day = date.fromisoformat(last) + timedelta(days=1)
            for offset in range(8):
                candidate = _scheduled_at(day + timedelta(days=offset), config)
                if candidate is not None and now < candidate + timedelta(seconds=self._due_window):
                    return candidate.astimezone(_zone(config["timezone"])).isoformat()
        except OverflowError:
            return None
        return None

    def snapshot(self):
        now = _aware(self._now())
        with self._lock:
            return {"config": copy.deepcopy(self._config), "running": self._active is not None,
                    "scheduler_running": bool(self._thread and self._thread.is_alive()),
                    "last_result": copy.deepcopy(self._last_result),
                    "last_scheduled_date": self._last_scheduled_date or None,
                    "next_run": self._next_run(self._config, now, self._last_scheduled_date),
                    "error": self._error, "stopped": self._closed}

    def start(self):
        with self._lock:
            if self._closed:
                raise UnavailableError("Morning briefing service is stopped")
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="morning-briefing", daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
            try:
                self.tick()
            except Exception:
                with self._lock:
                    self._error = "Morning briefing scheduling failed. Check storage and settings."
            self._wake.wait(self._poll_interval)

    def cancel(self):
        with self._lock:
            # Also invalidate a caller between its availability check and
            # registration as the active run.
            self._cancellation_epoch += 1
            if self._active is not None:
                self._active["event"].set()
        self._wake.set()

    def capture_cancellation(self):
        """Capture admission before a caller hands a manual run to its worker."""
        with self._lock:
            revision, epoch = self._revision, self._cancellation_epoch

        def cancelled():
            with self._lock:
                return self._closed or revision != self._revision or epoch != self._cancellation_epoch

        return cancelled

    def stop(self):
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active["event"].set()
            thread = self._thread
        self._wake.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def tick(self, now=None):
        return self._run(preview=False, scheduled=True, now=_aware(self._now() if now is None else now))

    def run(self, *, preview=False, cancellation=None):
        if type(preview) is not bool:
            raise ValidationError("Preview must be true or false")
        if cancellation is not None and not callable(cancellation) and not callable(getattr(cancellation, "is_set", None)):
            raise ValidationError("Morning briefing cancellation must be a callback or event")
        return self._run(preview=preview, scheduled=False, now=_aware(self._now()), cancellation=cancellation)

    def _run(self, *, preview, scheduled, now, cancellation=None):
        with self._lock:
            if self._closed:
                if scheduled:
                    return {"scheduled": False, "reason": "stopped"}
                raise UnavailableError("Morning briefing service is stopped")
            config, revision = copy.deepcopy(self._config), self._revision
            cancellation_epoch = self._cancellation_epoch
            local = now.astimezone(_zone(config["timezone"]))
            day = local.date().isoformat()
            if scheduled:
                if not config["enabled"]:
                    return {"scheduled": False, "reason": "disabled"}
                if self._last_scheduled_date >= day:
                    return {"scheduled": False, "reason": "already_attempted"}
                due = _scheduled_at(local.date(), config)
                if due is None:
                    return {"scheduled": False, "reason": "clock_gap"}
                elapsed = (now - due).total_seconds()
                if elapsed < 0:
                    return {"scheduled": False, "reason": "not_due"}
                if elapsed >= self._due_window:
                    return {"scheduled": False, "reason": "missed_window"}
            if self._active is not None:
                if scheduled:
                    return {"scheduled": False, "reason": "busy"}
                raise BusyError("A morning briefing is already running")
        if not preview and not self._available():
            return {"ok": False, "scheduled": False, "reason": "busy", "state": "busy",
                    "text": "Robbi is busy or paused. Try the briefing again when available.", "sections": [], "delivered": False}
        try:
            external = self._capture()
        except Exception:
            raise UnavailableError("Morning briefing cancellation state is unavailable") from None
        if not callable(external) and not callable(getattr(external, "is_set", None)):
            raise ValidationError("Morning briefing cancellation must be a callback or event")
        active = {"event": threading.Event(), "revision": revision,
                  "deadline": self._monotonic() + self._timeout}

        def cancelled():
            if active["event"].is_set() or self._monotonic() >= active["deadline"]:
                return True
            try:
                return (bool(external() if callable(external) else external.is_set())
                        or (cancellation is not None and bool(cancellation() if callable(cancellation) else cancellation.is_set())))
            except Exception:
                return True

        with self._lock:
            if self._closed or self._revision != revision or self._cancellation_epoch != cancellation_epoch or cancelled():
                return self._cancelled_result(day, config, preview, scheduled)
            if self._active is not None:
                if scheduled:
                    return {"scheduled": False, "reason": "busy"}
                raise BusyError("A morning briefing is already running")
            if scheduled:
                if self._last_scheduled_date >= day:
                    return {"scheduled": False, "reason": "already_attempted"}
                self._save(config, day)  # Persist admission before any data read or output.
                self._last_scheduled_date = day
            self._active = active
        try:
            sections = []
            for name, selected in config["sections"].items():
                if not selected:
                    continue
                if cancelled() or (not preview and not self._available()):
                    raise _Cancelled()
                sections.append(self._section(name, local, cancelled, active["deadline"]))
            if cancelled():
                raise _Cancelled()
            usable = any(section["ok"] for section in sections)
            text = "Good morning. " + " ".join(section["text"] for section in sections)
            if not sections:
                text = "No morning briefing sections are selected."
            result = {"ok": usable, "text": text[:4000], "sections": sections, "date": day,
                      "timezone": config["timezone"], "generated_at": now.isoformat(),
                      "preview": preview, "scheduled": scheduled, "delivered": False,
                      "state": "preview" if preview else "unavailable"}
            if usable and not preview:
                if cancelled() or not self._available():
                    raise _Cancelled()
                try:
                    receipt = self._deliver(copy.deepcopy(result), cancellation=cancelled, deadline=active["deadline"])
                    if cancelled():
                        raise _Cancelled()
                    delivered = receipt.get("delivered", receipt.get("ok", True)) if isinstance(receipt, dict) else receipt is not False
                    result["delivered"] = bool(delivered)
                    result["state"] = "completed" if delivered else "delivery_unavailable"
                    if not delivered:
                        result["ok"] = False
                except _Cancelled:
                    raise
                except Exception:
                    if cancelled():
                        raise _Cancelled()
                    result.update(ok=False, state="delivery_unavailable", delivery_error="Briefing delivery is unavailable.")
            return self._finish(active, result, cancelled)
        except _Cancelled:
            return self._finish(active, self._cancelled_result(day, config, preview, scheduled), cancelled)
        finally:
            with self._lock:
                if self._active is active:
                    self._active = None

    @staticmethod
    def _cancelled_result(day, config, preview, scheduled):
        return {"ok": False, "cancelled": True, "state": "cancelled", "text": "Morning briefing cancelled.",
                "sections": [], "date": day, "timezone": config["timezone"],
                "preview": preview, "scheduled": scheduled, "delivered": False}

    def _finish(self, active, result, cancelled):
        with self._lock:
            if self._closed or active["revision"] != self._revision or cancelled():
                result = self._cancelled_result(result["date"], result, result["preview"], result["scheduled"])
            if active["revision"] == self._revision:
                self._last_result = copy.deepcopy(result)
        return result

    def _section(self, name, local, cancelled, deadline):
        title, app, tool = _SECTIONS[name]
        base = {"id": name, "title": title, "app": app, "ok": False, "state": "unavailable",
                "permission_required": False}
        if name == "weather":
            arguments = {"day": local.date().isoformat()}
        elif name == "calendar":
            start = datetime.combine(local.date(), wall_time.min, local.tzinfo)
            end = datetime.combine(local.date() + timedelta(days=1), wall_time.min, local.tzinfo)
            arguments = {"time_min": start.isoformat(), "time_max": end.isoformat(), "limit": 5}
        elif name == "habits":
            arguments = {"action": "list"}
        else:
            arguments = {"query": "in:inbox is:unread", "limit": 3}
        try:
            response = self.app_host.call(app, tool, arguments, source="morning_briefing",
                                          cancellation=cancelled, deadline=min(deadline, self._monotonic() + 25))
            if cancelled():
                raise _Cancelled()
            if not isinstance(response, dict):
                raise ValueError("Invalid app result")
            if response.get("cancelled"):
                # A section timeout may leave room for other sections. A global
                # Stop/configuration change is caught by the shared callback.
                return {**base, "text": f"{title} could not finish in time.", "state": "timeout"}
            if response.get("permission_required"):
                permissions = response.get("permissions", [])
                denied = response.get("state") == "permission_denied"
                return {**base, "text": f"{title} permission was denied by the owner." if denied else f"{title} needs permission in the app library.",
                        "permission_required": True, "state": "permission_denied" if denied else "permission_required",
                        "permissions": [_text(item, 60) for item in permissions[:8] if isinstance(item, str)] if isinstance(permissions, list) else []}
            if not response.get("ok"):
                return {**base, "text": f"{title} is unavailable. Check its app settings."}
            data = response.get("data")
            if not isinstance(data, dict):
                raise ValueError("Invalid app data")
            text = self._section_text(name, data, response, local)
            return {**base, "ok": True, "state": "ready", "text": text[:900]}
        except _Cancelled:
            raise
        except Exception:
            if cancelled():
                raise _Cancelled()
            return {**base, "text": f"{title} is unavailable. Check that the app is enabled and configured."}

    @staticmethod
    def _section_text(name, data, response, local):
        if name == "weather":
            summary = _text(data.get("summary") or response.get("text"), 650)
            if not summary:
                raise ValueError("Missing forecast")
            sources = data.get("attribution", [])
            names = [_text(item.get("name"), 60) for item in sources[:3] if isinstance(item, dict)] if isinstance(sources, list) else []
            attribution = ", ".join(item for item in names if item)
            return (f"Weather from {attribution}: " if attribution else "Weather: ") + summary
        if name == "calendar":
            events = data.get("events")
            if not isinstance(events, list):
                raise ValueError("Missing appointments")
            entries = []
            for event in events[:5]:
                if not isinstance(event, dict):
                    raise ValueError("Invalid appointment")
                title = _text(event.get("title"), 100) or "Untitled appointment"
                if event.get("all_day"):
                    when = "all day"
                else:
                    moment = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
                    if moment.tzinfo is None:
                        raise ValueError("Missing appointment timezone")
                    moment = moment.astimezone(local.tzinfo)
                    when = moment.strftime("%H:%M") if moment.date() == local.date() else "continuing from " + moment.strftime("%Y-%m-%d %H:%M")
                entries.append(f"{when}, {title}")
            text = "Today's calendar: " + "; ".join(entries) + "." if entries else "No appointments found for today."
            return text + (" More appointments are available in Calendar." if data.get("has_more") else "")
        if name == "habits":
            habits = data.get("habits")
            if not isinstance(habits, list):
                raise ValueError("Missing habits")
            if not habits:
                return "No habits are set up yet."
            rows = [item for item in habits[:12] if isinstance(item, dict)]
            done = sum(item.get("completed_today") is True for item in rows)
            pending = [_text(item.get("name"), 60) for item in rows if item.get("completed_today") is not True]
            text = f"Habits: {done} of {len(rows)} completed."
            if pending:
                text += " Still to do: " + ", ".join(pending[:5]) + (" and more." if len(pending) > 5 else ".")
            if data.get("date") != local.date().isoformat():
                text += " The habit tracker uses its own timezone and date " + _text(data.get("date"), 10) + "."
            return text
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Missing mail summaries")
        if not messages:
            return "No unread inbox messages found."
        entries = []
        for message in messages[:3]:
            if not isinstance(message, dict):
                raise ValueError("Invalid mail summary")
            sender = _text(message.get("from"), 80) or "Unknown sender"
            subject = _text(message.get("subject"), 120) or "No subject"
            entries.append(f"{sender}: {subject}")
        text = "Unread inbox messages: " + "; ".join(entries) + "."
        return text + (" More unread messages are available in Gmail." if data.get("has_more") else "")
