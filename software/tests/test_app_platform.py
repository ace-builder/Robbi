"""Offline acceptance tests for the standalone app host and narrow SDK."""
from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import sys
import threading
import time
import zipfile

import pytest
import yaml
from PIL import Image

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.sdk import AppCancelled, AppContext, Artifacts, Cancellation
from robbi.errors import BusyError, UnavailableError, ValidationError


ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"type": "object", "properties": {}}
CODE = "def run(context, tool, arguments):\n    return {'ok': True, 'text': 'ready'}\n"


class Robot:
    def __init__(self):
        self.generation = 0
        self.cancelled = threading.Event()
        self.actions, self.images, self.captures = [], [], []

    def capture_token(self):
        return self.generation, self.cancelled

    def stop(self):
        self.cancelled.set()
        self.generation += 1
        self.cancelled = threading.Event()

    def status(self):
        return {"health": {"state": "degraded", "components": [{"name": "camera", "state": "degraded", "message": "Camera unplugged"}]}}

    def action(self, action, token, cancelled):
        if cancelled() or token[1].is_set():
            raise AppCancelled("cancelled")
        self.actions.append(action)
        return action

    def show_image(self, path, token, cancelled):
        if cancelled() or token[1].is_set():
            raise AppCancelled("cancelled")
        self.images.append(path)
        return {"displayed": True}

    def capture_image(self):
        self.captures.append(True)
        return None


def manifest(app_id="example", **changes):
    result = {"api_version": 2, "id": app_id, "name": app_id, "entrypoint": "main:run", "permissions": [],
              "tools": [{"name": "run", "description": "Run the example", "input_schema": EMPTY}], "config_schema": EMPTY}
    result.update(changes)
    return result


def project(base, app_id="example", *, code=CODE, **changes):
    directory = base / app_id
    directory.mkdir(parents=True)
    (directory / "app.yaml").write_text(yaml.safe_dump(manifest(app_id, **changes)), encoding="utf-8")
    (directory / "main.py").write_text(code, encoding="utf-8")
    return directory


def archive(app_id="example", *, code=CODE, entries=None, **changes):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("project/app.yaml", yaml.safe_dump(manifest(app_id, **changes)))
        output.writestr("project/main.py", code)
        for name, content in entries or []:
            output.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def make_host(tmp_path):
    created = []

    def make(*, bundled=False, services=None):
        root = ROOT if bundled else tmp_path / f"source-{len(created)}"
        root.mkdir(parents=True, exist_ok=True)
        host = AppHost(root, tmp_path / f"state-{len(created)}", Robot(), services=services, permissions_enabled=False)
        created.append(host)
        return host

    yield make
    for host in created:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def wait_until(predicate, timeout=2):
    until = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < until, "Timed out waiting for bounded app work"
        threading.Event().wait(.005)


def test_discovery_does_not_import_code_and_isolates_invalid_packages(make_host):
    host = make_host()
    project(host.packages.bundled, "healthy")
    project(host.packages.bundled, "broken", api_version=1)
    project(host.packages.bundled, "missing-dependency", code="import unavailable_dependency_for_robbi_test\n" + CODE)
    snapshot = host.scan()
    assert {app["id"] for app in snapshot["apps"]} == {"healthy", "missing-dependency"}
    assert snapshot["errors"][0]["id"] == "broken"
    assert host._modules == {}
    assert host.call("healthy", "run", {})["ok"]
    failure = host.call("missing-dependency", "run", {})
    assert failure["ok"] is False and "unavailable_dependency" in failure["text"]
    assert host.call("healthy", "run", {})["ok"]


