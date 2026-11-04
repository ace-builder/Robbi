import math

import pytest

from robot.turn_control import run_turn


class Rig:
    """Simulates asymmetric inertia and finite braking, not tyre slip."""
    def __init__(self, inertia=0.08, brake=0.03):
        self.now = 0.0
        self.positions = [0.0, 0.0]
        self.rates = [0.0, 0.0]
        self.duties = (0, 0)
        self.inertia, self.brake = inertia, brake
        self.outputs = []
        self.cancel_at = math.inf
        self.freeze = False
        self.work_s = 0

    def reset_counts(self):
        self.positions = [0.0, 0.0]

    def counts(self):
        return tuple(round(p) for p in self.positions)

    def set_wheel_pwm_with_brake(self, l, r, left_brake=False, right_brake=False):
        self.duties = (0 if left_brake else l, 0 if right_brake else r)
        self.outputs.append((self.now, self.duties))
        self.wait(self.work_s)

    def stop(self):
        self.duties = (0, 0)

    def is_set(self):
        return self.now >= self.cancel_at

    def wait(self, seconds):
        steps = max(1, math.ceil(seconds / .001))
        dt = seconds / steps
        for _ in range(steps):
            self.now += dt
            for i, duty in enumerate(self.duties):
                desired = math.copysign(max(0, abs(duty)-24) * (55 if i == 0 else 38), duty)
                tau = self.inertia if duty else self.brake
                self.rates[i] += (desired - self.rates[i]) * min(1, dt / tau)
                if not self.freeze:
                    self.positions[i] += self.rates[i] * dt


def turn(rig, **kw):
    args = dict(cap=50, start_duty=38, tolerance=12, timeout=8)
    args.update(kw)
    return run_turn(rig, rig, (-1152, 1152), clock=lambda: rig.now, **args)


@pytest.mark.parametrize("brake", [.015, .03, .06])
def test_inertia_and_asymmetry_are_settled_not_just_threshold_crossed(brake):
    rig = Rig(brake=brake)
    result = turn(rig)
    assert result["ok"], result
    assert all(abs(e) <= 12 for e in result["error_counts"].values())
    assert max(abs(d) for _, pair in rig.outputs for d in pair) <= 50
    assert rig.duties == (0, 0)
    assert result["control_hz"] == pytest.approx(50, abs=.1)


def test_overshoot_trims_in_reverse():
    rig = Rig(brake=.06)
    result = turn(rig)
    assert result["ok"], result
    assert result["correction_rounds"] > 0
    assert any(l > 0 or r < 0 for _, (l, r) in rig.outputs)


@pytest.mark.parametrize('cap,expected', [(100,100),(120,100),(45,45)])
def test_full_power_start_reaches_requested_cap_and_stop_still_deenergizes(cap,expected):
    rig=Rig();rig.cancel_at=.03
    result=turn(rig,cap=cap,start_duty=100)
    assert result['reason']=='stopped'
    assert result['pwm_cap']==expected
    assert rig.outputs[0][1]==(-expected,expected)
    assert max(abs(d) for _,pair in rig.outputs for d in pair)<=expected
    assert rig.duties==(0,0)


def test_full_power_turn_brakes_and_settles_with_asymmetric_wheels():
    rig=Rig()
    result=turn(rig,cap=100,start_duty=100)
    assert result['ok'],result
    assert result['pwm_cap']==100
    assert all(abs(e)<=12 for e in result['error_counts'].values())
    assert rig.outputs[0][1]==(-100,100)
    assert rig.duties==(0,0)


@pytest.mark.parametrize("cancel_at", [.001, .5, 1.0])
def test_stop_always_deenergizes_motor(cancel_at):
    rig = Rig()
    rig.cancel_at = cancel_at
    result = turn(rig)
    assert result["reason"] == "stopped"
    assert rig.now < cancel_at + .025
    assert rig.duties == (0, 0)


def test_encoder_stall_is_not_success():
    rig = Rig()
    rig.freeze = True
    result = turn(rig)
    assert result["reason"] == "encoder_stall"
    assert rig.now < .8
    assert rig.duties == (0, 0)


def test_work_time_is_subtracted_from_20ms_period():
    rig = Rig()
    rig.work_s = .006
    result = turn(rig)
    assert result["ok"], result
    assert result["control_hz"] == pytest.approx(50, abs=.1)


def test_excessive_scheduler_delay_stops_motion():
    rig = Rig()
    rig.work_s = .3
    result = turn(rig)
    assert result["reason"] == "control_loop_late"
    assert rig.duties == (0, 0)


def test_no_movement_on_zero_cap():
    rig = Rig()
    result = turn(rig, cap=0)
    assert result["reason"] == "zero_speed"
    assert not rig.outputs


def test_no_convergence_is_a_failure_not_completed_turn():
    rig = Rig(brake=.5)
    result = turn(rig)
    assert not result["ok"]
    assert rig.now <= 8.1
    assert rig.duties == (0, 0)


def test_stop_during_correction_or_settle():
    rig = Rig(brake=.06)
    original = rig.set_wheel_pwm_with_brake

    def cancel_on_reverse(l, r, **kw):
        original(l, r, **kw)
        if l > 0 or r < 0:
            rig.cancel_at = rig.now

    rig.set_wheel_pwm_with_brake = cancel_on_reverse
    result = turn(rig)
    assert result["reason"] == "stopped"
    assert rig.duties == (0, 0)


