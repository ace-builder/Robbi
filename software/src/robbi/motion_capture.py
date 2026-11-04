"""Passive demonstration capture from cached IMU and signed wheel counters.

No acceleration integration or motor commands. A motion lease keeps other
activities from driving during a demonstration; raw takes survive conversion.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
import uuid

from .errors import BusyError, UnavailableError, ValidationError
from .orchestration import Intent
from .storage import atomic_write

MAX_SECONDS = 60
HEARTBEAT_SECONDS = 5


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quaternion(imu):
    q = imu.get('quaternion')
    if not imu.get('fresh') or imu.get('calibration') != 'complete':
        raise UnavailableError('Keep Robbi still and calibrate the IMU before recording. Fresh readings are required.')
    if not isinstance(q, (list, tuple)) or len(q) != 4 or not all(finite(v) for v in q):
        raise UnavailableError('IMU orientation is unavailable')
    length = math.sqrt(sum(v*v for v in q))
    if not .5 < length < 1.5:
        raise UnavailableError('Invalid IMU orientation')
    return [v/length for v in q]


def relative_angles(q, reference):
    # current * inverse(initial): world-relative rotation, independent of PCB mounting.
    a, b, c, d = q
    w, x, y, z = reference[0], -reference[1], -reference[2], -reference[3]
    w, x, y, z = (a*w-b*x-c*y-d*z, a*x+b*w+c*z-d*y,
                  a*y-b*z+c*w+d*x, a*z+b*y-c*x+d*w)
    return (math.degrees(math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))),
            math.degrees(math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y))),
            math.degrees(math.asin(max(-1, min(1, 2*(w*y-z*x))))), [w, x, y, z])


def wrap(angle):
    return (angle+180) % 360-180


class PoseTracker:
    """Fuse relative IMU yaw with wheel distance, using an arc midpoint update."""
    def __init__(self, imu, motion, mode):
        self.reference = quaternion(imu)
        self.mode = mode
        self.config = copy.deepcopy(motion.get('config', {}))
        self.counts = self._counts(motion) if mode == 'wheels' else (0, 0)
        self.scale = math.pi*self.config['wheel_diameter_cm']/self.config['encoder_counts_per_rev'] if mode == 'wheels' else 0
        self.last_count = imu.get('sample_count', 0)
        self.gaps = imu.get('sample_gaps', 0)
        self.yaw = 0
        self.pose = dict(t=0., x=0., y=0., heading=0., roll=0., pitch=0., left=0., right=0., v=0., orientation=[1, 0, 0, 0])

    @staticmethod
    def _counts(motion):
        counts = motion.get('counts', {})
        values = counts.get('left'), counts.get('right')
        if not all(finite(v) for v in values):
            raise UnavailableError('Wheel encoder readings are unavailable')
        return values

    def update(self, imu, motion, elapsed):
        q = quaternion(imu)
        if imu.get('sample_count', 0) < self.last_count or imu.get('sample_gaps', 0) != self.gaps:
            raise UnavailableError('IMU restarted or missed samples. The partial take was kept.')
        self.last_count = imu.get('sample_count', 0)
        dt = elapsed-self.pose['t']
        if not 0 < dt <= .3:
            raise UnavailableError('Recording samples were interrupted. The partial take was kept.')
        yaw, roll, pitch, orientation = relative_angles(q, self.reference)
        change = wrap(yaw-self.yaw)
        if abs(change) > 360*dt+3:
            raise UnavailableError('IMU heading jumped. The partial take was kept.')
        dl = dr = 0
        if self.mode == 'wheels':
            if max(abs(roll), abs(pitch)) > 15:
                raise UnavailableError('Robbi was tilted or lifted. Keep both wheels on the floor; the partial take was kept.')
            if not motion.get('available') or not motion.get('encoder_enabled'):
                raise UnavailableError('Wheel encoders became unavailable. The partial take was kept.')
            if any(motion.get('config', {}).get(k) != self.config.get(k) for k in
                   ('wheel_diameter_cm', 'wheel_base_cm', 'encoder_counts_per_rev', 'swap_encoder_inputs')):
                raise UnavailableError('Wheel configuration changed during recording')
            counts = self._counts(motion)
            dl, dr = ((value-old)*self.scale for value, old in zip(counts, self.counts, strict=True))
            if max(abs(dl), abs(dr)) > 70*dt+.5:
                raise UnavailableError('Encoder counters jumped or Robbi moved too quickly. The partial take was kept.')
            self.counts = counts
        distance = (dl+dr)/2
        angle = math.radians(change)
        midpoint = math.radians(self.pose['heading'])+angle/2
        chord = distance*(math.sin(angle/2)/(angle/2) if abs(angle) > 1e-8 else 1)
        self.pose = dict(t=elapsed, x=self.pose['x']+chord*math.cos(midpoint),
                         y=self.pose['y']+chord*math.sin(midpoint), heading=self.pose['heading']+change,
                         roll=roll, pitch=pitch, orientation=orientation, left=self.pose['left']+dl, right=self.pose['right']+dr, v=distance/dt)
        if max(abs(self.pose['x']), abs(self.pose['y'])) > 245:
            raise UnavailableError('The take reached the editable stage boundary')
        self.yaw = yaw
        return dict(self.pose)


def simplify(samples, position_tolerance, heading_tolerance):
    """Time-based RDP retains corners, heading changes and rotations over 180°."""
    keep = {0, len(samples)-1}
    pending = [(0, len(samples)-1)]
    while pending:
        start, end = pending.pop()
        if end-start < 2:
            continue
        a, b = samples[start], samples[end]
        worst, split = 1., None
        for i in range(start+1, end):
            p = samples[i]
            f = (p['t']-a['t'])/max(.001, b['t']-a['t'])
            error = max(math.hypot(p['x']-(a['x']+(b['x']-a['x'])*f),
                                   p['y']-(a['y']+(b['y']-a['y'])*f))/position_tolerance,
                        abs(p['heading']-(a['heading']+(b['heading']-a['heading'])*f))/heading_tolerance)
            if error > worst:
                worst, split = error, i
        if split is None and abs(b['heading']-a['heading']) > 90:
            split = (start+end)//2
        if split is not None:
            keep.add(split)
            pending.extend(((start, split), (split, end)))
    return [samples[i] for i in sorted(keep)]


def to_project(take, tolerance=1.5):
    from .motion_studio import FORMAT, validate_project, number, compile_project
    tolerance = number(tolerance, 'Path detail (cm)', .5, 10)
    samples = take['samples']
    if len(samples) < 2:
        raise ValidationError('Record a longer take before creating an animation')
    def point(p, **extra):
        return dict(x=round(p['x'], 3), y=round(p['y'], 3), heading=round(wrap(p['heading']), 3),
                    speed=12, hold=0, tension=.38, face='happy', path='curve', direction='forward', **extra)
    points = [point(samples[0])]
    # Aggregate at ~5 Hz before classification to suppress encoder quantisation.
    filtered = [samples[0]]
    for p in samples[1:]:
        if p['t']-filtered[-1]['t'] >= .18 or p is samples[-1]:
            filtered.append(p)
    runs = []
    for a, b in zip(filtered, filtered[1:], strict=False):
        dt = b['t']-a['t']
        travel = ((b['left']-a['left'])+(b['right']-a['right']))/2
        kind = ('forward' if travel > 0 else 'reverse') if abs(travel) > max(.025, .5*dt) else 'turn' if abs(b['heading']-a['heading']) > max(.25, 2*dt) else 'hold'
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(b)
        else:
            runs.append((kind, [a, b]))
    for kind, run in runs:
        duration = run[-1]['t']-run[0]['t']
        if kind == 'hold':
            if duration >= .35:
                remaining = duration+points[-1]['hold']
                points[-1]['hold'] = round(min(30, remaining), 2)
                if remaining > 30:
                    p = dict(points[-1], hold=round(remaining-30, 2))
                    points.append(p)
            continue
        for a, b in zip((reduced := simplify(run, tolerance, max(2, tolerance*2))), reduced[1:], strict=False):
            p = point(b)
            if kind == 'turn':
                p['x'], p['y'] = points[-1]['x'], points[-1]['y']
            else:
                p['direction'] = kind
                length = math.hypot(b['x']-a['x'], b['y']-a['y'])
                p['speed'] = round(max(2, min(40, length/max(.05, b['t']-a['t']))), 2)
            points.append(p)
    clean = [points[0]]
    for p in points[1:]:
        previous = clean[-1]
        distance = math.hypot(p['x']-previous['x'], p['y']-previous['y'])
        turn = wrap(p['heading']-previous['heading'])
        # A classification window may straddle driving and turning. Treat its
        # sub-centimetre residual as an in-place turn rather than a tiny cusp.
        if distance < min(.5, tolerance) and abs(turn) > 2:
            p['x'], p['y'] = previous['x'], previous['y']
            distance = 0
        if distance < .01 and len(clean) > 1 and not previous['hold']:
            before = clean[-2]
            first_turn = wrap(previous['heading']-before['heading'])
            if (math.hypot(previous['x']-before['x'], previous['y']-before['y']) < .01
                    and first_turn*turn >= 0 and abs(first_turn+turn) <= 90):
                clean[-1] = p
                continue
        clean.append(p)
    if len(clean) > 64:
        raise ValidationError('This take needs more than 64 waypoints. Increase path detail spacing or record a shorter take.')
    project = validate_project(dict(format=FORMAT, name=take['name'], settings=take['settings'], waypoints=clean))
    # Manual demonstrations can contain sharper corners than a rolling curve
    # can execute. Keep the demonstrated endpoints with explicit pivot turns.
    for warning in compile_project(project)['warnings']:
        if 'Adjust its heading or use Turn & drive' in warning['message']:
            project['waypoints'][warning['index']]['path'] = 'line'
    g = project['settings']
    radius = max(9, math.hypot((g['wheelBase']+.9)/2, max(6.4, g['wheelDiameter']/2)))
    extent = max(max(abs(p['x']), abs(p['y'])) for p in clean)+radius+5
    project['settings']['stageSize'] = max(project['settings']['stageSize'], min(600, math.ceil(extent/5)*10))
    return project


class MotionCapture:
    def __init__(self, studio):
        self.studio = studio
        self.directory = studio.directory/'takes'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.take = None
        self.thread = None
        self.stop_event = threading.Event()
        self.heartbeat = 0

    def active(self):
        with self.lock:
            return bool(self.thread and self.thread.is_alive())

    def capabilities(self):
        result = dict(imu=False, wheels=False, reason='Open Motion Studio on Robbi’s dashboard to record its sensors.')
        if not self.studio.runtime:
            return result
        try:
            c = self.studio._controller()
            imu, motion = c.imu.status(), c.motion.status()
            result['calibration'] = imu.get('calibration')
            result['can_calibrate'] = bool(imu.get('fresh'))
            quaternion(imu)
            result.update(imu=True, reason='IMU ready. Turns can be recorded without wheel tracking.')
            cfg = motion.get('config', {})
            calibrated = self.studio.runtime.config.values.get('runtime', {}).get('calibration_confirmed')
            if calibrated and motion.get('available') and motion.get('encoder_enabled') and all(
                    finite(cfg.get(k)) and cfg[k] > 0 for k in ('wheel_base_cm', 'wheel_diameter_cm', 'encoder_counts_per_rev')):
                result.update(wheels=True, reason='IMU and calibrated encoders ready. Roll Robbi gently on a flat surface.')
            else:
                result['reason'] = 'IMU ready. Enable calibrated wheel encoders to also capture distance.'
        except (RuntimeError, AttributeError, ValueError) as error:
            result['reason'] = str(error)
        return result

    def status(self):
        with self.lock:
            if not self.take:
                return dict(state='idle')
            return {k: copy.deepcopy(v) for k, v in self.take.items() if k not in ('samples', 'settings')} | dict(
                count=len(self.take['samples']), pose=copy.deepcopy(self.take['samples'][-1]))

    def start(self, data):
        from .motion_studio import FORMAT, validate_project
        mode = data.get('mode', 'wheels')
        if mode not in ('wheels', 'imu'):
            raise ValidationError('Choose rolling path or IMU turns')
        project = validate_project(dict(format=FORMAT, name=data.get('name', 'Recorded movement'),
                                        settings=data.get('settings', {}), waypoints=[dict(x=0, y=0, heading=0)]))
        with self.studio._lock, self.lock:
            if self.active() or (self.studio._thread and self.studio._thread.is_alive()):
                raise BusyError('Stop the current recording or animation first')
            if self.take and self.take['state'] == 'unsaved':
                raise ValidationError('Download and delete the unsaved raw take before recording another one')
            if len(list(self.directory.glob('*.json'))) >= 100:
                raise ValidationError('The take library is full (100 takes). Delete an old take first.')
            caps = self.capabilities()
            if not caps[mode]:
                raise UnavailableError(caps['reason'])
            c = self.studio._controller()
            intent = Intent('motion-studio-recording', 100, deadline=time.monotonic()+MAX_SECONDS+2)
            lease = c.orchestrator.acquire(('motion',), intent=intent, ttl_s=MAX_SECONDS+2)
            try:
                # The lease excludes all other motor owners before bridges are inhibited.
                c.motion.stop()
                if c.motion.is_running():
                    raise BusyError('Motors are still stopping. Try recording again in a moment.')
                tracker = PoseTracker(c.imu.status(), c.motion.status(), mode)
                if mode == 'wheels':
                    project['settings'].update(wheelBase=tracker.config['wheel_base_cm'], wheelDiameter=tracker.config['wheel_diameter_cm'])
                    validate_project(project)
                self.take = dict(format='robbi.take/v1', id=uuid.uuid4().hex, name=project['name'], mode=mode,
                                 settings=project['settings'], created=time.time(), state='recording', reason='',
                                 samples=[dict(tracker.pose)])
                self.stop_event = threading.Event()
                self.heartbeat = time.monotonic()
                self.thread = threading.Thread(target=self._run, args=(c, tracker, lease), daemon=True, name='motion-capture')
                self.thread.start()
            except Exception:
                lease.release()
                raise
            return self.status()

    def _run(self, controller, tracker, lease):
        started = time.monotonic()
        reason = 'Recording stopped. Raw take saved.'
        state = 'completed'
        try:
            while not self.stop_event.wait(.05):
                now = time.monotonic()
                if lease.is_set():
                    raise UnavailableError('Recording stopped by Robbi. The partial take was kept.')
                if now-self.heartbeat > HEARTBEAT_SECONDS:
                    reason = 'Browser disconnected. The partial take was kept.'
                    break
                if now-started >= MAX_SECONDS:
                    reason = '60-second recording limit reached. Raw take saved.'
                    break
                if controller.motion.is_running():
                    raise UnavailableError('Unexpected motor activity; recording stopped')
                p = tracker.update(controller.imu.status(), controller.motion.status(), now-started)
                with self.lock:
                    self.take['samples'].append(p)
        except Exception as error:
            state, reason = 'interrupted', str(error)
        finally:
            lease.release()
            with self.lock:
                self.take.update(state=state, reason=reason)
                try:
                    atomic_write(self._path(self.take['id']), json.dumps(self.take, allow_nan=False)+'\n')
                except OSError as error:
                    self.take.update(state='unsaved', reason=f'Could not save the raw take: {error}. Download it before restarting Robbi.')

    def poll(self, take_id):
        with self.lock:
            if not self.take or self.take['id'] != take_id:
                raise ValidationError('This recording is no longer active. Reopen Record Robbi.')
            self.heartbeat = time.monotonic()
            return self.status()

    def stop(self, take_id=None):
        with self.lock:
            if take_id is not None and (not self.take or self.take['id'] != take_id):
                raise ValidationError('Recording identifier does not match')
            self.stop_event.set()
            thread = self.thread
        if thread:
            thread.join(timeout=2)
        return self.status()

    def _path(self, take_id):
        if not isinstance(take_id, str) or not re.fullmatch('[a-f0-9]{32}', take_id):
            raise ValidationError('Invalid recording identifier')
        return self.directory/(take_id+'.json')

    def get(self, take_id):
        path = self._path(take_id)
        with self.lock:
            if self.take and self.take['id'] == take_id:
                if self.active():
                    raise BusyError('Stop recording before opening the take')
                return copy.deepcopy(self.take)
            if not path.exists():
                raise ValidationError('Recording not found')
            if path.stat().st_size > 1024*1024:
                raise ValidationError('Raw recording exceeds 1 MiB')
            return json.loads(path.read_text(encoding='utf-8'))

    def library(self):
        takes = []
        for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            try:
                take = self.get(path.stem)
                takes.append(dict(id=take['id'], name=take['name'], mode=take['mode'], duration=take['samples'][-1]['t'], state=take['state']))
            except (ValueError, OSError, KeyError, BusyError):
                continue
        return takes

    def delete(self, take_id):
        with self.lock:
            if self.take and self.take['id'] == take_id:
                if self.active():
                    raise BusyError('Stop recording before deleting the take')
                self.take = None
            self._path(take_id).unlink(missing_ok=True)

    def convert(self, take_id, tolerance):
        project = to_project(self.get(take_id), tolerance)
        saved = self.studio.save(project)
        return dict(project=project, **saved)
