"""Composition root for standalone apps, tool discovery and bounded invocations."""
from __future__ import annotations

from collections import OrderedDict
import copy
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import types
import uuid
from contextlib import contextmanager

from robbi.errors import BusyError, ValidationError, UnavailableError
from robbi.execution import BoundedExecutor
from robbi.storage import write_json
from robbi.orchestration import ResourceBusy
from .packages import Packages
from .schema import bounded_json, identifier, validate_value, patch_config
from .sdk import AppContext, Artifacts, Cancellation, AppCancelled
from .permissions import PermissionStore, AppPermissionRequired
from .undo import UndoPlan, UndoConflict


class AppHost:
    PROMPT_LIMIT = 48000

    def __init__(self, project_root, data_dir, robot, *, services=None, permissions_enabled=True, on_game_turn=None):
        self.directory = Path(data_dir) / "app-runtime"
        self.permission_store = PermissionStore(self.directory / "permissions.json", enabled=permissions_enabled)
        self.packages = Packages(Path(project_root) / "apps", self.directory / "projects")
        self.robot = robot
        self._services = dict(services or {})
        self._service_lock = threading.Lock()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._changing = set()
        self._cancel_lock = threading.Lock()
        self._cancel_epoch = threading.Event()
        self._executor = BoundedExecutor(2, 2, "apps")
        self._read_executor = BoundedExecutor(2, 2, "app-reads")
        self._jobs, self._active, self._modules = OrderedDict(), {}, {}
        self._health, self._artifacts = {}, {}
        self._app_cancel = {}
        self._enabled = self._read_object(self.directory / "enabled.json")
        self._stopped = False
        self._revision = uuid.uuid4().hex
        self._catalog = ""
        self._game_turn = None
        self._on_game_turn = on_game_turn

    def game_session(self):
        """One ephemeral local game; reboot, Stop, removal and expiry revoke it."""
        with self._lock:
            turn = self._game_turn
            if (turn is None or time.monotonic() >= turn['expires_at']
                    or any(event.is_set() for event in turn['epochs'])
                    or turn['app'] not in self.packages.projects or not self.enabled(turn['app'])):
                self._game_turn = None
                return None
            return {key:turn[key] for key in ('app', 'revision', 'expires_at')}

    def _record_game_turn(self, project, tool_name, result, source, epochs):
        from robbi.mini_games import GAME_IDS
        if not source.startswith(('speech_input', 'ui_text')):
            return
        with self._lock:
            if project.id not in GAME_IDS or not project.bundled or tool_name != 'play':
                self._game_turn = None
                return
            data = result.get('data', {})
            if result.get('ok') is not True or type(data.get('active')) is not bool:
                return
            self._game_turn = ({'app':project.id, 'revision':uuid.uuid4().hex,
                'expires_at':time.monotonic()+1800, 'epochs':list(epochs)} if data['active'] else None)
        if callable(self._on_game_turn):
            try:
                self._on_game_turn(self.game_session(), source)
            except Exception:
                # Optional attention must never replace a completed game result.
                pass

    @staticmethod
    def _read_object(path):
        if not path.exists():
            return {}
        try:
            if path.stat().st_size > 65536:
                raise ValueError("File exceeds limit")
            result = bounded_json(json.loads(path.read_text(encoding="utf-8")))
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
            return result
        except (OSError, ValueError, RecursionError):
            # Discovery reports app-specific config errors separately. An invalid
            # enabled map never grants trust to imported code.
            return {}

    def bind_scheduler(self, scheduler):
        with self._lock:
            if self._active:
                raise BusyError("Wait for active apps before changing scheduler")
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = scheduler

    def _idle(self):
        if self._active or self._changing:
            raise BusyError("Wait for active app calls to finish before changing app projects")

    def scan(self):
        with self._lock:
            self._idle()
            self.packages.scan()
            installed = set(self.packages.projects)
            for mapping in (self._health, self._artifacts, self._app_cancel):
                for removed in set(mapping) - installed:
                    old = mapping.pop(removed)
                    if mapping is self._app_cancel:
                        old.set()
            for _, (_, module_name) in self._modules.items():
                for key in list(sys.modules):
                    if key == module_name or key.startswith(module_name + "."):
                        sys.modules.pop(key, None)
            self._modules.clear()
            self._revision = uuid.uuid4().hex
            for project in self.packages.projects.values():
                self._app_cancel.setdefault(project.id, threading.Event())
                try:
                    self.config(project.id)
                    self._health[project.id] = {"state": "ready" if self.enabled(project.id) else "disabled", "error": ""}
                except (OSError, ValueError) as error:
                    self._health[project.id] = {"state": "degraded", "error": str(error)[:500]}
            self._refresh_prompt()
            return self.snapshot()

    def project(self, app_id):
        identifier(app_id)
        project = self.packages.projects.get(app_id)
        if project is None:
            raise ValidationError("App is not installed")
        return project

    def enabled(self, app_id):
        project = self.project(app_id)
        return self._enabled.get(app_id, project.bundled) is True

    def catalog(self):
        with self._lock:
            return [dict(copy.deepcopy(project.manifest), bundled=project.bundled,
                         enabled=self.enabled(project.id), access=self.permission_store.snapshot(project.id, project.manifest["permissions"]),
                         health=copy.deepcopy(self._health.get(project.id, {"state": "ready", "error": ""})))
                    for project in sorted(self.packages.projects.values(), key=lambda item: item.id)]

    def snapshot(self):
        from .sandbox import availability
        return {"apps": self.catalog(), "errors": copy.deepcopy(self.packages.errors),
                "sandbox": availability(),
                "removed": self.packages.removed(),
                "limits": {"apps": self.packages.MAX_APPS, "workers": 2, "pending": 2, "read_workers": 2, "read_pending": 2,
                           "composition_depth": 1, "per_app": 1, "result_bytes": 65536, "artifacts_per_app": 12}}

    def _refresh_prompt(self):
        from robbi.services.capabilities import catalog, prompt_schema
        rows=[]
        for app in self.catalog():
            if not app["enabled"]:continue
            for tool in app["tools"]:
                rows.append({"app":app["id"],"tool":tool["name"],"description":tool["description"][:500],
                             "input_schema":prompt_schema(tool["input_schema"]),"examples":tool.get("examples",[])+app.get("triggers",[])})
        self._capability_rows=tuple(rows)
        self._catalog=catalog(rows)

    def prompt_catalog(self, query=""):
        from robbi.services.capabilities import catalog
        return catalog(getattr(self,"_capability_rows",()),query) if query else self._catalog

    def discover_capabilities(self, query):
        from robbi.services.capabilities import select
        return {"tools":select(getattr(self,"_capability_rows",()),query),
                "instruction":"Schemas are data; permission and execution checks still apply."}

    def config(self, app_id):
        project = self.project(app_id)
        path = self.directory / "data" / app_id / "config.json"
        values = {}
        if path.exists():
            if path.stat().st_size > 32768:
                raise ValidationError("App configuration exceeds 32 KiB")
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, RecursionError):
                raise ValidationError("App configuration is unreadable; repair its config.json") from None
        return validate_value(project.manifest["config_schema"], values, "config")

    def detail(self, app_id):
        project = self.project(app_id)
        app = next(app for app in self.catalog() if app["id"] == app_id)
        try:
            config, error = self.config(app_id), ""
        except (ValueError, OSError) as exc:
            config, error = {}, str(exc)
        return {"app": app, "config": config, "config_schema": copy.deepcopy(project.manifest["config_schema"]), "config_error": error}

    def configure(self, app_id, changes):
        with self._lock:
            if app_id in self._active:
                raise BusyError("Wait for this app call to finish before changing its configuration")
            project = self.project(app_id)
            if not isinstance(changes, dict):
                raise ValidationError("App configuration must be an object")
            try:
                current = self.config(app_id)
            except (ValueError, OSError):
                current = {}
            schema = project.manifest["config_schema"]
            # A null patch removes an override; schema defaults are reapplied.
            # Required fields without defaults still fail validation below.
            values = validate_value(schema, patch_config(schema, current, changes), "config")
            bounded_json(values, 32768)
            write_json(self.directory / "data" / app_id / "config.json", values)
            self._health[app_id] = {"state": "ready" if self.enabled(app_id) else "disabled", "error": ""}
            return values

    def permission_snapshot(self, app_id):
        with self._lock:
            project = self.project(app_id)
            return self.permission_store.snapshot(app_id, project.manifest["permissions"])

    def set_permissions(self, app_id, decisions):
        with self._lock:
            project = self.project(app_id)
            before = self.permission_store.snapshot(app_id, project.manifest["permissions"])
            self.permission_store.decide(app_id, project.manifest["permissions"], decisions)
            after = self.permission_store.snapshot(app_id, project.manifest["permissions"])
            previous_grants = {item["id"] for item in before["permissions"] if item["status"] == "granted"}
            current_grants = {item["id"] for item in after["permissions"] if item["status"] == "granted"}
            if previous_grants - current_grants:
                # Cancel the captured epoch of current AND completed invocations:
                # a completed tool can still own an idle screen or child output.
                self._app_cancel.setdefault(app_id, threading.Event()).set()
                for job_id, record in self._jobs.items():
                    if record["app"] == app_id:
                        self.cancel(job_id)
                self._app_cancel[app_id] = threading.Event()
            return after

    def set_enabled(self, app_id, enabled):
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be true or false")
        with self._lock:
            self.project(app_id)
            if not enabled:
                self._app_cancel.setdefault(app_id, threading.Event()).set()
                for job_id, record in self._jobs.items():
                    if record["app"] == app_id:
                        self.cancel(job_id)
            elif self._app_cancel.get(app_id) is None or self._app_cancel[app_id].is_set():
                self._app_cancel[app_id] = threading.Event()
            self._enabled[app_id] = enabled
            write_json(self.directory / "enabled.json", self._enabled)
            self._health[app_id] = {"state": "ready" if enabled else "disabled", "error": ""}
            self._refresh_prompt()
            return self.detail(app_id)

    @contextmanager
    def _changing_project(self, app_id, cancellation=None, deadline=None):
        from robbi.app_services.http import checkpoint
        identifier(app_id)
        if not self._lifecycle_lock.acquire(blocking=False):
            raise BusyError("Another app installation or removal is finishing")
        try:
            with self._lock:
                if self._stopped:
                    raise UnavailableError("App runtime stopped")
                checkpoint(cancellation, deadline)
                self._changing.add(app_id)
                self._app_cancel.setdefault(app_id, threading.Event()).set()
                for job_id, record in self._jobs.items():
                    if record["app"] == app_id:
                        self.cancel(job_id)
            # Let this app's cooperative SDK work finish. Other apps keep running.
            stop_by = min(deadline, time.monotonic() + 2) if deadline is not None else time.monotonic() + 2
            while True:
                checkpoint(cancellation, deadline)
                with self._lock:
                    if app_id not in self._active:
                        break
                if time.monotonic() >= stop_by:
                    raise BusyError("The app is still stopping. Try the change again when its current call finishes")
                threading.Event().wait(.02)
            yield
        finally:
            with self._lock:
                self._changing.discard(app_id)
                if app_id in self.packages.projects and app_id not in self._active:
                    self._app_cancel[app_id] = threading.Event()
            self._lifecycle_lock.release()

    def _refresh_project(self, app_id):
        """Refresh a changed package without disturbing unrelated loaded apps."""
        cached = self._modules.pop(app_id, None)
        if cached:
            prefix = cached[1]
            for key in list(sys.modules):
                if key == prefix or key.startswith(prefix + "."):
                    sys.modules.pop(key, None)
        self._revision = uuid.uuid4().hex
        for record in self._jobs.values():
            if record["app"] == app_id:
                record.update(_undo_plan=None, _undo_status="unavailable")
        self._artifacts.pop(app_id, None)
        if app_id in self.packages.projects:
            self._app_cancel[app_id] = threading.Event()
            try:
                self.config(app_id)
                self._health[app_id] = {"state": "ready" if self.enabled(app_id) else "disabled", "error": ""}
            except (ValueError, OSError) as error:
                self._health[app_id] = {"state": "degraded", "error": str(error)[:500]}
        else:
            self._app_cancel.pop(app_id, None)
            self._health.pop(app_id, None)
        self._refresh_prompt()

    def _compatible_settings(self, app_id, manifest):
        path = self.directory / "data" / app_id / "config.json"
        if path.exists():
            try:
                if path.stat().st_size > 32768:
                    raise ValueError
                validate_value(manifest["config_schema"], json.loads(path.read_text(encoding="utf-8")), "config")
            except (OSError, ValueError, RecursionError):
                raise ValidationError("Saved settings are incompatible with this app version. Adjust its settings before updating; the existing app and data are unchanged") from None

    def install(self, data, *, cancellation=None, deadline=None):
        preview = self.packages.preview(data)
        manifest = preview["manifest"]
        app_id = manifest["id"]
        from .manifest import version_key
        if not preview["compatibility"]["compatible"]:
            raise ValidationError("App requirements are not satisfied: " + "; ".join(preview["compatibility"]["reasons"]))
        with self._lock:
            old = self.packages.projects.get(app_id)
            if old and version_key(manifest["version"]) < version_key(old.manifest["version"]):
                raise ValidationError("An app update cannot downgrade its installed version")
            self._compatible_settings(app_id, manifest)
        with self._changing_project(app_id, cancellation, deadline), self._lock:
            self._compatible_settings(app_id, manifest)
            old = self.packages.projects.get(app_id)
            if old and version_key(manifest["version"]) < version_key(old.manifest["version"]):
                raise ValidationError("An app update cannot downgrade its installed version")
            retained = set(old.manifest["permissions"]) & set(manifest["permissions"]) if old else set()
            previous_enabled = self._enabled.get(app_id)
            previous_permissions = self.permission_store.lifecycle_snapshot(app_id)
            updated = {**self._enabled, app_id: False}
            write_json(self.directory / "enabled.json", updated)
            self._enabled = updated
            try:
                self.permission_store.retain(app_id, retained)
                self.packages.install(data)
            except BaseException:
                self.permission_store.lifecycle_restore(app_id, previous_permissions)
                if previous_enabled is None:
                    self._enabled.pop(app_id, None)
                else:
                    self._enabled[app_id] = previous_enabled
                write_json(self.directory / "enabled.json", self._enabled)
                self._refresh_project(app_id)
                raise
            self._refresh_project(app_id)
            return self.detail(app_id)

    def remove(self, app_id, *, cancellation=None, deadline=None):
        with self._changing_project(app_id, cancellation, deadline), self._lock:
            self.project(app_id)
            # Clear consent before removal. Restoring is explicit and never
            # silently resurrects old grants, timers or output ownership.
            previous_permissions = self.permission_store.lifecycle_snapshot(app_id)
            previous_enabled = dict(self._enabled)
            removed = False
            try:
                self.permission_store.forget(app_id)
                outcome = self.packages.remove(app_id)
                removed = True
                self._enabled.pop(app_id, None)
                write_json(self.directory / "enabled.json", self._enabled)
            except BaseException:
                if removed:
                    self.packages.restore(app_id)
                self.permission_store.lifecycle_restore(app_id, previous_permissions)
                self._enabled = previous_enabled
                write_json(self.directory / "enabled.json", self._enabled)
                self._refresh_project(app_id)
                raise
            self._refresh_project(app_id)
            return {**outcome, "data_preserved": True}

    def restore(self, app_id, *, enable=False, cancellation=None, deadline=None):
        with self._changing_project(app_id, cancellation, deadline), self._lock:
            previous_permissions = self.permission_store.lifecycle_snapshot(app_id)
            previous_enabled = dict(self._enabled)
            try:
                self.permission_store.forget(app_id)
                self._enabled[app_id] = False
                write_json(self.directory / "enabled.json", self._enabled)
                self.packages.restore(app_id)
            except BaseException:
                self.permission_store.lifecycle_restore(app_id, previous_permissions)
                self._enabled = previous_enabled
                write_json(self.directory / "enabled.json", self._enabled)
                raise
            self._refresh_project(app_id)
            if enable:
                return self.set_enabled(app_id, True)
            return self.detail(app_id)

    def artifacts(self, app_id):
        self.project(app_id)
        with self._lock:
            if app_id not in self._artifacts:
                self._artifacts[app_id] = Artifacts(self.directory / "data" / app_id / "images", app_id)
            return self._artifacts[app_id]

    def _service(self, name):
        with self._service_lock:
            if name not in self._services:
                if name == "images":
                    from robbi.app_services.images import ImageService
                    self._services[name] = ImageService()
                elif name == "weather":
                    from robbi.app_services.weather import WeatherService
                    self._services[name] = WeatherService()
                elif name == "google":
                    from robbi.app_services.google import GoogleService
                    self._services[name] = GoogleService()
                elif name == "search":
                    from robbi.app_services.search import SearchService
                    self._services[name] = SearchService()
                elif name == "maps":
                    from robbi.app_services.maps import MapsService
                    self._services[name] = MapsService()
                else:
                    raise ValidationError("Unknown app service")
            return self._services[name]

    def _handler(self, project):
        if not project.bundled:
            raise UnavailableError('Imported apps must run in the OS sandbox')
        with self._lock:
            cached = self._modules.get(project.id)
            if cached:
                return cached[0]
        prefix = f"_robbi_app_{project.id.encode('utf-8').hex()}_{self._revision}"
        package = types.ModuleType(prefix)
        package.__path__ = [str(project.root)]
        sys.modules[prefix] = package
        module, name = project.manifest["entrypoint"].split(":")
        try:
            imported = importlib.import_module(f"{prefix}.{module}")
            handler = getattr(imported, name)
            if not callable(handler):
                raise ValidationError("App entrypoint is not callable")
        except BaseException:
            for key in list(sys.modules):
                if key == prefix or key.startswith(prefix + "."):
                    sys.modules.pop(key, None)
            raise
        with self._lock:
            self._modules[project.id] = (handler, prefix)
        return handler

    def _error(self, error, config):
        result = str(error) or type(error).__name__
        secrets = {os.getenv(name, "") for key, name in config.items()
                   if key.endswith("_env") and isinstance(name, str)}
        # Redact complete credentials before truncating, longest first so one
        # configured credential cannot expose the suffix of another.
        for secret in sorted(secrets - {""}, key=len, reverse=True):
            result = result.replace(secret, "[redacted]")
        return result[:1000]

    def _undo_metadata(self, record):
        plan = record.get("_undo_plan")
        status = record.get("_undo_status", "unavailable")
        details = plan.describe() if plan is not None else {"actions": [], "limitations": []}
        if record.get("kind") == "undo":
            reason = "Undo cannot itself be undone."
            status = "unavailable"
        elif record["id"] in self._active.values():
            reason = "Wait for this invocation to finish before undoing it."
            status = "running"
        elif status == "pending":
            reason = "Undo is already queued or running."
        elif status == "undone":
            reason = "This action has already been undone."
        elif status == "used":
            reason = "Undo was already attempted. Check the affected resource before making another change."
        elif plan is not None and plan.available:
            return {"available": True, "status": "available", "reason": "", **details}
        else:
            reason = " ".join(details["limitations"]) or "This invocation has no recorded change or compensation to undo."
            status = "unavailable"
        return {"available": False, "status": status, "reason": reason, **details}

    def _history_item(self, record):
        return {key: copy.deepcopy(record.get(key)) for key in
                ("id", "app", "tool", "kind", "source", "state", "created_at", "completed_at", "undo_of")} | {
                    "arguments_ref": record["id"], "result_ref": record["id"], "undo": self._undo_metadata(record),
                    "context_valid": not any(event.is_set() for event in record.get("_receipt_epochs", []) + record.get("_dependencies", [])),
                    # Read evidence/artifacts survive a worker deadline, not Stop or app invalidation.
                    "evidence_context_valid": not any(event.is_set() for event in record.get("_receipt_epochs", [])[:2] + record.get("_dependencies", [])),
                    "artifact_context_valid": not any(event.is_set() for event in record.get("_receipt_epochs", [])[:2] + record.get("_dependencies", []))}

    def history(self, app_id=None, source=None):
        """Newest-first metadata only; arguments and result bodies remain private."""
        with self._lock:
            if app_id is not None:
                identifier(app_id)
            return [self._history_item(record) for record in reversed(self._jobs.values())
                    if (app_id is None or record["app"] == app_id) and (source is None or record["source"] == source)]

    def invocation(self, job_id, source=None):
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or (source is not None and record["source"] != source):
                raise ValidationError("App invocation is unknown or expired for this source")
            return {**self._history_item(record), "arguments": copy.deepcopy(record.get("_arguments", {})),
                    "result": copy.deepcopy(record.get("result")), "error": record.get("error")}

    def submit_undo(self, job_id=None, *, source="ui_text", cancellation=None, deadline=None):
        with self._lock:
            if job_id is None:
                target = next((item for item in reversed(self._jobs.values())
                    if item["source"] == source and item.get("kind") != "undo"), None)
            else:
                target = self._jobs.get(job_id)
            if target is None:
                raise ValidationError("No app invocation is available to undo in this session")
            if job_id is not None and source != "ui_text" and target["source"] != source:
                raise ValidationError("That invocation belongs to another conversation or source")
            # An explicit ID is a dashboard selection; automatic conversational
            # undo is restricted to the exact source above.
            metadata = self._undo_metadata(target)
            if not metadata["available"]:
                raise ValidationError(metadata["reason"])
            return self._submit(target["app"], "undo", {}, source=source, cancellation=cancellation,
                                deadline=deadline, undo_target=target)

    def submit(self, app_id, tool_name, arguments, *, source="ui_text", cancellation=None, deadline=None):
        return self._submit(app_id, tool_name, arguments, source=source, cancellation=cancellation, deadline=deadline)

    def _compose_read(self, parent, app_id, tool, arguments, signal, deadline):
        if app_id == parent["app"]:
            raise ValidationError("An app cannot compose itself")
        job = self._submit(app_id, tool, arguments, source="compose:" + parent["id"],
                           cancellation=signal, deadline=deadline, composed=True)
        with self._lock:
            # Retain consent/enablement epochs after the child has completed.
            # Revocation invalidates both ongoing collection and later context.
            parent["_dependencies"].extend(self._jobs[job["id"]]["_receipt_epochs"])
        return self._wait_for_job(job)

    def _submit(self, app_id, tool_name, arguments, *, source, cancellation, deadline, undo_target=None, composed=False):
        with self._lock:
            if self._stopped:
                raise UnavailableError("App runtime stopped")
            if app_id in self._changing:
                raise BusyError("This app is being updated or removed")
            project = self.project(app_id)
            if not self.enabled(app_id):
                raise UnavailableError("App is disabled; enable this trusted project in the app library")
            if not isinstance(source, str) or not 1 <= len(source) <= 200:
                raise ValidationError("Invocation source must contain 1 to 200 characters")
            if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
                raise ValidationError("Invocation deadline must be a finite monotonic timestamp")
            if cancellation is not None and not callable(cancellation):
                raise ValidationError("Invocation cancellation must be callable")
            tool = ({"timeout_s": 65, "input_schema": {"type": "object", "properties": {}}} if undo_target is not None else
                    next((tool for tool in project.manifest["tools"] if tool["name"] == tool_name), None))
            if tool is None:
                raise ValidationError("Unknown app tool")
            if composed and not tool.get("read_only", False):
                raise ValidationError("Only declared read-only tools can be composed")
            # Models commonly encode spoken square/guess numbers as JSON integers.
            # Coerce only this reviewed game field; all other schemas stay strict.
            if (project.bundled and app_id in {'tic-tac-toe', 'higher-lower'} and tool_name == 'play'
                    and isinstance(arguments, dict) and type(arguments.get('answer')) is int
                    and -999 <= arguments['answer'] <= 999):
                arguments = dict(arguments, answer=str(arguments['answer']))
            arguments = bounded_json(validate_value(tool["input_schema"], arguments), 32768)
            config = self.config(app_id)
            provider_config = (self._read_object(self.directory / "data" / app_id / "config.json")
                               if not project.bundled else {})
            if app_id in self._active:
                raise BusyError("This app already has a queued or running call")
            if sum(bool(self._jobs[key].get("_composed")) == composed for key in self._active.values()) >= 4:
                raise BusyError("App workers and pending queue are full")
            while len(self._jobs) >= 32:
                terminal = next((key for key, value in self._jobs.items()
                    if key not in self._active.values() and value.get("_undo_status") != "pending" and value is not undo_target), None)
                if terminal is None:
                    raise BusyError("App job history is full")
                del self._jobs[terminal]
            started = time.monotonic()
            deadline = min(started + tool["timeout_s"], deadline) if deadline is not None else started + tool["timeout_s"]
            token = self.robot.capture_token()
            app_cancel = self._app_cancel.setdefault(app_id, threading.Event())
            with self._cancel_lock:
                stop_epoch = self._cancel_epoch
            receipt_epochs = [stop_epoch, app_cancel] + ([token[1]] if token else [])
            dependencies = []
            external = lambda: bool(any(event.is_set() for event in receipt_epochs + dependencies) or (cancellation and cancellation()))
            signal = Cancellation(threading.Event(), external, deadline)
            if signal():
                raise AppCancelled("App request already cancelled or expired")
            job_id = uuid.uuid4().hex
            record = {"id": job_id, "app": app_id, "tool": tool_name, "state": "queued", "result": None,
                      "error": None, "created_at": time.time(), "completed_at": None, "source": source,
                      "kind": "undo" if undo_target is not None else "tool", "undo_of": undo_target["id"] if undo_target else None,
                      "_arguments": copy.deepcopy(arguments), "_signal": signal, "_future": None,
                      "_composed": composed,
                      "_receipt_epochs": receipt_epochs, "_dependencies": dependencies,
                      "_completed": threading.Event(), "_undo_plan": None if undo_target else UndoPlan(), "_undo_status": "ready"}
            if undo_target is not None:
                undo_target["_undo_status"] = "pending"
            self._jobs[job_id], self._active[app_id] = record, job_id

        def run():
            try:
                if signal() or time.monotonic() - started > 10:
                    raise AppCancelled("Queued app request expired; submit it again")
                with self._lock:
                    record["state"] = "running"
                context = AppContext(app_id=app_id, config=config, permissions=project.manifest["permissions"],
                                     directory=self.directory / "data" / app_id, artifacts=self.artifacts(app_id),
                                     cancelled=signal, deadline=deadline, robot=self.robot, robot_token=token, services=self._service,
                                     project_root=project.root, provider_config=provider_config,
                                     permission_check=lambda permission: self.permission_store.require(app_id, permission, project.manifest["permissions"]),
                                     permission_check_many=lambda permissions: self.permission_store.require_many(app_id, permissions, project.manifest["permissions"]),
                                     undo_plan=record["_undo_plan"], read_only=composed,
                                     app_reader=lambda target, name, values: self._compose_read(record, target, name, values, signal, deadline))
                context.checkpoint()
                from .presentation import AppPresentation
                presentation = (AppPresentation(context, project.manifest, trusted=project.bundled, arguments=arguments)
                                if undo_target is None else None)
                if undo_target is not None:
                    result = undo_target["_undo_plan"].execute(context)
                elif not project.bundled:
                    from .sandbox import run as run_isolated
                    result = run_isolated(project, context, tool_name, arguments)
                else:
                    result = self._handler(project)(context, tool_name, arguments)
                context.checkpoint()
                if not isinstance(result, dict):
                    raise ValidationError("App tool must return a JSON object")
                result = bounded_json(result)
                result.setdefault("ok", True)
                if undo_target is None:
                    try:
                        # Cosmetic metadata must never turn a valid completed
                        # business result into a result-size validation failure.
                        result = bounded_json(presentation.finish(copy.deepcopy(result)))
                    except ValidationError:
                        context.checkpoint()
                context.checkpoint()
                with self._lock:
                    if signal():
                        raise AppCancelled("App completed after cancellation")
                    record.update(state="done", result=result)
                    self._health[app_id] = {"state": "ready" if result["ok"] else "degraded", "error": "" if result["ok"] else str(result.get("text", "App needs attention"))[:500]}
                if not composed and undo_target is None:
                    self._record_game_turn(project, tool_name, result, source, receipt_epochs)
                return result
            except BaseException as error:
                cancelled = isinstance(error, AppCancelled) or signal()
                busy = isinstance(error, ResourceBusy) and not cancelled
                permission_required = isinstance(error, AppPermissionRequired) and not cancelled
                message = "App cancelled or its deadline expired" if cancelled else self._error(error, config)
                result = {"ok": False, "cancelled": cancelled, "text": message, "error": message}
                if busy:
                    result.update(busy=True, **bounded_json(error.details))
                if permission_required:
                    result.update(**bounded_json(error.details))
                if isinstance(error, UndoConflict):
                    result["conflict"] = True
                with self._lock:
                    record.update(state="cancelled" if cancelled else "failed", error=message, result=result if busy or permission_required else None)
                    if not cancelled and not busy and not permission_required:
                        self._health[app_id] = {"state": "degraded", "error": message}
                return result

        try:
            future = (self._read_executor if composed else self._executor).submit(run)
            record["_future"] = future
        except BaseException:
            with self._lock:
                self._active.pop(app_id, None)
                self._jobs.pop(job_id, None)
                if undo_target is not None:
                    undo_target["_undo_status"] = "ready"
            raise

        def complete(done):
            with self._lock:
                self._active.pop(app_id, None)
                if done.cancelled():
                    record.update(state="cancelled", error="App cancelled before execution")
                record["completed_at"] = time.time()
                if undo_target is not None:
                    plan = undo_target["_undo_plan"]
                    outcome = record.get("result") or {}
                    undo_target["_undo_status"] = ("undone" if outcome.get("ok") else "used") if plan.attempted else "ready"
                record["_completed"].set()
        future.add_done_callback(complete)
        return self.job(job_id)

    def job(self, job_id):
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise ValidationError("App job is unknown or has expired from history")
            if record["state"] in {"queued", "running"} and record["_signal"]():
                self.cancel(job_id)
            return copy.deepcopy({key: value for key, value in record.items() if not key.startswith("_")}) | {"undo": self._undo_metadata(record)}

    def cancel(self, job_id):
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise ValidationError("Unknown app job")
            if record["state"] in {"queued", "running"}:
                record["_signal"].event.set()
                record.update(state="cancelled", error="App cancelled")
                if record["_future"]:
                    record["_future"].cancel()
            return self.job(job_id)

    def cancel_all(self):
        # Stop must never wait for discovery, archive extraction or SD writes
        # under the host lock. Every queued/running/completed SDK action carries
        # this epoch, so one cheap swap invalidates delayed work too.
        with self._cancel_lock:
            self._cancel_epoch.set()
            self._cancel_epoch = threading.Event()

    def _wait_for_job(self, job, *, include_invocation=False):
        with self._lock:
            record = self._jobs[job["id"]]
        future, signal = record["_future"], record["_signal"]
        result = None
        while not future.done():
            if signal():
                self.cancel(job["id"])
                result = {"ok": False, "cancelled": True, "text": "App cancelled or its deadline expired"}
                break
            signal.wait(.05)
        if result is None:
            if future.cancelled() or signal():
                result = {"ok": False, "cancelled": True, "text": "App cancelled or its deadline expired"}
            else:
                result = future.result()
                record["_completed"].wait(1)
        if include_invocation:
            with self._lock:
                result = {**result, "invocation": self._history_item(record)}
        return result

    def call(self, app_id, tool, arguments, *, source="ui_text", cancellation=None, deadline=None, include_invocation=False):
        job = self.submit(app_id, tool, arguments, source=source, cancellation=cancellation, deadline=deadline)
        return self._wait_for_job(job, include_invocation=include_invocation)

    def call_readonly(self, app_id, tool, arguments, *, source="ui_text", cancellation=None, deadline=None, include_invocation=False):
        """Reuse composed-tool restrictions for model-selected continuation reads."""
        job = self._submit(app_id, tool, arguments, source=source, cancellation=cancellation,
                           deadline=deadline, composed=True)
        return self._wait_for_job(job, include_invocation=include_invocation)

    def undo(self, job_id=None, *, source="ui_text", cancellation=None, deadline=None):
        try:
            job = self.submit_undo(job_id, source=source, cancellation=cancellation, deadline=deadline)
        except (ValidationError, UnavailableError) as error:
            return {"ok": False, "text": str(error), "undo_available": False}
        return self._wait_for_job(job, include_invocation=True)

    def stop(self):
        self._stopped = True
        self.cancel_all()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._read_executor.shutdown(wait=False, cancel_futures=True)
