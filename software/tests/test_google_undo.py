"""Exact-resource Google compensation, mocked HTTP only; Gmail stays conservative."""
import json
from pathlib import Path
import threading

import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_services.google import GoogleService, TOKEN_URL
from robbi.app_services.http import HttpClient, HttpResponse, ProviderError
from robbi.errors import ValidationError


ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"access_token": "access", "expires_in": 3600, "token_type": "Bearer"}
EVENT = {"id": "created-event", "etag": '"version-one"', "summary": "Private appointment",
         "start": {"dateTime": "2026-10-24T10:00:00+02:00"}, "end": {"dateTime": "2026-10-24T11:00:00+02:00"}}
ARGS = {"title": "Private appointment", "start": "2026-10-24T10:00+02:00", "end": "2026-10-24T11:00+02:00"}


class FakeHTTP:
    def __init__(self, *values):
        self.values, self.calls = list(values), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, HttpResponse) else HttpResponse(200, {}, json.dumps(value).encode())


class Robot:
    def capture_token(self):
        return 0, threading.Event()


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
        monkeypatch.setenv("ROBBI_GOOGLE_" + name, "test-" + name)
    def forbidden(*args, **kwargs):
        pytest.fail("Undo tests must not contact a live Google account")
    monkeypatch.setattr(HttpClient, "request", forbidden)


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    def make(http, *, permissions=False):
        host = AppHost(ROOT, tmp_path / str(len(hosts)), Robot(), services={"google": GoogleService(http)}, permissions_enabled=permissions)
        hosts.append(host)
        assert not host.scan()["errors"]
        return host
    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def create(host):
    created = host.call("google-calendar", "create", ARGS, include_invocation=True)
    assert created["ok"], created
    return created["invocation"]["id"]


def test_created_calendar_event_undo_sends_exact_id_and_atomic_version_precondition(make_host):
    http = FakeHTTP(TOKEN, EVENT, HttpResponse(204, {}, b""))
    host = make_host(http)
    host.configure("google-calendar", {"calendar_id": "shared@example.com"})
    job_id = create(host)
    assert host.job(job_id)["undo"]["available"]
    assert "Private appointment" not in json.dumps(host.history())
    assert host.undo(job_id)["ok"]
    method, url, options = http.calls[-1]
    assert method == "DELETE" and "/shared%40example.com/events/created-event?sendUpdates=none" in url
    assert options["headers"]["If-Match"] == '"version-one"'
    assert options["headers"]["Authorization"] == "Bearer access"
    assert not host.undo(job_id)["ok"] and len(http.calls) == 3


def test_calendar_remote_edit_is_never_deleted_or_retried(make_host):
    http = FakeHTTP(TOKEN, EVENT, ProviderError("Provider HTTP 412: precondition failed"))
    host = make_host(http)
    job_id = create(host)
    failed = host.undo(job_id)
    assert not failed["ok"] and failed["conflict"]
    assert "edited event" in failed["text"]
    assert not host.undo(job_id)["ok"] and len(http.calls) == 3


@pytest.mark.parametrize("status", [404, 410])
def test_calendar_already_removed_event_has_truthful_receipt(status):
    http = FakeHTTP(TOKEN, EVENT, ProviderError(f"Provider HTTP {status}: gone"))
    service = GoogleService(http)
    receipt = service.calendar_create({}, **ARGS)["undo_receipt"]
    result = service.calendar_undo_create({}, receipt)
    assert result["ok"] and result["already_absent"]


def test_calendar_changed_credentials_prevent_deleting_from_another_account(make_host, monkeypatch):
    http = FakeHTTP(TOKEN, EVENT)
    host = make_host(http)
    job_id = create(host)
    monkeypatch.setenv("ROBBI_GOOGLE_REFRESH_TOKEN", "another-account")
    result = host.undo(job_id)
    assert not result["ok"] and result["conflict"]
    assert len(http.calls) == 2


def test_calendar_receipt_binds_credentials_used_for_creation_even_if_environment_changes(monkeypatch):
    def created():
        monkeypatch.setenv("ROBBI_GOOGLE_REFRESH_TOKEN", "changed-mid-request")
        return EVENT
    http = FakeHTTP(TOKEN, created)
    service = GoogleService(http)
    receipt = service.calendar_create({}, **ARGS)["undo_receipt"]
    result = service.calendar_undo_create({}, receipt)
    assert not result["ok"] and result["conflict"] and len(http.calls) == 2


@pytest.mark.parametrize("etag", [None, "*", 'W/"weak"', "\r\nIf-Match: *", "unquoted"])
def test_missing_or_unsafe_etag_disables_automatic_undo(make_host, etag):
    http = FakeHTTP(TOKEN, {**EVENT, "etag": etag})
    host = make_host(http)
    job_id = create(host)
    assert not host.job(job_id)["undo"]["available"]
    assert "no version receipt" in host.undo(job_id)["text"]
    assert len(http.calls) == 2


@pytest.mark.parametrize('permission', ['network', 'account.calendar'])
def test_undo_does_not_replace_calendar_permission_consent(make_host, permission):
    http = FakeHTTP(TOKEN, EVENT, HttpResponse(204, {}, b""))
    host = make_host(http, permissions=True)
    host.set_permissions("google-calendar", {"network": "granted", 'account.calendar': 'granted'})
    job_id = create(host)
    host.set_permissions("google-calendar", {permission: "denied"})
    result = host.undo(job_id)
    assert not result["ok"] and result["permission_required"] and len(http.calls) == 2
    host.set_permissions("google-calendar", {permission: "granted"})
    assert host.undo(job_id)["ok"]


def test_uncertain_calendar_deletion_is_consumed_without_retry(make_host):
    http = FakeHTTP(TOKEN, EVENT, TimeoutError("connection lost"))
    host = make_host(http)
    job_id = create(host)
    result = host.undo(job_id)
    assert not result["ok"] and "may have completed" in result["text"]
    assert not host.undo(job_id)["ok"] and len(http.calls) == 3


def test_gmail_draft_reports_unsafe_conditional_delete_without_calling_delete(make_host):
    http = FakeHTTP(TOKEN, {"id": "draft123", "message": {"id": "message123"}})
    host = make_host(http)
    draft = host.call("gmail", "draft", {"to": "maker@example.com", "subject": "Private", "body": "Private message"}, include_invocation=True)
    assert draft["ok"] and not draft["invocation"]["undo"]["available"]
    result = host.undo(draft["invocation"]["id"])
    assert not result["ok"] and "original draft version" in result["text"]
    assert not any(method == "DELETE" for method, _, _ in http.calls)
