"""Onboard U4 diagnostics. Never claims motor/audio GPIO or sends motion commands."""

from __future__ import annotations

import logging
import copy
import math
import threading
import time

from robbi.configuration.settings import DATA_DIR, settings
from robbi.imu_reference import UprightReference
from .orientation import GyroCalibration, OrientationFilter, euler_degrees
from .heading import GravityHeading
from .pcb import I2C_BUS, IMU_ADDRESSES


class IMUSensor:
    def __init__(self, stop_event):
        from adafruit_extended_bus import ExtendedI2C
        from adafruit_lsm6ds import AccelRange, GyroRange, Rate
        from adafruit_lsm6ds.lsm6ds3trc import LSM6DS3TRC

        class BoundedSensor(LSM6DS3TRC):
            def reset(self):
                self._sw_reset = True
                deadline = time.monotonic() + 1.0
                while self._sw_reset:
                    if stop_event.wait(0.005) or time.monotonic() > deadline:
                        raise TimeoutError("IMU reset timed out or was cancelled")

        self.bus = ExtendedI2C(I2C_BUS)
        self.sensor = None
        try:
            # Verify identity BEFORE writing; another device may occupy either address.
            self.address = self.find_address(self.bus)
            self.sensor = BoundedSensor(self.bus, address=self.address)
            self.sensor.accelerometer_range = AccelRange.RANGE_4G
            self.sensor.gyro_range = GyroRange.RANGE_500_DPS
            self.sensor.accelerometer_data_rate = Rate.RATE_104_HZ
            self.sensor.gyro_data_rate = Rate.RATE_104_HZ
            self._ready = bytearray(1)
        except Exception:
            self.close()
            raise

    @staticmethod
    def find_address(bus):
        reply = bytearray(1)
        for address in IMU_ADDRESSES:
            try:
                bus.writeto_then_readfrom(address, b"\x0f", reply)
                if reply[0] == 0x6A:
                    return address
            except OSError:
                continue
        raise RuntimeError("LSM6DS3TR-C not found at 0x6A/0x6B (WHO_AM_I must be 0x6A)")

    def read(self):
        self.bus.writeto_then_readfrom(self.address, b"\x1e", self._ready)
        if self._ready[0] & 3 != 3:
            return None
        return self.sensor.acceleration, self.sensor.gyro

    def temperature(self):
        return self.sensor.temperature

    def close(self):
        if self.sensor is not None:
            try:
                self.sensor.accelerometer_data_rate = 0
                self.sensor.gyro_data_rate = 0
            except Exception:
                pass
        self.sensor = None
        self.bus.deinit()


