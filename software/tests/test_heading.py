import math
from types import SimpleNamespace

import pytest

from robot.heading import GravityHeading, HeadingError, HeadingFeedback
from robot.orientation import GRAVITY, OrientationFilter
from robot.turn_control import run_turn
from robot.config import settings
from robot.motion import MotionManager
from controller.test_turn_control import Rig


def sample(angle=0., time=0., rate=0., epoch=1):
    return dict(ready=True, angle_deg=angle, sample_time=time, rate_deg_s=rate, epoch=epoch)


@pytest.mark.parametrize('up', [(0, 0, 1), (0, 0, -1), (1, 0, 0), (.6, 0, -.8)])
def test_gravity_projection_works_with_rotated_board_and_bias(up):
    acceleration = tuple(v*GRAVITY for v in up)
    attitude = OrientationFilter()
    q = attitude.update(acceleration, (0, 0, 0), 0)
    bias = (.07, -.09, -.02)
    gyro = tuple(b+u*math.pi/2 for b, u in zip(bias, up))
    heading = GravityHeading()
    for i in range(501):
        result = heading.update(q, gyro, bias, acceleration, i*.02, True)
    assert result['ready'] and result['angle_deg'] == pytest.approx(900)


def test_calibration_gap_and_freefall_invalidate_heading_epoch():
    h = GravityHeading()
    q, acc = (1, 0, 0, 0), (0, 0, GRAVITY)
    first = h.update(q, (0, 0, 1), (0, 0, 0), acc, 0, True)
    second = h.update(q, (0, 0, 1), (0, 0, 0), acc, 1, True)
    assert second['epoch'] != first['epoch'] and second['angle_deg'] == 0
    assert not h.update(q, (0, 0, 1), (0, 0, 0), acc, 1.02, False)['ready']
    assert not h.update(q, (0, 0, 1), (0, 0, 0), (0, 0, 0), 1.04, True)['ready']


def test_fusion_retains_encoder_distance_and_unwrapped_gyro_heading():
    current = sample()
    feedback = HeadingFeedback(lambda: current, math.pi/180, 1, now=0)  # one degree per differential count
    for i in range(1, 101):
        current.update(sample(angle=i*3.6, time=i*.02))
        counts = feedback.counts((i, i+4*i), i*.02)
    assert sum(counts) == pytest.approx(600)
    assert feedback.angle == pytest.approx(360.8)  # 98% gyro: 40-degree wheel error becomes 0.8 degree.
    assert feedback.encoder_angle == pytest.approx(400)
    assert feedback.status()['disagreement_deg'] == pytest.approx(-40)


@pytest.mark.parametrize('invalid,reason', [
    (None, 'unavailable'), (sample(time=-1), 'stale'),
    (sample(angle=float('nan')), 'invalid'), (sample(time=1), 'stale'),
])
def test_bad_initial_samples_rejected(invalid, reason):
    with pytest.raises(HeadingError, match=reason):
        HeadingFeedback(lambda: invalid, .01, 10, now=0)


def test_duplicate_reads_do_not_reintegrate_and_epoch_change_is_not_rebased():
    current = sample()
    f = HeadingFeedback(lambda: current, math.pi/180, 1, now=0)
    current.update(sample(angle=10, time=.1))
    first = f.counts((-5, 5), .1)
    assert f.counts((-5, 5), .1) == first
    current['epoch'] = 2
    with pytest.raises(HeadingError, match='reset'): f.counts((-5, 5), .12)


class SlipRig(Rig):
    def __init__(self, slip=.85):
        super().__init__()
        self.slip = slip
        self.invalid_at = math.inf
        self.reset_at = math.inf
        self.bias = 0.
        self.flip = False
        self.frozen_sample = None

    def heading(self):
        if self.frozen_sample is not None:
            return self.frozen_sample
        if self.now >= self.invalid_at:
            return None
        angle = (self.positions[1]-self.positions[0]) * .05 * self.slip
        return sample(angle=(-angle if self.flip else angle)+self.bias,
                      time=self.now, epoch=2 if self.now >= self.reset_at else 1)


def assisted_turn(rig, angle=90):
    f = HeadingFeedback(rig.heading, math.radians(.05), 1, now=rig.now)
    ticks = round(angle/.1)
    return run_turn(rig, rig, (-ticks, ticks), cap=45, start_duty=38,
                    tolerance=20, feedback=f, clock=lambda: rig.now)


