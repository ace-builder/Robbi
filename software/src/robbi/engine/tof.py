from __future__ import annotations

import logging
from collections import deque
import math
import struct
import threading
import time

from robbi.configuration.settings import settings
from .pcb import I2C_BUS, TOF_BOOT_ADDRESS, TOF_CHANNELS


def decode_observation(block):
    """One coherent VL53L1X result read (0x0089 through range at 0x0096)."""
    if len(block)!=15:raise ValueError('Incomplete ToF result block')
    status=block[0]&0x1f
    distance=struct.unpack('>H',block[13:15])[0]/10 if status==9 else None
    # ST raw status 4 maps to SIGNAL_FAIL: repeated weak/no-target evidence is
    # eligible for an open-space hypothesis, not a measured distance.
    return {'distance_cm':distance,'raw_range_status':status,'empty_candidate':status==4}

def _new_sensor(i2c, stop_event):
    import adafruit_vl53l1x

    class BoundedVL53L1X(adafruit_vl53l1x.VL53L1X):
        # The upstream initialization waits for data_ready without a deadline.
        def __init__(self):
            self._init_deadline = time.monotonic() + 2.0
            super().__init__(i2c, address=TOF_BOOT_ADDRESS)
            self._init_deadline = None

        @property
        def data_ready(self):
            if stop_event.is_set():
                raise RuntimeError("ToF service stopping")
            if self._init_deadline is not None and time.monotonic() > self._init_deadline:
                raise TimeoutError("ToF did not become ready within 2 seconds")
            return super().data_ready

        def set_intermeasurement_ms(self, milliseconds):
            # ST ULD SetInterMeasurementInMs: oscillator calibration is per chip.
            clock = struct.unpack('>H', self._read_register(0x00DE, 2))[0] & 0x3FF
            if not clock:
                raise RuntimeError('ToF oscillator calibration is invalid')
            self._write_register(0x006C, struct.pack('>I', int(clock * milliseconds * 1.075)))

        def ranging_observation(self):
            return decode_observation(self._read_register(0x0089,15))

    return BoundedVL53L1X()


