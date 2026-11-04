"""Bounded, session-local undo receipts for committed productivity records."""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import json
import time
import uuid

from robbi.errors import ValidationError

_source = ContextVar("productivity_change_source", default=None)
_changes = ContextVar("productivity_scope_changes", default=None)


def normalized_source(source):
    return {"ui": "ui_text", "telegram": "telegram_text"}.get(source, source) or "ui_text"


@contextmanager
def change_scope(source):
    token = _source.set(normalized_source(source))
    changes = []
    change_token = _changes.set(changes)
    try:
        yield changes
    finally:
        _changes.reset(change_token)
        _source.reset(token)


def recorded(table, *, creates=False):
    """Record the outermost store mutation while its existing lock is held."""
    def decorate(method):
        @wraps(method)
        def invoke(self, *args, **kwargs):
            with self._lock:
                if getattr(self, "_undo_depth", 0):
                    return method(self, *args, **kwargs)
                item_id = None if creates else (args[0] if args else kwargs.get("note_id", kwargs.get("item_id")))
                before = self._undo_row(table, item_id) if item_id is not None else None
                self._undo_depth = 1
                try:
                    result = method(self, *args, **kwargs)
                finally:
                    self._undo_depth = 0
                if creates:
                    item_id = result.get("id") if isinstance(result, dict) else None
                if item_id is not None:
                    after = self._undo_row(table, item_id)
                    if before != after:
                        source = normalized_source(_source.get() or kwargs.get("source") or "ui_text")
                        receipt = self._undo_journal.record(table, item_id, before, after, source, method.__name__)
                        if _changes.get() is not None:
                            _changes.get().append(receipt)
                return result
        return invoke
    return decorate


class ChangeJournal:
    LIMIT = 32

    def __init__(self):
        self.entries = OrderedDict()

    def record(self, table, item_id, before, after, source, operation):
        # Large existing structured notes remain usable without retaining a
        # second unbounded copy in an undo journal.
        available = len(json.dumps([before, after], ensure_ascii=True).encode()) <= 131072
        receipt = {"id": uuid.uuid4().hex, "kind": "productivity", "table": table,
                   "item_id": item_id, "operation": operation, "source": source,
                   "created_at": time.time(), "undone": False,
                   "undo": {"available": available, "reason": "" if available else "This record exceeds the undo history size limit."},
                   "_before": deepcopy(before) if available else None,
                   "_after": deepcopy(after) if available else None}
        self.entries[receipt["id"]] = receipt
        while len(self.entries) > self.LIMIT:
            self.entries.popitem(last=False)
        return deepcopy({key: value for key, value in receipt.items() if not key.startswith("_")})

    def history(self, source=None):
        return [deepcopy({key: value for key, value in record.items() if not key.startswith("_")})
                for record in reversed(self.entries.values())
                if source is None or record["source"] == normalized_source(source)]

    def select(self, identifier, source=None):
        record = self.entries.get(identifier)
        if record is None or (source is not None and record["source"] != normalized_source(source)):
            raise ValidationError("This change is not in the current conversation's undo history.")
        if record["undone"] or not record["undo"]["available"]:
            raise ValidationError(record["undo"]["reason"] or "This change has already been undone.")
        return record
