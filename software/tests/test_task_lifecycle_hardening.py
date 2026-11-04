"""Deterministic persistence and lifecycle races, without hardware/providers."""
from concurrent.futures import Future
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError
from robbi.health import HealthRegistry
from robbi.jobs import JobRegistry
from robbi.runtime import Runtime
from robbi.services.background_work import BackgroundTaskService, BackgroundWorkOptions


class Scheduler:
    def __init__(self):
        self.pending = []

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.pending.append((future, fn, args, kwargs))
        return future

    def run(self):
        future, fn, args, kwargs = self.pending.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                value = fn(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(value)
        return future


def submit(service, work=lambda _: {"speak": "Result"}):
    return service.submit(text="A requested task", source="ui_text", provider="test",
                          model="test", work=work)


def service_at(tmp_path):
    scheduler = Scheduler()
    service = BackgroundTaskService(tmp_path / "tasks.sqlite", scheduler=scheduler,
                                    options=lambda: BackgroundWorkOptions(max_history=4))
    return service, scheduler


@pytest.mark.parametrize("failure", [OSError, sqlite3.OperationalError])
def test_failed_task_save_preserves_all_previous_history(tmp_path, monkeypatch, failure):
    service, scheduler = service_at(tmp_path)
    ids = []
    for _ in range(4):
        ids.append(submit(service)["id"])
        scheduler.run()
    before = service.snapshot()
    stored = service._store.load(32)
    epochs = dict(service._delivery_epochs)

    def fail(*_):
        raise failure("simulated full or unavailable storage")

    with monkeypatch.context() as patch:
        patch.setattr(service._store, "save", fail)
        with pytest.raises(failure):
            submit(service)
    assert service.snapshot() == before
    assert service._delivery_epochs == epochs
    assert service._store.load(32) == stored
    assert not scheduler.pending
    latest = submit(service)
    scheduler.run()
    assert {row["id"] for row in service.snapshot()["tasks"]} == set(ids[1:] + [latest["id"]])


def test_completed_result_is_detached_from_provider_owned_objects(tmp_path):
    service, scheduler = service_at(tmp_path)
    result = {"speak": "Original", "data": {"items": ["first"]}}
    task = submit(service, lambda _: result)
    scheduler.run()
    before = service.get(task["id"])
    result["speak"] = "Changed after persistence"
    result["data"]["items"].append(float("nan"))
    assert service.get(task["id"]) == before
    assert service._store.load(32)[0]["result"] == before["result"]


def test_failed_retention_sql_rolls_back_insert_and_preserves_memory(tmp_path, monkeypatch):
    service, scheduler = service_at(tmp_path)
    for _ in range(4):
        submit(service)
        scheduler.run()
    before, stored = service.snapshot(), service._store.load(32)
    connect = service._store.connect

    class FailingTransaction:
        def __init__(self):
            self.db = connect()

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *args):
            return self.db.__exit__(*args)

        def execute(self, sql, parameters=()):
            if sql.startswith("DELETE"):
                raise sqlite3.OperationalError("failure after insert")
            return self.db.execute(sql, parameters)

        def close(self):
            self.db.close()

    with monkeypatch.context() as patch:
        patch.setattr(service._store, "connect", FailingTransaction)
        with pytest.raises(sqlite3.OperationalError):
            submit(service)
    assert service.snapshot() == before
    assert service._store.load(32) == stored
    assert not scheduler.pending


def test_shutdown_does_not_wait_for_storage_or_execute_previously_admitted_work(tmp_path, monkeypatch):
    service, scheduler = service_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls, outcome = [], []
    save = service._store.save

    def blocked_save(record, retained):
        if record["status"] == "queued":
            entered.set()
            assert release.wait(2)
        save(record, retained)

    monkeypatch.setattr(service._store, "save", blocked_save)
    thread = threading.Thread(target=lambda: outcome.append(submit(service, lambda _: calls.append("ran"))))
    thread.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        service.shutdown()
        assert time.monotonic() - started < .1
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and len(outcome) == 1
    scheduler.run()
    assert calls == []
    assert service.get(outcome[0]["id"])["status"] == "cancelled"


