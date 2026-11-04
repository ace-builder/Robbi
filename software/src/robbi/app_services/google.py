"""Bounded Google Calendar/Gmail REST adapters with lazy OAuth token refresh.

AppContext's service facade checks network consent before entering this service.
Credentials are resolved only from named environment variables; message bodies
and access tokens are never persisted by this service. No operation is retried.
"""
from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
from datetime import datetime, timedelta, timezone as utc_timezone
from email.message import EmailMessage
from email.parser import Parser
from email import policy
import hashlib
from html.parser import HTMLParser
import math
import os
import re
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robbi.errors import ValidationError
from .http import HttpClient, ProviderError, bounded_number, checkpoint


TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ITEMS = 10
MAX_BODY = 6000
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ETAG = re.compile(r'"[!#-~]{1,254}"\Z')
_TEXT_CONTROLS = {code: " " for code in range(32) if code not in (9, 10)}


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValidationError(f"{name} must contain {'0' if empty else '1'} to {maximum} characters")
    if "\x00" in value:
        raise ValidationError(f"{name} must not contain null characters")
    return value.strip()


def _short(value, maximum):
    return value[:maximum].translate(_TEXT_CONTROLS) if isinstance(value, str) else ""


def _object(value, description="response"):
    if not isinstance(value, dict):
        raise ProviderError(f"Google returned an invalid {description}")
    return value


def _items(value, field):
    result = value.get(field, [])
    if not isinstance(result, list):
        raise ProviderError(f"Google returned an invalid {field} list")
    return result


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ITEMS:
        raise ValidationError(f"Result limit must be an integer from 1 to {MAX_ITEMS}")
    return value


def _zone(value):
    name = _text(value, "Timezone", 80)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError("Use a valid IANA timezone, for example Europe/Berlin or UTC") from None


def _moment(value, name, zone):
    value = _text(value, name, 64)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", value):
        raise ValidationError(f"{name} must be an ISO date and time, for example 2026-10-23T14:00:00+02:00")
    offset = re.search(r"[+-](\d{2}):(\d{2})$", value)
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
        raise ValidationError(f"{name} has an invalid UTC offset")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{name} is not a valid date and time") from None
    if result.tzinfo is not None:
        _utc(result, name)
        return result
    # Reject daylight-saving gaps and repeated wall times unless an offset was
    # supplied. Merely assigning ZoneInfo would silently choose a wrong instant.
    candidates = []
    for fold in (0, 1):
        aware = result.replace(tzinfo=zone, fold=fold)
        try:
            returned = _utc(aware, name).astimezone(zone).replace(tzinfo=None)
        except OverflowError:
            raise ValidationError(f"{name} must remain between years 1 and 9999 when converted to UTC") from None
        if returned == result and aware.utcoffset() not in [item.utcoffset() for item in candidates]:
            candidates.append(aware)
    if len(candidates) != 1:
        raise ValidationError(f"{name} falls in a daylight-saving gap or repeated hour; specify an explicit UTC offset")
    return candidates[0]


