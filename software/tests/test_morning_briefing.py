"""Opt-in schedule, persistence, cancellation and real app permission boundaries."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time

import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_services.http import HttpClient
from robbi.errors import BusyError, UnavailableError, ValidationError
from robbi.services.morning_briefing import MorningBriefingService

NOW = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)  # 08:00 Berlin.
ROOT = Path(__file__).resolve().parents[1]


class FakeHost:
    def __init__(self, respond=None):
        self.calls = []
        self.respond = respond

    def call(self, app, tool, arguments, **kwargs):
        self.calls.append((app, tool, arguments, kwargs))
        if self.respond:
            return self.respond(app, tool, arguments, **kwargs)
        data = {
            "weather": {"summary": "Berlin: 10–15 °C.", "attribution": [{"name": "Open-Meteo"}]},
            "google-calendar": {"events": [{"title": "Dentist", "start": "2026-10-23T09:00:00Z", "all_day": False}], "has_more": False},
            "habit-tracker": {"date": "2026-10-23", "habits": [{"name": "Reading", "completed_today": False}, {"name": "Walk", "completed_today": True}]},
            "gmail": {"messages": [{"from": "maker", "subject": "Meeting", "snippet": "PRIVATE BODY", "body": "PRIVATE BODY"}], "has_more": True},
        }[app]
        return {"ok": True, "data": data}


@pytest.fixture
def make_service(tmp_path):
    created = []

    def make(host=None, **kwargs):
        service = MorningBriefingService(tmp_path / "briefing.json", host or FakeHost(), now=kwargs.pop("now", lambda: NOW), **kwargs)
        created.append(service)
        return service

    yield make
    for service in created:
        service.stop()


def test_default_schedule_and_mail_are_off_and_import_does_not_write(make_service):
    service = make_service()
    state = service.snapshot()
    assert state["config"]["enabled"] is False and state["config"]["sections"]["mail"] is False
    assert state["next_run"] is None and not service.path.exists()
    assert service.tick() == {"scheduled": False, "reason": "disabled"}
    assert service.app_host.calls == []


def test_settings_persist_and_snapshot_cannot_mutate_them(make_service):
    service = make_service()
    result = service.configure({"enabled": True, "time": "09:30", "timezone": "Asia/Kathmandu", "sections": {"mail": True}})
    result["config"]["sections"]["mail"] = False
    restored = make_service()
    assert restored.snapshot()["config"] == service.snapshot()["config"]
    assert restored.snapshot()["config"]["sections"]["mail"] is True
    assert restored.snapshot()["next_run"] == "2026-10-24T09:30:00+05:45"


@pytest.mark.parametrize("patch", [{"enabled": 1}, {"time": "8:00"}, {"time": "24:00"}, {"time": "08:60"},
                                    {"timezone": "No/Such_Zone"}, {"sections": {"mail": "yes"}}, {"sections": {"unknown": True}},
                                    {"unknown": True}, {"enabled": True, "sections": {"weather": False, "calendar": False, "habits": False}}])
def test_invalid_settings_leave_saved_schedule_unchanged(make_service, patch):
    service = make_service()
    service.configure({"time": "07:15"})
    before = service.path.read_bytes()
    with pytest.raises(ValidationError):
        service.configure(patch)
    assert service.path.read_bytes() == before


def test_corrupt_saved_configuration_fails_closed(make_service):
    service = make_service()
    service.path.write_text('{"config":{"enabled":true},"version":1,"last_scheduled_date":"wrong"}')
    restored = make_service()
    assert restored.snapshot()["config"]["enabled"] is False
    assert restored.snapshot()["error"]
    assert restored.tick()["reason"] == "disabled"


def test_disabled_schedule_allows_preview_without_output_or_consuming_schedule(make_service):
    deliveries = []
    service = make_service(deliver=lambda result, **kwargs: deliveries.append(result), can_run=lambda: False)
    result = service.run(preview=True)
    assert result["ok"] and result["preview"] and not result["delivered"]
    assert "Open-Meteo" in result["text"] and "11:00, Dentist" in result["text"]
    assert "Reading" in result["text"] and "1 of 2" in result["text"]
    assert [call[0] for call in service.app_host.calls] == ["weather", "google-calendar", "habit-tracker"]
    assert service.app_host.calls[0][2] == {"day": "2026-10-23"}
    assert all(call[3]["source"] == "morning_briefing" and callable(call[3]["cancellation"]) for call in service.app_host.calls)
    assert not deliveries and service.snapshot()["last_scheduled_date"] is None
    assert not service.path.exists()


def test_manual_run_can_deliver_while_schedule_disabled_and_keeps_schedule_separate(make_service):
    deliveries = []
    service = make_service(deliver=lambda result, **kwargs: deliveries.append((result, kwargs)))
    result = service.run()
    assert result["ok"] and result["delivered"] and result["state"] == "completed"
    assert len(deliveries) == 1 and callable(deliveries[0][1]["cancellation"])
    assert service.snapshot()["last_scheduled_date"] is None


def test_mail_requires_opt_in_and_only_includes_sender_subject(make_service):
    service = make_service()
    service.configure({"sections": {"mail": True}})
    result = service.run(preview=True)
    mail = next(section for section in result["sections"] if section["id"] == "mail")
    assert "maker: Meeting" in mail["text"] and "More unread" in mail["text"]
    assert "PRIVATE BODY" not in json.dumps(result)
    assert service.app_host.calls[-1][2] == {"query": "in:inbox is:unread", "limit": 3}
    assert "maker" not in service.path.read_text()


@pytest.mark.parametrize("day,hours", [(datetime(2026, 3, 29, 6, tzinfo=timezone.utc), 23),
                                       (datetime(2026, 10, 25, 7, tzinfo=timezone.utc), 25)])
def test_calendar_boundaries_follow_complete_local_dst_day(make_service, day, hours):
    service = make_service(now=lambda: day)
    service.run(preview=True)
    arguments = next(call[2] for call in service.app_host.calls if call[0] == "google-calendar")
    start, end = (datetime.fromisoformat(arguments[name]).astimezone(timezone.utc) for name in ("time_min", "time_max"))
    assert (end - start).total_seconds() == hours * 3600
    assert arguments["limit"] == 5


def test_scheduled_run_claims_date_and_never_repeats_across_restart(make_service):
    deliveries = []
    service = make_service(deliver=lambda result, **kwargs: deliveries.append(result))
    service.configure({"enabled": True})
    first = service.tick()
    assert first["scheduled"] and first["delivered"]
    assert service.tick()["reason"] == "already_attempted"
    restored = make_service()
    assert restored.tick()["reason"] == "already_attempted" and restored.app_host.calls == []
    assert len(deliveries) == 1
    saved = json.loads(service.path.read_text())
    assert saved["last_scheduled_date"] == "2026-10-23" and "last_result" not in saved
    assert "Dentist" not in service.path.read_text()


def test_scheduler_has_small_due_window_and_no_hours_late_catchup(make_service):
    service = make_service(deliver=lambda *a, **kw: True)
    service.configure({"enabled": True})
    assert service.tick(NOW - timedelta(seconds=1))["reason"] == "not_due"
    assert service.tick(NOW + timedelta(minutes=5))["reason"] == "missed_window"
    assert service.tick(NOW + timedelta(hours=4))["reason"] == "missed_window"
    assert not service.app_host.calls
    assert service.tick(NOW + timedelta(days=1, seconds=30))["delivered"]


def test_busy_robot_can_run_later_only_within_due_window(make_service):
    available = [False]
    service = make_service(can_run=lambda: available[0], deliver=lambda *a, **kw: True)
    service.configure({"enabled": True})
    assert service.tick()["reason"] == "busy" and service.snapshot()["last_scheduled_date"] is None
    available[0] = True
    assert service.tick(NOW + timedelta(minutes=2))["delivered"]


def test_nonexistent_dst_schedule_is_skipped_and_repeated_hour_runs_once(make_service):
    service = make_service(deliver=lambda *a, **kw: True)
    service.configure({"enabled": True, "time": "02:30"})
    assert service.tick(datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc))["reason"] == "clock_gap"
    first_fold = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert service.tick(first_fold)["delivered"]
    assert service.tick(first_fold + timedelta(hours=1))["reason"] == "already_attempted"


def test_restart_in_second_dst_fold_does_not_catch_up_first_fold(make_service):
    service = make_service()
    service.configure({"enabled": True, "time": "02:30"})
    assert service.tick(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))["reason"] == "missed_window"
    assert not service.app_host.calls


def test_clock_rollback_and_schedule_edit_do_not_replay_claimed_day(make_service):
    service = make_service(deliver=lambda *a, **kw: True)
    service.configure({"enabled": True})
    service.tick()
    service.configure({"time": "09:00"})
    assert service.tick(NOW + timedelta(hours=1))["reason"] == "already_attempted"
    assert service.tick(NOW - timedelta(days=1))["reason"] == "already_attempted"


def test_section_failure_is_isolated_and_provider_errors_are_not_echoed(make_service):
    original = FakeHost()

    def reply(app, tool, arguments, **kwargs):
        if app == "weather":
            raise RuntimeError("PRIVATE-SECRET")
        return original.call(app, tool, arguments, **kwargs)

    service = make_service(FakeHost(reply), deliver=lambda *a, **kw: True)
    result = service.run()
    assert result["ok"] and result["delivered"]
    assert not result["sections"][0]["ok"] and result["sections"][1]["ok"]
    assert "PRIVATE-SECRET" not in json.dumps(result)


def test_all_section_failures_are_reported_without_speaking(make_service):
    deliveries = []
    service = make_service(FakeHost(lambda *a, **kw: {"ok": False, "text": "not configured"}),
                           deliver=lambda *a, **kw: deliveries.append(a))
    result = service.run()
    assert not result["ok"] and result["state"] == "unavailable" and not deliveries


@pytest.mark.parametrize("change", [{"enabled": False}, {"time": "09:45"}, {"sections": {"mail": True}}])
def test_configuration_changes_cancel_active_work_without_late_output(make_service, change):
    entered = threading.Event()
    deliveries, results = [], []

    def reply(app, tool, args, **kwargs):
        entered.set()
        until = time.monotonic() + 3
        while not kwargs["cancellation"]() and time.monotonic() < until:
            threading.Event().wait(.01)
        return {"ok": True, "data": {"summary": "Must not be spoken"}}

    service = make_service(FakeHost(reply), deliver=lambda *a, **kw: deliveries.append(a))
    service.configure({"enabled": True})
    worker = threading.Thread(target=lambda: results.append(service.tick()))
    worker.start()
    try:
        assert entered.wait(1)
        service.configure(change)
        worker.join(1)
        assert not worker.is_alive() and results[0]["cancelled"]
        assert not deliveries and len(service.app_host.calls) == 1
        assert service.snapshot()["last_result"] is None
        assert make_service().snapshot()["last_scheduled_date"] == "2026-10-23"
    finally:
        service.cancel()
        worker.join(3)


def test_robot_stop_during_collection_discards_result(make_service):
    stopped = threading.Event()

    def reply(*args, **kwargs):
        stopped.set()
        return {"ok": True, "data": {"summary": "Late forecast"}}

    service = make_service(FakeHost(reply), capture_cancellation=lambda: stopped)
    result = service.run(preview=True)
    assert result["cancelled"] and result["sections"] == []
    assert len(service.app_host.calls) == 1


def test_cancel_during_delivery_is_visible_to_callback(make_service):
    service = None

    def deliver(result, *, cancellation, deadline):
        service.cancel()
        assert cancellation()
        return True

    service = make_service(deliver=deliver)
    result = service.run()
    assert result["cancelled"] and not result["delivered"]


def test_cancel_between_availability_check_and_admission_prevents_reads(make_service):
    service = None

    def available():
        service.cancel()
        return True

    service = make_service(can_run=available)
    service.configure({"enabled": True})
    result = service.tick()
    assert result["cancelled"] and not service.app_host.calls
    assert service.snapshot()["last_scheduled_date"] is None


def test_configuration_change_before_result_commit_discards_preview(make_service, monkeypatch):
    service = make_service()
    finish = service._finish

    def after_collection(active, result, cancelled):
        service.configure({"sections": {"weather": False}})
        return finish(active, result, cancelled)

    monkeypatch.setattr(service, "_finish", after_collection)
    result = service.run(preview=True)
    assert result["cancelled"] and not result["sections"]
    assert service.snapshot()["last_result"] is None


def test_global_deadline_discards_late_data_and_stops_remaining_sections(make_service):
    clock = [100.0]

    def reply(*args, **kwargs):
        assert kwargs["deadline"] == 125.0
        clock[0] = 190.0
        return {"ok": True, "data": {"summary": "Late private forecast"}}

    service = make_service(FakeHost(reply), monotonic=lambda: clock[0])
    result = service.run(preview=True)
    assert result["cancelled"] and result["sections"] == []
    assert len(service.app_host.calls) == 1 and "Late private" not in json.dumps(result)


def test_delivery_ownership_does_not_cancel_its_own_output(make_service):
    available = [True]

    def deliver(result, *, cancellation, deadline):
        available[0] = False  # The delivery itself now holds the audio lease.
        assert not cancellation()
        return {"delivered": True}

    service = make_service(deliver=deliver, can_run=lambda: available[0])
    assert service.run()["delivered"]


def test_owner_denial_is_reported_distinctly_without_grant_or_read(make_service):
    host = FakeHost(lambda *a, **kw: {"ok": False, "permission_required": True,
                                    "state": "permission_denied", "permissions": ["network"]})
    result = make_service(host).run(preview=True)
    assert not result["ok"]
    assert all(section["state"] == "permission_denied" and "denied by the owner" in section["text"]
               for section in result["sections"])


def test_delivery_failure_does_not_repeat_scheduled_attempt(make_service):
    def deliver(*args, **kwargs):
        raise RuntimeError("secret output failure")

    service = make_service(deliver=deliver)
    service.configure({"enabled": True})
    result = service.tick()
    assert not result["ok"] and result["state"] == "delivery_unavailable"
    assert "secret" not in json.dumps(result)
    assert service.tick()["reason"] == "already_attempted"


def test_single_scheduler_and_shutdown_cancel_waiting_app(make_service):
    entered = threading.Event()

    def reply(app, tool, args, **kwargs):
        entered.set()
        until = time.monotonic() + 3
        while not kwargs["cancellation"]() and time.monotonic() < until:
            threading.Event().wait(.01)
        return {"ok": False, "cancelled": True}

    service = make_service(FakeHost(reply), poll_interval_s=.01)
    service.configure({"enabled": True})
    service.start()
    service.start()
    assert entered.wait(1)
    with pytest.raises(BusyError):
        service.run(preview=True)
    assert service.tick()["reason"] == "already_attempted"
    service.stop()
    assert not service.snapshot()["running"] and not service.snapshot()["scheduler_running"]
    assert service.snapshot()["stopped"]
    with pytest.raises(UnavailableError):
        service.run()


def test_real_apphost_preserves_permissions_and_disabled_apps(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **kw: pytest.fail("Unexpected live request"))
    robot = type("Robot", (), {"capture_token": lambda self: (0, threading.Event())})()
    host = AppHost(ROOT, tmp_path / "host", robot)
    host.scan()
    service = MorningBriefingService(tmp_path / "briefing.json", host, now=lambda: NOW)
    try:
        result = service.run(preview=True)
        assert not result["ok"]
        assert all(section["permission_required"] for section in result["sections"])
        for section in result["sections"]:
            assert host.permission_snapshot(section["app"])["pending"]
        host.set_enabled("weather", False)
        result = service.run(preview=True)
        assert not result["sections"][0]["ok"] and not result["sections"][0]["permission_required"]
        assert host.enabled("weather") is False
    finally:
        service.stop()
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def test_schedule_claim_failure_precedes_every_app_read(make_service, monkeypatch):
    service = make_service()
    service.configure({"enabled": True})
    monkeypatch.setattr("robbi.services.morning_briefing.write_json", lambda *a, **kw: (_ for _ in ()).throw(OSError("full disk")))
    with pytest.raises(OSError):
        service.tick()
    assert not service.app_host.calls and not service.snapshot()["running"]
    assert service.snapshot()["last_scheduled_date"] is None
