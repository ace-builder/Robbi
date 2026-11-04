"""Actual PWM/encoder simulations for the studio turn profile; no GPIO."""
import math

import pytest

from controller.test_turn_control import FiniteRig
from robbi.engine.turn_control import run_turn
from robbi.orchestration import Intent,intent_scope


class ContinuousRig(FiniteRig):
    def __init__(self,jitter=0):
        super().__init__(jitter)
        self.braking_positions=[]

    def set_wheel_pwm_with_brake(self,l,r,**kwargs):
        # A normal PWM write replaces any preceding finite correction train.
        self.ends=[math.inf,math.inf]
        if not l and not r:self.braking_positions.append(self.counts())
        return super().set_wheel_pwm_with_brake(l,r,**kwargs)


def smooth(rig,target=580,**options):
    return run_turn(rig,rig,(-target,target),cap=100,start_duty=100,tolerance=24,
                    clock=lambda:rig.now,continuous=True,rate_counts_per_s=1152,
                    rolling_duty=30,**options)


@pytest.mark.parametrize('target',[138,261,580,1152,4608])
@pytest.mark.parametrize('jitter',[0,.03,.06])
@pytest.mark.parametrize('direction',[-1,1])
def test_turn_traverses_continuously_with_asymmetric_motors_and_delayed_ticks(target,jitter,direction):
    rig=ContinuousRig(jitter)
    result=smooth(rig,target*direction)
    assert result['ok'] and result['precision_reached'],result
    assert max(abs(e) for e in result['error_counts'].values())<=24
    assert not rig.bursts,'A normal studio turn must not be a train of micro-pulses'
    assert any(0<abs(d)<100 for _,pair in rig.outputs for d in pair)
    assert all(max(abs(c) for c in pair)>=abs(target)*.75 for pair in rig.braking_positions)
    assert rig.duties==(0,0)


def test_authored_turn_speed_changes_actual_traversal_time_at_same_power_ceiling():
    times=[]
    for rate in (550,1152):
        rig=ContinuousRig()
        result=run_turn(rig,rig,(-2304,2304),cap=100,start_duty=100,tolerance=24,
                        clock=lambda:rig.now,continuous=True,rate_counts_per_s=rate,timeout=15)
        assert result['ok'],result
        assert max(abs(d) for _,pair in rig.outputs for d in pair)<=100
        times.append(rig.now)
    assert times[1]<times[0]*.8


@pytest.mark.parametrize('failure',['stopped','encoder_stall','picked_up'])
def test_stop_and_physical_faults_still_deenergize_a_continuous_turn(failure):
    rig=ContinuousRig()
    if failure=='stopped':rig.cancel_at=.08
    if failure=='encoder_stall':rig.freeze=True
    result=smooth(rig,interlock=lambda:failure=='picked_up')
    assert not result['ok'] and result['reason']==failure
    assert rig.duties==(0,0) and not rig.bursts
    assert rig.now<.8


def test_static_friction_gets_full_torque_without_repeated_braking():
    class HighFriction(ContinuousRig):
        rolling=False
        def wait(self,seconds):
            if max(map(abs,self.duties))>=80:self.rolling=True
            self.freeze=not self.rolling
            return super().wait(seconds)
    rig=HighFriction();result=smooth(rig,1152)
    assert result['ok'],result
    assert any(max(map(abs,pair))==100 for _,pair in rig.outputs)
    assert rig.braking_positions and all(max(map(abs,pair))>900 for pair in rig.braking_positions)


def test_studio_selects_continuous_profile_while_manual_profile_is_preserved(monkeypatch):
    from robbi.engine import motion
    manager=motion.MotionManager();calls=[]
    monkeypatch.setattr(manager,'_heading_feedback',lambda *a:(None,''))
    monkeypatch.setattr(motion,'run_turn',lambda *a,**kw:calls.append(kw) or {'ok':True})
    for attr in ('motion_pwm_max_duty','motion_turn_max_duty','motion_turn_start_duty'):
        monkeypatch.setattr(motion.settings,attr,100)
    for owner in ('motion-studio','manual:dashboard'):
        with intent_scope(Intent(owner,80)):
            manager._run_target_blocking('turn',100,1,(-261,261),False,turn_rate_deg_s=90)
    assert calls[0]['continuous'] and not calls[0]['pulsed']
    assert not calls[1]['continuous'] and calls[1]['pulsed']
    assert all(c['cap']==100 and c['rate_counts_per_s']>0 for c in calls)


@pytest.mark.parametrize('angle',[-90,90,180])
@pytest.mark.parametrize('slip',[.8,1.1])
def test_continuous_profile_uses_actual_gyro_heading_when_wheels_slip(angle,slip):
    from test_heading import SlipRig
    from robbi.engine.heading import HeadingFeedback
    rig=SlipRig(slip)
    feedback=HeadingFeedback(rig.heading,math.radians(.05),1,now=rig.now)
    target=round(angle/.1)
    result=run_turn(rig,rig,(-target,target),cap=100,start_duty=100,tolerance=20,
                    clock=lambda:rig.now,continuous=True,rate_counts_per_s=900,
                    rolling_duty=30,feedback=feedback)
    assert result['ok'],result
    assert abs(rig.heading()['angle_deg']-angle)<=2.1
    assert result['heading']['source']=='imu_encoder' and rig.duties==(0,0)


@pytest.mark.parametrize('cap',[35,50])
def test_continuous_profile_respects_an_explicit_lower_power_ceiling(cap):
    rig=ContinuousRig()
    result=run_turn(rig,rig,(-580,580),cap=cap,start_duty=100,tolerance=24,
                    clock=lambda:rig.now,continuous=True,rate_counts_per_s=900)
    assert result['ok'],result
    assert max(abs(d) for _,pair in rig.outputs for d in pair)<=cap
    assert rig.duties==(0,0)
