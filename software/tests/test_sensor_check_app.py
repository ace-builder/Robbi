"""Load the real sensor project through AppHost and SDK, without hardware."""
from pathlib import Path
import threading

from PIL import Image
import pytest

from robbi.app_runtime.host import AppHost
from robbi.errors import BusyError


ROOT = Path(__file__).resolve().parents[1]


class SensorPort:
    def __init__(self):
        self.cancelled = threading.Event()
        self.reads, self.shown = [], []
        self.missing = False
        self.display_busy = False

    def capture_token(self):
        return 0, self.cancelled

    def tof(self, sensor):
        self.reads.append(sensor)
        stale = self.missing and sensor == "h1"
        return {"sensor": sensor, "enabled": True, "available": True,
                "fresh": not stale, "distance_cm": None if stale else 42 if sensor == "h1" else 17,
                "state": "stale" if stale else "ready", "age_s": 2 if stale else .02,
                "units": {"distance_cm": "cm"}}

    def imu(self):
        self.reads.append("imu")
        return {"enabled": True, "available": not self.missing, "fresh": not self.missing,
                "orientation_deg": None if self.missing else {"roll": 1, "pitch": -2, "yaw": 15},
                "acceleration_m_s2": None if self.missing else [0, 0, 9.81],
                "gyro_deg_s": None if self.missing else [0, 0, 0], "calibration": "complete",
                "heading_reference": "relative_gyro_no_magnetometer"}

    def show_image(self, path, token, cancelled, *, hold_s):
        assert not cancelled() and not token[1].is_set()
        if self.display_busy:
            raise BusyError("Screen is presenting another activity")
        self.shown.append((path, hold_s))
        return {"displayed": True, "reason": ""}


@pytest.fixture
def host(tmp_path):
    instance = AppHost(ROOT, tmp_path, SensorPort(), permissions_enabled=False)
    assert instance.scan()["errors"] == []
    yield instance
    instance.stop()
    instance._executor.shutdown(wait=True, cancel_futures=True)


def test_sensor_check_reads_both_ranges_and_imu_without_output(host):
    result = host.call("sensor-check", "inspect", {})
    assert result["ok"] is True and result["data"]["all_sensors_fresh"] is True
    assert host.robot.reads == ["h1", "h2", "imu"]
    assert host.robot.shown == []
    assert "H1 front: 42 cm" in result["text"]
    assert "H2 auxiliary: 17 cm" in result["text"]
    assert "relative yaw 15.0" in result["text"] and "not a compass" in result["text"]
    assert result["data"]["imu"]["acceleration_m_s2"] == [0, 0, 9.81]
    assert host.artifacts("sensor-check").list() == []


def test_sensor_check_reports_stale_and_missing_without_reusing_numbers(host):
    host.robot.missing = True
    result = host.call("sensor-check", "inspect", {})
    assert result["ok"] is False
    assert "H1 front: stale" in result["text"]
    assert "H2 auxiliary: 17 cm" in result["text"]
    assert "IMU: unavailable" in result["text"]
    assert result["data"]["tof"]["h1"]["distance_cm"] is None
    assert result["data"]["imu"]["orientation_deg"] is None
    assert host.robot.shown == []


def test_sensor_check_accepts_intentionally_absent_h2_but_still_requires_h1(host):
    original = host.robot.tof
    def single_sensor(sensor):
        result = original(sensor)
        if sensor == "h2":
            result.update(enabled=False, available=False, fresh=False, distance_cm=None, state="disabled")
        return result
    host.robot.tof = single_sensor
    result = host.call("sensor-check", "inspect", {})
    assert result["ok"] is True
    assert "H2 auxiliary: disabled" in result["text"]
    host.robot.tof = lambda sensor: ({**single_sensor(sensor), "fresh": False, "distance_cm": None}
                                    if sensor == "h1" else single_sensor(sensor))
    result = host.call("sensor-check", "inspect", {})
    assert result["ok"] is False
    assert "H1 front: stale" in result["text"]


def test_sensor_check_optional_display_uses_sdk_artifact_and_ten_second_hold(host):
    result = host.call("sensor-check", "inspect", {"show_on_screen": True})
    assert result["ok"] and result["data"]["display"]["displayed"]
    path, hold = host.robot.shown[0]
    assert hold == 10
    with Image.open(path) as image:
        assert image.size == (280, 240)
        with Image.open(host.project('sensor-check').root / 'images/screen.png') as artwork:
            assert image.getpixel((0, 0)) == artwork.convert('RGB').getpixel((0, 0))
    assert len(result["artifacts"]) == 1


def test_sensor_data_remains_available_when_optional_display_is_busy(host):
    host.robot.display_busy = True
    result = host.call("sensor-check", "inspect", {"show_on_screen": True})
    assert result["ok"] is True
    assert result["data"]["display"]["displayed"] is False
    assert "Screen is presenting another activity" in result["text"]
    assert result["data"]["tof"]["h1"]["distance_cm"] == 42
