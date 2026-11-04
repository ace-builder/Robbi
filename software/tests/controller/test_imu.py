import math
import time

import pytest

from robot.config import settings
from robot.imu import IMUSensor, IMUService
from robot.orientation import GRAVITY, GyroCalibration, OrientationFilter, euler_degrees, norm


def test_gravity_initializes_level_sideways_and_upside_down():
    for acceleration, roll, pitch in [((0, 0, GRAVITY), 0, 0),
                                       ((0, GRAVITY, 0), 90, 0),
                                       ((-GRAVITY, 0, 0), 0, 90),
                                       ((0, 0, -GRAVITY), 180, 0)]:
        f = OrientationFilter()
        q = f.update(acceleration, (0, 0, 0), 0)
        assert norm(q) == pytest.approx(1)
        attitude = euler_degrees(q)
        assert attitude["roll"] == pytest.approx(roll)
        assert attitude["pitch"] == pytest.approx(pitch)


def test_gyro_yaw_integration_and_unit_quaternion():
    f = OrientationFilter()
    for i in range(101):
        f.update((0, 0, GRAVITY), (0, 0, math.pi / 2), i / 100)
    assert euler_degrees(f.q)["yaw"] == pytest.approx(90, abs=0.01)
    assert norm(f.q) == pytest.approx(1)


def test_gravity_correction_converges_without_compass_yaw_correction():
    f = OrientationFilter()
    f.update((0, 0, GRAVITY), (0, 0, 0), 0)
    f.q = (math.cos(math.pi / 12), math.sin(math.pi / 12), 0, 0)
    for i in range(1, 501):
        f.update((0, 0, GRAVITY), (0, 0, 0), i / 100)
    assert abs(euler_degrees(f.q)["roll"]) < 0.01


def test_bias_correction_stops_static_drift_and_gap_does_not_invent_rotation():
    f = OrientationFilter()
    f.bias = (0, 0, 0.03)
    f.update((0, 0, GRAVITY), f.bias, 0)
    for i in range(1, 100):
        f.update((0, 0, GRAVITY), f.bias, i / 100)
    assert euler_degrees(f.q)["yaw"] == pytest.approx(0)
    before = f.q
    f.update((0, 0, GRAVITY), (0, 0, 4), 10)
    assert f.q == before
    assert f.sample_gaps == 1


def test_freefall_does_not_initialize_or_corrupt_filter():
    f = OrientationFilter()
    f.update((0, 0, 0), (0, 0, 0), 0)
    assert not f.initialized
    f.update((0, 0, GRAVITY), (0, 0, 0), 1)
    f.update((0, 0, 0), (0, 0, 0), 1.02)
    assert norm(f.q) == pytest.approx(1)
    with pytest.raises(ValueError):
        f.update((float("nan"), 0, 0), (0, 0, 0), 1.03)


def test_calibration_requires_continuous_stationary_window():
    c = GyroCalibration(0)
    bias = None
    for i in range(151):
        bias = c.update((0, 0, GRAVITY), (0.01, -0.02, 0.005), i / 50)
    assert bias == pytest.approx((0.01, -0.02, 0.005))
    assert c.state == "complete"
    c = GyroCalibration(0)
    for i in range(100):
        assert c.update((0, 0, GRAVITY), (0, 0, 0), i / 50) is None
    assert c.update((0, 0, GRAVITY), (0, 0, 1), 2) is None
    assert c.progress == 0
    c.update((0, 0, GRAVITY), (0, 0, 0), 31)
    assert c.state == "timed_out"


def test_address_detection_verifies_chip_id_without_configuration_writes():
    calls = []
    class Bus:
        def writeto_then_readfrom(self, address, register, reply):
            calls.append((address, register))
            reply[0] = 0 if address == 0x6A else 0x6A
    assert IMUSensor.find_address(Bus()) == 0x6B
    assert calls == [(0x6A, b"\x0f"), (0x6B, b"\x0f")]
    class OtherDevice:
        def writeto_then_readfrom(self, address, register, reply):
            reply[0] = 0x42
    with pytest.raises(RuntimeError, match="WHO_AM_I"):
        IMUSensor.find_address(OtherDevice())


def test_calibration_accepts_measured_pcb_bias_but_rejects_changing_rotation():
    c = GyroCalibration(0)
    gyro = tuple(math.radians(v) for v in (4.06, -5.819, -1.217))
    for i in range(151):
        bias = c.update((0, 0, GRAVITY), gyro, i / 50)
    assert bias == pytest.approx(gyro)
    c = GyroCalibration(0)
    c.update((0, 0, GRAVITY), gyro, 0)
    c.update((0, 0, GRAVITY), (gyro[0]+.02, gyro[1], gyro[2]), .02)
    assert c.window_start is None


def test_service_snapshots_are_cached_and_stale_readings_are_visible(monkeypatch):
    monkeypatch.setattr(settings, "imu_enabled", True)
    monkeypatch.setattr(settings, "imu_poll_hz", 50)
    service = IMUService()
    service._publish(available=True, last_sample_monotonic=time.monotonic()-2,
                     quaternion=[1, 0, 0, 0])
    assert not service.status()["fresh"]
    with pytest.raises(ValueError):
        service.calibrate()
    service._publish(last_sample_monotonic=time.monotonic())
    assert service.calibrate()["ok"]
    assert service._calibrate_requested
    service._can_calibrate = lambda: False
    with pytest.raises(ValueError, match="Stop motor"):
        service.calibrate()


def test_known_motor_motion_prevents_calibration_even_with_constant_yaw():
    c = GyroCalibration(0)
    for i in range(151):
        assert c.update((0, 0, GRAVITY), (0, 0, 0.1), i/50, stationary_allowed=False) is None
    assert c.progress == 0


def test_background_worker_collects_samples_and_releases_only_its_sensor(monkeypatch):
    monkeypatch.setattr(settings, "imu_enabled", True)
    monkeypatch.setattr(settings, "imu_poll_hz", 50)
    closed = []
    class Sensor:
        address = 0x6B
        def __init__(self, stop):
            pass
        def read(self):
            return (0, 0, GRAVITY), (0, 0, 0)
        def temperature(self):
            return 25
        def close(self):
            closed.append(True)
    service = IMUService(Sensor)
    service.start()
    deadline = time.monotonic()+2
    try:
        while service.status()["sample_count"] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.status()["fresh"]
        assert service.status()["sample_count"] >= 3
        assert service.status()["quaternion"] == [1, 0, 0, 0]
    finally:
        service.shutdown()
    assert closed == [True]
    assert not service.status()["available"]