@pytest.mark.parametrize('angle', [-90, 90, 180, 360, 10])
@pytest.mark.parametrize('slip', [.8, .95, 1.1])
def test_assisted_turn_reaches_real_heading_with_wheel_slip(angle, slip):
    rig = SlipRig(slip)
    result = assisted_turn(rig, angle)
    assert result['ok'], result
    actual = rig.heading()['angle_deg']
    assert abs(actual-angle) <= 2.1, result
    assert result['heading']['source'] == 'imu_encoder'
    assert max(abs(d) for _, pair in rig.outputs for d in pair) <= 45
    assert rig.duties == (0, 0)


@pytest.mark.parametrize('failure,reason', [('invalid_at', 'unavailable'), ('reset_at', 'reset'),
                                          ('freeze', 'encoder_stall'), ('cancel_at', 'stopped')])
def test_assisted_turn_fails_closed(failure, reason):
    rig = SlipRig()
    setattr(rig, failure, True if failure == 'freeze' else .4)
    result = assisted_turn(rig)
    assert not result['ok'] and reason in result['reason'], result
    assert rig.duties == (0, 0) and rig.now < 1


def test_stale_sample_during_move_stops_without_encoder_fallback():
    rig = SlipRig()
    rig.frozen_sample = sample()
    result = assisted_turn(rig)
    assert result['reason'] == 'imu_heading_stale' and rig.now < .18
    assert rig.duties == (0, 0)


def test_wrong_heading_direction_stops_instead_of_chasing_target():
    rig = SlipRig()
    rig.flip = True
    result = assisted_turn(rig)
    assert result['reason'] == 'imu_encoder_disagreement'
    assert rig.duties == (0, 0)


def test_initial_fallback_and_setting_disable(monkeypatch):
    manager = MotionManager()
    assert manager._heading_feedback()[1] == 'imu_not_connected'
    manager.set_heading_getter(lambda: None)
    assert manager._heading_feedback()[1] == 'imu_heading_unavailable'
    monkeypatch.setattr(settings, 'motion_imu_heading_enabled', False)
    assert manager._heading_feedback()[1] == 'disabled'


@pytest.mark.parametrize('failure', ['unavailable', 'stale', 'reset', 'invalid', 'disagreement'])
def test_filming_heading_fallback_keeps_progress_and_latches(failure):
    current = sample()
    feedback = HeadingFeedback(lambda: current, math.pi/180, 1, now=0, allow_encoder_fallback=True)
    current.update(sample(angle=16, time=.02))
    previous = feedback.counts((-10, 10), .02)
    before = feedback.angle
    current.update({'unavailable': {'ready':False}, 'stale': {'sample_time':-1},
                    'reset': {'epoch':2}, 'invalid': {'angle_deg':float('nan')},
                    'disagreement': {'angle_deg':170,'sample_time':.04}}[failure])
    actual = feedback.counts((-15, 15), .04)
    assert feedback.angle == pytest.approx(before+10)
    assert actual[1]-previous[1] == pytest.approx(5)
    current.update(sample(angle=300, time=.06))  # Recovered stream has a different origin.
    feedback.counts((-20, 20), .06)
    assert feedback.angle == pytest.approx(before+20)
    assert feedback.status()['source']=='encoders'
    assert feedback.status()['reason']==('imu_encoder_disagreement' if failure=='disagreement' else 'imu_heading_'+failure)


@pytest.mark.parametrize('failure', ['invalid_at', 'reset_at', 'frozen_sample', 'disagreement'])
def test_filming_turn_finishes_on_encoders_after_sensor_loss(failure):
    rig = SlipRig(1)
    if failure=='frozen_sample':rig.frozen_sample=sample()
    else:setattr(rig,failure,.3)
    def heading():
        value=rig.heading()
        if failure=='disagreement' and rig.now>=.3:value['angle_deg']+=100
        return value
    feedback=HeadingFeedback(heading,math.radians(.05),1,now=0,allow_encoder_fallback=True)
    result=run_turn(rig,rig,(-900,900),cap=45,start_duty=38,tolerance=20,
                    feedback=feedback,clock=lambda:rig.now)
    assert result['ok'],result
    assert result['heading']['source']=='encoders'
    assert abs(result['heading']['error_deg'])<=2
    assert rig.duties==(0,0)


