import importlib

import json

from pathlib import Path

import runpy

import sys

import types

import threading

import pytest



from robot.config import settings





def _motor_gpio_module(monkeypatch):

    fake_lgpio = types.SimpleNamespace(

        BOTH_EDGES=3,

        SET_PULL_UP=32,

        gpiochip_open=lambda *_args, **_kwargs: 0,

        gpio_claim_output=lambda *_args, **_kwargs: None,

        gpio_claim_alert=lambda *_args, **_kwargs: None,

        tx_pwm=lambda *_args, **_kwargs: None,

        tx_busy=lambda *_args, **_kwargs: False,

        TX_PWM=0,

        gpio_write=lambda *_args, **_kwargs: None,

        gpio_read=lambda *_args, **_kwargs: 0,

        callback=lambda *_args, **_kwargs: types.SimpleNamespace(cancel=lambda: None),

        gpiochip_close=lambda *_args, **_kwargs: None,

    )

    monkeypatch.setitem(sys.modules, "lgpio", fake_lgpio)

    module = importlib.import_module("robot.motor_gpio")

    return importlib.reload(module)





def test_encoder_counts_use_quadrature_direction(monkeypatch):

    motor_gpio = _motor_gpio_module(monkeypatch)



    assert motor_gpio._transition_delta(0b00, 0b01) == 1

    assert motor_gpio._transition_delta(0b01, 0b00) == -1

    assert motor_gpio._transition_delta(0b00, 0b11) == 0





def test_encoder_state_uses_callback_edge_level(monkeypatch):

    motor_gpio = _motor_gpio_module(monkeypatch)



    assert motor_gpio._state_with_edge(0b00, motor_gpio.ENCODER1_A, 1, motor_gpio.ENCODER1_A, motor_gpio.ENCODER1_B) == 0b10

    assert motor_gpio._state_with_edge(0b10, motor_gpio.ENCODER1_B, 1, motor_gpio.ENCODER1_A, motor_gpio.ENCODER1_B) == 0b11

    assert motor_gpio._state_with_edge(0b11, motor_gpio.ENCODER1_A, 0, motor_gpio.ENCODER1_A, motor_gpio.ENCODER1_B) == 0b01





def test_logical_encoder_mapping_swaps_before_inverting(monkeypatch):

    motor_gpio = _motor_gpio_module(monkeypatch)

    monkeypatch.setattr(settings, "motion_swap_encoder_inputs", True)

    monkeypatch.setattr(settings, "motion_encoder_left_invert", True)

    monkeypatch.setattr(settings, "motion_encoder_right_invert", False)



    assert motor_gpio._logical_encoder_pair(10, -20) == (20, 10)





def test_encoder_counts_measure_all_edges_and_reverse_without_command(monkeypatch):

    motor_gpio = _motor_gpio_module(monkeypatch)

    monkeypatch.setattr(settings, "motion_swap_encoder_inputs", False)

    monkeypatch.setattr(settings, "motion_encoder_left_invert", False)

    monkeypatch.setattr(settings, "motion_encoder_right_invert", False)

    motor_gpio.initialize()

    a, b = motor_gpio.ENCODER1_A, motor_gpio.ENCODER1_B

    for tick, (pin, level) in enumerate(((b, 1), (a, 1), (b, 0), (a, 0))):

        motor_gpio._left_encoder_callback(0, pin, level, tick * 100_000)

    assert motor_gpio.counts() == (4, 0)

    for tick, (pin, level) in enumerate(((a, 1), (b, 1), (a, 0), (b, 0))):

        motor_gpio._left_encoder_callback(0, pin, level, tick * 100_000)

    assert motor_gpio.counts() == (0, 0)