def test_imported_code_is_disabled_until_trusted_and_relative_modules_are_isolated(make_host, tmp_path):
    host = make_host()
    sentinel = tmp_path / "imported.txt"
    code = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported')\nfrom .helper import message\ndef run(context, tool, arguments):\n    return {{'text': message}}\n"
    detail = host.install(archive(code=code, entries=[("project/helper.py", "message = 'relative helper works'\n")]))
    assert detail["app"]["enabled"] is False
    assert not sentinel.exists()
    assert all(json.loads(line)["app"] != "example" for line in host.prompt_catalog().splitlines() if line.startswith('{'))
    with pytest.raises(UnavailableError, match="disabled"):
        host.call("example", "run", {})
    assert not sentinel.exists()
    host.set_enabled("example", True)
    assert not sentinel.exists()  # Enabling is still manifest-only.
    # Import side effects cannot write to the host, even after owner enablement.
    result = host.call("example", "run", {})
    assert result["ok"] is False
    assert not sentinel.exists()
    assert host._modules == {}


def test_replacing_imported_project_invalidates_old_module_and_disables_replacement(make_host, trusted_app_runner):
    host = make_host()
    host.install(archive())
    host.set_enabled("example", True)
    assert host.call("example", "run", {})["text"] == "ready"
    detail = host.install(archive(code=CODE.replace("ready", "replacement")))
    assert detail["app"]["enabled"] is False
    assert host._modules == {}
    with pytest.raises(UnavailableError):
        host.call("example", "run", {})
    host.set_enabled("example", True)
    assert host.call("example", "run", {})["text"] == "replacement"


@pytest.mark.parametrize("path", ["../escape.txt", "/absolute.txt", "C:/escape.txt", "project/../../escape", "project\\..\\..\\escape"])
def test_zip_path_traversal_is_rejected_before_install(make_host, path):
    host = make_host()
    # ZipInfo normalizes the Windows path separator in its constructor. Set
    # the raw archive name afterwards to exercise malformed archive bytes.
    entry = zipfile.ZipInfo("placeholder")
    entry.filename = path
    with pytest.raises(ValidationError):
        host.install(archive(entries=[(entry, "escaped")]))
    assert host.catalog() == []


def test_zip_links_duplicates_and_many_entries_are_rejected(make_host):
    host = make_host()
    link = zipfile.ZipInfo("project/linked")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    cases = [archive(entries=[(link, "../../outside")]),
             archive(entries=[("project/MAIN.py", "duplicate")]),
             archive(entries=[(f"project/{number}.txt", "x") for number in range(257)])]
    for payload in cases:
        with pytest.raises(ValidationError):
            host.install(payload)
    assert host.catalog() == []


def test_bundled_apps_support_overlay_updates_and_reversible_removal(make_host):
    host = make_host()
    project(host.packages.bundled, "core")
    host.scan()
    original = (host.packages.bundled / "core" / "main.py").read_bytes()
    host.install(archive("core"))
    assert not host.project("core").bundled and not host.enabled("core")
    assert host.remove("core")["data_preserved"]
    assert "core" not in host.packages.projects
    host.restore("core", enable=True)
    assert host.enabled("core")
    assert (host.packages.bundled / "core" / "main.py").read_bytes() == original
    host.install(archive("example"))
    host.remove("example")
    assert [app["id"] for app in host.catalog()] == ["core"]


def test_input_validation_and_defaults_precede_handler_loading(make_host):
    host = make_host()
    schema = {"type": "object", "required": ["location"], "properties": {
        "location": {"type": "string", "minLength": 1, "maxLength": 10},
        "days": {"type": "integer", "minimum": 1, "maximum": 7, "default": 1},
    }}
    project(host.packages.bundled, code="def run(context, tool, arguments):\n    return {'data': arguments}\n",
            tools=[{"name": "run", "description": "Example", "input_schema": schema}])
    host.scan()
    for arguments in ({}, {"location": "Berlin", "extra": True}, {"location": "Berlin", "days": True},
                      {"location": "Berlin", "days": 100}, {"location": "long" * 10}):
        with pytest.raises(ValidationError):
            host.submit("example", "run", arguments)
    assert host._modules == {}
    assert host.call("example", "run", {"location": "Berlin"})["data"] == {"location": "Berlin", "days": 1}


