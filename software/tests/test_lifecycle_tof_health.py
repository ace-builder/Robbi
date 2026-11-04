"""Per-channel distance health uses cached readings without touching hardware."""
import threading
import time
from types import SimpleNamespace

import pytest

from robbi.health import HealthRegistry
from robbi.runtime import Runtime
from robot.config import settings
from robot.lifecycle import LifecycleMixin
from robot.tof import ToFService


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(settings, "tof_enabled", True)
    monkeypatch.setattr(settings, "tof_h2_enabled", True)
    monkeypatch.setattr(settings, "tof_max_age_s", 0.75)
    controller = LifecycleMixin()
    controller._init_lifecycle()
    controller.tof = controller._component("tof", ToFService)
    for channel in ("h1", "h2"):
        controller.tof._set_status(channel, available=True, distance_cm=35,
                                   last_read_ts=time.monotonic(), last_error="")
    return controller


def publish_health(host, registry):
    # Match supervisor publication, retaining entries between samples so stale
    # failures cannot disappear merely by omitting a channel on the next tick.
    for entry in host.health_snapshot():
        registry.set(entry["name"], entry["state"], entry["message"], entry["hint"], entry["retries"])
    runtime = SimpleNamespace(health=registry, _stop=threading.Event(),
                              _started_at=time.monotonic(), config=SimpleNamespace(values={}))
    return Runtime.health_snapshot(runtime)


def test_missing_secondary_degrades_overall_health_and_recovers_without_affecting_primary(host):
    host.tof._set_status("h2", available=False, distance_cm=None,
                         last_error="No I2C device at address: 0x29")
    registry = HealthRegistry()
    health = publish_health(host, registry)
    entries = {entry["name"]: entry for entry in health["components"]}
    assert health["state"] == "degraded"
    assert entries["tof"]["state"] == entries["tof.h1"]["state"] == "ready"
    assert entries["tof.h2"]["state"] == "degraded"
    assert "H2" in entries["tof.h2"]["message"] and "0x29" in entries["tof.h2"]["message"]
    assert "XSHUT" in entries["tof.h2"]["hint"]
    assert host.tof.distance_cm() == 35

    host.tof._set_status("h2", available=True, distance_cm=50,
                         last_read_ts=time.monotonic(), last_error="")
    recovered = publish_health(host, registry)
    assert recovered["state"] == "ready"
    assert all(entry["state"] == "ready" and not entry["message"]
               for entry in recovered["components"])
    assert host.tof._thread is None


def test_disabling_tof_clears_previously_reported_channel_failure(host, monkeypatch):
    host.tof._set_status("h2", available=False, last_error="Disconnected")
    registry = HealthRegistry()
    assert publish_health(host, registry)["state"] == "degraded"
    monkeypatch.setattr(settings, "tof_enabled", False)
    host._record_health("tof", "disabled", "Disabled by configuration")
    disabled = publish_health(host, registry)
    assert disabled["state"] == "ready"
    assert {entry["name"]: entry["state"] for entry in disabled["components"]} == {
        "tof": "disabled", "tof.h1": "disabled", "tof.h2": "disabled"}


def test_stale_secondary_is_visible_while_fresh_primary_remains_usable(host):
    host.tof._set_status("h2", last_read_ts=time.monotonic() - 5)
    health = publish_health(host, HealthRegistry())
    entries = {entry["name"]: entry for entry in health["components"]}
    assert health["state"] == "degraded"
    assert entries["tof"]["state"] == entries["tof.h1"]["state"] == "ready"
    assert entries["tof.h2"]["state"] == "degraded"
    assert "stale" in entries["tof.h2"]["message"]
    assert host.tof.distance_cm() == 35


def test_intentionally_absent_secondary_is_disabled_without_overall_fault(host, monkeypatch):
    host.tof._set_status("h2", available=False, last_error="No I2C device at address: 0x29")
    registry = HealthRegistry()
    assert publish_health(host, registry)["state"] == "degraded"
    monkeypatch.setattr(settings, "tof_h2_enabled", False)
    health = publish_health(host, registry)
    entries = {entry["name"]: entry for entry in health["components"]}
    assert health["state"] == "ready"
    assert entries["tof.h1"]["state"] == "ready"
    assert entries["tof.h2"]["state"] == "disabled"
    assert "0x29" not in entries["tof.h2"]["message"]
    assert host.tof.distance_cm() == 35
