"""Fault-injection contracts for memory, worker admission and shutdown."""
import gc
import io
import threading
import weakref
from concurrent.futures import CancelledError
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from robbi.app_services.http import HttpClient
from robbi.configuration import store
from robbi.errors import ValidationError
from robbi.health import HealthRegistry
from robbi.jobs import BoundedExecutor
from robbi.runtime import Runtime


@pytest.mark.parametrize("outcome", ["return", "discard", "raise"])
def test_idle_worker_releases_completed_payload_and_future(monkeypatch, outcome):
    class Payload:
        pass

    executor = BoundedExecutor(1, 0)
    idle = threading.Event()
    original_get = executor._queue.get
    reads = 0

    def observe_get(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            idle.set()
        return original_get(*args, **kwargs)

    monkeypatch.setattr(executor._queue, "get", observe_get)

    def work(value):
        if outcome == "raise":
            raise RuntimeError(value)
        return value if outcome == "return" else None

    try:
        payload = Payload()
        reference = weakref.ref(payload)
        future = executor.submit(work, payload)
        try:
            future.result(2)
        except RuntimeError:
            assert outcome == "raise"
        assert idle.wait(2)
        del payload, future
        gc.collect()
        assert reference() is None, "Idle worker retained completed request data"
    finally:
        executor.shutdown()


def test_thread_start_failure_does_not_queue_rejected_work_or_consume_capacity(monkeypatch):
    executor = BoundedExecutor(1, 0)
    calls = []
    original_start = threading.Thread.start

    def fail_start(_):
        raise RuntimeError("simulated thread resource exhaustion")

    try:
        monkeypatch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="resource exhaustion"):
            executor.submit(lambda: calls.append("rejected"))
        monkeypatch.setattr(threading.Thread, "start", original_start)
        assert executor.submit(lambda: 42).result(2) == 42
        assert calls == []
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.parametrize("workers,pending", [(True, 0), (1, False), (1.5, 0),
                         (1, float("nan")), (float("inf"), 0), ("2", 0)])
def test_executor_capacity_requires_integer_counts(workers, pending):
    with pytest.raises(ValueError):
        BoundedExecutor(workers, pending)


COMPONENTS = [
    ("jobs", "stop"), ("morning_briefing", "stop"), ("briefing_jobs", "stop"),
    ("audio_studio", "stop"), ("filming", "shutdown"), ("app_host", "stop"),
    ("app_manager", "stop"), ("app_management_jobs", "stop"),
    ("_controller", "stop"), ("recording", "stop"),
]


@pytest.mark.parametrize("broken", [name for name, _ in COMPONENTS])
def test_shutdown_attempts_every_component_after_a_failure(broken):
    runtime = Runtime.__new__(Runtime)
    runtime._lock = threading.Lock()
    runtime._stop, runtime._ready = threading.Event(), threading.Event()
    runtime._ready.set()
    runtime.health = HealthRegistry()
    calls = []
    runtime.emergency_stop = lambda **_: calls.append("emergency_stop")

    def stop(name):
        calls.append(name)
        if name == broken:
            raise OSError("simulated close failure")

    for name, method in COMPONENTS:
        setattr(runtime, name, SimpleNamespace(**{method: lambda n=name: stop(n)}))
    runtime.stop()
    assert calls == ["emergency_stop"] + [name for name, _ in COMPONENTS]
    assert runtime._stop.is_set() and not runtime._ready.is_set()
    assert any(row["state"] == "failed" for row in runtime.health.snapshot())
    runtime.stop()
    assert len(calls) == len(COMPONENTS) + 1


def test_deep_yaml_becomes_a_recoverable_configuration_error(tmp_path):
    text = "nested: " + "[" * 1600 + "0" + "]" * 1600
    with pytest.raises(ValidationError):
        store.parse_yaml(text)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/desktop.yaml").write_text("server: {port: 8082}\n")
    source = tmp_path / "robot.yaml"
    source.write_text(text)
    loaded = store.load(tmp_path, source)
    assert loaded.issues
    assert loaded.values["hardware"]["enable_motors"] is False
    assert source.read_text() == text


@pytest.mark.parametrize("operation", ["load", "defaults", "yaml", "secrets"])
def test_configuration_reads_are_bounded_before_parsing(tmp_path, monkeypatch, operation):
    from pathlib import Path
    from robbi.configuration.secrets import read_env

    (tmp_path / "config").mkdir()
    source = tmp_path / "config/desktop.yaml" if operation == "defaults" else tmp_path / "input"
    source.write_bytes(b"x" * (300 * 1024))
    original_open = Path.open
    requests = []
    limit = 65536 if operation == "secrets" else 256 * 1024

    class GuardedReader(io.BytesIO):
        def read(self, count=-1):
            requests.append(count)
            assert 0 <= count <= limit + 1, "Unbounded allocation before validation"
            return super().read(count)

    def guarded_open(path, *args, **kwargs):
        if path == source:
            return GuardedReader(b"x" * (300 * 1024))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    if operation == "load":
        assert store.load(tmp_path, source).issues
    elif operation == "defaults":
        assert store.read_defaults(tmp_path)[1]
    elif operation == "yaml":
        issues = []
        assert store.read_yaml(source, errors=issues) == {}
        assert issues
    else:
        with pytest.raises(ValueError, match="64 KiB"):
            read_env(source)
    assert requests == [limit + 1]


def test_bounded_reader_counts_utf8_bytes_and_preserves_complete_text(tmp_path):
    from robbi.storage import read_text_limited

    path = tmp_path / "text"
    path.write_bytes("é".encode("utf-8") * 4)
    assert read_text_limited(path, 8) == "é" * 4
    with pytest.raises(ValueError, match="exceeds"):
        read_text_limited(path, 7)


@pytest.mark.parametrize("cancelled", [False, True])
def test_http_not_modified_closes_response_and_honors_cancellation(cancelled):
    body = io.BytesIO(b"")
    hit = HTTPError("https://provider.test", 304, "Not modified", {"ETag": "v1"}, body)
    opened = False

    class Opener:
        def open(self, *_, **__):
            nonlocal opened
            opened = True
            raise hit

    client = HttpClient(opener=Opener())
    if cancelled:
        with pytest.raises(CancelledError):
            client.request("GET", "https://provider.test", cancellation=lambda: opened)
    else:
        response = client.request("GET", "https://provider.test")
        assert response.status == 304 and response.headers["etag"] == "v1"
    assert body.closed