class ToFService:
    """Single I/O owner for forward H1 and optional H2; cached public reads."""

    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._thread = None
        self._stop_event = threading.Event()
        self._reload_event = threading.Event()
        self._lock = threading.Lock()
        self._sensors = {}
        self._chip = None
        self._gpio = None
        self._i2c = None
        self._claimed = []
        self._sensor_config = None
        self._retry_at = {name: 0.0 for name in TOF_CHANNELS}
        self._ready_at = {}
        self._history = {name: deque(maxlen=512) for name in TOF_CHANNELS}
        self._status = {
            name: {"enabled": self._channel_enabled(name), "available": False,
                   "address": address, "xshut_gpio": pin,
                   "role": "forward" if name == "h1" else "auxiliary",
                   "distance_cm": None, "last_read_ts": 0.0, "last_error": "",
                   "sample_count": 0}
            for name, (pin, address) in TOF_CHANNELS.items()
        }

    @staticmethod
    def _channel_enabled(name: str) -> bool:
        return bool(settings.tof_enabled and (name == "h1" or
                    (name == "h2" and settings.tof_h2_enabled)))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tof")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        # Cleanup belongs to the worker, including when an I2C call is in flight.

    def reload_runtime_settings(self) -> None:
        self._reload_event.set()
        if settings.tof_enabled and (self._thread is None or not self._thread.is_alive()):
            self.start()

    def status(self) -> dict:
        with self._lock:
            sensors = {name: dict(value) for name, value in self._status.items()}
            for name, data in sensors.items():
                recent = list(self._history[name])[-100:]
                span = recent[-1]['last_read_ts']-recent[0]['last_read_ts'] if len(recent)>1 else 0
                data['measured_hz'] = round((len(recent)-1)/span,1) if span>0 else 0.
                data['valid_return_percent'] = round(100*sum(s['distance_cm'] is not None for s in recent)/len(recent),1) if recent else 0.
        now = time.monotonic()
        for name, data in sensors.items():
            age = max(0.0, now - data["last_read_ts"]) if data["last_read_ts"] else None
            data["age_s"] = round(age, 3) if age is not None else None
            data["enabled"] = self._channel_enabled(name)
            data["hint"] = "Check I2C bus 1, sensor cable and XSHUT pin; unavailable channels retry independently."
            data["fresh"] = bool(data["enabled"] and data["available"] and
                                 data["distance_cm"] is not None and age is not None and
                                 age <= float(settings.tof_max_age_s))
            if not data["enabled"]:
                data.update(available=False, distance_cm=None, last_error="", age_s=None, hint="")
            data["range_state"] = (
                "disabled" if not data["enabled"] else
                "unavailable" if not data["available"] else
                "waiting" if age is None else
                "stale" if age > float(settings.tof_max_age_s) else
                "no_return" if data["distance_cm"] is None else "ready")
            if not data['available'] or age is None or age > float(settings.tof_max_age_s):
                data['measured_hz'] = 0.
        # Existing motion and SLAM APIs keep using the forward sensor only.
        return {**sensors["h1"], "sensors": sensors, "primary_sensor": "h1", "i2c_bus": I2C_BUS,
                "poll_hz": settings.tof_poll_hz, "max_age_s": settings.tof_max_age_s,
                "timing_budget_ms": settings.tof_timing_budget_ms,
                "intermeasurement_ms": self._period_ms(), "roi_size": settings.tof_roi_size,
                "nominal_hz": round(1000/self._period_ms(),1)}

    def samples_since(self, sequence=0, sensor='h1'):
        """Bounded cached history; navigation never issues I2C reads."""
        with self._lock:
            rows = self._history[sensor]
            return {'samples':[dict(s) for s in rows if s['sample_count']>sequence],
                    'dropped':max(0, rows[0]['sample_count']-sequence-1) if rows and sequence else 0}

    @staticmethod
    def _period_ms():
        value = int(settings.tof_intermeasurement_ms)
        return value or max(int(settings.tof_timing_budget_ms), math.ceil(1000/settings.tof_poll_hz))

    def distance_cm(self, max_age_s: float | None = None, sensor: str = "h1") -> float | None:
        with self._lock:
            data = dict(self._status.get(sensor, {}))
        age_limit = float(settings.tof_max_age_s if max_age_s is None else max_age_s)
        if not self._channel_enabled(sensor) or not data.get("available") or data.get("distance_cm") is None:
            return None
        if time.monotonic() - data["last_read_ts"] > max(0.05, age_limit):
            return None
        return float(data["distance_cm"])

    @staticmethod
    def _desired_sensor_config() -> tuple:
        mode, budget = int(settings.tof_distance_mode), int(settings.tof_timing_budget_ms)
        if mode not in (1, 2):
            raise ValueError("ToF distance mode must be 1 or 2")
        budgets = (15, 20, 33, 50, 100, 200, 500) if mode == 1 else (20, 33, 50, 100, 200, 500)
        if budget not in budgets:
            raise ValueError(f"ToF timing budget must be one of {budgets}")
        hz = float(settings.tof_poll_hz)
        if not math.isfinite(hz) or not 0.5 <= hz <= 200:
            raise ValueError("ToF readiness poll rate must be 0.5 to 200 Hz")
        period = ToFService._period_ms()
        if period < budget:
            raise ValueError('ToF intermeasurement period must be at least its timing budget')
        roi = int(settings.tof_roi_size)
        if not 4 <= roi <= 16:
            raise ValueError('ToF ROI size must be 4 to 16')
        return mode, budget, bool(settings.tof_h2_enabled), period, roi

    def _open_bus(self) -> None:
        import lgpio
        from adafruit_extended_bus import ExtendedI2C

        self._gpio = lgpio
        self._chip = lgpio.gpiochip_open(0)
        # Keep unused XSHUT low as well: a physically present but disabled H2
        # must not reappear at 0x29 while the forward sensor is being assigned.
        for pin, _address in TOF_CHANNELS.values():
            lgpio.gpio_claim_output(self._chip, pin, 0)
            self._claimed.append(pin)
        self._stop_event.wait(0.01)
        self._i2c = ExtendedI2C(I2C_BUS)
        self._sensor_config = self._desired_sensor_config()
        self._retry_at = {name: 0.0 for name in TOF_CHANNELS}

    def _init_channel(self, name: str) -> None:
        if not self._channel_enabled(name):
            return
        pin, address = TOF_CHANNELS[name]
        try:
            self._gpio.gpio_write(self._chip, pin, 0)
            if self._stop_event.wait(0.01):
                return
            self._gpio.gpio_write(self._chip, pin, 1)
            if self._stop_event.wait(0.05):
                return
            sensor = _new_sensor(self._i2c, self._stop_event)
            sensor.set_address(address)
            sensor.distance_mode = int(settings.tof_distance_mode)
            sensor.timing_budget = int(settings.tof_timing_budget_ms)
            sensor.set_intermeasurement_ms(self._period_ms())
            sensor.roi_xy = (int(settings.tof_roi_size),)*2
            sensor.start_ranging()
            self._sensors[name] = sensor
            self._ready_at[name] = time.monotonic()
            self._set_status(name, available=True, distance_cm=None, last_read_ts=0.0, last_error="")
            self._log.info("ToF %s: BCM XSHUT=%s, I2C bus 1, 0x29 -> 0x%02x", name, pin, address)
        except Exception as exc:
            self._fail_channel(name, exc)

    def _fail_channel(self, name: str, exc: Exception) -> None:
        with self._lock:
            changed = self._status[name]["available"] or self._status[name]["last_error"] != str(exc)
        self._sensors.pop(name, None)
        self._ready_at.pop(name, None)
        self._set_status(name, available=False, distance_cm=None, last_error=str(exc))
        self._retry_at[name] = time.monotonic() + 5.0
        # A failed device must not stay at 0x29 when the next one boots.
        self._gpio.gpio_write(self._chip, TOF_CHANNELS[name][0], 0)
        if changed:
            self._log.warning("ToF %s unavailable: %s", name, exc)

    def _poll_channel(self, name: str, sensor) -> None:
        if not self._channel_enabled(name):
            return
        poll_started = time.monotonic()
        if not sensor.data_ready:
            # A sensor can stop producing interrupts without raising an I2C
            # error. Restart only that channel; never reuse an old range.
            deadline = max(2.0, float(settings.tof_timing_budget_ms) / 250,
                           3 / max(.5, float(settings.tof_poll_hz)))
            if time.monotonic() - self._ready_at.get(name, time.monotonic()) > deadline:
                raise TimeoutError("ToF stopped producing readings; restarting channel")
            return
        self._ready_at[name] = time.monotonic()
        observe=getattr(sensor,'ranging_observation',None)
        observation=observe() if callable(observe) else {'distance_cm':sensor.distance,'empty_candidate':False,'raw_range_status':None}
        distance=observation['distance_cm']
        sensor.clear_interrupt()
        valid = distance is not None and math.isfinite(float(distance)) and float(distance) >= 0
        received = time.monotonic()
        with self._lock:
            row = dict(available=True, distance_cm=round(float(distance),1) if valid else None,
                       raw_range_status=observation.get('raw_range_status'),
                       empty_candidate=not valid and observation.get('empty_candidate') is True,
                       last_read_ts=received, sample_count=self._status[name]['sample_count']+1,
                       sample_time=poll_started-settings.tof_timing_budget_ms/2000-.5/settings.tof_poll_hz,
                       timestamp_source='estimated_integration_midpoint',
                       timing_uncertainty_ms=round(500/settings.tof_poll_hz+(received-poll_started)*500,2),
                       read_duration_ms=round((received-poll_started)*1000,2),
                       last_error='' if valid else 'No valid range (out of range or poor return)')
            self._status[name].update(row)
            self._history[name].append(row)

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    if self._reload_event.is_set():
                        self._reload_event.clear()
                        if not settings.tof_enabled or self._sensor_config != self._desired_sensor_config():
                            self._close_bus()
                    if not settings.tof_enabled:
                        self._close_bus()
                        self._stop_event.wait(0.2)
                        continue
                    if self._i2c is None:
                        self._desired_sensor_config()
                        self._open_bus()
                    started = time.monotonic()
                    for name in TOF_CHANNELS:
                        if self._stop_event.is_set():
                            break
                        if not self._channel_enabled(name):
                            continue
                        if name not in self._sensors and started >= self._retry_at[name]:
                            self._init_channel(name)
                        sensor = self._sensors.get(name)
                        if sensor is not None:
                            try:
                                self._poll_channel(name, sensor)
                            except Exception as exc:
                                self._fail_channel(name, exc)
                    interval = 1.0 / max(0.5, min(200.0, float(settings.tof_poll_hz)))
                    self._stop_event.wait(max(0.001, interval - (time.monotonic() - started)))
                except Exception as exc:
                    for name in TOF_CHANNELS:
                        self._set_status(name, available=False, distance_cm=None,
                                         last_error=str(exc) if self._channel_enabled(name) else "")
                    self._log.warning("ToF bus unavailable: %s", exc)
                    self._close_bus()
                    self._stop_event.wait(5.0)
        finally:
            self._close_bus()

    def _close_bus(self) -> None:
        for sensor in self._sensors.values():
            try:
                sensor.stop_ranging()
            except Exception as exc:
                self._log.warning('ToF sensor cleanup failed: %s', exc)
        self._sensors.clear()
        self._ready_at.clear()
        with self._lock:
            for history in self._history.values():history.clear()
        if self._chip is not None:
            for pin in self._claimed:
                try:
                    self._gpio.gpio_write(self._chip, pin, 0)
                except Exception as exc:
                    self._log.warning('ToF shutdown GPIO cleanup failed: %s', exc)
            try:
                self._gpio.gpiochip_close(self._chip)
            except Exception as exc:
                self._log.warning("ToF GPIO cleanup failed: %s", exc)
            self._chip = None
        self._claimed.clear()
        if self._i2c is not None:
            try:
                self._i2c.deinit()
            except Exception as exc:
                self._log.warning("ToF I2C cleanup failed: %s", exc)
            self._i2c = None
        self._sensor_config = None
        for name in TOF_CHANNELS:
            self._set_status(name, available=False, distance_cm=None, last_read_ts=0.0)

    def _set_status(self, name: str, **values) -> None:
        with self._lock:
            self._status[name].update(values)
