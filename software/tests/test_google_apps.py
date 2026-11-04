"""Google apps exercise real manifests/SDK with injected HTTP; no live accounts."""
import base64
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import importlib.util
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.schema import bounded_json
from robbi.app_services.google import GoogleService, TOKEN_URL
from robbi.app_services.http import HttpClient, HttpResponse, ProviderError
from robbi.errors import ValidationError


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
TOKEN = {"access_token": "test-access-secret", "expires_in": 3600, "token_type": "Bearer"}


class FakeHTTP:
    def __init__(self, *values):
        self.values, self.calls = list(values), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if callable(value):
            value = value(method, url, **kwargs)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, HttpResponse) else HttpResponse(200, {}, json.dumps(value).encode())


class Robot:
    def __init__(self):
        self.cancelled = threading.Event()

    def capture_token(self):
        return 0, self.cancelled


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("ROBBI_GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ROBBI_GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("ROBBI_GOOGLE_REFRESH_TOKEN", "test-refresh-secret")
    def forbidden(*args, **kwargs):
        pytest.fail("Google tests must never make a real network request")
    monkeypatch.setattr(HttpClient, "request", forbidden)


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    def make(http, *, permissions=False):
        host = AppHost(ROOT, tmp_path / str(len(hosts)), Robot(),
                       services={"google": GoogleService(http, now=lambda: NOW)}, permissions_enabled=permissions)
        hosts.append(host)
        host.scan()
        assert "google-calendar" in host.packages.projects and "gmail" in host.packages.projects
        return host
    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def event(**changes):
    return {"id": "event123", "summary": "Dentist", "start": {"dateTime": "2026-10-24T10:00:00+02:00"},
            "end": {"dateTime": "2026-10-24T11:00:00+02:00"}, "htmlLink": "https://calendar.google.com/calendar/event?eid=test", **changes}


def message(identifier="abc123", *, body="Hello, Robbi!", mime="text/plain"):
    return {"id": identifier, "threadId": "thread123", "snippet": "A message preview", "payload": {
        "mimeType": mime, "headers": [{"name": "From", "value": "maker@example.com"}, {"name": "Subject", "value": "Meeting"}],
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")}}}


def test_calendar_real_manifest_uses_lazy_oauth_cache_and_bounded_range(make_host):
    http = FakeHTTP(TOKEN, {"items": [event()], "nextPageToken": "next"}, {})
    host = make_host(http)
    host.configure("google-calendar", {"timezone": "Europe/Berlin"})
    result = host.call("google-calendar", "events", {"query": "Dentist"})
    assert result["ok"] and result["data"]["events"][0]["title"] == "Dentist"
    assert result["data"]["has_more"] and "More results" in result["text"]
    assert http.calls[0][0:2] == ("POST", TOKEN_URL)
    assert http.calls[0][2]["form_body"]["grant_type"] == "refresh_token"
    assert "test-refresh-secret" not in http.calls[0][1]
    params = parse_qs(urlsplit(http.calls[1][1]).query)
    assert params["singleEvents"] == ["true"] and params["orderBy"] == ["startTime"]
    assert params["q"] == ["Dentist"] and params["timeZone"] == ["Europe/Berlin"]
    assert params["timeMin"] == [NOW.isoformat()]
    assert params["timeMax"] == ["2026-11-22T12:00:00+00:00"]
    assert http.calls[1][2]["headers"]["Authorization"] == "Bearer test-access-secret"
    assert http.calls[0][2]["deadline"] == http.calls[1][2]["deadline"]
    assert callable(http.calls[1][2]["cancellation"])
    assert "secret" not in json.dumps(result)
    second = host.call("google-calendar", "events", {})
    assert second["ok"] and second["data"]["events"] == []
    assert len(http.calls) == 3  # Cached OAuth token, no mailbox/event result cache.


def test_token_expiry_and_changed_credentials_refresh(monkeypatch):
    now = [100.0]
    http = FakeHTTP(TOKEN, {}, {}, TOKEN, {}, TOKEN, {})
    service = GoogleService(http, now=lambda: NOW, monotonic=lambda: now[0])
    service.calendar_events({})
    now[0] += 100
    service.calendar_events({})
    now[0] += 3500
    service.calendar_events({})
    monkeypatch.setenv("ROBBI_GOOGLE_REFRESH_TOKEN", "new-refresh-secret")
    service.calendar_events({})
    assert sum(url == TOKEN_URL for _, url, _ in http.calls) == 3


def test_missing_credentials_is_actionable_without_network(make_host, monkeypatch):
    monkeypatch.delenv("ROBBI_GOOGLE_REFRESH_TOKEN")
    http = FakeHTTP()
    result = make_host(http).call("gmail", "search", {})
    assert not result["ok"] and "ROBBI_GOOGLE_REFRESH_TOKEN" in result["text"]
    assert "restart" in result["text"] and not http.calls


def test_network_permission_precedes_token_refresh(make_host):
    http = FakeHTTP(TOKEN, {})
    host = make_host(http, permissions=True)
    first = host.call("gmail", "search", {})
    assert not first["ok"] and not http.calls
    host.set_permissions("gmail", {"network": "granted"})
    assert not host.call('gmail', 'search', {})['ok'] and not http.calls
    host.set_permissions('gmail', {'account.gmail': 'granted'})
    result = host.call("gmail", "search", {})
    assert result["ok"] and result["data"]["messages"] == []


@pytest.mark.parametrize("response", [{}, [], {"access_token": "secret\nheader", "expires_in": 30}, {"access_token": "secret", "expires_in": float("inf")}, {"access_token": "secret", "expires_in": True}])
def test_malformed_token_responses_never_expose_secret(response):
    http = FakeHTTP(response)
    with pytest.raises(ProviderError) as error:
        GoogleService(http).gmail_search({})
    assert "secret" not in str(error.value) and len(http.calls) == 1


def test_refresh_error_is_sanitized_and_not_retried():
    http = FakeHTTP(ProviderError("test-refresh-secret"))
    with pytest.raises(ProviderError, match="reconnect") as error:
        GoogleService(http).gmail_search({})
    assert "test-refresh-secret" not in str(error.value) and len(http.calls) == 1


def test_calendar_create_timezones_and_no_invitations(make_host):
    http = FakeHTTP(TOKEN, event())
    host = make_host(http)
    host.configure("google-calendar", {"timezone": "Europe/Berlin", "calendar_id": "shared@example.com"})
    result = host.call("google-calendar", "create", {"title": "Dentist", "start": "2026-10-24T10:00", "end": "2026-10-24T11:00"})
    assert result["ok"] and result["data"]["created"]
    assert "/shared%40example.com/events?sendUpdates=none" in http.calls[1][1]
    body = http.calls[1][2]["json_body"]
    assert body["start"] == {"dateTime": "2026-10-24T10:00:00+02:00", "timeZone": "Europe/Berlin"}
    assert "attendees" not in body


@pytest.mark.parametrize("start,end,zone", [
    ("2026-03-29T02:30", "2026-03-29T04:00", "Europe/Berlin"),
    ("2026-10-25T02:30", "2026-10-25T04:00", "Europe/Berlin"),
    ("2026-10-23T12:00Z", "2026-10-23T11:00Z", "UTC"),
    ("2026-10-23T12:00Z", "2026-10-23T13:00Z", "Mars/Olympus"),
    ("2026-10-23", "2026-10-24", "UTC"),
])
def test_invalid_calendar_dates_rejected_before_auth(start, end, zone):
    http = FakeHTTP()
    with pytest.raises(ValidationError):
        GoogleService(http).calendar_create({}, title="Meeting", start=start, end=end, timezone=zone)
    assert not http.calls


def test_explicit_offset_resolves_repeated_hour_and_utc_order():
    http = FakeHTTP(TOKEN, event())
    GoogleService(http).calendar_create({}, title="Clock change", start="2026-10-25T02:30+02:00", end="2026-10-25T02:15+01:00", timezone="Europe/Berlin")
    assert len(http.calls) == 2


def test_calendar_all_day_and_huge_provider_response_are_bounded():
    item = event(start={"date": "2026-10-24"}, end={"date": "2026-10-25"}, summary="x" * 3000)
    http = FakeHTTP(TOKEN, {"items": [item] * 50})
    result = GoogleService(http).calendar_events({}, limit=2)
    assert len(result["events"]) == 2 and result["has_more"]
    assert result["events"][0]["all_day"]
    assert len(result["events"][0]["title"]) == 200


def test_maximum_calendar_output_fits_host_budget(make_host):
    item = event(id="e" * 1024, summary="😀" * 500, location="😀" * 500,
                 htmlLink="https://calendar.google.com/calendar/event?eid=" + "x" * 950)
    result = make_host(FakeHTTP(TOKEN, {"items": [item] * 10})).call("google-calendar", "events", {})
    assert result["ok"] and len(result["data"]["events"]) == 10
    bounded_json(result)


@pytest.mark.parametrize("response", [[], {"items": None}, {"items": [None]}, {"items": [{"id": "id", "start": "wrong"}]}])
def test_malformed_calendar_response(response):
    with pytest.raises(ProviderError):
        GoogleService(FakeHTTP(TOKEN, response)).calendar_events({})


def test_gmail_search_gets_bounded_metadata_with_exact_query(make_host):
    http = FakeHTTP(TOKEN, {"messages": [{"id": "abc123"}, {"id": "ignored"}], "nextPageToken": "next"}, message())
    result = make_host(http).call("gmail", "search", {"query": "is:unread from:maker@example.com", "limit": 1})
    assert result["ok"] and result["data"]["has_more"]
    assert len(result["data"]["messages"]) == 1
    assert result["data"]["messages"][0]["subject"] == "Meeting"
    params = parse_qs(urlsplit(http.calls[1][1]).query)
    assert params["q"] == ["is:unread from:maker@example.com"] and params["maxResults"] == ["1"]
    params = parse_qs(urlsplit(http.calls[2][1]).query)
    assert params["format"] == ["metadata"]
    assert set(params["metadataHeaders"]) == {"From", "Subject", "Date"}
    assert all(options["deadline"] == http.calls[0][2]["deadline"] for _, _, options in http.calls)


@pytest.mark.parametrize("response", [[], {"messages": {}}, {"messages": [None]}, {"messages": [{"id": "../../bad"}]}])
def test_malformed_gmail_search_references(response):
    with pytest.raises(ProviderError):
        GoogleService(FakeHTTP(TOKEN, response)).gmail_search({})


def test_gmail_read_mime_plain_preferred_and_attachment_names(make_host):
    value = message()
    value["payload"].update(mimeType="multipart/mixed", body={}, parts=[
        {"mimeType": "multipart/alternative", "parts": [
            message(body="<p>HTML version</p>", mime="text/html")["payload"],
            message(body="Plain version")["payload"],
        ]},
        {"mimeType": "application/pdf", "filename": "agenda.pdf", "body": {"attachmentId": "remote"}},
    ])
    http = FakeHTTP(TOKEN, value)
    result = make_host(http).call("gmail", "read", {"message_id": "abc123"})
    assert result["ok"] and result["data"]["body"] == "Plain version"
    assert result["data"]["attachments"] == ["agenda.pdf"] and len(http.calls) == 2
    assert "modify" not in http.calls[1][1]


def test_html_is_text_and_never_fetches_remote_assets():
    http = FakeHTTP(TOKEN, message(body='<head><style>secret-style</style></head><p>Hello &amp; welcome</p><script>secret-script</script><img src="https://tracker.test/x">', mime="text/html"))
    result = GoogleService(http).gmail_read({}, "abc123")
    assert result["body"] == "Hello & welcome"
    assert len(http.calls) == 2


def test_non_utf8_message_and_invalid_base64():
    value = message()
    value["payload"]["headers"].append({"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"})
    value["payload"]["body"]["data"] = base64.urlsafe_b64encode(b"caf\xe9").decode()
    assert GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")["body"] == "café"
    value["payload"]["body"]["data"] = "!not-base64!"
    with pytest.raises(ProviderError, match="encoding"):
        GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")


def test_multibyte_read_and_search_fit_host_result_limit(make_host):
    value = message(body="😀" * 20000)
    value["payload"]["headers"] = [{"name": name, "value": "😀" * 500} for name in ("From", "To", "Cc", "Subject", "Date")]
    http = FakeHTTP(TOKEN, value, {"messages": [{"id": "abc123"}] * 10}, *([value] * 10))
    host = make_host(http)
    result = host.call("gmail", "read", {"message_id": "abc123"})
    assert result["ok"] and result["data"]["truncated"] and len(result["data"]["body"]) == 6000
    bounded_json(result)
    result = host.call("gmail", "search", {"limit": 10})
    assert result["ok"] and len(result["data"]["messages"]) == 10
    bounded_json(result)


def test_gmail_draft_is_mime_and_never_sent(make_host):
    http = FakeHTTP(TOKEN, {"id": "draft123", "message": {"id": "abc123"}})
    result = make_host(http).call("gmail", "draft", {"to": "maker@example.com", "cc": "sam@example.com", "subject": "Grüße", "body": "Hello\nWorld ä"})
    assert result["ok"] and result["data"]["created"] and result["data"]["sent"] is False
    method, url, options = http.calls[1]
    assert method == "POST" and url.endswith("/drafts") and "/send" not in url
    decoded = base64.urlsafe_b64decode(options["json_body"]["message"]["raw"])
    parsed = BytesParser(policy=policy.default).parsebytes(decoded)
    assert str(parsed["To"]) == "maker@example.com" and str(parsed["Cc"]) == "sam@example.com"
    assert str(parsed["Subject"]) == "Grüße" and "World ä" in parsed.get_content()
    assert "Review and send" in result["text"]


@pytest.mark.parametrize("changes", [{"to": "maker@example.com\r\nBcc:evil@example.com"}, {"to": "not-an-email"}, {"subject": "Hi\nBcc:evil@example.com"}, {"body": " "}])
def test_bad_draft_input_never_refreshes_or_writes(changes):
    http = FakeHTTP()
    values = {"to": "maker@example.com", "subject": "Hi", "body": "Hello", **changes}
    with pytest.raises(ValidationError):
        GoogleService(http).gmail_draft({}, **values)
    assert not http.calls


def test_write_failure_reports_uncertainty_without_retry():
    http = FakeHTTP(TOKEN, ProviderError("Provider HTTP 503: request failed"))
    with pytest.raises(ProviderError, match="avoid a duplicate"):
        GoogleService(http).gmail_draft({}, to="maker@example.com", subject="Hi", body="Hello")
    assert len(http.calls) == 2


def test_write_timeout_reports_possible_remote_change():
    http = FakeHTTP(TOKEN, TimeoutError("timed out"))
    with pytest.raises(TimeoutError, match="change may already exist"):
        GoogleService(http).gmail_draft({}, to="maker@example.com", subject="Hi", body="Hello")
    assert len(http.calls) == 2


def test_cancelled_before_request_or_after_refresh_never_calls_api():
    http = FakeHTTP()
    with pytest.raises(CancelledError):
        GoogleService(http).gmail_search({}, cancellation=lambda: True)
    assert not http.calls
    cancelled = [False]
    def finish(*args, **kwargs):
        cancelled[0] = True
        return TOKEN
    http = FakeHTTP(finish)
    with pytest.raises(CancelledError):
        GoogleService(http).gmail_search({}, cancellation=lambda: cancelled[0])
    assert len(http.calls) == 1


def test_cancelled_between_gmail_reads_discards_results():
    cancelled = [False]
    def finish(*args, **kwargs):
        cancelled[0] = True
        return message()
    http = FakeHTTP(TOKEN, {"messages": [{"id": "abc123"}, {"id": "abc456"}]}, finish)
    with pytest.raises(CancelledError):
        GoogleService(http).gmail_search({}, cancellation=lambda: cancelled[0])
    assert len(http.calls) == 3


def test_expired_deadline_prevents_network():
    http = FakeHTTP()
    with pytest.raises(TimeoutError):
        GoogleService(http).gmail_search({}, deadline=time.monotonic() - 1)
    assert not http.calls


def test_host_cancellation_discards_late_google_result(make_host):
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"items": [event()]}
    http = FakeHTTP(TOKEN, slow)
    host = make_host(http)
    results = []
    worker = threading.Thread(target=lambda: results.append(host.call("google-calendar", "events", {})))
    try:
        worker.start()
        assert entered.wait(1)
        host.cancel_all()
        worker.join(1)
        assert results and results[0]["cancelled"]
    finally:
        release.set()
        worker.join(3)
    assert len(http.calls) == 2


def setup_helper():
    spec = importlib.util.spec_from_file_location("google_oauth_setup_test", ROOT / "tools/accounts/google_oauth_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_pkce_state_scopes_and_private_no_overwrite(tmp_path):
    helper = setup_helper()
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636 test vector.
    scopes = helper.selected_scopes("write", "draft")
    params = helper.authorization_parameters("id", "http://127.0.0.1:1234/oauth2callback", scopes, state="state", verifier=verifier)
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert params["access_type"] == "offline" and params["prompt"] == "consent"
    assert len(scopes) == 3 and any(scope.endswith("gmail.compose") for scope in scopes)
    assert helper.parse_callback("/oauth2callback?" + urlencode({"state": "state", "code": "private-code"}), "state") == "private-code"
    assert helper.parse_callback("/oauth2callback?state=state&error=access_denied", "state") is None
    for path in ("/oauth2callback?state=wrong&code=x", "/wrong?state=state&code=x", "/oauth2callback?state=state&state=state&code=x", "/oauth2callback?state=%C3%A4&code=x", "/oauth2callback?state=state&code=x&code=y"):
        with pytest.raises(ValueError):
            helper.parse_callback(path, "state")
    target = tmp_path / "private.env"
    client = {"client_id": "test-client", "client_secret": "test-secret"}
    helper.write_credentials(target, client, "test-refresh")
    assert target.read_text().count("ROBBI_GOOGLE_") == 3
    with pytest.raises(FileExistsError):
        helper.write_credentials(target, client, "replacement")
    assert "replacement" not in target.read_text()


def test_setup_rejects_web_client_and_invalid_credentials(tmp_path):
    helper = setup_helper()
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(ValueError, match="Desktop"):
        helper.load_client(path)
    with pytest.raises(ValueError):
        helper.write_credentials(tmp_path / "bad.env", {"client_id": "id", "client_secret": "secret\nINJECT=true"}, "token")
    assert not (tmp_path / "bad.env").exists()


def test_mixed_mime_keeps_independent_html_sections_and_skips_attached_subtrees():
    value = message()
    value["payload"].update(mimeType="multipart/mixed", body={}, parts=[
        {"mimeType": "multipart/alternative", "parts": [
            message(body="Intro plain")["payload"],
            message(body="<p>Intro HTML</p>", mime="text/html")["payload"],
        ]},
        message(body="<p>Separate HTML section</p>", mime="text/html")["payload"],
        {"mimeType": "message/rfc822", "filename": "forwarded.eml", "parts": [
            message(body="Text inside an attached email")["payload"],
        ]},
    ])
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["body"] == "Intro plain\nSeparate HTML section"
    assert result["attachments"] == ["forwarded.eml"]


def test_empty_plain_alternative_falls_back_to_html():
    value = message()
    value["payload"].update(mimeType="multipart/alternative", body={}, parts=[
        message(body="  \n  ")["payload"],
        message(body="<p>The actual message</p>", mime="text/html")["payload"],
    ])
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["body"] == "The actual message" and not result["body_from_snippet"]


def test_html_prefix_truncation_remains_marked_after_whitespace_trim():
    value = message(body="<p>" + " " * 7000 + "important final text</p>", mime="text/html")
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["truncated"]


@pytest.mark.parametrize("address", [".maker@example.com", "maker.@example.com", "maker..sam@example.com",
    "maker@sub..example.com", "maker@sub-.example.com", "maker@-sub.example.com",
    "a" * 65 + "@example.com", "maker@" + "a" * 64 + ".com"])
def test_draft_rejects_malformed_dot_atoms_and_domain_labels_before_auth(address):
    http = FakeHTTP()
    with pytest.raises(ValidationError):
        GoogleService(http).gmail_draft({}, to=address, subject="Hello", body="Body")
    assert not http.calls


@pytest.mark.parametrize("start,end,zone", [
    ("0001-01-01T00:00+01:00", "0001-01-02T00:00+01:00", "UTC"),
    ("0001-01-01T00:00", "0001-01-02T00:00", "Europe/Berlin"),
    ("2026-10-23T12:00+00:99", "2026-10-23T14:00+00:99", "UTC"),
])
def test_calendar_extreme_dates_and_invalid_offsets_are_actionable(start, end, zone):
    http = FakeHTTP()
    with pytest.raises(ValidationError):
        GoogleService(http).calendar_create({}, title="Meeting", start=start, end=end, timezone=zone)
    assert not http.calls


def test_calendar_default_range_overflow_is_actionable():
    http = FakeHTTP()
    with pytest.raises(ValidationError):
        GoogleService(http).calendar_events({}, time_min="9999-12-20T12:00Z")
    assert not http.calls


@pytest.mark.parametrize("query", ["state=state&code=%00", "state=state&code=line%0Abreak",
    "state=state&code=%FF", "state=state&code=x&error=access_denied", "state=state&error=",
    "state=state&error=access_denied&error=other"])
def test_setup_rejects_invalid_callback_codes_and_conflicting_outcomes(query):
    with pytest.raises(ValueError):
        setup_helper().parse_callback("/oauth2callback?" + query, "state")


def test_setup_rejects_deeply_nested_json_without_traceback(tmp_path):
    path = tmp_path / "client.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError):
        setup_helper().load_client(path)


def test_concurrent_calls_share_one_refresh_and_keep_independent_cancellation():
    entered, release, waiter_started = threading.Event(), threading.Event(), threading.Event()
    cancelled = threading.Event()
    lock = threading.Lock()
    requests, results, errors = [], [], []
    checks = 0

    class ConcurrentHTTP:
        def request(self, method, url, **kwargs):
            with lock:
                requests.append(url)
            if url == TOKEN_URL:
                entered.set()
                assert release.wait(3)
                value = TOKEN
            else:
                value = {}
            return HttpResponse(200, {}, json.dumps(value).encode())

    service = GoogleService(ConcurrentHTTP())
    def run(*, cancellation=None):
        try:
            results.append(service.gmail_search({}, cancellation=cancellation))
        except Exception as error:
            errors.append(error)
    def waiter_cancelled():
        nonlocal checks
        checks += 1
        if checks >= 4:
            waiter_started.set()
        return cancelled.is_set()
    owner = threading.Thread(target=run)
    waiter = threading.Thread(target=lambda: run(cancellation=waiter_cancelled))
    try:
        owner.start()
        assert entered.wait(1)
        waiter.start()
        assert waiter_started.wait(1)
        cancelled.set()
        waiter.join(1)
        assert not waiter.is_alive(), "A cancelled waiter must not wait for another app's refresh"
        assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    finally:
        release.set()
        owner.join(3)
        waiter.join(3)
    assert len(results) == 1
    assert requests.count(TOKEN_URL) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_concurrent_calendar_and_gmail_share_one_refresh_outcome(failed):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    requests, results, errors = [], [], []
    checks = 0

    class ConcurrentHTTP:
        def request(self, method, url, **kwargs):
            with lock:
                requests.append(url)
            if url == TOKEN_URL:
                entered.set()
                assert release.wait(3)
                if failed:
                    raise ProviderError("Provider HTTP 400: token rejected")
            return HttpResponse(200, {}, json.dumps(TOKEN if url == TOKEN_URL else {}).encode())
    service = GoogleService(ConcurrentHTTP())
    def checkpoint_waiter():
        # Called again while waiting; this proves overlap without sleeping.
        nonlocal checks
        checks += 1
        if checks >= 4:
            waiting.set()
        return False
    def run(method, **kwargs):
        try:
            results.append(method({}, **kwargs))
        except ProviderError as error:
            errors.append(error)
    first = threading.Thread(target=lambda: run(service.calendar_events))
    second = threading.Thread(target=lambda: run(service.gmail_search, cancellation=checkpoint_waiter))
    try:
        first.start()
        assert entered.wait(1)
        second.start()
        assert waiting.wait(1)
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert len(results) == (0 if failed else 2)
    assert len(errors) == (2 if failed else 0)
    assert requests.count(TOKEN_URL) == 1


def test_401_does_not_discard_other_accounts_cached_token(monkeypatch):
    monkeypatch.setenv("OTHER_GOOGLE_REFRESH_TOKEN", "other-refresh")
    second = {"refresh_token_env": "OTHER_GOOGLE_REFRESH_TOKEN"}
    token_b = {**TOKEN, "access_token": "account-b-access"}
    http = FakeHTTP(TOKEN, {}, token_b, {}, ProviderError("Provider HTTP 401: unauthorized"), {})
    service = GoogleService(http)
    service.gmail_search({})
    service.gmail_search(second)
    with pytest.raises(ProviderError, match="authorization expired"):
        service.gmail_search({})
    service.gmail_search(second)
    assert sum(url == TOKEN_URL for _, url, _ in http.calls) == 2


@pytest.mark.parametrize("explicit_root", [False, True])
def test_related_mime_renders_only_its_root_not_inline_text_resources(explicit_root):
    html = message(body="<p>The actual body</p>", mime="text/html")["payload"]
    html["headers"].append({"name": "Content-ID", "value": "<root@example>"})
    resource = message(body="Auxiliary resource")["payload"]
    parts = [resource, html] if explicit_root else [html, resource]
    headers = [{"name": "Content-Type", "value": 'multipart/related; type="text/html"' + ('; start="<root@example>"' if explicit_root else "")}]
    value = message()
    value["payload"].update(mimeType="multipart/related", body={}, parts=parts, headers=headers)
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["body"] == "The actual body"


def test_attachment_disposition_without_filename_is_not_message_text():
    attached = message(body="Attached secret text")["payload"]
    attached["headers"].append({"name": "Content-Disposition", "value": "attachment"})
    value = message()
    value["payload"].update(mimeType="multipart/mixed", body={}, parts=[message(body="Body")["payload"], attached])
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["body"] == "Body"


def test_refresh_waiter_deadline_does_not_cancel_owner():
    entered, release = threading.Event(), threading.Event()
    results = []
    def refresh(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return TOKEN
    http = FakeHTTP(refresh, {})
    service = GoogleService(http)
    owner = threading.Thread(target=lambda: results.append(service.gmail_search({})))
    try:
        owner.start()
        assert entered.wait(1)
        with pytest.raises(TimeoutError):
            service.calendar_events({}, deadline=time.monotonic() + .1)
    finally:
        release.set()
        owner.join(3)
    assert len(results) == 1 and len(http.calls) == 2


@pytest.mark.parametrize("deadline", [False, True, "soon", float("inf"), float("nan")])
def test_invalid_deadline_types_are_rejected_before_auth(deadline):
    http = FakeHTTP()
    with pytest.raises(ValidationError):
        GoogleService(http).gmail_search({}, deadline=deadline)
    assert not http.calls


def test_valid_dotted_plus_and_punycode_draft_addresses_remain_supported():
    http = FakeHTTP(TOKEN, {"id": "draft123"})
    recipients = "maker.smith+robot@example.com, user@xn--bcher-kva.example"
    result = GoogleService(http).gmail_draft({}, to=recipients, subject="Hello", body="Body")
    assert result["to"] == recipients


def test_mime_part_and_nesting_limits_keep_previews_bounded():
    value = message()
    value["payload"].update(mimeType="multipart/mixed", body={},
                            parts=[message(body="x")["payload"] for _ in range(110)])
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["truncated"] and len(result["body"]) < 6000
    nested = message(body="Too deeply nested")["payload"]
    for _ in range(25):
        nested = {"mimeType": "multipart/mixed", "parts": [nested]}
    value["payload"] = nested
    result = GoogleService(FakeHTTP(TOKEN, value)).gmail_read({}, "abc123")
    assert result["truncated"] and result["body_from_snippet"]
