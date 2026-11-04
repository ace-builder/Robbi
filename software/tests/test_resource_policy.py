import threading

import pytest

from robbi.errors import BusyError
from robbi.resources import ExecutionResources


def test_feature_budget_bounds_multiple_pools_and_reserves_recovery():
    resources = ExecutionResources(task_limit=2)
    release = threading.Event()
    try:
        first = resources.lane("research").submit(lambda: release.wait(2))
        second = resources.lane("speech").submit(lambda: release.wait(2))
        with pytest.raises(BusyError, match="budget"):
            resources.lane("llm-live").submit(lambda: None)
        assert resources.lane("maintenance").submit(lambda: "recovered").result(1) == "recovered"
        assert resources.snapshot()["feature_tasks"]["outstanding"] == 2
        release.set()
        first.result(1)
        second.result(1)
        assert resources.lane("llm-live").submit(lambda: 7).result(1) == 7
    finally:
        release.set()
        resources.shutdown()


def test_cancelled_rejected_and_failed_work_release_admission():
    resources = ExecutionResources(task_limit=8)
    entered, release = threading.Event(), threading.Event()
    lane = resources.lane("research")
    try:
        running = lane.submit(lambda: (entered.set(), release.wait(2)))
        assert entered.wait(1)
        queued = lane.submit(lambda: None)
        with pytest.raises(BusyError):
            lane.submit(lambda: None)
        assert resources.snapshot()["feature_tasks"]["outstanding"] == 2
        assert queued.cancel()
        assert resources.snapshot()["feature_tasks"]["outstanding"] == 1
        release.set()
        running.result(1)
        def fail(): raise OSError("provider failed")
        with pytest.raises(OSError):
            resources.lane("llm-live").submit(fail).result(1)
    finally:
        release.set()
        resources.shutdown()
    assert resources.snapshot()["feature_tasks"]["outstanding"] == 0


def test_parent_and_child_use_independent_workers_without_admission_wait():
    resources = ExecutionResources(task_limit=2)
    try:
        def parent():
            return resources.lane("tts-prefetch").submit(lambda: "speech").result(1)
        assert resources.lane("presentation").submit(parent).result(2) == "speech"
    finally:
        resources.shutdown()