def _utc(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must include a timezone")
    try:
        return value.astimezone(utc_timezone.utc)
    except (ValueError, OverflowError):
        raise ValidationError(f"{name} must remain between years 1 and 9999 when converted to UTC") from None


def _google_link(value):
    if not isinstance(value, str) or len(value) > 1000 or not value.isascii() or any(ord(char) <= 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname in {"calendar.google.com", "www.google.com", "mail.google.com"} and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.size, self.hidden = [], 0, 0
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "tr"}:
            self.handle_data("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"}:
            self.handle_data("\n")

    def handle_data(self, data):
        if not self.hidden:
            remaining = max(0, MAX_BODY + 1 - self.size)
            self.truncated |= len(data) > remaining
            part = data[:remaining]
            self.parts.append(part)
            self.size += len(part)


class GoogleService:
    def __init__(self, http=None, *, now=None, monotonic=None):
        self.http = http or HttpClient()
        self.now = now or (lambda: datetime.now(utc_timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self._tokens = OrderedDict()
        self._refreshes = {}
        self._lock = threading.Lock()

    @staticmethod
    def _budget(config, cancellation, deadline):
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise ValidationError("Google request deadline must be finite")
        checkpoint(cancellation, deadline)
        timeout = bounded_number(config.get("timeout_s"), 25, 3, 60)
        return min(time.monotonic() + timeout, deadline) if deadline is not None else time.monotonic() + timeout

    @staticmethod
    def _credentials(config):
        credentials = {}
        for field, default in (("client_id", "ROBBI_GOOGLE_CLIENT_ID"), ("client_secret", "ROBBI_GOOGLE_CLIENT_SECRET"), ("refresh_token", "ROBBI_GOOGLE_REFRESH_TOKEN")):
            env = config.get(field + "_env", default)
            if not isinstance(env, str) or not _ENV.fullmatch(env):
                raise ValidationError(f"{field}_env must name an environment variable, not contain its secret value")
            value = os.environ.get(env, "").strip()
            if not value:
                raise ProviderError(f"Google is not connected: set {env} in Robbi's environment and restart. See docs/GOOGLE_APPS.md and tools/google_oauth_setup.py")
            if len(value) > 8192 or any(ord(char) < 32 for char in value):
                raise ProviderError(f"Google credential in {env} is invalid; run the OAuth setup helper again")
            credentials[field] = value
        return credentials

    def _access_token(self, config, cancellation, deadline, *, with_identity=False):
        checkpoint(cancellation, deadline)
        credentials = self._credentials(config)
        identity = hashlib.sha256("\x00".join(credentials.values()).encode("utf-8")).digest()
        while True:
            checkpoint(cancellation, deadline)
            with self._lock:
                cached = self._tokens.get(identity)
                if cached and cached[1] > self.monotonic():
                    self._tokens.move_to_end(identity)
                    return (cached[0], identity.hex()) if with_identity else cached[0]
                pending = self._refreshes.get(identity)
                if pending is None:
                    pending = {"done": threading.Event(), "error": None}
                    self._refreshes[identity] = pending
                    break
            # Calendar and Gmail share credentials, but not their cancellation
            # signals or deadlines. Never hold the cache lock during network I/O.
            while not pending["done"].wait(.05):
                checkpoint(cancellation, deadline)
            checkpoint(cancellation, deadline)
            if pending["error"]:
                raise ProviderError(pending["error"])
        try:
            token, lifetime = self._refresh_token(credentials, cancellation, deadline)
            checkpoint(cancellation, deadline)
            with self._lock:
                self._tokens[identity] = (token, self.monotonic() + lifetime - min(30, lifetime / 10))
                self._tokens.move_to_end(identity)
                while len(self._tokens) > 16:
                    self._tokens.popitem(last=False)
            return (token, identity.hex()) if with_identity else token
        except ProviderError as error:
            # Share a failed refresh with current waiters, without retrying it.
            # A later user invocation can reconnect or try again independently.
            pending["error"] = str(error)
            raise
        finally:
            with self._lock:
                self._refreshes.pop(identity, None)
                pending["done"].set()

    def _refresh_token(self, credentials, cancellation, deadline):
        try:
            response = self.http.request("POST", TOKEN_URL,
                                         form_body={"grant_type": "refresh_token", **credentials},
                                         timeout_s=20, max_bytes=32768, cancellation=cancellation, deadline=deadline)
            checkpoint(cancellation, deadline)
            value = _object(response.json(), "OAuth response")
        except ProviderError:
            raise ProviderError("Google sign-in could not be refreshed. Check the OAuth client and refresh token, enable the APIs, or reconnect using tools/google_oauth_setup.py") from None
        token, lifetime = value.get("access_token"), value.get("expires_in")
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
                or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or not 0 < lifetime <= 86400
                or str(value.get("token_type", "Bearer")).lower() != "bearer"):
            raise ProviderError("Google returned an invalid access token; reconnect using the OAuth setup helper")
        return token, lifetime

    def _api(self, config, method, url, *, cancellation, deadline, body=None, max_bytes=262144, with_identity=False):
        token, identity = self._access_token(config, cancellation, deadline, with_identity=True)
        checkpoint(cancellation, deadline)
        try:
            response = self.http.request(method, url, headers={"Authorization": "Bearer " + token},
                                         json_body=body, timeout_s=20, max_bytes=max_bytes,
                                         cancellation=cancellation, deadline=deadline)
            checkpoint(cancellation, deadline)
            value = _object(response.json())
            return (value, identity) if with_identity else value
        except ProviderError as error:
            if "HTTP 401" in str(error):
                with self._lock:
                    for identity, cached in list(self._tokens.items()):
                        if cached[0] == token:
                            del self._tokens[identity]
                hint = "Google authorization expired. Reconnect the account using the OAuth setup helper."
            elif "HTTP 403" in str(error):
                hint = "Google denied access. Enable the Calendar/Gmail API and reconnect with the required scopes; check account or calendar permissions."
            else:
                hint = str(error)
            if method != "GET":
                hint += " Google has not confirmed this change; check Calendar or Gmail before trying again to avoid a duplicate."
            raise ProviderError(hint) from None
        except TimeoutError:
            if method != "GET":
                raise TimeoutError("Google request timed out. Check Calendar or Gmail before trying again; the change may already exist.") from None
            raise

    @staticmethod
    def _event(value):
        value = _object(value, "calendar event")
        if not isinstance(value.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value["id"]):
            raise ProviderError("Google returned a calendar event without a valid ID")
        result = {"id": value["id"], "title": _short(value.get("summary"), 200) or "Untitled event",
                  "location": _short(value.get("location"), 200), "url": _google_link(value.get("htmlLink"))}
        for field in ("start", "end"):
            moment = _object(value.get(field), "event time")
            text = moment.get("dateTime") or moment.get("date")
            if not isinstance(text, str) or not 1 <= len(text) <= 64:
                raise ProviderError("Google returned an invalid event time")
            result[field] = text
        result["all_day"] = "date" in value["start"]
        return result

    def calendar_events(self, config, *, query="", time_min="", time_max="", limit=10, cancellation=None, deadline=None):
        deadline = self._budget(config, cancellation, deadline)
        limit, query = _limit(limit), _text(query, "Search query", 300, empty=True)
        zone = _zone(config.get("timezone") or "UTC")
        start = _moment(time_min, "Start of range", zone) if time_min else self.now()
        try:
            end = _moment(time_max, "End of range", zone) if time_max else start + timedelta(days=30)
        except OverflowError:
            raise ValidationError("The default 30-day range exceeds year 9999; provide an earlier range") from None
        if _utc(end, "End of range") <= _utc(start, "Start of range"):
            raise ValidationError("End of range must be after start of range")
        calendar = _text(config.get("calendar_id") or "primary", "Calendar ID", 300)
        parameters = {"singleEvents": "true", "orderBy": "startTime", "showDeleted": "false", "maxResults": limit,
                      "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": zone.key,
                      "fields": "items(id,summary,start,end,location,htmlLink,status),nextPageToken,timeZone"}
        if query:
            parameters["q"] = query
        value = self._api(config, "GET", CALENDAR_URL + quote(calendar, safe="") + "/events?" + urlencode(parameters), cancellation=cancellation, deadline=deadline)
        items = _items(value, "items")
        events = [self._event(item) for item in items[:limit] if _object(item, "calendar event").get("status") != "cancelled"]
        return {"events": events, "has_more": bool(value.get("nextPageToken")) or len(items) > limit,
                "timezone": zone.key, "time_min": start.isoformat(), "time_max": end.isoformat(), "source": "Google Calendar"}

    def calendar_create(self, config, *, title, start, end, timezone="", description="", location="", cancellation=None, deadline=None):
        deadline = self._budget(config, cancellation, deadline)
        title = _text(title, "Event title", 200)
        description, location = _text(description, "Description", 2000, empty=True), _text(location, "Location", 200, empty=True)
        zone = _zone(timezone or config.get("timezone") or "UTC")
        start_at, end_at = _moment(start, "Event start", zone), _moment(end, "Event end", zone)
        if _utc(end_at, "Event end") <= _utc(start_at, "Event start"):
            raise ValidationError("Event end must be after event start")
        calendar = _text(config.get("calendar_id") or "primary", "Calendar ID", 300)
        body = {"summary": title, "description": description, "location": location,
                "start": {"dateTime": start_at.isoformat(), "timeZone": zone.key},
                "end": {"dateTime": end_at.isoformat(), "timeZone": zone.key}}
        value, identity = self._api(config, "POST", CALENDAR_URL + quote(calendar, safe="") + "/events?sendUpdates=none",
                          body=body, cancellation=cancellation, deadline=deadline, with_identity=True)
        try:
            event = self._event(value)
        except ProviderError:
            raise ProviderError("Google returned an incomplete event receipt. Check Calendar before trying again to avoid a duplicate.") from None
        result = {"event": event, "timezone": zone.key, "created": True, "source": "Google Calendar"}
        etag = value.get("etag")
        if isinstance(etag, str) and _ETAG.fullmatch(etag):
            result["undo_receipt"] = {"event_id": event["id"], "calendar_id": calendar, "etag": etag, "credential_id": identity}
        return result

    def calendar_undo_create(self, config, receipt, *, cancellation=None, deadline=None):
        """Delete only the exact version created earlier; never retry deletion.

        Calendar explicitly supports If-Match for deletion. A read-then-delete
        check alone would lose concurrent edits, so the server checks the etag.
        """
        deadline = self._budget(config, cancellation, deadline)
        if not isinstance(receipt, dict) or set(receipt) != {"event_id", "calendar_id", "etag", "credential_id"}:
            raise ValidationError("Calendar undo needs its original creation receipt")
        event_id = receipt["event_id"]
        calendar = _text(receipt["calendar_id"], "Calendar ID", 300)
        etag = receipt["etag"]
        if (not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", event_id)
                or not isinstance(etag, str) or not _ETAG.fullmatch(etag)):
            raise ValidationError("Calendar undo receipt is invalid")
        identity = hashlib.sha256("\x00".join(self._credentials(config).values()).encode("utf-8")).hexdigest()
        if receipt["credential_id"] != identity:
            return {"ok": False, "conflict": True, "text": "The Google account credentials changed. Remove the original event in Calendar."}
        token, token_identity = self._access_token(config, cancellation, deadline, with_identity=True)
        if token_identity != receipt["credential_id"]:
            return {"ok": False, "conflict": True, "text": "The Google account credentials changed. Remove the original event in Calendar."}
        url = CALENDAR_URL + quote(calendar, safe="") + "/events/" + quote(event_id, safe="") + "?sendUpdates=none"
        try:
            self.http.request("DELETE", url, headers={"Authorization": "Bearer " + token, "If-Match": etag},
                              timeout_s=20, max_bytes=32768, cancellation=cancellation, deadline=deadline)
            checkpoint(cancellation, deadline)
        except ProviderError as error:
            if "HTTP 412" in str(error):
                return {"ok": False, "conflict": True, "text": "That calendar event changed after creation. Undo did not delete the edited event."}
            if "HTTP 404" in str(error) or "HTTP 410" in str(error):
                return {"ok": True, "already_absent": True, "text": "That event no longer exists; no further change was needed."}
            raise ProviderError("Calendar undo was not confirmed. Check Google Calendar before trying another change.") from None
        except TimeoutError:
            raise TimeoutError("Calendar undo timed out and may have completed. Check Google Calendar; undo will not be retried automatically.") from None
        return {"ok": True, "text": "Deleted the unchanged event created by this action.", "event_id": event_id, "deleted": True}

    @staticmethod
    def _message(value):
        value = _object(value, "message")
        if not isinstance(value.get("id"), str) or not _MESSAGE_ID.fullmatch(value["id"]):
            raise ProviderError("Google returned a message without a valid ID")
        result = {"id": value["id"], "thread_id": _short(value.get("threadId"), 128), "snippet": _short(value.get("snippet"), 240)}
        payload = _object(value.get("payload", {}), "message payload")
        for item in _items(payload, "headers")[:200]:
            item = _object(item, "message header")
            name = _short(item.get("name"), 50).lower()
            if name in {"from", "to", "cc", "subject", "date"} and name not in result:
                result[name] = _short(item.get("value"), 240)
        return result

    def gmail_search(self, config, *, query="in:inbox", limit=5, cancellation=None, deadline=None):
        deadline = self._budget(config, cancellation, deadline)
        query, limit = _text(query, "Gmail query", 500), _limit(limit)
        url = GMAIL_URL + "/messages?" + urlencode({"q": query, "maxResults": limit, "fields": "messages(id),nextPageToken"})
        value = self._api(config, "GET", url, cancellation=cancellation, deadline=deadline, max_bytes=32768)
        items = _items(value, "messages")
        messages = []
        for item in items[:limit]:
            checkpoint(cancellation, deadline)
            identifier = _object(item, "message reference").get("id")
            if not isinstance(identifier, str) or not _MESSAGE_ID.fullmatch(identifier):
                raise ProviderError("Google returned an invalid message ID")
            parameters = {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"], "fields": "id,threadId,snippet,payload/headers"}
            message = self._api(config, "GET", GMAIL_URL + "/messages/" + identifier + "?" + urlencode(parameters, doseq=True),
                                cancellation=cancellation, deadline=deadline, max_bytes=32768)
            normalized = self._message(message)
            messages.append({key: _short(normalized.get(key), 80 if key == "date" else 180)
                             for key in ("id", "thread_id", "from", "subject", "date", "snippet")})
        return {"messages": messages, "has_more": bool(value.get("nextPageToken")) or len(items) > limit, "source": "Gmail"}

    @staticmethod
    def _body(payload):
        filenames, visited, truncated = [], 0, False

        def walk(value, depth=0, *, body_enabled=True):
            nonlocal visited, truncated
            if visited >= 100 or depth > 20:
                truncated = True
                return "", 0
            part = _object(value, "MIME part")
            visited += 1
            headers = {}
            for header in _items(part, "headers")[:200]:
                header = _object(header, "message header")
                name = _short(header.get("name"), 50).lower()
                if name in {"content-type", "content-disposition"} and name not in headers:
                    headers[name] = _short(header.get("value"), 500).replace("\r", " ").replace("\n", " ")
            parsed = Parser(policy=policy.default).parsestr("\n".join(f"{key}: {value}" for key, value in headers.items()) + "\n\n")
            filename = _short(part.get("filename"), 100)
            if filename or parsed.get_content_disposition() == "attachment":
                if filename and len(filenames) < 10:
                    filenames.append(filename)
                elif filename:
                    truncated = True
                # A forwarded email or multipart attachment can itself have
                # children; none of that attached content is the message body.
                return "", 0
            if not body_enabled:
                return "", 0
            mime = _short(part.get("mimeType"), 100).lower()
            if mime.startswith("multipart/"):
                children = _items(part, "parts")
                related_root = 0
                if mime == "multipart/related" and parsed.get_param("start"):
                    root_id = parsed.get_param("start")
                    related_root = None
                    for index, child in enumerate(children[:100]):
                        for header in _items(_object(child, "MIME part"), "headers")[:200]:
                            header = _object(header, "message header")
                            if _short(header.get("name"), 50).lower() == "content-id" and _short(header.get("value"), 500).strip() == root_id:
                                related_root = index
                                break
                        if related_root is not None:
                            break
                    if related_root is None:
                        truncated = True
                rendered = []
                for index, child in enumerate(children):
                    if visited >= 100:
                        truncated = True
                        break
                    content, preference = walk(child, depth + 1,
                        body_enabled=mime != "multipart/related" or index == related_root)
                    if content:
                        rendered.append((content, preference))
                if not rendered:
                    return "", 0
                if mime == "multipart/alternative":
                    # Alternatives describe the same content. Prefer useful
                    # plain text only within this group, not across siblings.
                    return max(reversed(rendered), key=lambda item: item[1])
                content = "\n".join(item[0] for item in rendered)
                truncated |= len(content) > MAX_BODY
                return content[:MAX_BODY], max(item[1] for item in rendered)
            if mime not in {"text/plain", "text/html"}:
                return "", 0
            body = _object(part.get("body", {}), "MIME body")
            if body.get("attachmentId"):
                truncated = True  # Remote bodies/attachments are intentionally not downloaded.
            encoded = body.get("data", "")
            if not isinstance(encoded, str):
                raise ProviderError("Google returned an invalid message body")
            if not encoded:
                return "", 0
            try:
                decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            except (ValueError, binascii.Error):
                raise ProviderError("Google returned invalid message encoding") from None
            charset = parsed.get_content_charset() or "utf-8"
            try:
                content = decoded.decode(charset, errors="replace")
            except (LookupError, UnicodeError):
                content = decoded.decode("utf-8", errors="replace")
            if mime == "text/html":
                parser = _PlainHTML()
                parser.feed(content)
                parser.close()
                content = "".join(parser.parts)
                truncated |= parser.truncated
            truncated |= len(content) > MAX_BODY
            return content[:MAX_BODY].strip(), 2 if mime == "text/plain" else 1

        content, _ = walk(payload)
        return _short(content, MAX_BODY), filenames, truncated

    def gmail_read(self, config, message_id, *, cancellation=None, deadline=None):
        deadline = self._budget(config, cancellation, deadline)
        if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
            raise ValidationError("Use the message ID returned by Gmail search")
        value = self._api(config, "GET", GMAIL_URL + "/messages/" + message_id + "?format=full&fields=id,threadId,snippet,payload",
                          cancellation=cancellation, deadline=deadline, max_bytes=524288)
        message = self._message(value)
        body, attachments, truncated = self._body(_object(value.get("payload", {}), "message payload"))
        message.update(body=body or message["snippet"], attachments=attachments, truncated=truncated,
                       body_from_snippet=not bool(body), source="Gmail")
        return message

    @staticmethod
    def _recipients(value, name, *, optional=False):
        value = _text(value, name, 1000, empty=optional)
        if not value and optional:
            return ""
        if any(char in value for char in "\r\n"):
            raise ValidationError(f"{name} must not contain line breaks")
        # Use bare, comma-separated addresses. This deliberately avoids silently
        # accepting misspelled display-name syntax or injecting extra headers.
        addresses = [address.strip() for address in value.split(",")]
        def valid(address):
            if len(address) > 254 or address.count("@") != 1:
                return False
            local, domain = address.split("@")
            labels = domain.split(".")
            return (1 <= len(local) <= 64 and all(re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+", atom) for atom in local.split("."))
                    and len(labels) >= 2 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)
                    and re.fullmatch(r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])", labels[-1]))
        if len(addresses) > 10 or any(not valid(address) for address in addresses):
            raise ValidationError(f"{name} must contain up to 10 comma-separated email addresses")
        return ", ".join(addresses)

    def gmail_draft(self, config, *, to, subject, body, cc="", cancellation=None, deadline=None):
        deadline = self._budget(config, cancellation, deadline)
        recipients, copies = self._recipients(to, "To"), self._recipients(cc, "Cc", optional=True)
        subject, body = _text(subject, "Subject", 200), _text(body, "Email body", 10000)
        if "\r" in subject or "\n" in subject:
            raise ValidationError("Subject must not contain line breaks")
        message = EmailMessage(policy=policy.SMTP)
        message["To"], message["Subject"] = recipients, subject
        if copies:
            message["Cc"] = copies
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        value = self._api(config, "POST", GMAIL_URL + "/drafts", body={"message": {"raw": raw}}, cancellation=cancellation, deadline=deadline, max_bytes=32768)
        identifier = value.get("id")
        if not isinstance(identifier, str) or not _MESSAGE_ID.fullmatch(identifier):
            raise ProviderError("Google returned an incomplete draft receipt. Check Gmail Drafts before trying again to avoid a duplicate.")
        return {"draft_id": identifier, "to": recipients, "cc": copies, "subject": subject,
                "created": True, "sent": False, "url": "https://mail.google.com/mail/#drafts", "source": "Gmail"}
