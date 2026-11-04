import sys
import time
import types
import pytest

from robot.config import settings
from robot import tof


def rig(monkeypatch):
    addresses = {13: 0x30, 12: 0x31}  # Simulate a warm service restart.
    levels = {13: 1, 12: 1}
    history = []
    def output(chip, pin, level):
        history.append((pin, level))
        levels[pin] = level
        if not level:
            addresses[pin] = 0x29
    bus = types.SimpleNamespace(deinit=lambda: history.append("close_bus"))
    monkeypatch.setitem(sys.modules, "lgpio", types.SimpleNamespace(
        gpiochip_open=lambda chip: chip, gpio_claim_output=output,
        gpio_write=output, gpiochip_close=lambda chip: history.append("close_gpio")))
    monkeypatch.setitem(sys.modules, "adafruit_extended_bus", types.SimpleNamespace(
        ExtendedI2C=lambda number: bus if number == 1 else None))
    class Sensor:
        distance = 42.5
        data_ready = True
        def __init__(self, *_args):
            booting = [p for p in levels if levels[p] and addresses[p] == 0x29]
            assert len(booting) == 1, "I2C default address collision"
            self.pin = booting[0]
        def set_address(self, address):
            addresses[self.pin] = address
            history.append(("address", self.pin, address))
        def set_intermeasurement_ms(self, value):
            self.period_ms = value
        def start_ranging(self):
            pass
        def stop_ranging(self):
            pass
        def clear_interrupt(self):
            history.append(("clear", self.pin))
    monkeypatch.setattr(tof, "_new_sensor", Sensor)
    monkeypatch.setattr(settings, "tof_enabled", True)
    monkeypatch.setattr(settings, "tof_h2_enabled", True)
    monkeypatch.setattr(settings, "tof_poll_hz", 10.0)
    monkeypatch.setattr(settings, "tof_timing_budget_ms", 50)
    service = tof.ToFService()
    monkeypatch.setattr(service._stop_event, "wait", lambda *_args: False)
    return service, history, levels, addresses


def test_two_tofs_reset_and_assign_sequentially_on_warm_restart(monkeypatch):
    service, history, levels, addresses = rig(monkeypatch)
    service._open_bus()
    assert history[:2] == [(13, 0), (12, 0)]
    service._init_channel("h1")
    service._init_channel("h2")
    assert addresses == {13: 0x30, 12: 0x31}
    assert history.index(("address", 13, 0x30)) < history.index((12, 1))
    service._close_bus()
    assert levels == {13: 0, 12: 0}


def test_failed_h1_does_not_prevent_h2_or_supply_forward_distance(monkeypatch):
    service, _, levels, _ = rig(monkeypatch)
    factory = tof._new_sensor
    def missing_h1(*args):
        if levels[13]:
            raise OSError("No I2C device at 0x29")
        return factory(*args)
    monkeypatch.setattr(tof, "_new_sensor", missing_h1)
    service._open_bus()
    service._init_channel("h1")
    service._init_channel("h2")
    service._poll_channel("h2", service._sensors["h2"])
    assert not service.status()["available"]
    assert service.distance_cm() is None
    assert service.distance_cm(sensor="h2") == 42.5
    assert levels[13] == 0


def test_invalid_or_stale_ranges_cannot_reuse_old_obstacle_distance(monkeypatch):
    service, *_ = rig(monkeypatch)
    service._open_bus()
    service._init_channel("h1")
    sensor = service._sensors["h1"]
    service._poll_channel("h1", sensor)
    assert service.distance_cm() == 42.5
    sensor.distance = None
    service._poll_channel("h1", sensor)
    assert service.distance_cm() is None
    assert not service.status()["fresh"]
    service._set_status("h1", distance_cm=42.5, last_read_ts=time.monotonic() - 10)
    assert service.distance_cm() is None


def test_reload_only_signals_worker_and_never_closes_bus_in_request_thread(monkeypatch):
    service, history, *_ = rig(monkeypatch)
    service._thread = types.SimpleNamespace(is_alive=lambda: True)
    service._open_bus()
    service.reload_runtime_settings()
    assert service._reload_event.is_set()
    assert "close_bus" not in history


def test_optional_h2_is_never_probed_and_reports_disabled(monkeypatch):
    service, history, _, _ = rig(monkeypatch)
    monkeypatch.setattr(settings, "tof_h2_enabled", False)
    service._open_bus()
    service._init_channel("h1")
    service._init_channel("h2")
    service._poll_channel("h1", service._sensors["h1"])
    assert set(service._sensors) == {"h1"}
    assert (12, 1) not in history
    assert service.distance_cm() == 42.5
    secondary = service.status()["sensors"]["h2"]
    assert not secondary["enabled"] and not secondary["fresh"]
    assert not secondary["last_error"]
    assert service.distance_cm(sensor="h2") is None
    service._close_bus()


