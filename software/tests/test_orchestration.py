from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading
import time

import pytest

from robbi.errors import UnavailableError, ValidationError
from robbi.orchestration import (
    APP, AUTONOMY, BACKGROUND, INTERACTIVE, MANUAL,
    Intent, Orchestrator, ResourceBusy, current_intent, intent_scope,
)


def test_atomic_scene_reservation_never_takes_partial_resources_or_preempts_partially():
    broker = Orchestrator()
    sound = broker.acquire({"audio"}, intent=Intent("speaker", MANUAL))
    screen = broker.acquire({"display"}, intent=Intent("ambient", BACKGROUND))
    with pytest.raises(ResourceBusy) as error:
        broker.acquire({"audio", "display"}, intent=Intent("app", APP))
    assert error.value.details["resources"] == ["audio", "display"]
    assert not screen.is_set()
    assert set(broker.snapshot()["resources"]) == {"audio", "display"}
    sound.release()
    screen.release()


def test_nested_subsets_keep_parent_and_child_ownership_until_both_release():
    broker, intent = Orchestrator(), Intent("scene", APP)
    parent = broker.acquire({"display", "audio"}, intent=intent)
    with intent_scope(intent):
        child = broker.acquire("display")
    parent.release()
    assert broker.snapshot()["resources"]["display"]["references"] == 1
    assert "audio" not in broker.snapshot()["resources"]
    with pytest.raises(ResourceBusy):
        broker.acquire("display", intent=Intent("other", APP))
    child.release()
    assert broker.snapshot()["resources"] == {}


def test_active_sound_can_add_free_motion_without_waiting():
    broker, intent = Orchestrator(), Intent("scene")
    with broker.acquire("audio", intent=intent):
        with broker.acquire({"audio", "motion"}, intent=intent):
            assert set(broker.snapshot()["resources"]) == {"audio", "motion"}


def test_active_owner_expansion_conflict_never_preempts_or_acquires_partial_outputs():
    broker, intent = Orchestrator(), Intent("scene", INTERACTIVE)
    low = broker.acquire("display", intent=Intent("ambient", BACKGROUND))
    with broker.acquire("audio", intent=intent):
        with pytest.raises(ResourceBusy, match="complete scene"):
            broker.acquire({"display", "motion"}, intent=intent)
        assert not low.is_set()
        assert set(broker.snapshot()["resources"]) == {"audio", "display"}
    low.release()


def test_equal_and_lower_priorities_do_not_interrupt_active_owner():
    broker, intent = Orchestrator(), Intent("app", APP)
    with broker.acquire("motion", intent=intent):
        for priority in (APP, AUTONOMY, BACKGROUND):
            with pytest.raises(ResourceBusy):
                broker.acquire("motion", intent=Intent("contender", priority))
        assert not intent.is_set()


def test_higher_priority_waits_for_real_cleanup_before_handover():
    broker = Orchestrator()
    old = broker.acquire("motion", intent=Intent("mapping", AUTONOMY))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(broker.acquire, "motion", intent=Intent("manual", MANUAL))
        assert old.intent.wait(.15)
        assert not pending.done()
        assert broker.snapshot()["resources"]["motion"]["state"] == "stopping"
        old.release()
        new = pending.result(.5)
        assert new.intent.owner == "manual"
        assert not new.is_set()
        new.release()


def test_blocked_native_owner_is_not_released_on_handover_timeout_or_stop():
    broker = Orchestrator()
    old = broker.acquire("display", intent=Intent("blocked image", APP))
    started = time.monotonic()
    with pytest.raises(ResourceBusy, match="still stopping"):
        broker.acquire("display", intent=Intent("manual", MANUAL), wait_s=.015)
    assert time.monotonic()-started < .2
    broker.cancel_all()
    assert broker.snapshot()["resources"]["display"]["references"] == 1
    assert old.is_set()
    with pytest.raises(ResourceBusy):
        broker.acquire("display", intent=Intent("another manual", MANUAL), wait_s=0)
    old.release()
    with broker.acquire("display", intent=Intent("new request", MANUAL)):
        pass


def test_stop_cancels_waiters_without_early_transfer():
    broker = Orchestrator()
    old = broker.acquire("audio", intent=Intent("blocked", APP))
    waiter = Intent("manual", MANUAL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.acquire, "audio", intent=waiter)
        assert old.intent.wait(.15)
        broker.cancel_all()
        with pytest.raises(UnavailableError):
            future.result(.5)
    assert waiter.is_set()
    assert broker.snapshot()["waiting"] == 0
    assert "audio" in broker.snapshot()["resources"]
    old.release()


