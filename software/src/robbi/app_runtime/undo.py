"""Session-only bounded undo receipts; never execute a handler to guess reversal."""
from __future__ import annotations

import copy
import hashlib
import json

from robbi.errors import ValidationError
from robbi.storage import atomic_write
from .manifest import PERMISSIONS
from .schema import bounded_json


class UndoConflict(ValidationError):
    """The recorded effect no longer matches the current resource."""


def state_fingerprint(value):
    # JSON booleans must not compare equal to numeric 0/1 as Python values do.
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).digest()


def write_state_file(path, value):
    # Pretty-printing an already bounded object can exceed the SDK's read limit.
    atomic_write(path, json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


class UndoPlan:
    MAX_ACTIONS = 8

    def __init__(self):
        self.before = self.after = None
        self.before_exists = False
        self.before_fingerprint = self.after_fingerprint = None
        self.actions = []
        self.limitations = []
        self.attempted = False

    def state_written(self, before, after, before_exists):
        if self.before is None:
            self.before = copy.deepcopy(bounded_json(before))
            self.before_exists = before_exists
            self.before_fingerprint = state_fingerprint(self.before)
        self.after = copy.deepcopy(bounded_json(after))
        self.after_fingerprint = state_fingerprint(self.after)

    def register(self, callback, description, permissions):
        if not callable(callback):
            raise ValidationError("An undo callback must be callable")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 160:
            raise ValidationError("Undo description must contain 1 to 160 characters")
        if not isinstance(permissions, (list, tuple, set, frozenset)) or any(name not in PERMISSIONS for name in permissions):
            raise ValidationError("Undo permissions must use supported SDK capabilities")
        if len(self.actions) >= self.MAX_ACTIONS:
            raise ValidationError("An invocation can register at most eight undo actions")
        self.actions.append((callback, description.strip(), tuple(sorted(set(permissions)))))

    def note(self, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240:
            raise ValidationError("Undo limitation must contain 1 to 240 characters")
        if reason not in self.limitations and len(self.limitations) < 8:
            self.limitations.append(reason)

    @property
    def state_changed(self):
        return self.before is not None and self.before_fingerprint != self.after_fingerprint

    @property
    def available(self):
        return self.state_changed or bool(self.actions)

    def describe(self):
        actions = ["Restore the app's previous saved data"] if self.state_changed else []
        actions.extend(description for _, description, _ in reversed(self.actions))
        return {"actions": actions, "limitations": list(self.limitations)}

    def execute(self, context):
        permissions = {name for _, _, values in self.actions for name in values}
        if self.state_changed:
            permissions.add("state")
        context.request_permissions(*sorted(permissions))
        if self.state_changed and state_fingerprint(context.read_state()) != self.after_fingerprint:
            raise UndoConflict("This app's saved data changed after that action. Undo would overwrite newer changes.")
        outcomes = []
        for callback, description, _ in reversed(self.actions):
            context.checkpoint()
            self.attempted = True
            outcome = callback(context)
            context.checkpoint()
            if not isinstance(outcome, dict) or not outcome.get("ok", False):
                message = outcome.get("text", "Undo was not confirmed") if isinstance(outcome, dict) else "Undo did not return a valid receipt"
                return {"ok": False, "text": message, "partial": bool(outcomes),
                        "conflict": bool(outcome.get("conflict")) if isinstance(outcome, dict) else False,
                        "data": {"completed": outcomes, "limitations": list(self.limitations)}}
            outcomes.append(description)
        if self.state_changed:
            # AppHost admits only one invocation per app. Check again after
            # callbacks, so external state edits cannot be silently overwritten.
            if state_fingerprint(context.read_state()) != self.after_fingerprint:
                raise UndoConflict("The saved app data changed while undo was running; it was not replaced.")
            context.require("state")
            self.attempted = True
            path = context._directory / "state.json"
            if self.before_exists:
                write_state_file(path, self.before)
            else:
                path.unlink(missing_ok=True)
            outcomes.append("Restored the app's previous saved data")
        text = "Undid the recorded change."
        if self.limitations:
            text += " " + " ".join(self.limitations)
        return {"ok": True, "text": text, "data": {"completed": outcomes, "limitations": list(self.limitations)}}