def _recording_motor(monkeypatch):

    module = _motor_gpio_module(monkeypatch)

    calls = []

    for fn in ("gpio_claim_output", "gpio_claim_alert", "gpio_write", "tx_pwm"):

        monkeypatch.setattr(module.lgpio, fn, lambda *args, fn=fn: calls.append((fn, *args)))

    monkeypatch.setattr(module.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    for name in ("motion_left_motor_invert", "motion_right_motor_invert", "motion_swap_motor_outputs"):

        monkeypatch.setattr(settings, name, False)

    monkeypatch.setattr(settings, "motion_pwm_max_duty", 100.0)

    return module, calls





def test_pcb_pin_ownership_enable_delay_and_pwm_limit(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    motor.initialize()

    assert [c[2] for c in calls if c[0] == "gpio_claim_output"] == [26, 4, 17, 22, 23]

    assert [c[2] for c in calls if c[0] == "gpio_claim_alert"] == [27, 5, 6, 16]

    assert all(c[3:] == (3, 32) for c in calls if c[0] == "gpio_claim_alert")

    assert not any(c[2] in (15, 7) for c in calls if c[0] in ("gpio_claim_output", "gpio_claim_alert"))

    assert not motor.driver_status()["enabled"]

    motor.set_wheel_pwm(100, -100)

    enable = calls.index(("gpio_write", 0, 26, 1))

    delay = calls.index(("sleep", 0.002))

    pwm = [c for c in calls if c[0] == "tx_pwm" and c[-1] > 0]

    assert enable < delay < calls.index(pwm[0])

    assert pwm == [("tx_pwm", 0, 4, 1000, 100.0), ("tx_pwm", 0, 23, 1000, 100.0)]

    assert not any(c[2] in (12, 13, 18, 19, 20, 21, 7, 9) for c in calls if c[0] in ("gpio_write", "tx_pwm"))

    calls.clear()

    motor.stop()

    assert calls[0] == ("gpio_write", 0, 26, 0)

    assert not motor.driver_status()["enabled"]





def test_gpio_failure_disables_driver_and_rejects_restart(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    def fail_pwm(chip, pin, hz, duty):

        if duty > 0:

            raise RuntimeError("PWM failed")

    monkeypatch.setattr(motor.lgpio, "tx_pwm", fail_pwm)

    with pytest.raises(RuntimeError, match="PWM failed"):

        motor.set_wheel_pwm(30, 30)

    assert ("gpio_write", 0, 26, 0) in calls

    assert not motor.driver_status()["enabled"]

    with pytest.raises(RuntimeError, match="restart service"):

        motor.set_wheel_pwm(30, 30)





def test_partial_initialization_disables_enable_and_releases_claims(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    def fail_claim(*args):

        raise RuntimeError("encoder busy")

    monkeypatch.setattr(motor.lgpio, "gpio_claim_alert", fail_claim)

    with pytest.raises(RuntimeError, match="encoder busy"):

        motor.initialize()

    assert ("gpio_write", 0, 26, 0) in calls

    assert motor._chip is None





def test_brake_uses_drv8833_truth_table(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    motor.set_wheel_pwm(30, 30)

    calls.clear()

    motor.set_wheel_pwm_with_brake(0, 30, left_brake=True)

    assert ("gpio_write", 0, 4, 1) in calls

    assert ("gpio_write", 0, 17, 1) in calls

    assert not any(c[0] == "tx_pwm" and c[-1] == 100 for c in calls)





def test_brake_cancels_pwm_before_static_high_and_stop_disables_first(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    active = set()



    def pwm(chip, pin, hz, duty):

        calls.append(("tx_pwm", chip, pin, hz, duty))

        if hz:

            active.add(pin)

        else:

            assert pin in active, "Cancelling inactive PWM is an lgpio error"

            active.remove(pin)



    def write(chip, pin, level):

        if level and pin != motor.ROBOT_EN:

            assert pin not in active, "An active PWM thread would overwrite brake HIGH"

        calls.append(("gpio_write", chip, pin, level))



    monkeypatch.setattr(motor.lgpio, "tx_pwm", pwm)

    monkeypatch.setattr(motor.lgpio, "tx_busy", lambda chip, pin, kind: pin in active)

    monkeypatch.setattr(motor.lgpio, "gpio_write", write)

    motor.set_wheel_pwm(45, -45)

    motor.set_wheel_pwm_with_brake(0, 0, True, True)

    assert not active

    assert ("tx_pwm", 0, 4, 0, 0) in calls

    assert ("tx_pwm", 0, 23, 0, 0) in calls

    motor.set_wheel_pwm(-38, 38)

    calls.clear()

    motor.stop()

    assert calls[0] == ("gpio_write", 0, 26, 0)

    assert not active





def test_encoder_callbacks_are_not_blocked_by_output_lock(monkeypatch):

    motor = _motor_gpio_module(monkeypatch)

    motor.initialize()

    completed = threading.Event()



    def callback():

        motor._right_encoder_callback(0, motor.ENCODER2_B, 1, 10)

        completed.set()



    with motor._lock:

        worker = threading.Thread(target=callback, daemon=True)

        worker.start()

        responsive = completed.wait(.5)

    worker.join(1)

    assert responsive

    assert motor.physical_counts()["encoder2_quad"] == 1





def test_reset_excludes_old_queued_edges_without_losing_quadrature_phase(monkeypatch):

    motor = _motor_gpio_module(monkeypatch)

    motor.initialize()

    monkeypatch.setattr(motor.time, "monotonic_ns", lambda: 100)

    motor.reset_counts()

    motor._right_encoder_callback(0, motor.ENCODER2_B, 1, 99)

    assert motor.physical_counts()["encoder2_quad"] == 0

    motor._right_encoder_callback(0, motor.ENCODER2_A, 1, 101)

    assert motor.physical_counts()["encoder2_quad"] == 1





def test_stop_latch_rejects_all_late_pwm_and_brake_writes_until_explicit_arm(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    motor.set_wheel_pwm(35, -35)

    motor.inhibit()

    calls.clear()

    motor.set_wheel_pwm(35, -35)

    motor.set_wheel_pwm_with_brake(0, 0, True, True)

    motor.pulse_wheel_pwm(-38, 38, .008, .008)

    assert not calls

    assert not motor.driver_status()["enabled"]

    assert motor.driver_status()["inhibited"]

    motor.arm()

    motor.set_wheel_pwm(35, -35)

    assert motor.driver_status()["enabled"]





def test_correction_pwm_is_finite_and_respects_motor_inversion(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    monkeypatch.setattr(settings, "motion_left_motor_invert", True)

    motor.pulse_wheel_pwm(-35, 35, .008, .025)

    pulses = [c for c in calls if c[0] == "tx_pwm"]

    assert pulses == [("tx_pwm", 0, 4, 1000, 35, 0, 8), ("tx_pwm", 0, 22, 1000, 35, 0, 25)]

    # A second identical pulse is a new burst, not suppressed by output caching.

    motor.pulse_wheel_pwm(-35, 35, .008, .025)

    assert len([c for c in calls if c[0] == "tx_pwm"]) == 4

    with pytest.raises(ValueError):

        motor.pulse_wheel_pwm(35, 35, .5, .5)





def test_late_background_command_cannot_rearm_after_shutdown(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    motor.initialize()

    motor.cleanup()

    calls.clear()

    with pytest.raises(RuntimeError, match="shut down"):

        motor.set_wheel_pwm(30, 30)

    assert ("gpio_write", 0, 26, 1) not in calls





def test_configured_pwm_limit_still_applies(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    monkeypatch.setattr(settings, "motion_pwm_max_duty", 42)

    motor.set_wheel_pwm(100, -100)

    assert max(c[-1] for c in calls if c[0] == "tx_pwm") == 42





def test_left_motor_inversion_does_not_change_encoder_sign(monkeypatch):

    motor, calls = _recording_motor(monkeypatch)

    monkeypatch.setattr(settings, "motion_left_motor_invert", True)

    monkeypatch.setattr(settings, "motion_swap_encoder_inputs", False)

    monkeypatch.setattr(settings, "motion_encoder_left_invert", False)

    monkeypatch.setattr(settings, "motion_encoder_right_invert", False)

    motor.set_wheel_pwm(70, 70)

    assert ("tx_pwm", 0, 17, 1000, 70) in calls

    assert ("tx_pwm", 0, 22, 1000, 70) in calls

    assert motor._logical_encoder_pair(100, 100) == (100, 100)





def test_right_encoder_decodes_full_cycles_and_reports_both_channels(monkeypatch):

    motor = _motor_gpio_module(monkeypatch)

    monkeypatch.setattr(settings, "motion_swap_encoder_inputs", False)

    monkeypatch.setattr(settings, "motion_encoder_right_invert", False)

    motor.initialize()

    a, b = motor.ENCODER2_A, motor.ENCODER2_B

    assert (a, b) == (6, 16)

    for cycle in range(100):

        for pin, level in ((b, 1), (a, 1), (b, 0), (a, 0)):

            motor._right_encoder_callback(0, pin, level, cycle)

    assert motor.counts() == (0, 400)

    channels = motor.physical_counts()["channels"]

    assert channels["right_a"]["edges"] == 200

    assert channels["right_b"]["edges"] == 200

    assert channels["right_b"]["gpio"] == 16

    motor.reset_counts()

    assert motor.counts() == (0, 0)

    assert motor.physical_counts()["channels"]["right_a"]["edges"] == 200





@pytest.mark.parametrize("channel", ["ENCODER2_A", "ENCODER2_B"])

def test_one_right_channel_only_does_not_fabricate_revolutions(monkeypatch, channel):

    motor = _motor_gpio_module(monkeypatch)

    motor.initialize()

    for tick in range(200):

        motor._right_encoder_callback(0, getattr(motor, channel), (tick + 1) % 2, tick)

    assert motor.physical_counts()["encoder2_quad"] == 0

    channels = motor.physical_counts()["channels"]

    assert channels["right_a"]["edges"] == (200 if channel == "ENCODER2_A" else 0)

    assert channels["right_b"]["edges"] == (200 if channel == "ENCODER2_B" else 0)





def test_independent_probe_matches_standard_right_encoder_wiring():

    from robot.pcb import LEFT_ENCODER, RIGHT_ENCODER



    probe = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools" / "hardware" / "encoder_probe.py"))

    assert probe["PAIRS"] == {"left": LEFT_ENCODER, "right": RIGHT_ENCODER}

    assert probe["PINS"][16] == ("right_b", 36)

    assert 15 not in probe["PINS"]

    assert 7 not in probe["PINS"]





@pytest.mark.parametrize("initial", [0, 1, 2, 3])

@pytest.mark.parametrize("direction", [-1, 1])

def test_registered_right_callbacks_count_from_every_initial_state(monkeypatch, initial, direction):

    motor = _motor_gpio_module(monkeypatch)

    registered = {}

    levels = {6: initial >> 1, 16: initial & 1}

    monkeypatch.setattr(motor.lgpio, "gpio_read", lambda _chip, pin: levels.get(pin, 0))



    def register(chip, pin, edges, handler):

        assert chip == 0 and edges == motor.lgpio.BOTH_EDGES

        registered[pin] = handler

        return types.SimpleNamespace(cancel=lambda: None)



    monkeypatch.setattr(motor.lgpio, "callback", register)

    motor.initialize()

    assert set(registered) == {27, 5, 6, 16}

    assert registered[6] is registered[16] is motor._right_encoder_callback

    ring = [0, 1, 3, 2]

    index = ring.index(initial)

    previous = initial

    # Dispatch through the actually registered GPIO callback, not a direct

    # decoder call; the initial A/B level can be any quadrature phase.

    for tick in range(80):

        index = (index + direction) % 4

        state = ring[index]

        pin = 6 if (previous ^ state) == 2 else 16

        level = state >> 1 if pin == 6 else state & 1

        registered[pin](0, pin, level, tick)

        previous = state

    physical = motor.physical_counts()

    assert physical["encoder1_quad"] == 0

    assert physical["encoder2_quad"] == 80 * direction

    assert physical["invalid_transitions"]["right"] == 0

    assert physical["channels"]["right_b"]["edges"] == 40





def test_servo_one_diagnostic_cannot_drive_encoder_input(monkeypatch):

    monkeypatch.setattr("robot.pcb.RIGHT_ENCODER", (6, 7))

    diagnostic = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools" / "hardware" / "pcb_diagnostics.py"))

    monkeypatch.setattr(sys, "argv", ["pcb_diagnostics.py", "servo", "--servo", "1"])

    monkeypatch.setattr(settings, "enable_servos", True)

    monkeypatch.setattr(diagnostic["subprocess"], "run", lambda *a, **k: types.SimpleNamespace(stdout="inactive"))



    def unexpected(*args, **kwargs):

        pytest.fail("Servo conflict must be rejected before pinctrl or GPIO setup")



    monkeypatch.setattr(diagnostic["subprocess"], "check_output", unexpected)

    with pytest.raises(SystemExit) as error:

        diagnostic["main"]()

    assert error.value.code == 2





def test_servo_pin_reservation_keeps_servo_two_available(monkeypatch):

    from robot.pcb import available_servo_pin

    monkeypatch.setattr("robot.pcb.RIGHT_ENCODER", (6, 7))



    with pytest.raises(ValueError, match="reserved for an encoder"):

        available_servo_pin(1)

    assert available_servo_pin(2) == 9

    with pytest.raises(ValueError, match="must be 1 or 2"):

        available_servo_pin(0)





@pytest.mark.parametrize("logical", [(4313, 4313), (-4313, -4313), (-1106, 1106), (1106, -1106)])

def test_right_sign_calibration_preserves_raw_counts_and_normalizes_direction(monkeypatch, logical):

    motor = _motor_gpio_module(monkeypatch)

    patch = {"motion": {"encoder_right_invert": True}}  # Synthetic reversed encoder fixture.

    monkeypatch.setattr(settings, "motion_encoder_right_invert", patch["motion"]["encoder_right_invert"])

    monkeypatch.setattr(settings, "motion_encoder_left_invert", False)

    monkeypatch.setattr(settings, "motion_swap_encoder_inputs", False)

    motor.initialize()

    motor._left_count, motor._right_count = logical[0], -logical[1]

    assert motor.counts() == logical

    assert motor.physical_counts()["encoder2_quad"] == -logical[1]



def test_raw_wheel_test_reaches_full_duty_without_changing_normal_limit(monkeypatch):
    motor,calls=_recording_motor(monkeypatch)
    monkeypatch.setattr(settings,'motion_pwm_max_duty',50)
    motor.set_wheel_pwm_raw(100,-100)
    assert ('tx_pwm',0,4,1000,100.0) in calls
    assert ('tx_pwm',0,23,1000,100.0) in calls
    calls.clear()
    motor.set_wheel_pwm(100,-100)
    assert ('tx_pwm',0,4,1000,50.0) in calls
    assert ('tx_pwm',0,23,1000,50.0) in calls
    motor.inhibit();calls.clear()
    motor.set_wheel_pwm_raw(100,100)
    assert not calls


@pytest.mark.parametrize('pair',[(75,100),(-75,100),(75,-100),(-75,-100)])
def test_pwm_saturation_preserves_steering_ratio(monkeypatch,pair):
    motor,_=_recording_motor(monkeypatch)
    motor.initialize()
    monkeypatch.setattr(settings,'motion_pwm_max_duty',50)
    output=[]
    monkeypatch.setattr(motor,'_set_one',lambda chip,a,b,duty,*args,**kw:output.append(duty))
    motor.set_wheel_pwm(*pair)
    assert output==pytest.approx([v*.5 for v in pair])
