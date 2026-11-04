import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robbi.diagnostics import DiagnosticSampler, DiagnosticSection


def test_fast_health_does_not_rescan_slow_catalog_or_query_summaries():
    clock = [0.0]
    reads = {key: Mock(return_value={"value": key}) for key in ("device", "summary", "catalog")}
    sampler = DiagnosticSampler({key: DiagnosticSection(reads[key], interval)
                                 for key, interval in (("device", 2), ("summary", 15), ("catalog", 60))},
                                clock=lambda: clock[0])
    for now in range(0, 61, 2):
        clock[0] = now
        assert set(sampler.sample()) == set(reads)
    assert reads["device"].call_count == 31
    assert reads["summary"].call_count == 4
    assert reads["catalog"].call_count == 2
    assert sampler.metrics()["summary"]["age_s"] == 12


def test_explicit_refresh_and_targeted_invalidation_preserve_other_caches():
    reads = {name: Mock(return_value=[]) for name in ("assets", "memory")}
    sampler = DiagnosticSampler({name: DiagnosticSection(read, 60) for name, read in reads.items()})
    sampler.sample()
    sampler.invalidate("assets")
    sampler.sample()
    assert reads["assets"].call_count == 2
    assert reads["memory"].call_count == 1
    sampler.sample(force=True)
    assert reads["memory"].call_count == 2


def test_failed_producer_is_visible_and_does_not_abort_other_sections():
    faulty = Mock(side_effect=[{"available": True}, OSError("device removed"), {"available": True}])
    sampler = DiagnosticSampler({"faulty": DiagnosticSection(faulty, 0),
                                 "local": DiagnosticSection(lambda: "usable", 0)})
    assert sampler.sample()["faulty"]["available"]
    failed = sampler.sample()
    assert failed["local"] == "usable"
    assert failed["faulty"] == {"available": False, "error": "device removed"}
    assert sampler.metrics()["faulty"]["error"] == "device removed"
    assert sampler.sample()["faulty"]["available"]
    assert sampler.metrics()["faulty"]["error"] == ""


def test_producer_and_reader_mutations_do_not_change_cached_values():
    value = {"nested": [1]}
    sampler = DiagnosticSampler({"local": DiagnosticSection(lambda: value, 60)})
    result = sampler.sample()
    value["nested"].append(2)
    result["local"]["nested"].append(3)
    assert sampler.snapshot() == {"local": {"nested": [1]}}


def test_slow_refresh_does_not_block_readers_or_lose_mid_refresh_invalidation():
    entered, release = threading.Event(), threading.Event()
    count = [0]
    def read():
        count[0] += 1
        if count[0] == 2:
            entered.set()
            assert release.wait(2)
        return count[0]
    sampler = DiagnosticSampler({"value": DiagnosticSection(read, 60)})
    assert sampler.sample() == {"value": 1}
    worker = threading.Thread(target=lambda: sampler.sample(force=True))
    worker.start()
    try:
        assert entered.wait(1)
        assert sampler.sample() == {"value": 1}
        sampler.invalidate("value")
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert sampler.sample() == {"value": 3}


@pytest.mark.parametrize("interval", [-1, float("inf"), float("nan")])
def test_invalid_refresh_intervals_are_rejected(interval):
    with pytest.raises(ValueError):
        DiagnosticSampler({"value": DiagnosticSection(lambda: 1, interval)})


def test_composition_resolves_recovered_devices_at_sample_time():
    from robot.diagnostics import controller_diagnostics
    controller = SimpleNamespace(camera=SimpleNamespace(health=lambda: {"available": False}))
    sampler = controller_diagnostics(controller, SimpleNamespace(), fast_s=0)
    assert not sampler.sample()["camera"]["available"]
    controller.camera = SimpleNamespace(health=lambda: {"available": True})
    assert sampler.sample()["camera"]["available"]


def test_background_daily_diagnostics_do_not_touch_memory_access_times():
    from robot.domains.productivity import ProductivityDomain
    domain = ProductivityDomain()
    domain.daily_loop = SimpleNamespace(status=lambda: {}, build_state=Mock(return_value={}))
    domain.reminders = SimpleNamespace(list_items=lambda **_: [])
    domain.memory = SimpleNamespace(recent_messages=lambda: [], search=Mock(return_value=[]),
                                    pending_entries=lambda **_: [])
    domain.productivity = SimpleNamespace(list_items=lambda *_, **__: [])
    domain.events = SimpleNamespace(recent=lambda **_: [])
    domain.heartbeat_status = lambda: {}
    assert "error" not in domain.daily_loop_status()
    assert domain.memory.search.call_args.kwargs["touch"] is False