def test_config_accepts_env_names_without_disclosing_secret_and_redacts_errors(make_host, monkeypatch):
    monkeypatch.setenv("ROBBI_TEST_API_KEY", "secret-provider-value")
    schema = {"type": "object", "properties": {"api_key_env": {"type": "string", "format": "env", "default": "ROBBI_TEST_API_KEY"}}}
    code = "import os\ndef run(context, tool, arguments):\n    raise ValueError('Request failed with ' + os.environ[context.config['api_key_env']])\n"
    host = make_host()
    project(host.packages.bundled, code=code, config_schema=schema)
    host.scan()
    for changes in ({"api_key_env": "sk-literal-value"}, {"api_key": "secret-provider-value"}):
        with pytest.raises(ValidationError):
            host.configure("example", changes)
    result = host.call("example", "run", {})
    assert "[redacted]" in result["text"]
    visible = json.dumps({"detail": host.detail("example"), "catalog": host.catalog(), "result": result}) + host.prompt_catalog()
    assert "secret-provider-value" not in visible
    assert "ROBBI_TEST_API_KEY" in visible


def test_errors_redact_long_and_overlapping_credentials_before_truncation(make_host, monkeypatch):
    long_secret = "credential-prefix" + "private-token-fragment-" * 80
    monkeypatch.setenv("ROBBI_TEST_SHORT_KEY", "credential-prefix")
    monkeypatch.setenv("ROBBI_TEST_LONG_KEY", long_secret)
    config = {"short_env": "ROBBI_TEST_SHORT_KEY", "long_env": "ROBBI_TEST_LONG_KEY"}
    host = make_host()
    result = host._error(ValueError("x" * 900 + long_secret), config)
    assert result == "x" * 900 + "[redacted]"


