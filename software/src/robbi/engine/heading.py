"""Cached gravity-axis gyro integration and bounded per-move encoder fusion."""
import math

from .orientation import GRAVITY, norm


class HeadingError(ValueError):
    pass


class GravityHeading:
    """Owned by the IMU sampler; no second sensor reader or thread."""
    def __init__(self):
        self.epoch = 0
        self.reset()

    def reset(self):
        self.epoch += 1
        self.angle = 0.0
        self.rate = 0.0
        self.timestamp = None

    def update(self, quaternion, gyro, bias, acceleration, timestamp, calibrated):
        if not calibrated:
            if self.timestamp is not None:
                self.reset()
            return self.snapshot(False)
        values = (*quaternion, *gyro, *bias, *acceleration, timestamp)
        if not all(math.isfinite(v) for v in values):
            self.reset()
            return self.snapshot(False)
        if not .5 * GRAVITY < norm(acceleration) < 1.5 * GRAVITY:
            self.reset()
            return self.snapshot(False)
        w, x, y, z = quaternion
        up = (2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y))
        rate = math.degrees(sum((g-b)*u for g, b, u in zip(gyro, bias, up, strict=True)))
        if abs(rate) > 450:
            self.reset()
            return self.snapshot(False)
        if self.timestamp is not None:
            dt = timestamp - self.timestamp
            if dt <= 0 or dt > .15:
                self.reset()
            else:
                self.angle += .5 * (self.rate + rate) * dt
        self.timestamp, self.rate = timestamp, rate
        return self.snapshot(True)

    def snapshot(self, ready):
        return {'ready': ready, 'epoch': self.epoch, 'angle_deg': self.angle,
                'rate_deg_s': self.rate, 'sample_time': self.timestamp}