@pytest.mark.parametrize('failure,reason', [('freeze','encoder_stall'), ('cancel_at','stopped')])
def test_filming_fallback_retains_actual_fault_stops(failure,reason):
    rig=SlipRig()
    setattr(rig,failure,.3 if failure=='cancel_at' else True)
    feedback=HeadingFeedback(rig.heading,math.radians(.05),1,now=0,allow_encoder_fallback=True)
    result=run_turn(rig,rig,(-900,900),cap=45,start_duty=38,tolerance=20,
                    feedback=feedback,clock=lambda:rig.now)
    assert result['reason']==reason and not result['ok']
    assert rig.duties==(0,0)


@pytest.mark.parametrize('mode', ['distance', 'arc', 'sine', 'wiggle', 'wiggle_forward'])
@pytest.mark.parametrize('dropout', [False,True,'disagreement'])
def test_measured_driving_uses_heading_but_keeps_raw_final_counts(monkeypatch, mode, dropout):
    from robot import motion
    rig = SlipRig(.9)
    manager = MotionManager()
    monkeypatch.setattr(motion, 'motor', rig)
    monkeypatch.setattr(motion.time, 'monotonic', lambda: rig.now)
    monkeypatch.setattr(settings, 'motion_wheel_diameter_cm', 6.2)
    monkeypatch.setattr(settings, 'motion_wheel_base_cm', 10.4)
    monkeypatch.setattr(settings, 'motion_encoder_counts_per_rev', 2800)
    monkeypatch.setattr(settings, 'motion_encoder_timeout_s', 20)
    monkeypatch.setattr(settings, 'motion_pwm_min_duty', 30)
    manager._stop_event = SimpleNamespace(is_set=rig.is_set, wait=lambda timeout: rig.wait(timeout))
    manager._distance_getter = lambda: 100
    # Use the configured wheel geometry for this end-to-end manager simulation.
    scale = math.degrees(manager.cm_per_count()/settings.motion_wheel_base_cm)
    def heading():
        if dropout is True and rig.now>=.3:return None
        return sample(angle=(rig.positions[1]-rig.positions[0])*scale*.9+
                      (100 if dropout=='disagreement' and rig.now>=.3 else 0),time=rig.now)
    manager.set_heading_getter(heading)
    manager._brake_then_stop = rig.stop
    spec = {'mode': mode, 'distance_cm': 10, 'radius_cm': 15, 'angle_deg': 30, 'cycles': 1}
    curve = manager._curve_for_spec(spec)
    target = curve.targets(manager.cm_per_count()) if curve else manager._targets_for_spec(spec)[0]
    result = manager._run_target_blocking(mode, 45, .5, target, False, curve=curve,
                                          allow_encoder_fallback=bool(dropout))
    assert result['ok'], result
    assert result['heading']['source'] == ('encoders' if dropout else 'imu_encoder')
    assert tuple(result['final'].values()) == rig.counts()
    if curve:
        assert math.degrees(result['odometry']['heading_rad']) == pytest.approx(result['heading']['heading_deg'], abs=.01)


def test_heading_settings_roundtrip_and_validation(monkeypatch, tmp_path):
    from robot import config
    import yaml
    path = tmp_path/'robot.yaml'
    path.write_text('llm:\n  model: keep-me\n')
    monkeypatch.setattr(config, 'CONFIG_FILE', path)
    monkeypatch.setattr(config, 'CONFIG_SOURCE', path)
    for key in ('enabled', 'gyro_weight', 'turn_tolerance_deg'):
        attr = 'motion_imu_heading_enabled' if key == 'enabled' else 'motion_imu_'+key
        monkeypatch.setattr(settings, attr, getattr(settings, attr))
    payload = {'imu_heading_enabled': False, 'imu_gyro_weight': .95, 'imu_turn_tolerance_deg': 1.5}
    result = config.update_settings_from_payload({'motion': payload})
    assert all(result['values']['motion'][k] == v for k, v in payload.items())
    assert yaml.safe_load(path.read_text())['llm']['model'] == 'keep-me'
    previous = path.read_bytes()
    for invalid in ({'imu_heading_enabled': 'false'}, {'imu_gyro_weight': 2}, {'imu_turn_tolerance_deg': 0}):
        with pytest.raises(ValueError): config.update_settings_from_payload({'motion': invalid})
        assert path.read_bytes() == previous


def test_pickup_interlock_stops_assisted_turn():
    rig = SlipRig()
    feedback = HeadingFeedback(rig.heading, math.radians(.05), 1, now=0)
    result = run_turn(rig, rig, (-900, 900), cap=45, start_duty=38, tolerance=20,
                      feedback=feedback, interlock=lambda: rig.now >= .2, clock=lambda: rig.now)
    assert result['reason'] == 'picked_up' and rig.duties == (0, 0)


