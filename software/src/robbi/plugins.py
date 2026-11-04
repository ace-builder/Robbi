"""Opt-in trusted Python app API. Plugins return validated robot actions.

Entry points are code installed by the operator, not a security sandbox. YAML
app uploads never load Python. Slow plugins occupy one bounded command slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
import json
import re
import threading

from .actions import validate_action
from .errors import ValidationError
from .storage import write_json


@dataclass(frozen=True)
class AppContext:
    """A small stable interface; avoid coupling new apps to RobotController."""
    app_id: str
    state_directory: object

    def read_state(self):
        path = self.state_directory / f"{self.app_id}.json"
        if not path.exists():
            return {}
        if path.stat().st_size > 65536:
            raise ValidationError("App state exceeds 64 KiB")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValidationError("App state must be an object")
        return data

    def write_state(self, state: dict):
        if not isinstance(state, dict) or len(json.dumps(state, allow_nan=False).encode()) > 65536:
            raise ValidationError("App state must be an object of at most 64 KiB")
        write_json(self.state_directory / f"{self.app_id}.json", state)


@dataclass(frozen=True)
class AppSpec:
    id: str
    title: str
    description: str
    run: object


class PluginRegistry:
    def __init__(self, enabled, data_dir, health):
        self._enabled = set(enabled)
        self._data_dir = data_dir / "apps"
        self._health = health
        self._apps = {}
        self._lock = threading.RLock()

    def load(self):
        discovered = set()
        for entry in entry_points(group="robbi.apps"):
            if entry.name not in self._enabled:
                continue
            discovered.add(entry.name)
            try:
                app = entry.load()()
                if (not isinstance(app, AppSpec) or not isinstance(app.id, str)
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", app.id) or app.id != entry.name or not callable(app.run)
                    or not isinstance(app.title, str) or not 1 <= len(app.title) <= 120
                    or not isinstance(app.description, str) or len(app.description) > 2000):
                    raise ValidationError("Invalid AppSpec or mismatched entry point ID")
                with self._lock:
                    self._apps[app.id] = app
                self._health.set(f"app:{app.id}", "ready", app.title)
            except Exception as error:
                self._health.set(f"app:{entry.name}", "failed", str(error), "Fix or disable this trusted extension")
        for missing in self._enabled - discovered:
            self._health.set(f"app:{missing}", "failed", "Enabled app could not load", "Install its Python package or remove it from plugins.enabled")

    def catalog(self):
        with self._lock:
            return [{"id": app.id, "title": app.title, "description": app.description} for app in self._apps.values()]

    def run(self, app_id, text):
        if not isinstance(app_id, str):
            raise ValidationError("App ID must be text")
        with self._lock:
            app = self._apps.get(app_id)
        if app is None:
            raise ValidationError("App is not installed and enabled")
        if not isinstance(text, str) or len(text) > 12000:
            raise ValidationError("App input must be text of at most 12000 characters")
        try:
            result = validate_action(app.run(AppContext(app_id, self._data_dir), text))
        except Exception as error:
            self._health.set(f"app:{app_id}", "degraded", str(error), "The app failed; other apps remain available")
            raise
        self._health.set(f"app:{app_id}", "ready", app.title)
        return result