def test_active_expiry_cancels_without_forcing_release(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("robbi.orchestration.time.monotonic", lambda: now[0])
    broker = Orchestrator()
    lease = broker.acquire("motion", ttl_s=2)
    now[0] = 103
    assert broker.snapshot()["resources"]["motion"]["state"] == "stopping"
    assert lease.is_set()
    with pytest.raises(ResourceBusy):
        broker.acquire("motion", intent=Intent("manual", MANUAL), wait_s=0)
    lease.release()
    assert not broker.snapshot()["resources"]


def test_static_display_hold_blocks_background_but_equal_priority_can_replace():
    broker = Orchestrator()
    image = broker.acquire("display", intent=Intent("image", APP))
    image.release(hold_s=60)
    assert broker.snapshot()["resources"]["display"]["state"] == "held"
    with pytest.raises(ResourceBusy):
        broker.acquire("display", intent=Intent("ambient", BACKGROUND))
    with broker.acquire("display", intent=Intent("another app", APP)):
        assert image.intent.is_set()
        with pytest.raises(UnavailableError):
            broker.acquire("display", intent=image.intent)
    # Repeated release of an old lease cannot remove its successor.
    image.release()
    assert broker.snapshot()["resources"] == {}
    with pytest.raises(UnavailableError):
        broker.acquire("display", intent=image.intent)


def test_replacing_idle_display_also_cancels_old_owners_active_audio():
    broker, original = Orchestrator(), Intent("original", APP)
    broker.acquire("display", intent=original).release(hold_s=60)
    sound = broker.acquire("audio", intent=original)
    with broker.acquire("display", intent=Intent("replacement", APP)):
        assert sound.is_set()
        assert broker.snapshot()["resources"]["audio"]["state"] == "stopping"
        assert broker.snapshot()["preemptions"] == 1
    assert "audio" in broker.snapshot()["resources"]
    sound.release()


def test_failed_atomic_request_does_not_revoke_displaceable_idle_owner():
    broker, image = Orchestrator(), Intent("image", APP)
    broker.acquire("display", intent=image).release(hold_s=60)
    with broker.acquire("audio", intent=Intent("manual audio", MANUAL)):
        with pytest.raises(ResourceBusy):
            broker.acquire({"display", "audio"}, intent=Intent("new app", APP))
        assert not image.is_set()
        assert broker.snapshot()["resources"]["display"]["owner"] == "image"
        assert broker.snapshot()["preemptions"] == 0


def test_idle_hold_expiry_reclaims_without_thread_or_revoking_intent(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("robbi.orchestration.time.monotonic", lambda: now[0])
    broker, intent = Orchestrator(), Intent("image", APP)
    broker.acquire("display", intent=intent).release(hold_s=10)
    now[0] = 111
    assert broker.snapshot()["resources"] == {}
    assert not intent.is_set()
    with broker.acquire("motion", intent=intent):
        pass


def test_child_hold_survives_parent_release_but_stop_clears_idle_hold():
    broker, intent = Orchestrator(), Intent("scene", APP)
    parent = broker.acquire({"display", "audio"}, intent=intent)
    child = broker.acquire("display", intent=intent)
    child.release(hold_s=60)
    parent.release()
    assert set(broker.snapshot()["resources"]) == {"display"}
    broker.cancel_all()
    assert broker.snapshot()["resources"] == {}


def test_idle_image_does_not_prevent_sequential_sound_or_movement():
    broker, intent = Orchestrator(), Intent("app", APP)
    broker.acquire("display", intent=intent).release(hold_s=60)
    with broker.acquire("audio", intent=intent):
        assert set(broker.snapshot()["resources"]) == {"display", "audio"}
    with broker.acquire("motion", intent=intent):
        assert set(broker.snapshot()["resources"]) == {"display", "motion"}
    broker.cancel_all()


def test_local_audio_cancellation_does_not_revoke_scene_motion():
    broker, intent, local = Orchestrator(), Intent("scene", APP), threading.Event()
    parent = broker.acquire({"motion", "audio"}, intent=intent)
    sound = broker.acquire("audio", intent=intent, cancel_event=local)
    local.set()
    assert sound.is_set()
    assert not parent.is_set()
    assert not intent.is_set()
    sound.release(hold_s=20)
    parent.release()
    assert not broker.snapshot()["resources"]


def test_external_cancellation_is_permanent_and_idle_owner_is_reclaimed():
    broker, external = Orchestrator(), threading.Event()
    owner = Intent("app", APP, cancelled=external)
    broker.acquire("display", intent=owner).release(hold_s=60)
    external.set()
    with broker.acquire("display", intent=Intent("ambient", BACKGROUND)):
        pass
    external.clear()
    assert owner.is_set()
    with pytest.raises(UnavailableError):
        broker.acquire("motion", intent=owner)


def test_cancellation_callbacks_are_outside_metadata_lock_and_snapshot_is_callback_free():
    broker = Orchestrator()
    calls = []
    def cancelled():
        calls.append(broker.snapshot())
        return False
    lease = broker.acquire("display", intent=Intent("app", cancelled=cancelled))
    count = len(calls)
    broker.snapshot()
    assert len(calls) == count
    with pytest.raises(ResourceBusy):
        broker.acquire("display", intent=Intent("other", APP))
    lease.release()


def test_context_propagates_explicitly_to_existing_executor_workers():
    intent = Intent("conversation", INTERACTIVE)
    assert current_intent() is None
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(current_intent).result() is None
        with intent_scope(intent):
            context = copy_context()
            assert pool.submit(context.run, current_intent).result() is intent
        assert pool.submit(current_intent).result() is None
    assert current_intent() is None


def test_stop_and_status_never_wait_for_a_blocked_external_cancellation_callback():
    broker = Orchestrator()
    block, entered, finish = threading.Event(), threading.Event(), threading.Event()
    def cancelled():
        if block.is_set():
            entered.set()
            finish.wait(1)
        return False
    lease = broker.acquire("display", intent=Intent("app", APP, cancelled=cancelled))
    block.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(broker.acquire, "display", intent=Intent("manual", MANUAL))
        try:
            assert entered.wait(.5)
            started = time.monotonic()
            broker.cancel_all()
            assert broker.snapshot()["resources"]["display"]["state"] == "stopping"
            assert time.monotonic()-started < .1
        finally:
            finish.set()
        with pytest.raises(UnavailableError):
            pending.result(.5)
    lease.release()
def test_intent_and_lease_wait_follow_cancellation_and_deadlines():
    intent = Intent("past", deadline=time.monotonic()-1)
    assert intent.wait(0)
    broker, local = Orchestrator(), threading.Event()
    with broker.acquire("audio", cancel_event=local) as lease:
        assert not lease.wait(.001)
        local.set()
        assert lease.wait(.05)


def test_waiter_admission_is_bounded_and_stop_remains_prompt():
    broker = Orchestrator(max_waiters=1)
    lease = broker.acquire("motion", intent=Intent("native", APP))
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(broker.acquire, "motion", intent=Intent("first", MANUAL))
        assert lease.intent.wait(.15)
        with pytest.raises(ResourceBusy, match="wait limit"):
            broker.acquire("motion", intent=Intent("second", MANUAL))
        assert broker.snapshot()["waiting"] == 1
        started = time.monotonic()
        broker.cancel_all()
        assert time.monotonic()-started < .1
        with pytest.raises(UnavailableError):
            waiting.result(.5)
    lease.release()


def test_active_lease_limit_rejects_nested_leaks_before_preemption():
    broker, scene = Orchestrator(), Intent("scene", APP)
    leases = [broker.acquire("audio", intent=scene) for _ in range(63)]
    other = broker.acquire("display", intent=Intent("other", BACKGROUND))
    assert broker.snapshot()["active_leases"] == 64
    with pytest.raises(ResourceBusy, match="lease limit"):
        broker.acquire("audio", intent=scene)
    with pytest.raises(ResourceBusy, match="lease limit"):
        broker.acquire("display", intent=Intent("manual", MANUAL))
    assert not other.intent.is_set()
    assert broker.snapshot()["resources"]["audio"]["references"] == 63
    leases.pop().release()
    with broker.acquire("motion", intent=scene):
        assert broker.snapshot()["active_leases"] == 64
    for lease in leases:
        lease.release()
        lease.release()
    other.release(hold_s=60)
    assert broker.snapshot()["active_leases"] == 0
    assert broker.snapshot()["resources"]["display"]["state"] == "held"


@pytest.mark.parametrize("kwargs", [
    {"resources": []}, {"resources": {"camera"}}, {"resources": {"motion"}, "ttl_s": float("nan")},
    {"resources": {"motion"}, "ttl_s": 121}, {"resources": {"motion"}, "wait_s": 1},
    {"resources": {"motion"}, "wait_s": True},
])
def test_invalid_reservations_fail_before_hardware_admission(kwargs):
    broker = Orchestrator()
    with pytest.raises(ValidationError):
        broker.acquire(**kwargs)
    assert not broker.snapshot()["resources"]
