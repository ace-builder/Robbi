"""Durable SDK consent; sandbox.py provides OS isolation for imported code."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from robbi.errors import ValidationError
from robbi.storage import atomic_write
from .manifest import PERMISSIONS
from .schema import identifier

CAPABILITIES = {
    "apps.read": ("Read other apps", "Read selected tools using their saved configuration and existing permissions, including connected account data."),
    "robot.microphone": ("Clap listening", "Listen for up to eight seconds on request and return clap timings. Audio is not saved or sent to a provider."),
    "robot.reminders": ("Kitchen timers", "Schedule, list and cancel kitchen timers with audible reminders, including after restart."),
    "network": ("Configured cloud services", "Use configured weather, search, image and other provider services; generated content may incur charges."),
    "account.gmail": ("Gmail account", "Search/read connected Gmail messages and create drafts; does not send mail."),
    "account.calendar": ("Calendar account", "Read connected appointments and create calendar events."),
    "state": ("Saved app data", "Read and write this app's persistent data."),
    "robot.status": ("Robot health", "Read device availability and health, without sensor measurements."),
    "robot.speak": ("Speech", "Generate speech and play it through Robbi's speaker."),
    "robot.display": ("Display", "Create image previews and show images, text, faces and video on Robbi's display."),
    "robot.camera": ("Camera images", "Read recent camera images and their capture metadata."),
    "robot.motion": ("Driving", "Request bounded motor movement through Robbi's safety controls."),
    "robot.tof": ("Distance sensors", "Read fresh ToF distance measurements."),
    "robot.imu": ("Motion sensor", "Read acceleration, angular velocity and relative orientation."),
    "robot.audio": ("Sound playback", "Play sound files and video audio through Robbi's speaker."),
    "robot.assets": ("Asset names", "List available face, sound and video asset names."),
}
_STORED = frozenset({"pending", "granted", "denied"})


def _json_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValidationError("Duplicate permission file key")
        value[key] = child
    return value


class AppPermissionRequired(Exception):
    """Ends an invocation without consuming a worker while approval is pending."""

    def __init__(self, app_id, permissions, *, denied=False, text=None):
        permissions = sorted(set(permissions))
        state = "permission_denied" if denied else "permission_required"
        labels = ", ".join(CAPABILITIES.get(permission, (permission, ""))[0] for permission in permissions)
        text = text or (f"The owner denied permission for {app_id}: {labels}. " if denied
                        else f"Owner approval is required for {app_id}: {labels}. ") + (
                            f"Open App library → {app_id} → App permissions, review access, then run it again.")
        self.details = {"permission_required": True, "state": state, "app": app_id,
                        "permissions": permissions, "text": text}
        super().__init__(text)


class PermissionStore:
    MAX_APPS = 48
    MAX_BYTES = 65536

    def __init__(self, path, enabled=True):
        if not isinstance(enabled, bool):
            raise ValidationError("Permission enforcement must be true or false")
        self.path, self.enabled = Path(path), enabled
        self._lock = threading.RLock()
        self._apps = {}
        self._error = ""
        try:
            with self.path.open("rb") as source:
                encoded = source.read(self.MAX_BYTES + 1)
            if len(encoded) > self.MAX_BYTES:
                raise ValidationError("Permission file exceeds 64 KiB")
            self._apps = self._validate_document(json.loads(encoded, object_pairs_hook=_json_object))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError) as error:
            self._error = f"Permission decisions could not be read ({type(error).__name__}). No stored approvals are trusted."

    @classmethod
    def _validate_document(cls, value):
        if (not isinstance(value, dict) or set(value) != {"version", "apps"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["apps"], dict) or len(value["apps"]) > cls.MAX_APPS):
            raise ValidationError("Invalid permission store")
        apps = {}
        for app_id, decisions in value["apps"].items():
            identifier(app_id)
            if not isinstance(decisions, dict) or len(decisions) > len(PERMISSIONS):
                raise ValidationError("Invalid app permission decisions")
            if any(permission not in PERMISSIONS or not isinstance(state, str) or state not in _STORED
                   for permission, state in decisions.items()):
                raise ValidationError("Unknown permission decision")
            if decisions:
                apps[app_id] = dict(decisions)
        return apps

    @staticmethod
    def _permissions(values, name):
        if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > len(PERMISSIONS):
            raise ValidationError(f"{name} must be a bounded collection of supported permissions")
        if any(not isinstance(item, str) or item not in PERMISSIONS for item in values):
            raise ValidationError(f"{name} contains an unknown app permission")
        return frozenset(values)

    def _publish(self, apps):
        # Validate and persist before replacing the memory snapshot. Failed writes
        # never create a grant that disappears after restart.
        document = {"version": 1, "apps": apps}
        cleaned = self._validate_document(document)
        encoded = json.dumps({"version": 1, "apps": cleaned}, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.MAX_BYTES:
            raise ValidationError("Permission file exceeds 64 KiB")
        try:
            atomic_write(self.path, encoded)
        except OSError as error:
            self._error = f"Permission decisions could not be saved ({type(error).__name__}). The requested change was not applied."
            raise
        self._apps, self._error = cleaned, ""

    def require(self, app_id, permission, declared):
        return self.require_many(app_id, (permission,), declared)

    def require_many(self, app_id, permissions, declared):
        app_id = identifier(app_id)
        requested = self._permissions(permissions, "Requested permissions")
        if not self.enabled:
            return
        declared = self._permissions(declared, "Declared permissions")
        if not requested <= declared:
            raise ValidationError("App does not declare permission: " + ", ".join(sorted(requested-declared)))
        if not requested:
            return
        with self._lock:
            decisions = self._apps.get(app_id, {})
            missing = [permission for permission in requested if decisions.get(permission) != "granted"]
            if not missing:
                return
            denied = any(decisions.get(permission) == "denied" for permission in missing)
            if self._error:
                raise AppPermissionRequired(app_id, missing, denied=denied, text=self._error)
            new = [permission for permission in missing if permission not in decisions]
            if new:
                updated = {name: dict(values) for name, values in self._apps.items()}
                updated.setdefault(app_id, {}).update({permission: "pending" for permission in new})
                try:
                    self._publish(updated)
                except OSError:
                    raise AppPermissionRequired(app_id, missing, denied=denied, text=self._error) from None
            raise AppPermissionRequired(app_id, missing, denied=denied)

    def snapshot(self, app_id, declared):
        app_id = identifier(app_id)
        declared = self._permissions(declared, "Declared permissions")
        with self._lock:
            decisions = self._apps.get(app_id, {})
            permissions = []
            for permission in sorted(declared if self.enabled else PERMISSIONS):
                label, description = CAPABILITIES[permission]
                permissions.append({"id": permission, "label": label, "description": description,
                                    "status": decisions.get(permission, "unrequested") if self.enabled else "bypassed"})
            return {"enforced": self.enabled, "permissions": permissions,
                    "pending": [item["id"] for item in permissions if item["status"] == "pending"],
                    "error": self._error}

    def decide(self, app_id, declared, decisions):
        app_id = identifier(app_id)
        declared = self._permissions(declared, "Declared permissions")
        if not isinstance(decisions, dict) or not 1 <= len(decisions) <= len(PERMISSIONS):
            raise ValidationError("Supply a bounded nonempty permission decision object")
        if any(permission not in declared or not isinstance(state, str)
               or state not in {"granted", "denied", "unrequested"} for permission, state in decisions.items()):
            raise ValidationError("Decisions must use declared permissions and granted, denied or unrequested")
        with self._lock:
            if not self.enabled:
                # Debug mode is temporary and must not silently rewrite consent.
                return self.snapshot(app_id, declared)
            updated = {name: dict(values) for name, values in self._apps.items()}
            app_decisions = updated.setdefault(app_id, {})
            for permission, state in decisions.items():
                if state == "unrequested":
                    app_decisions.pop(permission, None)
                else:
                    app_decisions[permission] = state
            if not app_decisions:
                updated.pop(app_id, None)
            self._publish(updated)
            return self.snapshot(app_id, declared)

    def forget(self, app_id):
        app_id = identifier(app_id)
        with self._lock:
            if app_id not in self._apps:
                return
            updated = {name: dict(values) for name, values in self._apps.items() if name != app_id}
            self._publish(updated)

    def retain(self, app_id, permissions):
        """An update keeps consent only for capabilities in both versions."""
        app_id = identifier(app_id)
        allowed = self._permissions(permissions, "Retained permissions")
        with self._lock:
            existing = self._apps.get(app_id, {})
            retained = {key: value for key, value in existing.items() if key in allowed}
            if retained == existing:
                return
            updated = {name: dict(values) for name, values in self._apps.items() if name != app_id}
            if retained:
                updated[app_id] = retained
            self._publish(updated)

    def lifecycle_snapshot(self, app_id):
        """Private host transaction receipt; never exposed through the app SDK."""
        identifier(app_id)
        with self._lock:
            return dict(self._apps.get(app_id, {}))

    def lifecycle_restore(self, app_id, decisions):
        identifier(app_id)
        self._validate_document({"version": 1, "apps": {app_id: decisions}})
        with self._lock:
            if self._apps.get(app_id, {}) == decisions:
                return
            updated = {key: dict(value) for key, value in self._apps.items() if key != app_id}
            if decisions:
                updated[app_id] = dict(decisions)
            self._publish(updated)