class FiniteRig(Rig):
    def __init__(self, jitter=0):
        super().__init__(inertia=.01, brake=.015)
        self.ends = [math.inf, math.inf]
        self.jitter = jitter
        self.bursts = []

    def pulse_wheel_pwm(self, l, r, left_s, right_s):
        self.duties = (l if left_s else 0, r if right_s else 0)
        self.ends = [self.now + left_s, self.now + right_s]
        self.bursts.append((left_s, right_s))

    def wait(self, seconds):
        if seconds == 0:
            return
        end = self.now + seconds + self.jitter
        while self.now + 1e-9 < end:
            self.duties = tuple(d if self.now < deadline else 0 for d, deadline in zip(self.duties, self.ends))
            super().wait(min(.001, end - self.now))


@pytest.mark.parametrize('target',[64,138,261,580])
@pytest.mark.parametrize('jitter',[0,.03,.06])
def test_full_power_short_turns_use_finite_pulses_and_settle_under_jitter(target,jitter):
    rig=FiniteRig(jitter)
    result=run_turn(rig,rig,(-target,target),cap=100,start_duty=100,tolerance=12,
                    timeout=8,clock=lambda:rig.now,pulsed=True)
    assert result['ok'],result
    assert result['startup_control']=='finite_pulses'
    assert all(abs(e)<=12 for e in result['error_counts'].values())
    assert rig.bursts and all(0<=s<=.025 for pair in rig.bursts for s in pair)
    assert rig.duties==(0,0)


@pytest.mark.parametrize('failure',['stopped','encoder_stall','picked_up'])
def test_pulsed_short_turns_keep_stop_stall_and_pickup_guards(failure):
    rig=FiniteRig()
    if failure=='stopped':rig.cancel_at=.1
    if failure=='encoder_stall':rig.freeze=True
    result=run_turn(rig,rig,(-138,138),cap=100,start_duty=100,tolerance=12,
                    clock=lambda:rig.now,pulsed=True,interlock=lambda:failure=='picked_up')
    assert not result['ok'] and result['reason']==failure,result
    assert rig.duties==(0,0)


def test_pulsed_turn_does_not_treat_single_channel_oscillation_as_progress():
    rig=FiniteRig();rig.freeze=True
    rig.counts=lambda:(-int(rig.now*100)%2,int(rig.now*100)%2)
    result=run_turn(rig,rig,(-138,138),cap=100,start_duty=100,tolerance=12,
                    clock=lambda:rig.now,pulsed=True)
    assert not result['ok'] and result['reason']=='encoder_stall'
    assert rig.now<1
    assert rig.duties==(0,0)


@pytest.mark.parametrize("target", [64, 116, 128, 200])
@pytest.mark.parametrize("jitter", [0, .03, .06])
def test_small_scan_turn_remains_accurate_with_bounded_corrections_when_delayed(target, jitter):
    rig = FiniteRig(jitter)
    result = run_turn(rig, rig, (-target, target), cap=35, start_duty=35,
                      tolerance=12, clock=lambda: rig.now)
    assert result["ok"], result
    assert rig.outputs
    assert max((t for pair in rig.bursts for t in pair),default=0) <= .025
    assert all(abs(e) <= 12 for e in result["error_counts"].values())
    assert rig.duties == (0, 0)


def test_scan_rate_target_changes_measured_speed_without_exceeding_pwm_cap():
    durations=[]
    for rate in (550,850):
        rig=Rig()
        result=run_turn(rig,rig,(-3000,3000),cap=50,start_duty=38,tolerance=12,
                        timeout=15,clock=lambda:rig.now,rate_counts_per_s=rate)
        assert result['ok'],result
        assert all(abs(v)<=50 for _,pair in rig.outputs for v in pair)
        durations.append(result['elapsed_s'])
    assert durations[1]<durations[0]*.85


def test_full_start_scan_never_reduces_cruise_pwm_to_chase_a_slow_rate():
    rig=Rig()
    result=run_turn(rig,rig,(-4697,4697),cap=100,start_duty=100,tolerance=12,
                    timeout=15,clock=lambda:rig.now,rate_counts_per_s=300)
    assert result['ok'],result
    assert result['rate_control']=='full_pwm'
    assert all(abs(value) in (0,100) for _,pair in rig.outputs for value in pair)
    assert rig.duties==(0,0)


def test_short_wiggle_can_overcome_startup_delay_before_fine_corrections():
    class StartupDelay(Rig):
        energized_at=None
        def wait(self,seconds):
            if any(self.duties):
                if self.energized_at is None:self.energized_at=self.now
                self.freeze=self.now-self.energized_at<.05
            else:
                self.energized_at=None;self.freeze=False
            super().wait(seconds)
    rig=StartupDelay()
    result=run_turn(rig,rig,(-160,160),cap=50,start_duty=38,tolerance=12,
                    timeout=8,clock=lambda:rig.now)
    assert result['ok'],result
    assert min(abs(v) for v in rig.positions)>=148
    assert rig.now<2


def test_manual_reports_unsettled_turn_as_warning_after_motor_cleanup():
    rig=Rig(brake=.5)
    result=turn(rig,best_effort=True)
    assert result['ok'] and not result['precision_reached']
    assert any(w in result['warnings'] for w in ['turn_not_settled','turn_accuracy_not_reached'])
    assert rig.duties==(0,0)


def test_manual_scheduler_delay_recomputes_feedback_instead_of_rejecting():
    rig=Rig();rig.work_s=.28
    result=run_turn(rig,rig,(-2000,2000),cap=35,start_duty=35,tolerance=20,
                    timeout=15,clock=lambda:rig.now,best_effort=True)
    assert result['ok'],result
    assert 'control_loop_late' in result['warnings']
    assert rig.duties==(0,0)