class HeadingFeedback:
    """Wheel mean distance + gyro-led angular increments, relative to this move."""
    def __init__(self, getter, cm_per_count, track_cm, weight=.98, *, now, allow_encoder_fallback=False,
                 recover=False):
        self.getter = getter
        self.degrees_per_count = math.degrees(cm_per_count / track_cm)
        self.weight = max(.5, min(1., weight))
        self.angle = self.encoder_angle = self.imu_angle = 0.0
        self.last_encoder = self.last_gyro = 0.0
        self.latest = None
        self.allow_encoder_fallback = allow_encoder_fallback or recover
        self.recover = recover
        self.warnings = set()
        self.recoveries = 0
        self._candidate = None
        self._good_samples = 0
        self._rejoined_at = None
        self.fallback_reason = ''
        try:
            sample = self._sample(now)
        except HeadingError as error:
            if not recover:raise
            self.fallback_reason = str(error)
            self.warnings.add(str(error))
            self.epoch, self.baseline = None, 0.
            return
        self.epoch = sample['epoch']
        self.baseline = self._angle(sample, now)
        self.latest = dict(sample)

    def _sample(self, now):
        try:
            sample = self.getter()
            if not sample or not sample.get('ready'):
                raise HeadingError('imu_heading_unavailable')
            if not all(type(sample.get(k)) in (int, float) and math.isfinite(sample[k])
                       for k in ('sample_time', 'angle_deg', 'rate_deg_s', 'epoch')):
                raise HeadingError('imu_heading_invalid')
            if not -.01 <= now - sample['sample_time'] <= .15:
                raise HeadingError('imu_heading_stale')
            if abs(sample['rate_deg_s']) > 450:
                raise HeadingError('imu_heading_invalid')
            return sample
        except HeadingError:
            raise
        except Exception:
            raise HeadingError('imu_heading_unavailable') from None

    @staticmethod
    def _angle(sample, now):
        # Align the 50 Hz cached sample to the encoder tick; never extrapolate
        # a long outage. This is especially important while braking a turn.
        return sample['angle_deg'] + sample['rate_deg_s'] * max(0, min(.04, now-sample['sample_time']))

    def counts(self, counts, now):
        if not self.fallback_reason:
            try:
                return self._fused_counts(counts, now)
            except HeadingError as error:
                if not self.allow_encoder_fallback or str(error) not in {
                    'imu_heading_unavailable', 'imu_heading_stale', 'imu_heading_reset',
                    'imu_heading_invalid', 'imu_encoder_disagreement', 'imu_heading_jump',
                    'imu_heading_direction', 'imu_heading_unresponsive'
                }:
                    raise
                self.fallback_reason = str(error)
                self.warnings.add(str(error))
                if str(error) == 'imu_heading_unresponsive':
                    # A flat gyro supplied no usable yaw for this move. Keep
                    # the encoder travel already made instead of adding 30 deg.
                    self.angle = self.last_encoder
                self._candidate, self._good_samples = None, 0
        # Preserve the last fused heading and add only new wheel increments.
        # Filming latches fallback; manual controls may rejoin a stable IMU
        # without a target jump, restart, or repeated movement.
        self.encoder_angle = (counts[1]-counts[0]) * self.degrees_per_count
        self.angle += self.encoder_angle-self.last_encoder
        self.last_encoder = self.encoder_angle
        if self.recover and self.fallback_reason not in {'imu_heading_direction', 'imu_heading_unresponsive'}:self._try_rejoin(now)
        mean = sum(counts)/2
        half = self.angle / (2*self.degrees_per_count)
        return mean-half, mean+half

    @staticmethod
    def _continuous(a, b):
        dt = b['sample_time']-a['sample_time']
        if dt < 0 or dt > .15:return False
        delta = abs(b['angle_deg']-a['angle_deg'])
        if dt == 0:return delta < 1e-6
        # Compare gyro integration with gyro rate, not encoder yaw: ordinary
        # wheel slip should not disqualify an otherwise continuous IMU stream.
        return delta <= 3 + dt*(abs(a['rate_deg_s'])+abs(b['rate_deg_s']))

    def _try_rejoin(self, now):
        try:
            sample = self._sample(now)
        except HeadingError:
            self._candidate, self._good_samples = None, 0
            return
        previous = self._candidate
        if previous is None or sample['epoch'] != previous['epoch'] or not self._continuous(previous, sample):
            self._good_samples = 1
        elif sample['sample_time'] > previous['sample_time']:
            self._good_samples += 1
        self._candidate = dict(sample)
        if self._good_samples < 3:return
        # Rejoin at the current fused pose, never snap to an IMU reset or add
        # the outage's gyro delta a second time. Subsequent increments blend in.
        self.epoch = sample['epoch']
        self.baseline = self._angle(sample, now)-self.angle
        self.last_gyro = self.imu_angle = self.angle
        self.latest = dict(sample)
        self.fallback_reason = ''
        self._rejoined_at = now
        self.recoveries += 1

    def _fused_counts(self, counts, now):
        sample = self._sample(now)
        if sample['epoch'] != self.epoch or sample['sample_time'] < self.latest['sample_time']:
            raise HeadingError('imu_heading_reset')
        if self.recover and not self._continuous(self.latest, sample):
            raise HeadingError('imu_heading_jump')
        self.latest = dict(sample)
        self.encoder_angle = (counts[1]-counts[0]) * self.degrees_per_count
        self.imu_angle = self._angle(sample, now) - self.baseline
        if abs(self.imu_angle-self.encoder_angle) > max(30, abs(self.encoder_angle)*.45):
            if not self.recover:raise HeadingError('imu_encoder_disagreement')
            self.warnings.add('imu_encoder_disagreement')
            if abs(self.imu_angle) < 2 and abs(self.encoder_angle) > 30:
                raise HeadingError('imu_heading_unresponsive')
            if self.imu_angle*self.encoder_angle < 0 and min(abs(self.imu_angle),abs(self.encoder_angle)) > 10:
                raise HeadingError('imu_heading_direction')
        dg, de = self.imu_angle-self.last_gyro, self.encoder_angle-self.last_encoder
        weight = 1. if abs(dg-de) > max(.25, abs(de)*.2) else self.weight
        if self._rejoined_at is not None:
            weight *= min(1., max(0., (now-self._rejoined_at)/.1))
        self.angle += weight*dg + (1-weight)*de
        self.last_gyro, self.last_encoder = self.imu_angle, self.encoder_angle
        mean = sum(counts)/2
        half = self.angle / (2*self.degrees_per_count)
        return mean-half, mean+half

    def status(self, target=None):
        result = {'source': 'imu_encoder', 'heading_deg': round(self.angle, 3),
                  'imu_deg': round(self.imu_angle, 3), 'encoder_deg': round(self.encoder_angle, 3),
                  'disagreement_deg': round(self.imu_angle-self.encoder_angle, 3)}
        if self.warnings:result['warnings'] = sorted(self.warnings)
        if self.recover:result['imu_recoveries'] = self.recoveries
        if self.fallback_reason:
            result.update(source='encoders', reason=self.fallback_reason)
            # The last IMU sample is no longer a live comparison.
            result.pop('disagreement_deg')
            result.pop('imu_deg')
        if target is not None:
            wanted = (target[1]-target[0]) * self.degrees_per_count
            result.update(target_deg=round(wanted, 3), error_deg=round(wanted-self.angle, 3))
        return result