@pytest.mark.parametrize('angle', [-90, 90, 360])
def test_cached_50hz_gyro_feedback_with_asynchronous_control_ticks(angle):
    class SampledRig(SlipRig):
        def __init__(self):
            super().__init__(.85)
            self.cached = sample()
            self.next_sample = .013  # Intentionally out of phase with the motor loop.

        def heading(self):
            return dict(self.cached)

        def wait(self, seconds):
            end = self.now+seconds
            while self.now < end-1e-9:
                super().wait(min(.001, end-self.now))
                if self.now >= self.next_sample:
                    self.cached = sample(angle=(self.positions[1]-self.positions[0])*.05*self.slip,
                        time=self.now, rate=(self.rates[1]-self.rates[0])*.05*self.slip)
                    self.next_sample += .02
    rig = SampledRig()
    result = assisted_turn(rig, angle)
    assert result['ok'], result
    actual = (rig.positions[1]-rig.positions[0])*.05*rig.slip
    assert abs(actual-angle) <= 2.1, result


def test_imu_motion_snapshot_stays_cheap_and_invalidates_on_calibration(monkeypatch):
    from robot.imu import IMUService
    import time
    monkeypatch.setattr(settings, 'imu_enabled', True)
    imu = IMUService()
    imu._publish(available=True, last_sample_monotonic=time.monotonic(),
                 drive_heading=sample(time=time.monotonic()), calibration='complete')
    cached = imu.heading_sample()
    cached['angle_deg'] = 99
    assert imu.heading_sample()['angle_deg'] == 0
    imu.calibrate()
    assert imu.heading_sample() is None


def test_manual_imu_dropout_recovery_and_reset_never_jump_heading():
    current=[sample()]
    f=HeadingFeedback(lambda:current[0],math.pi/180,1,now=0,recover=True)
    current[0]=sample(angle=2,time=.02,rate=100)
    f.counts((-1,1),.02)
    current[0]=None
    f.counts((-2,2),.04)
    assert f.angle==pytest.approx(4)
    assert f.status()['source']=='encoders'
    # New IMU epoch with an arbitrary angle, independent of its old baseline.
    for i in range(3,6):
        current[0]=sample(angle=1000+2*i,time=i*.02,rate=100,epoch=2)
        previous=f.angle
        f.counts((-i,i),i*.02)
        assert f.angle-previous==pytest.approx(2)
    assert f.status()['source']=='imu_encoder'
    assert f.status()['imu_recoveries']==1
    current[0]=sample(angle=1012,time=.12,rate=100,epoch=2)
    f.counts((-6,6),.12)
    assert f.angle==pytest.approx(12)
    assert 'imu_heading_unavailable' in f.status()['warnings']


def test_manual_starts_without_imu_and_requires_distinct_good_samples_to_rejoin():
    current=[None]
    f=HeadingFeedback(lambda:current[0],math.pi/180,1,now=0,recover=True)
    current[0]=sample(angle=70,time=.02)
    for _ in range(10):f.counts((0,0),.02)
    assert f.status()['source']=='encoders'
    for t in [.04,.06]:
        current[0]=sample(angle=70,time=t);f.counts((0,0),t)
    assert f.status()['source']=='imu_encoder' and f.angle==0


def test_manual_continuous_gyro_slip_disagreement_is_a_warning_not_a_stop():
    current=sample()
    f=HeadingFeedback(lambda:current,math.pi/180,1,now=0,recover=True)
    for i in range(1,101):
        current.update(sample(angle=i*2,time=i*.02,rate=100))
        f.counts((-i*2,i*2),i*.02)
    assert f.status()['source']=='imu_encoder'
    assert f.angle==pytest.approx(200,abs=1)
    assert 'imu_encoder_disagreement' in f.status()['warnings']


def test_manual_rejects_sudden_gyro_jump_and_uses_encoder_delta_instead():
    current=sample()
    f=HeadingFeedback(lambda:current,math.pi/180,1,now=0,recover=True)
    current.update(sample(angle=100,time=.02,rate=0))
    values=f.counts((-1,1),.02)
    assert values==pytest.approx((-1,1))
    assert f.status()['source']=='encoders'
    assert f.status()['reason']=='imu_heading_jump'