class IMUService:
    def __init__(self, sensor_factory=IMUSensor, can_calibrate=lambda: True):
        self._factory = sensor_factory
        self._can_calibrate = can_calibrate
        self._log = logging.getLogger(__name__)
        self._stop = threading.Event()
        self._thread = None
        self._start_lock = threading.Lock()
        self._lock = threading.Lock()
        self._calibrate_requested = False
        self._heading = GravityHeading()
        self._upright = UprightReference(DATA_DIR)
        self._data = {"available": False, "last_error": "", "sample_count": 0,
                      "last_sample_monotonic": None, "timestamp": None,
                      "quaternion": None, "orientation_deg": None,
                      "acceleration_m_s2": None, "gyro_deg_s": None,
                      "gyro_bias_deg_s": [0, 0, 0], "temperature_c": None,
                      "address": None, "calibration": "waiting", "calibration_progress": 0,
                      "measured_hz": 0, "sample_gaps": 0, "drive_heading": None}

    def start(self):
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="imu", daemon=True)
            self._thread.start()

    def reload_runtime_settings(self):
        # An initially disabled component has never started a sampler. Enabling
        # it must work without restarting the process or opening I2C here.
        if settings.imu_enabled:
            self.start()

    def shutdown(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def status(self):
        with self._lock:
            data = copy.deepcopy(self._data)
        last = data.pop("last_sample_monotonic")
        age = max(0, time.monotonic() - last) if last is not None else None
        return {**data, "sensor": "LSM6DS3TR-C", "i2c_bus": I2C_BUS,
                "upright_reference": self._upright.snapshot(),
                "hint": "Check I2C bus 1 and IMU identity; keep robot still during gyro calibration.",
                "enabled": bool(settings.imu_enabled), "poll_hz": settings.imu_poll_hz,
                "age_s": round(age, 3) if age is not None else None,
                "fresh": bool(settings.imu_enabled and data["available"] and age is not None and age < 0.5),
                "heading_reference": "relative_gyro_no_magnetometer", "quaternion_order": "wxyz"}

    def calibrate(self):
        if not self.status()["fresh"]:
            raise ValueError("IMU is not providing fresh readings")
        if not self._can_calibrate():
            raise ValueError("Stop motor movement before calibrating the gyro")
        with self._lock:
            self._calibrate_requested = True
            self._data['drive_heading'] = None
        return {"ok": True, "message": "Keep Robbi completely still for 3 seconds."}

    def set_upright_reference(self, acceleration):
        if not self._can_calibrate():
            raise ValueError("Stop motor movement before setting the upright pose")
        return self._upright.save(acceleration)

    def heading_sample(self):
        """Small cached snapshot for motion; never reads I2C or copies diagnostics."""
        with self._lock:
            sample = self._data['drive_heading']
            if not settings.imu_enabled or not self._data['available'] or sample is None:
                return None
            return dict(sample)

    def _publish(self, **fields):
        with self._lock:
            self._data.update(fields)

    def _loop(self):
        sensor = None
        try:
            while not self._stop.is_set():
                if not settings.imu_enabled:
                    if sensor is not None:
                        sensor.close()
                        sensor = None
                    self._publish(available=False, calibration="disabled", drive_heading=None)
                    self._stop.wait(0.2)
                    continue
                try:
                    if sensor is None:
                        sensor = self._factory(self._stop)
                        attitude = OrientationFilter()
                        self._heading.reset()
                        now = time.monotonic()
                        calibration = GyroCalibration(now)
                        temperature_at = rate_start = last_sample = now
                        count = 0
                        temperature = None
                        self._publish(available=True, address=sensor.address, last_error="",
                                      last_sample_monotonic=None, quaternion=None, orientation_deg=None,
                                      calibration="collecting", calibration_progress=0, drive_heading=None)
                        self._log.info("IMU LSM6DS3TR-C connected on bus %s at 0x%02x", I2C_BUS, sensor.address)
                    started = time.monotonic()
                    with self._lock:
                        requested = self._calibrate_requested
                        self._calibrate_requested = False
                    if requested:
                        calibration = GyroCalibration(started)
                    sample = sensor.read()
                    if sample is not None:
                        acceleration, gyro = sample
                        if not all(math.isfinite(v) for v in (*acceleration, *gyro)):
                            raise ValueError("Non-finite IMU sample")
                        now = time.monotonic()
                        last_sample = now
                        if calibration.state == "collecting":
                            bias = calibration.update(acceleration, gyro, now, self._can_calibrate())
                            if bias is not None:
                                attitude.bias = bias
                        quaternion = attitude.update(acceleration, gyro, now)
                        heading = self._heading.update(quaternion, gyro, attitude.bias, acceleration, now,
                                                       calibration.state == 'complete' and attitude.initialized)
                        if now >= temperature_at:
                            temperature = sensor.temperature()
                            temperature_at = now + 1
                        count += 1
                        if now - rate_start >= 1:
                            self._publish(measured_hz=round(count / (now-rate_start), 1))
                            count, rate_start = 0, now
                        self._publish(available=True, timestamp=time.time(), last_sample_monotonic=now,
                                      sample_count=self._data["sample_count"] + 1,
                                      quaternion=list(quaternion) if attitude.initialized else None,
                                      orientation_deg=euler_degrees(quaternion) if attitude.initialized else None,
                                      acceleration_m_s2=list(acceleration), gyro_deg_s=[math.degrees(v) for v in gyro],
                                      gyro_bias_deg_s=[math.degrees(v) for v in attitude.bias],
                                      temperature_c=temperature, sample_gaps=attitude.sample_gaps,
                                      calibration=calibration.state, calibration_progress=round(calibration.progress, 2),
                                      drive_heading=heading)
                    elif time.monotonic() - last_sample > 2:
                        raise TimeoutError("IMU stopped producing fresh samples")
                    interval = 1 / max(10, min(100, float(settings.imu_poll_hz)))
                    self._stop.wait(max(0.001, interval - (time.monotonic()-started)))
                except Exception as exc:
                    if self._data["last_error"] != str(exc):
                        self._log.warning("IMU unavailable: %s", exc)
                    self._publish(available=False, last_error=str(exc), calibration="unavailable", drive_heading=None)
                    if sensor is not None:
                        try:
                            sensor.close()
                        except Exception:
                            pass
                        sensor = None
                    self._stop.wait(5)
        finally:
            if sensor is not None:
                try:
                    sensor.close()
                except Exception:
                    self._log.exception("IMU cleanup failed")
            self._publish(available=False, drive_heading=None)
