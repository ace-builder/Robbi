"""The deployment report must remain useful without probing hardware."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "hardware" / "pi_preflight.py"
spec = importlib.util.spec_from_file_location("pi_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def snapshot():
    return {"host": {"python": [3, 11, 2], "platform": "linux", "machine": "aarch64",
                     "userspace_bits": 64, "model": "Raspberry Pi Zero 2 W Rev 1.0"},
            "memory": {"MemAvailable": 200 * preflight.MIB},
            "disk": {"free_bytes": 1000 * preflight.MIB},
            "modules": {name: True for name in preflight.CORE_MODULES},
            "environment": {name: "1" for name in preflight.THREAD_VARIABLES},
            "commands": {"aplay": True, "arecord": True}, "temperature_c": 42.1,
            "firmware": {"state": "skipped", "detail": "not queried"},
            "profile": {"hardware": {"enable_motors": False}}, "profile_error": None}


def by_name(report, name):
    return next(check for check in report["checks"] if check["name"] == name)


def test_missing_camera_dependency_warns_without_failing_core():
    data = snapshot()
    data["profile"]["camera"] = {"enabled": True}
    report = preflight.evaluate(data)
    assert report["status"] == "warn"
    assert by_name(report, "optional.picamera2")["state"] == "warn"
    assert report["hardware_verified"] is False


def test_local_wake_checks_undeclared_speech_features_scipy_dependency():
    data = snapshot()
    data["profile"]["voice"] = {"enabled": True, "wake_engine": "local_wake"}
    data["modules"].update(numpy=True, python_speech_features=True, scipy=False)
    report = preflight.evaluate(data)
    assert by_name(report, "optional.scipy")["state"] == "warn"


def test_unsupported_python_and_missing_core_fail():
    data = snapshot()
    data["host"]["python"] = [3, 13, 5]
    data["modules"]["yaml"] = False
    report = preflight.evaluate(data)
    assert by_name(report, "python")["state"] == "fail"
    assert by_name(report, "package.yaml")["state"] == "fail"


def test_desktop_not_confused_with_pi_and_32bit_reported():
    data = snapshot()
    data["host"].update(model=None, platform="win32", userspace_bits=32)
    assert by_name(preflight.evaluate(data), "target")["state"] == "info"
    report = preflight.evaluate(data, require_pi=True)
    assert by_name(report, "target")["state"] == "fail"
    assert by_name(report, "architecture")["state"] == "warn"


def test_resource_pressure_and_unsafe_motor_profile():
    data = snapshot()
    data["memory"]["MemAvailable"] = 30 * preflight.MIB
    data["disk"]["free_bytes"] = 100 * preflight.MIB
    data["temperature_c"] = 80.0
    data["profile"]["hardware"]["enable_motors"] = True
    data["environment"]["OPENBLAS_NUM_THREADS"] = "4"
    report = preflight.evaluate(data)
    for name in ("memory", "disk", "temperature", "native.OPENBLAS_NUM_THREADS"):
        assert by_name(report, name)["state"] == "warn"
    assert by_name(report, "motors")["state"] == "fail"


def test_budget_validation_does_not_echo_arbitrary_values():
    data = snapshot()
    data["profile"]["camera"] = {"fps": float("nan"), "width": "private value"}
    data["profile"]["providers"] = {"url": "https://private:secret@example.test"}
    report = preflight.evaluate(data)
    assert by_name(report, "budget.camera.fps")["state"] == "fail"
    assert "private" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_meminfo_and_throttle_parsers():
    assert preflight.parse_meminfo("MemTotal: 450000 kB\nMemAvailable: 123456 kB\nNoise: 8 kB") == {
        "MemTotal": 450000 * 1024, "MemAvailable": 123456 * 1024}
    result = preflight.parse_throttled("throttled=0x50005\n")
    assert result["flags"] == ["undervoltage_now", "throttled_now", "undervoltage_since_boot", "throttled_since_boot"]
    with pytest.raises(ValueError):
        preflight.parse_throttled("malformed data")


def test_firmware_is_opt_in_and_timeout_bounded(monkeypatch):
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, 2)
    monkeypatch.setattr(sys, "platform", "linux")
    assert preflight.firmware_info(False, "Raspberry Pi Zero 2 W", runner=runner)["state"] == "skipped"
    assert not calls
    result = preflight.firmware_info(True, "Raspberry Pi Zero 2 W", runner=runner, which=lambda _: "/usr/bin/vcgencmd")
    assert result["state"] == "warn"
    assert calls[0][0] == ["/usr/bin/vcgencmd", "get_throttled"]
    assert calls[0][1]["timeout"] == 2
    assert calls[0][1]["shell"] is False


def test_module_discovery_never_imports_parent_package():
    with pytest.raises(ValueError):
        preflight.module_available("picamera2.driver")


def test_profile_merge_and_corruption_are_read_only(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "desktop.yaml").write_text("camera:\n  fps: 3\n  enabled: false\n", encoding="utf-8")
    selected = config_dir / "pi.yaml"
    selected.write_text("camera:\n  enabled: true\n", encoding="utf-8")
    values, issue = preflight.inspect_profile(tmp_path, selected)
    assert issue is None
    assert values["camera"] == {"fps": 3, "enabled": True}
    selected.write_text("camera: [broken", encoding="utf-8")
    values, issue = preflight.inspect_profile(tmp_path, selected)
    assert values is None and issue
    assert selected.read_text(encoding="utf-8") == "camera: [broken"
    assert not (tmp_path / "var").exists()


def test_missing_optional_yaml_reports_incomplete_inspection(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "module_available", lambda _: False)
    values, issue = preflight.inspect_profile(tmp_path, tmp_path / "pi.yaml")
    assert values is None and "PyYAML" in issue


def test_cli_json_smoke_does_not_create_state(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--json", "--data-dir", str(tmp_path / "uncreated")],
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["hardware_verified"] is False
    assert not (tmp_path / "uncreated").exists()


def test_cli_without_site_packages_still_reports_missing_dependencies(tmp_path):
    result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--json", "--data-dir", str(tmp_path / "uncreated")],
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert by_name(report, "package.yaml")["state"] == "fail"
    assert by_name(report, "profile")["state"] == "warn"
    assert not (tmp_path / "uncreated").exists()