def test_corrupt_one_app_configuration_does_not_break_other_apps(make_host):
    host = make_host()
    project(host.packages.bundled, "first")
    project(host.packages.bundled, "second")
    directory = host.directory / "data" / "first"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{bad")
    snapshot = host.scan()
    assert next(app for app in snapshot["apps"] if app["id"] == "first")["health"]["state"] == "degraded"
    assert host.detail("first")["config_error"]
    assert host.call("second", "run", {})["ok"]


def test_per_app_limit_does_not_block_independent_app(make_host, monkeypatch):
    host = make_host()
    project(host.packages.bundled, "slow")
    project(host.packages.bundled, "fast")
    host.scan()
    entered, release = threading.Event(), threading.Event()

    def slow(context, tool, arguments):
        entered.set()
        assert release.wait(3)
        return {"text": "done"}

    monkeypatch.setattr(host, "_handler", lambda project: slow if project.id == "slow" else lambda *args: {"text": "fast"})
    try:
        job = host.submit("slow", "run", {})
        assert entered.wait(1)
        with pytest.raises(BusyError, match="already"):
            host.submit("slow", "run", {})
        assert host.call("fast", "run", {})["text"] == "fast"
        with pytest.raises(BusyError):
            host.scan()
        with pytest.raises(BusyError):
            host.configure("slow", {})
        assert host.job(job["id"])["state"] == "running"
    finally:
        release.set()


def test_two_workers_and_two_pending_are_bounded_and_cancelled_queue_never_runs(make_host, monkeypatch):
    host = make_host()
    for number in range(5):
        project(host.packages.bundled, f"app-{number}")
    host.scan()
    release = threading.Event()
    entered = []

    def handler(context, tool, arguments):
        entered.append(context.app_id)
        assert release.wait(3)
        return {"text": "done"}

    monkeypatch.setattr(host, "_handler", lambda project: handler)
    try:
        jobs = [host.submit(f"app-{number}", "run", {}) for number in range(4)]
        wait_until(lambda: len(entered) == 2)
        with pytest.raises(BusyError):
            host.submit("app-4", "run", {})
        assert host.job(jobs[2]["id"])["state"] == "queued"
        host.cancel(jobs[2]["id"])
        release.set()
        wait_until(lambda: not host._active)
        assert "app-2" not in entered
        assert host.job(jobs[2]["id"])["state"] == "cancelled"
    finally:
        release.set()


@pytest.mark.parametrize("cancel_kind", ["job", "robot", "disable", "shutdown", "deadline"])
def test_cancel_releases_waiter_promptly_but_keeps_blocked_owner_and_discards_late_output(make_host, monkeypatch, cancel_kind):
    host = make_host()
    project(host.packages.bundled, permissions=["robot.speak"])
    host.scan()
    entered, release = threading.Event(), threading.Event()
    results = []

    def blocked(context, tool, arguments):
        entered.set()
        assert release.wait(3)
        context.robot.speak("late output")
        return {"text": "late result"}

    monkeypatch.setattr(host, "_handler", lambda project: blocked)
    deadline = time.monotonic() + .12 if cancel_kind == "deadline" else None
    waiter = threading.Thread(target=lambda: results.append(host.call("example", "run", {}, deadline=deadline)))
    try:
        waiter.start()
        assert entered.wait(1)
        job_id = next(iter(host._jobs))
        started = time.monotonic()
        if cancel_kind == "job":
            host.cancel(job_id)
        elif cancel_kind == "robot":
            host.robot.stop()
        elif cancel_kind == "disable":
            host.set_enabled("example", False)
        elif cancel_kind == "shutdown":
            host.stop()
        waiter.join(.6)
        assert not waiter.is_alive()
        assert time.monotonic() - started < .6
        assert results[0]["cancelled"]
        assert "example" in host._active  # Still owns the blocked worker.
        release.set()
        wait_until(lambda: not host._active)
        assert host.robot.actions == []
        assert host.job(job_id)["result"] is None
        assert host.job(job_id)["state"] == "cancelled"
    finally:
        release.set()
        waiter.join(3)


def test_oversized_or_non_json_app_result_isolated_from_other_calls(make_host, monkeypatch):
    host = make_host()
    project(host.packages.bundled)
    host.scan()
    for value in ({"text": "x" * 70000}, {"data": float("nan")}, {"data": object()}, ["wrong-shape"]):
        monkeypatch.setattr(host, "_handler", lambda project, value=value: lambda *args: value)
        assert host.call("example", "run", {})["ok"] is False
    monkeypatch.setattr(host, "_handler", lambda project: lambda *args: {"text": "recovered"})
    assert host.call("example", "run", {})["text"] == "recovered"


def test_hyphen_and_underscore_ids_have_distinct_python_module_namespaces(make_host):
    host = make_host()
    project(host.packages.bundled, "foo-bar", code=CODE.replace("ready", "hyphen"))
    project(host.packages.bundled, "foo_bar", code=CODE.replace("ready", "underscore"))
    host.scan()
    assert host.call("foo-bar", "run", {})["text"] == "hyphen"
    assert host.call("foo_bar", "run", {})["text"] == "underscore"
    assert host._modules["foo-bar"][1] != host._modules["foo_bar"][1]


def test_stop_epoch_does_not_wait_for_host_lock_during_discovery_or_disk_write(make_host):
    host = make_host()
    acquired, release, cancelled = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with host._lock:
            acquired.set()
            assert release.wait(3)

    holder = threading.Thread(target=hold)
    stopper = threading.Thread(target=lambda: (host.cancel_all(), cancelled.set()))
    try:
        holder.start()
        assert acquired.wait(1)
        stopper.start()
        assert cancelled.wait(.5)
    finally:
        release.set()
        holder.join(3)
        stopper.join(3)


@pytest.mark.parametrize("cancel_kind", ["disable", "stop"])
def test_delayed_robot_output_from_completed_app_retains_invalidation_signal(make_host, monkeypatch, cancel_kind):
    host = make_host()
    project(host.packages.bundled, permissions=["robot.speak"])
    host.scan()
    saved_signals = []
    monkeypatch.setattr(host.robot, "action", lambda action, token, cancelled: saved_signals.append(cancelled) or {})

    def handler(context, tool, arguments):
        context.robot.speak("queued speech")
        return {"text": "speech queued"}

    monkeypatch.setattr(host, "_handler", lambda project: handler)
    assert host.call("example", "run", {})["ok"]
    assert saved_signals[0]() is False
    if cancel_kind == "disable":
        host.set_enabled("example", False)
        host.set_enabled("example", True)
    else:
        host.cancel_all()
    assert saved_signals[0]() is True
    assert host.call("example", "run", {})["ok"]
    assert saved_signals[1]() is False


def test_deep_or_cyclic_app_data_fails_without_exhausting_recursion(make_host, monkeypatch):
    host = make_host()
    project(host.packages.bundled)
    host.scan()
    nested = {}
    cursor = nested
    for _ in range(30):
        cursor["child"] = {}
        cursor = cursor["child"]
    cycle = {}
    cycle["self"] = cycle
    for value in (nested, cycle):
        monkeypatch.setattr(host, "_handler", lambda project, value=value: lambda *args: value)
        assert host.call("example", "run", {})["ok"] is False


def context(tmp_path, permissions=(), *, robot=None):
    robot = robot or Robot()
    signal = Cancellation(threading.Event(), None, time.monotonic() + 10)
    return AppContext(app_id="example", config={}, permissions=permissions, directory=tmp_path,
                      artifacts=Artifacts(tmp_path / "images", "example"), cancelled=signal, deadline=signal.deadline,
                      robot=robot, robot_token=robot.capture_token(), services=lambda name: pytest.fail("network dependency instantiated"))


@pytest.mark.parametrize("operation", [
    lambda c: c.read_state(), lambda c: c.write_state({}), lambda c: c.save_image(b"x"),
    lambda c: c.robot.status(), lambda c: c.robot.speak("hello"), lambda c: c.robot.show_image("x"),
    lambda c: c.robot.capture_image(), lambda c: c.robot.move(direction="forward"),
    lambda c: c.services.images, lambda c: c.services.weather,
    lambda c: c.services.google, lambda c: c.services.search, lambda c: c.services.maps,
])
def test_sdk_permissions_precede_all_capability_access(tmp_path, operation):
    subject = context(tmp_path)
    with pytest.raises(ValidationError, match="permission"):
        operation(subject)
    assert subject.robot._port.actions == []
    assert not tmp_path.joinpath("state.json").exists()


def test_state_is_bounded_atomic_and_reports_corruption(tmp_path):
    subject = context(tmp_path, ["state"])
    subject.write_state({"note": "kept"})
    assert subject.read_state() == {"note": "kept"}
    with pytest.raises(ValidationError):
        subject.write_state({"note": "x" * 70000})
    assert subject.read_state() == {"note": "kept"}
    subject.cancelled.event.set()
    with pytest.raises(AppCancelled):
        subject.write_state({"note": "late"})
    assert json.loads((tmp_path / "state.json").read_text()) == {"note": "kept"}
    subject.cancelled.event.clear()
    (tmp_path / "state.json").write_text("[]")
    with pytest.raises(ValidationError, match="unreadable"):
        subject.read_state()


def png(size=(500, 300), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def test_generated_images_are_normalized_retained_and_confined(tmp_path):
    subject = context(tmp_path, ["robot.display"])
    saved = [subject.save_image(png(), f"Diagram {number}") for number in range(14)]
    assert len(subject._artifacts.list()) == 12
    assert len(list((tmp_path / "images").glob("*.png"))) == 12
    with Image.open(subject._artifacts.path(saved[-1]["id"])) as result:
        assert result.size == (280, 240) and result.mode == "RGB"
    with pytest.raises(UnavailableError):
        subject._artifacts.path(saved[0]["id"])
    with pytest.raises(ValidationError):
        subject.robot.show_image("../other-app/image")
    assert subject.robot.show_image(saved[-1]["id"])["displayed"]
    assert subject.robot._port.images == [subject._artifacts.path(saved[-1]["id"])]


@pytest.mark.parametrize("payload", [b"not an image", b"", b"x" * (12 * 1024 * 1024 + 1)], ids=["invalid", "empty", "oversized"])
def test_invalid_or_oversized_image_bytes_do_not_create_artifacts(tmp_path, payload):
    subject = context(tmp_path, ["robot.display"])
    with pytest.raises(ValidationError):
        subject.save_image(payload)
    assert subject._artifacts.list() == []


def test_image_pixel_bound_precedes_full_decode_and_write(tmp_path):
    subject = context(tmp_path, ["robot.display"])
    with pytest.raises(ValidationError, match="16 million"):
        subject.save_image(png((4001, 4000)))
    assert subject._artifacts.list() == []


def test_exif_orientation_is_applied_before_final_display_fit(tmp_path):
    subject = context(tmp_path, ["robot.display"])
    stream = io.BytesIO()
    image = Image.new("RGB", (400, 200), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(stream, "JPEG", exif=exif)
    artifact = subject.save_image(stream.getvalue())
    with Image.open(subject._artifacts.path(artifact["id"])) as result:
        # A rotated 2:1 image fits into 120x240, centered with 80px padding.
        # Fitting before rotation produces a cropped 140x280 image instead.
        assert min(result.getpixel((75, 120))) > 245
        assert result.getpixel((140, 120))[0] > 245
        assert result.getpixel((140, 120))[1] < 10


def test_artifact_metadata_write_failure_leaves_no_orphan_image(tmp_path, monkeypatch):
    import robbi.app_runtime.sdk as sdk

    subject = context(tmp_path, ["robot.display"])

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(sdk, "write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        subject.save_image(png())
    assert list((tmp_path / "images").glob("*.png")) == []


def test_cancel_after_decode_discards_image_before_storage(tmp_path):
    subject = context(tmp_path, ["robot.display"])
    checks = []

    def checkpoint():
        checks.append(True)
        if len(checks) == 2:
            raise AppCancelled("cancelled during decode")

    with pytest.raises(AppCancelled):
        subject._artifacts.save(png(), "image", checkpoint)
    assert subject._artifacts.list() == []


def test_missing_camera_has_actionable_error_without_breaking_status(tmp_path):
    subject = context(tmp_path, ["robot.camera", "robot.status"])
    with pytest.raises(UnavailableError, match="camera connection"):
        subject.robot.capture_image()
    assert subject.robot.status()["health"]["state"] == "degraded"


def test_bundled_local_apps_run_without_provider_dependencies(make_host):
    host = make_host(bundled=True)
    assert host.scan()["errors"] == []
    assert host.call("calculator", "calculate", {"expression": "(18 * 24) / 3"})["data"]["value"] == 144
    assert host.call("calculator", "convert", {"value": 20, "from_unit": "C", "to_unit": "F"})["data"]["value"] == 68
    assert host.call("calculator", "calculate", {"expression": "__import__('os').getcwd()"})["ok"] is False
    assert host.call("calculator", "calculate", {"expression": "9 ** 999"})["ok"] is False
    added = host.call("quick-notes", "add", {"text": "Buy screws"})
    assert added["ok"]
    listed = host.call("quick-notes", "list", {})
    assert listed["data"]["notes"][0]["text"] == "Buy screws"
    assert host.call("quick-notes", "delete", {"id": added["data"]["id"]})["ok"]
    assert host.call("quick-notes", "list", {})["data"]["total"] == 0
    status = host.call("robot-status", "check", {})
    assert "Camera unplugged" in status["text"]
    assert host._services == {}  # No provider client imported/created for local tools.
