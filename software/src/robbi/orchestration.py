"""Bounded ownership of robot outputs; hardware cleanup owns lease release.

Inputs remain shared snapshots. This coordinator invokes no driver or external
cancellation callback while holding its metadata lock and starts no threads.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import threading
import time
import uuid

from .errors import BusyError, UnavailableError, ValidationError

BACKGROUND = 10
AUTONOMY = 20
APP = 50
INTERACTIVE = 60
MANUAL = 80
RESOURCES = frozenset({"display", "audio", "motion"})
_CURRENT: ContextVar[Intent | None] = ContextVar("robbi_hardware_intent", default=None)


def _cancelled(signal) -> bool:
    if signal is None:
        return False
    try:
        return bool(signal.is_set() if hasattr(signal, "is_set") else signal())
    except Exception:
        # A broken cancellation source must never allow more hardware activity.
        return True


def _finite(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{name} must be a finite number")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be between {minimum:g} and {maximum:g}")
    return float(value)


class Intent:
    """Trusted activity identity, shared by all its asynchronous descendants."""

    def __init__(self, owner, priority=INTERACTIVE, *, cancelled=None, deadline=None):
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 120:
            raise ValidationError("Hardware owner must be a nonempty label of at most 120 characters")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ValidationError("Hardware priority must be an integer from 0 to 100")
        if deadline is not None:
            deadline = _finite(deadline, "deadline", 0, float("inf"))
        self.id = uuid.uuid4().hex
        self.owner, self.priority = owner, priority
        self.deadline, self._external = deadline, cancelled
        self._revoked = threading.Event()
        self._cancel_lock = threading.Lock()
        self._reason = ""

    @property
    def reason(self):
        return self._reason

    def cancel(self, reason="Cancelled"):
        # First cause survives later Stop/timeout cleanup for honest diagnostics.
        reason = str(reason)[:160]
        with self._cancel_lock:
            if not self._revoked.is_set():
                self._reason = reason
                self._revoked.set()

    def _known_cancelled(self, now):
        """Internal event/deadline only; safe under coordinator metadata lock."""
        if self.deadline is not None and now >= self.deadline:
            self.cancel("Intent deadline expired")
        return self._revoked.is_set()

    def is_set(self):
        if self._known_cancelled(time.monotonic()):
            return True
        if _cancelled(self._external):
            self.cancel("Activity cancelled")
        return self._revoked.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self._revoked.wait(.025 if deadline is None else min(.025, max(0, deadline-time.monotonic())))
        return True


def current_intent():
    return _CURRENT.get()


@contextmanager
def intent_scope(intent):
    if intent is not None and not isinstance(intent, Intent):
        raise TypeError("intent must be an Intent or None")
    token = _CURRENT.set(intent)
    try:
        yield intent
    finally:
        _CURRENT.reset(token)


class ResourceBusy(BusyError):
    def __init__(self, reason, resources, owners=None):
        self.details = {"state": "busy", "reason": reason,
                        "resources": sorted(resources), "owners": owners or {}}
        occupants = ", ".join(f"{resource}: {info['owner']}" for resource, info in (owners or {}).items())
        super().__init__(f"Hardware busy: {reason}" + (f" ({occupants})" if occupants else ""))


@dataclass
class _Claim:
    intent: Intent
    references: int
    deadline: float
    hold_until: float = 0.0


class Lease:
    def __init__(self, coordinator, intent, resources, claims, deadline, cancel_event):
        self._coordinator = coordinator
        self.intent, self.resources = intent, frozenset(resources)
        self._claims, self._deadline = claims, deadline
        self._cancel_event = cancel_event
        self._released = False

    def is_set(self):
        if self._released or self.intent.is_set() or _cancelled(self._cancel_event):
            return True
        if time.monotonic() >= self._deadline:
            self.intent.cancel("Hardware lease expired")
            return True
        return False

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self.intent._revoked.wait(.025 if deadline is None else min(.025, max(0, deadline-time.monotonic())))
        return True

    def release(self, hold_s=0):
        hold_s = _finite(hold_s, "hold_s", 0, 120)
        # External cancellation can be application code. Evaluate before lock.
        cancelled = self.is_set()
        self._coordinator._release(self, 0 if cancelled else hold_s)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class Orchestrator:
    MAX_LEASES = 64

    def __init__(self, *, max_waiters=8):
        if isinstance(max_waiters, bool) or not isinstance(max_waiters, int) or not 1 <= max_waiters <= 8:
            raise ValidationError("max_waiters must be an integer from 1 to 8")
        self._condition = threading.Condition(threading.Lock())
        self._claims: dict[str, _Claim] = {}
        self._active_leases = 0
        self._waiters: dict[str, Intent] = {}
        self._max_waiters = max_waiters
        self._waiter_sequence = 0
        self._epoch = 0
        self._rejections = 0
        self._preemptions = 0
        self._exclusive_owner = None

    def set_exclusive_owner(self, owner):
        """Mode admission gate; does not allocate an idle worker or endless lease."""
        if owner is not None and (not isinstance(owner, str) or not owner or len(owner)>120):
            raise ValidationError('Invalid exclusive output owner')
        with self._condition:
            self._exclusive_owner = owner
            for claim in self._claims.values():
                if owner is not None and claim.intent.owner != owner:
                    claim.intent.cancel('Outputs reserved for '+owner)
            for intent in self._waiters.values():
                if owner is not None and intent.owner != owner:
                    intent.cancel('Outputs reserved for '+owner)
            self._sweep(time.monotonic())
            self._condition.notify_all()

    def _sweep(self, now):
        for resource, claim in tuple(self._claims.items()):
            cancelled = claim.intent._known_cancelled(now)
            if claim.references:
                if now >= claim.deadline:
                    claim.intent.cancel("Hardware lease expired")
                # Cancelled native work keeps ownership until real cleanup.
            elif cancelled or now >= claim.deadline or now >= claim.hold_until:
                del self._claims[resource]

    def _refresh_external(self):
        with self._condition:
            owners = {claim.intent.id: claim.intent for claim in self._claims.values()}
        for intent in owners.values():
            intent.is_set()

    def _busy(self, reason, resources):
        self._rejections += 1
        owners = {resource: {"owner": self._claims[resource].intent.owner,
                             "priority": self._claims[resource].intent.priority,
                             "state": "stopping" if self._claims[resource].intent._revoked.is_set()
                             else "active" if self._claims[resource].references else "held"}
                  for resource in resources if resource in self._claims}
        return ResourceBusy(reason, resources, owners)

    def acquire(self, resources, *, intent=None, cancel_event=None, ttl_s=120, wait_s=.25):
        if isinstance(resources, str):
            resources = [resources]
        if not isinstance(resources, (list, tuple, set, frozenset)) or not 1 <= len(resources) <= 3:
            raise ValidationError("Reserve one to three output resources")
        if any(not isinstance(resource, str) or resource not in RESOURCES for resource in resources):
            raise ValidationError("Output resources are display, audio and motion")
        resources = frozenset(resources)
        ttl_s = _finite(ttl_s, "ttl_s", .001, 120)
        wait_s = _finite(wait_s, "wait_s", 0, .25)
        intent = intent or current_intent() or Intent("system")
        if not isinstance(intent, Intent):
            raise TypeError("intent must be an Intent")
        started = time.monotonic()
        wait_deadline = started + wait_s
        waiter_id = None
        with self._condition:
            epoch = self._epoch
        try:
            while True:
                if intent.is_set() or _cancelled(cancel_event):
                    raise UnavailableError(intent.reason or "Hardware request cancelled")
                self._refresh_external()
                with self._condition:
                    now = time.monotonic()
                    if self._exclusive_owner is not None and intent.owner != self._exclusive_owner:
                        raise self._busy('Outputs reserved for '+self._exclusive_owner, resources)
                    if epoch != self._epoch:
                        intent.cancel("Stop")
                    self._sweep(now)
                    if intent._known_cancelled(now):
                        raise UnavailableError(intent.reason or "Hardware request cancelled")
                    if self._active_leases >= self.MAX_LEASES:
                        raise self._busy("Active hardware lease limit reached", resources)
                    held = {name for name, claim in self._claims.items() if claim.intent is intent}
                    active = any(claim.intent is intent and claim.references for claim in self._claims.values())
                    if active and any(resource in self._claims for resource in resources-held):
                        # Existing holders may expand into free outputs, but
                        # never wait or preempt while retaining another output.
                        raise self._busy("Another output is owned; reserve the complete scene before starting", resources)
                    conflicts = {name: self._claims[name] for name in resources
                                 if name in self._claims and self._claims[name].intent is not intent}
                    blocked = {name: claim for name, claim in conflicts.items()
                               if claim.references or intent.priority < claim.intent.priority}
                    # An atomic request never cancels one owner when another
                    # resource prevents admission at this priority.
                    if any(intent.priority <= claim.intent.priority for claim in blocked.values()):
                        raise self._busy("Another activity owns the requested outputs", resources)
                    if not blocked:
                        deadline = min(now + ttl_s, intent.deadline if intent.deadline is not None else float("inf"))
                        # Replacing a held image is an explicit handover. Revoke
                        # the old activity, including asynchronous descendants,
                        # only after the complete request is admissible.
                        for claim in conflicts.values():
                            if not claim.intent._revoked.is_set():
                                claim.intent.cancel(f"Preempted by {intent.owner}")
                                self._preemptions += 1
                        claims = {}
                        for resource in resources:
                            claim = self._claims.get(resource)
                            if claim is None or claim.intent is not intent:
                                claim = _Claim(intent, 0, deadline)
                                self._claims[resource] = claim
                            else:
                                claim.deadline = min(claim.deadline, deadline)
                            claim.references += 1
                            claims[resource] = claim
                            deadline = min(deadline, claim.deadline)
                        self._active_leases += 1
                        return Lease(self, intent, resources, claims, deadline, cancel_event)
                    for claim in blocked.values():
                        if not claim.intent._revoked.is_set():
                            claim.intent.cancel(f"Preempted by {intent.owner}")
                            self._preemptions += 1
                    if now >= wait_deadline:
                        raise self._busy("Previous activity is still stopping", resources)
                    if waiter_id is None:
                        if len(self._waiters) >= self._max_waiters:
                            raise self._busy("Hardware handover wait limit reached", resources)
                        self._waiter_sequence += 1
                        waiter_id = str(self._waiter_sequence)
                        self._waiters[waiter_id] = intent
                    self._condition.wait(min(.025, max(0, wait_deadline-now)))
        finally:
            if waiter_id is not None:
                with self._condition:
                    self._waiters.pop(waiter_id, None)

    def _release(self, lease, hold_s):
        with self._condition:
            if lease._released:
                return
            lease._released = True
            self._active_leases -= 1
            now = time.monotonic()
            for resource, original in lease._claims.items():
                claim = self._claims.get(resource)
                if claim is not original:
                    continue
                claim.references = max(0, claim.references-1)
                claim.hold_until = max(claim.hold_until, min(claim.deadline, now+hold_s))
                if not claim.references and (claim.intent._known_cancelled(now) or now >= claim.hold_until):
                    del self._claims[resource]
            self._sweep(now)
            self._condition.notify_all()

    def cancel_owner(self, intent, reason="Cancelled"):
        if not isinstance(intent, Intent):
            raise TypeError("intent must be an Intent")
        intent.cancel(reason)
        with self._condition:
            self._sweep(time.monotonic())
            self._condition.notify_all()

    def cancel_all(self, reason="Stop"):
        with self._condition:
            self._epoch += 1
            for claim in self._claims.values():
                claim.intent.cancel(reason)
            for intent in self._waiters.values():
                intent.cancel(reason)
            self._sweep(time.monotonic())
            self._condition.notify_all()

    def snapshot(self):
        # No external callback here: diagnostics stay bounded even if an app's
        # cancellation function is faulty or native code has stopped returning.
        with self._condition:
            now = time.monotonic()
            self._sweep(now)
            resources = {name: {"owner": claim.intent.owner, "intent_id": claim.intent.id,
                                "priority": claim.intent.priority, "references": claim.references,
                                "state": "stopping" if claim.intent._revoked.is_set()
                                else "active" if claim.references else "held",
                                "remaining_s": round(max(0, claim.deadline-now), 3),
                                "hold_remaining_s": round(max(0, claim.hold_until-now), 3),
                                "reason": claim.intent.reason}
                         for name, claim in self._claims.items()}
            return {"resources": resources, "waiting": len(self._waiters), "active_leases": self._active_leases,
                    "exclusive_owner": self._exclusive_owner,
                    "limits": {"resources": 3, "waiters": self._max_waiters,
                               "max_wait_s": .25, "max_lease_s": 120, "active_leases": self.MAX_LEASES},
                    "rejections": self._rejections, "preemptions": self._preemptions}