def test_cancelled_future_save_failure_still_releases_admission(tmp_path, monkeypatch, caplog):
    service, scheduler = service_at(tmp_path)
    task = submit(service)

    def fail(*_):
        raise sqlite3.OperationalError("simulated storage outage")

    monkeypatch.setattr(service._store, "save", fail)
    assert scheduler.pending[0][0].cancel()
    row = service.get(task["id"])
    assert row["status"] == "cancelled" and row["delivery"] == "retained"
    assert row["error"] == "Task result could not be saved"
    assert not service._active
    assert "exception calling callback" not in caplog.text


def test_shutdown_cannot_be_bypassed_between_admission_check_and_epoch_capture(tmp_path):
    service, scheduler = service_at(tmp_path)
    waiting, proceed = threading.Event(), threading.Event()
    original = service._epoch_lock
    outcome = []

    class Gate:
        def __enter__(self):
            if threading.current_thread().name == "admission-race":
                waiting.set()
                assert proceed.wait(2)
            original.acquire()

        def __exit__(self, *_):
            original.release()

    service._epoch_lock = Gate()

    def request():
        try:
            outcome.append(submit(service))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=request, name="admission-race")
    thread.start()
    try:
        assert waiting.wait(2)
        service.shutdown()
    finally:
        proceed.set()
        thread.join(2)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], BusyError)
    assert not scheduler.pending
    assert not service.snapshot()["tasks"]


@pytest.mark.parametrize("error", [SystemExit, KeyboardInterrupt])
def test_background_worker_abnormal_exit_leaves_a_terminal_record(tmp_path, error):
    service, scheduler = service_at(tmp_path)

    def abort(_):
        raise error("private adapter details")

    task = submit(service, abort)
    scheduler.run()
    row = service.get(task["id"])
    assert row["status"] == "failed" and row["phase"] == "finished"
    assert "private adapter details" not in row["error"]
    assert row["delivery"] == "retained" and service.snapshot()["active"] == 0
    assert service._store.load(32)[0]["status"] == "failed"


@pytest.mark.parametrize("error", [SystemExit, KeyboardInterrupt])
def test_http_job_worker_abnormal_exit_does_not_remain_running(error):
    jobs = JobRegistry()
    jobs.executor.shutdown()
    scheduler = jobs.executor = Scheduler()

    def abort():
        raise error("private adapter details")

    job = jobs.submit("probe", abort)
    scheduler.run()
    row = jobs.get(job["id"])
    assert row["state"] == "failed"
    assert "private adapter details" not in row["error"]


@pytest.mark.parametrize("mode", ["missing", "property_failure", "absent"])
def test_incomplete_shutdown_adapter_cannot_skip_emergency_stop_or_other_cleanup(mode):
    runtime = Runtime.__new__(Runtime)
    runtime._lock = threading.Lock()
    runtime._stop, runtime._ready = threading.Event(), threading.Event()
    runtime.health = HealthRegistry()
    calls = []
    runtime.emergency_stop = lambda **_: calls.append("emergency")
    names = ["jobs", "morning_briefing", "briefing_jobs", "audio_studio", "filming",
             "app_host", "app_manager", "app_management_jobs", "_controller", "recording"]
    for name in names:
        method = "shutdown" if name == "filming" else "stop"
        setattr(runtime, name, SimpleNamespace(**{method: lambda n=name: calls.append(n)}))

    class Faulty:
        @property
        def stop(self):
            raise OSError("adapter was only partly initialized")

    runtime.audio_studio = {"missing": SimpleNamespace(), "property_failure": Faulty(), "absent": None}[mode]
    runtime.stop()
    assert calls == ["emergency"] + [n for n in names if n != "audio_studio"]
    if mode != "absent":
        assert any(row["name"] == "shutdown-audio-studio" and row["state"] == "failed"
                   for row in runtime.health.snapshot())