def test_worker_reconfigures_optional_h2_without_changing_primary_role(monkeypatch):
    service, history, _, _ = rig(monkeypatch)
    monkeypatch.setattr(settings, "tof_h2_enabled", False)
    monkeypatch.setattr(tof.time, "monotonic", lambda: 100.0)
    samples = []

    def wait(seconds):
        if seconds == 0.1:  # End of each polling cycle, after both channels.
            samples.append(service.status())
            if len(samples) == 3:
                service._stop_event.set()
                return True
            monkeypatch.setattr(settings, "tof_h2_enabled", len(samples) == 1)
            service._reload_event.set()
        return False

    monkeypatch.setattr(service._stop_event, "wait", wait)
    service._loop()
    assert len(samples) == 3
    assert all(sample["fresh"] and sample["primary_sensor"] == "h1" for sample in samples)
    assert [sample["sensors"]["h2"]["enabled"] for sample in samples] == [False, True, False]
    assert samples[1]["sensors"]["h2"]["fresh"]
    assert not samples[2]["sensors"]["h2"]["last_error"]
    assert history.count((12, 1)) == 1


def test_stuck_data_ready_restarts_only_failed_channel(monkeypatch):
    service, _, _, _ = rig(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(tof.time, 'monotonic', lambda: clock[0])
    service._open_bus()
    service._init_channel('h1')
    service._init_channel('h2')
    first, second = service._sensors['h1'], service._sensors['h2']
    service._poll_channel('h1', first)
    first.data_ready = False
    clock[0] += 1
    service._poll_channel('h1', first)
    assert not service.status()['fresh']
    clock[0] += 2
    with pytest.raises(TimeoutError) as error:
        service._poll_channel('h1', first)
    service._fail_channel('h1', error.value)
    assert not service.status()['available']
    assert service._sensors['h2'] is second
    assert service._retry_at['h1'] == 108
    clock[0] = 108
    service._init_channel('h1')
    service._poll_channel('h1', service._sensors['h1'])
    assert service.status()['fresh'] and service.distance_cm() == 42.5


def test_poor_returns_do_not_trigger_disconnect_or_stuck_sensor_reset(monkeypatch):
    service, *_ = rig(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(tof.time, 'monotonic', lambda: clock[0])
    service._open_bus()
    service._init_channel('h1')
    sensor = service._sensors['h1']
    sensor.distance = None
    for _ in range(20):
        clock[0] += .5
        service._poll_channel('h1', sensor)
        assert service.status()['range_state'] == 'no_return'
        assert service.status()['available'] and not service.status()['fresh']
        assert service.distance_cm() is None
    sensor.distance = 28
    service._poll_channel('h1', sensor)
    assert service.status()['range_state'] == 'ready'


def test_calibrated_intermeasurement_register(monkeypatch):
    writes=[]
    class Chip:
        def __init__(self,*a,**kw):pass
        def _read_register(self,reg,count):
            assert (reg,count)==(0x00DE,2)
            return bytes.fromhex('01a0')
        def _write_register(self,reg,value):writes.append((reg,value))
    monkeypatch.setitem(sys.modules,'adafruit_vl53l1x',types.SimpleNamespace(VL53L1X=Chip))
    sensor=tof._new_sensor(None,types.SimpleNamespace(is_set=lambda:False))
    sensor.set_intermeasurement_ms(20)
    assert writes==[(0x006C,int(416*20*1.075).to_bytes(4,'big'))]
    monkeypatch.setattr(sensor,'_read_register',lambda *a:b'\0\0')
    with pytest.raises(RuntimeError,match='oscillator'):sensor.set_intermeasurement_ms(20)


def test_fast_ranges_are_buffered_and_no_return_is_counted(monkeypatch):
    service,*_=rig(monkeypatch)
    for key,value in dict(tof_poll_hz=200,tof_distance_mode=1,tof_timing_budget_ms=20,
                          tof_intermeasurement_ms=20,tof_roi_size=8).items():
        monkeypatch.setattr(settings,key,value)
    service._open_bus();service._init_channel('h1');sensor=service._sensors['h1']
    assert sensor.period_ms==20 and sensor.roi_xy==(8,8)
    clock=[100.]
    monkeypatch.setattr(tof.time,'monotonic',lambda:clock[0])
    for i in range(600):
        clock[0]=100+i*.02
        sensor.distance=42.5 if i%4 else None
        service._poll_channel('h1',sensor)
    status=service.status()
    assert status['measured_hz']==50 and status['nominal_hz']==50
    assert status['valid_return_percent']==75
    batch=service.samples_since(1)
    assert len(batch['samples'])==512 and batch['dropped']==87
    assert batch['samples'][-1]['sample_time']==pytest.approx(clock[0]-.0125)
    assert len(service.samples_since(596)['samples'])==4
    batch['samples'][-1]['distance_cm']=999
    assert service.distance_cm()==42.5
    monkeypatch.setattr(settings,'tof_intermeasurement_ms',19)
    with pytest.raises(ValueError,match='at least'):service._desired_sensor_config()
