"""Owner-requested app lifecycle operations; never run by an app SDK facade."""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from robbi.app_services.http import checkpoint
from robbi.actions import validate_app_manager
from robbi.errors import BusyError, ValidationError, UnavailableError
from robbi.storage import write_json
from .schema import identifier
from .manifest import version_key
from .store_client import StoreClient, StoreNotConfigured


OPERATIONS = {"search", "info", "install", "update", "remove", "restore", "enable", "disable"}


def result(ok, state, text, **data):
    return {"ok": ok, "state": state, "text": text, "data": data}


class AppManager:
    def __init__(self, host, data_dir, *, store=None):
        self.host = host
        self.directory = Path(data_dir) / "app-management"
        self.store = store or StoreClient(self.directory / "store.json")
        self._mutations, self._lock = threading.Lock(), threading.RLock()
        self._epoch, self._closed = threading.Event(), False
        self._origins, self._audit, self._error = {}, [], ""
        try:
            path = self.directory / "installed.json"
            with path.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("apps"), dict)
                    or len(data["apps"]) > 128):
                raise ValueError
            for name, origin in data["apps"].items():
                identifier(name)
                if (not isinstance(origin, dict) or set(origin) != {"store_id", "public_key", "sha256", "version"}
                        or any(not isinstance(value, str) or not 1 <= len(value) <= 120 for value in origin.values())):
                    raise ValueError
            self._origins = data["apps"]
        except FileNotFoundError:
            pass
        except (ValueError, OSError, TypeError, RecursionError):
            self._error = "App installation provenance is unreadable; repair app-management/installed.json before store changes"

    def cancel_all(self):
        with self._lock:
            self._epoch.set()
            self._epoch = threading.Event()

    def stop(self):
        with self._lock:
            self._closed = True
            self._epoch.set()

    def status(self):
        store_status = self.store.status()
        return {**store_status, "error": self._error or store_status["error"],
                "removed": self.host.packages.removed(), "busy": self._mutations.locked()}

    def _change_store(self, callback, *, cancelled=None, deadline=None):
        """Trust changes and package mutations share one admission lock."""
        if not self._mutations.acquire(blocking=False):
            raise BusyError("Wait for the current app or store change to finish")
        try:
            with self._lock:
                if self._closed:
                    raise UnavailableError("App management is stopped")
                epoch = self._epoch
            signal = lambda: epoch.is_set() or bool(cancelled and cancelled())
            limit = time.monotonic() + 15
            deadline = min(limit, deadline) if deadline is not None else limit
            checkpoint(signal, deadline)
            return callback(signal, deadline)
        finally:
            self._mutations.release()

    def connect_store(self, url, public_key, *, allow_http=False, cancelled=None, deadline=None):
        return self._change_store(lambda signal, limit: self.store.connect(url, public_key,
            allow_http=allow_http, cancelled=signal, deadline=limit), cancelled=cancelled, deadline=deadline)

    def disconnect_store(self, *, cancelled=None, deadline=None):
        return self._change_store(lambda signal, limit: self.store.disconnect(), cancelled=cancelled, deadline=deadline)

    def _same_store(self, expected):
        current = self.store.status()["store"]
        return bool(current and expected and all(current.get(key) == expected.get(key)
                                                for key in ("url", "store_id", "public_key")))

    def _remember(self, operation, reply, source):
        entry = {"at": datetime.now(timezone.utc).isoformat(), "operation": operation,
                 "state": reply["state"], "ok": reply["ok"], "app": reply.get("data", {}).get("app_id"),
                 "source": str(source)[:200]}
        with self._lock:
            self._audit = [*self._audit[-31:], entry]
        return reply

    @staticmethod
    def _match(spec, apps):
        target = (spec.get("app_id") or spec.get("query") or "").strip().casefold()
        if not target:
            return None, result(False, "needs_target", "Name the app you want to manage.")
        # An explicitly selected ID is never a display-name search. Otherwise a
        # publisher can make an unrelated app ambiguous by copying that ID as a name.
        matches = ([item for item in apps if item["id"] == spec["app_id"]] if spec.get("app_id")
                   else [item for item in apps if target in {item["id"].casefold(), item.get("name", "").casefold()}])
        if len(matches) == 1:
            return matches[0], None
        suggestions = matches or [item for item in apps if target in (item["id"] + " " + item.get("name", "")).casefold()]
        choices = [{key: item.get(key) for key in ("id", "name", "version")} for item in suggestions[:12]]
        return None, result(False, "ambiguous" if suggestions else "not_found",
                            "Choose an exact app name or ID: " + ", ".join(item["id"] for item in choices) if choices
                            else "No app matches that name. Search the Robbi App Hub to find its exact name.", matches=choices)

    def browse(self, *, query="", refresh=False, cancelled=None, deadline=None):
        installed = {app["id"]: app for app in self.host.catalog()}
        payload = self.store.catalog(refresh=refresh, cancelled=cancelled, deadline=deadline)
        query = query.strip().casefold()
        rows = []
        for item in payload["apps"]:
            if query and query not in " ".join((item["id"], item["name"], item["description"], " ".join(item.get("tags", [])))).casefold():
                continue
            local = installed.get(item["id"])
            rows.append({**item, "installed": local is not None, "installed_version": local["version"] if local else None,
                         "enabled": local["enabled"] if local else False})
        return {"apps": rows, "store": self.store.status()["store"], "revision": payload["revision"]}

    def execute(self, spec, *, source="ui_text", user_text="", cancelled=None, deadline=None):
        try:
            spec = validate_app_manager(spec)
        except ValidationError as error:
            return result(False, "invalid", str(error))
        operation = spec["operation"]
        mutation = operation not in {"search", "info"}
        acquired = False
        try:
            with self._lock:
                if self._closed:
                    raise UnavailableError("App management is stopped")
                epoch = self._epoch
            signal = lambda: epoch.is_set() or bool(cancelled and cancelled())
            deadline = min(deadline, time.monotonic() + 90) if deadline is not None else time.monotonic() + 90
            checkpoint(signal, deadline)
            if mutation:
                acquired = self._mutations.acquire(blocking=False)
                if not acquired:
                    raise BusyError("Another app change is finishing; try again shortly")
            reply = self._execute(spec, signal, deadline)
        except StoreNotConfigured as error:
            reply = result(False, "store_not_configured", str(error))
        except (CancelledError, TimeoutError):
            reply = result(False, "cancelled", "App management cancelled or timed out. Check the library before trying again.")
        except BusyError as error:
            reply = result(False, "busy", str(error))
        except (ValidationError, UnavailableError) as error:
            reply = result(False, "unavailable", str(error))
        except OSError:
            reply = result(False, "storage_error", "App files could not be saved. Check available storage and the app library before trying again.")
        finally:
            if acquired:
                self._mutations.release()
        return self._remember(operation, reply, source)

    def _execute(self, spec, cancelled, deadline):
        operation = spec["operation"]
        local = self.host.catalog()
        if operation == "search":
            query = spec.get("query", "").strip().casefold()
            matches = [dict(id=app["id"], name=app["name"], version=app["version"], installed=True, enabled=app["enabled"])
                       for app in local if query in (app["id"] + " " + app["name"] + " " + app["description"]).casefold()]
            warning = ""
            try:
                items = self.browse(query=query, cancelled=cancelled, deadline=deadline)["apps"]
                seen = {item["id"] for item in matches}
                matches.extend({key: app.get(key) for key in ("id", "name", "version", "installed", "enabled")}
                               for app in items if app["id"] not in seen)
            except (StoreNotConfigured, UnavailableError, ValidationError, TimeoutError) as error:
                warning = str(error)
            return result(True, "results", ("Apps: " + ", ".join(item["name"] for item in matches[:40]) + "." if matches else "No matching apps.")
                          + (" " + warning if warning else ""), matches=matches[:40], total=len(matches), store_warning=warning)
        if operation in {"remove", "enable", "disable"}:
            app, refusal = self._match(spec, local)
            if refusal:
                return refusal
            checkpoint(cancelled, deadline)
            if operation == "remove":
                outcome = self.host.remove(app["id"], cancellation=cancelled, deadline=deadline)
                return result(True, "removed", f"Removed {app['name']} from Robbi. Saved data is kept; use Restore to add it back.", app_id=app["id"], **outcome)
            self.host.set_enabled(app["id"], operation == "enable")
            return result(True, "enabled" if operation == "enable" else "disabled", f"{app['name']} is now {'enabled' if operation == 'enable' else 'disabled'}.", app_id=app["id"])
        if operation in {"restore", "install"} and not spec.get("version"):
            removed = self.host.packages.removed()
            app, refusal = self._match(spec, removed)
            if app:
                checkpoint(cancelled, deadline)
                detail = self.host.restore(app["id"], enable=True, cancellation=cancelled, deadline=deadline)
                return result(True, "restored", f"Restored {app['name']}. Saved data is available and permissions can be reviewed in the app page.", app_id=app["id"], app=detail["app"])
            if operation == "restore":
                return refusal
        if operation == "info":
            app, refusal = self._match(spec, local)
            if app and (not spec.get("version") or spec["version"] == app["version"]):
                return result(True, "info", f"{app['name']}: {app['description']}", app=self.host.detail(app["id"])["app"], installed=True)
        if self._error:
            raise ValidationError(self._error)
        catalog = self.store.catalog(cancelled=cancelled, deadline=deadline)["apps"]
        app, refusal = self._match(spec, catalog)
        if refusal:
            return refusal
        if spec.get("version"):
            app = self.store.app(app["id"], version=spec["version"], cancelled=cancelled, deadline=deadline)
        if operation == "info":
            return result(True, "info", f"{app['name']}: {app['description']}", app=app, installed=False)
        current = next((item for item in local if item["id"] == app["id"]), None)
        if operation == "update" and current is None:
            return result(False, "not_installed", "That app is not installed. Ask to install it first.", app_id=app["id"])
        if current and operation == "install":
            return result(True, "already_installed", f"{current['name']} is already installed. Use update to change its version.", app_id=app["id"])
        if current and current["version"] == app["version"]:
            return result(True, "up_to_date", f"{app['name']} already has version {app['version']}.", app_id=app["id"])
        store = self.store.status()["store"]
        origin = self._origins.get(app["id"])
        if origin and any(origin[key] != store[key] for key in ("store_id", "public_key")):
            raise ValidationError("This app came from another store. Reconnect its original store or review a manual ZIP import before changing publishers")
        if current and version_key(app["version"]) < version_key(current["version"]):
            # Package installation forbids downgrades even with an explicit
            # version. Refuse before changing enablement or retained consent.
            raise ValidationError("App updates cannot downgrade the installed version. Choose the installed version or a newer release")
        data = self.store.download(app, cancelled=cancelled, deadline=deadline)
        preview = self.host.packages.preview(data)
        manifest = preview["manifest"]
        if any(app.get(key) != value for key, value in manifest.items()):
            raise ValidationError("Package manifest differs from its signed store listing")
        if not self._same_store(store):
            raise ValidationError("Store connection changed before installation; review the current store and run the command again")
        checkpoint(cancelled, deadline)
        # Validation and extraction do not execute Python. Leave the package
        # disabled until its provenance is durable; grants are never automated.
        # Owner routes cannot change trust concurrently. The callback also fails
        # closed if a trusted internal caller replaces the StoreClient directly.
        install_cancelled = lambda: cancelled() or not self._same_store(store)
        detail = self.host.install(data, cancellation=install_cancelled, deadline=deadline)
        with self._lock:
            updated = {**self._origins, app["id"]: {"store_id": store["store_id"], "public_key": store["public_key"],
                       "sha256": app["sha256"], "version": app["version"]}}
            write_json(self.directory / "installed.json", {"version": 1, "apps": updated})
            self._origins = updated
        # A Stop after committing code leaves it safely installed and disabled.
        checkpoint(install_cancelled, deadline)
        detail = self.host.set_enabled(app["id"], current["enabled"] if current else True)
        verb = "Updated" if current else "Installed"
        return result(True, "updated" if current else "installed", f"{verb} {app['name']} {app['version']}. Open its app page to use it and review any new permissions.",
                      app_id=app["id"], app=detail["app"], permissions_automatically_granted=False)
