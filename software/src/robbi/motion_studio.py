"""Versioned, editable motion clips and a bounded encoder-backed player.

Geometry mirrors web/motion-studio/motion.js. Untrusted files contain waypoints,
never executable actions, raw PWM, filesystem paths, or obstacle overrides.
"""
from __future__ import annotations

import copy
import json
import logging
import math
from pathlib import Path
import re
import threading
import time
import uuid

from .errors import BusyError, UnavailableError, ValidationError
from .storage import atomic_write

LOG = logging.getLogger(__name__)
FORMAT = 'robbi.motion/v2'
CUE_DEFAULTS = dict(screen='face', media='', sound='', speech='', duration=0, wait=True)
FACES = {'happy', 'neutral', 'cute', 'wink', 'surprised', 'thinking', 'sleepy',
         'joyful', 'mischievous', 'annoyed', 'aware', 'grumpy'}
DEFAULTS = dict(wheelBase=10.2, wheelDiameter=6.2, maxWheelSpeed=22, acceleration=18,
                angularSpeed=90, minRadius=8, motorPower=100, gridSize=5, stageSize=140)
SETTING_LIMITS = dict(wheelBase=(4, 30), wheelDiameter=(2, 15), maxWheelSpeed=(2, 60),
                      acceleration=(1, 100), angularSpeed=(10, 180), minRadius=(3, 100),
                      motorPower=(10, 100), gridSize=(1, 20), stageSize=(40, 600))
POINT_LIMITS = dict(x=(-250, 250), y=(-250, 250), heading=(-180, 180), speed=(2, 40),
                    hold=(0, 30), tension=(.1, .65))
POINT_DEFAULTS = dict(direction='forward', path='curve', speed=12, hold=0, tension=.38, face='happy')


def number(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f'{label} must be a finite number')
    if not minimum <= value <= maximum:
        raise ValidationError(f'{label} must be between {minimum:g} and {maximum:g}')
    return value


def validate_project(raw):
    if not isinstance(raw, dict) or raw.get('format') not in (FORMAT, 'robbi.motion/v1'):
        raise ValidationError('Choose a Robbi Motion Studio .robbi.json file (v1 or v2)')
    name = raw.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValidationError('Animation name must be 1–80 characters')
    values = raw.get('settings', {})
    if not isinstance(values, dict):
        raise ValidationError('settings must be an object')
    settings = {key: number(values.get(key, DEFAULTS[key]), key, *bounds)
                for key, bounds in SETTING_LIMITS.items()}
    points = raw.get('waypoints')
    if not isinstance(points, list) or not 1 <= len(points) <= 64:
        raise ValidationError('An animation needs 1–64 waypoints')
    clean = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValidationError('Every waypoint must be an object')
        value = {**POINT_DEFAULTS, **point}
        p = {key: number(value.get(key), f'Waypoint {index+1}: {key}', *bounds)
             for key, bounds in POINT_LIMITS.items()}
        for key, choices in (('direction', {'forward', 'reverse'}), ('path', {'curve', 'line'}), ('face', FACES)):
            if not isinstance(value[key], str) or value[key] not in choices:
                raise ValidationError(f'Waypoint {index+1}: unknown {key}')
            p[key] = value[key]
        p['cue'] = validate_cue(value.get('cue', {}))
        clean.append(p)
    return dict(format=FORMAT, name=name.strip(), settings=settings, waypoints=clean)


def validate_cue(raw):
    if not isinstance(raw, dict):
        raise ValidationError('Media cue must be an object')
    cue = {key: raw.get(key, default) for key, default in CUE_DEFAULTS.items()}
    if cue['screen'] not in ('face', 'image', 'video'):
        raise ValidationError('Choose a face, image, or video for the screen')
    for key in ('media', 'sound'):
        if not isinstance(cue[key], str) or (cue[key] and not re.fullmatch('[a-z0-9_-]{1,80}', cue[key])):
            raise ValidationError('Select media from the library')
    if cue['screen'] != 'face' and not cue['media']:
        raise ValidationError('Choose an image or video for the screen')
    if not isinstance(cue['speech'], str) or len(cue['speech']) > 1000:
        raise ValidationError('Speech is limited to 1,000 characters')
    cue['speech'] = cue['speech'].strip()
    if type(cue['wait']) is not bool:
        raise ValidationError('Wait for media must be true or false')
    number(cue['duration'], 'Cue duration', 0, 110)
    return cue


def cue_duration(cue):
    return max(cue['duration'], (cue['duration'] or 1 if cue['sound'] else 0)+len(cue['speech'])/14,
               1 if cue['screen'] == 'video' else 0)


def wrap(value):
    return (value + math.pi) % (2 * math.pi) - math.pi


def cubic_samples(a, b):
    direction = -1 if b['direction'] == 'reverse' else 1
    d = math.hypot(b['x']-a['x'], b['y']-a['y'])
    handle = d * b['tension']
    h0, h1 = map(math.radians, (a['heading'], b['heading']))
    c1 = (a['x'] + math.cos(h0)*handle*direction, a['y'] + math.sin(h0)*handle*direction)
    c2 = (b['x'] - math.cos(h1)*handle*direction, b['y'] - math.sin(h1)*handle*direction)
    count = max(80, math.ceil(d*3))
    samples, s, previous_heading = [], 0.0, h0
    for i in range(count+1):
        t, u = i/count, 1-i/count
        x = u**3*a['x']+3*u*u*t*c1[0]+3*u*t*t*c2[0]+t**3*b['x']
        y = u**3*a['y']+3*u*u*t*c1[1]+3*u*t*t*c2[1]+t**3*b['y']
        dx = 3*u*u*(c1[0]-a['x'])+6*u*t*(c2[0]-c1[0])+3*t*t*(b['x']-c2[0])
        dy = 3*u*u*(c1[1]-a['y'])+6*u*t*(c2[1]-c1[1])+3*t*t*(b['y']-c2[1])
        ddx = 6*u*(c2[0]-2*c1[0]+a['x'])+6*t*(b['x']-2*c2[0]+c1[0])
        ddy = 6*u*(c2[1]-2*c1[1]+a['y'])+6*t*(b['y']-2*c2[1]+c1[1])
        length = math.hypot(dx, dy)
        heading = previous_heading + wrap(math.atan2(dy, dx)+(math.pi if direction < 0 else 0)-previous_heading)
        if samples:
            s += math.hypot(x-samples[-1]['x'], y-samples[-1]['y'])
        samples.append(dict(x=x, y=y, s=s, heading=heading,
                            curvature=(dx*ddy-dy*ddx)/length**3 if length >= 1e-7 else 1e12,
                            cusp=length < 1e-5))
        previous_heading = heading
    return samples


def timed(samples, limits, acceleration):
    velocities = list(limits)
    velocities[0] = velocities[-1] = 0
    for i in range(1, len(samples)):
        velocities[i] = min(velocities[i], math.sqrt(velocities[i-1]**2+2*acceleration*(samples[i]['s']-samples[i-1]['s'])))
    for i in range(len(samples)-2, -1, -1):
        velocities[i] = min(velocities[i], math.sqrt(velocities[i+1]**2+2*acceleration*(samples[i+1]['s']-samples[i]['s'])))
    elapsed = 0
    for i, point in enumerate(samples):
        if i:
            elapsed += 2*(point['s']-samples[i-1]['s'])/max(.001, velocities[i]+velocities[i-1])
        point.update(t=elapsed, v=velocities[i])
    return samples


def compile_project(raw):
    project = validate_project(raw)
    g, points = project['settings'], project['waypoints']
    segments, beats, cues, warnings, arrivals = [], [], [], [], [0]
    elapsed = distance = left = right = 0.0

    def add(kind, samples, index, direction=1):
        nonlocal elapsed, left, right
        for p in samples:
            turn = (p['heading']-samples[0]['heading'])*g['wheelBase']/2
            p.update(left=left+direction*p['s']-turn, right=right+direction*p['s']+turn)
        segments.append(dict(kind=kind, samples=samples, index=index, direction=direction,
                             start=elapsed, duration=samples[-1]['t'], face=points[max(0, index-1)]['face']))
        beats.append(segments[-1])
        elapsed += samples[-1]['t']
        left, right = samples[-1]['left'], samples[-1]['right']

    def hold(p, index):
        nonlocal elapsed
        duration = max(p['hold'], cue_duration(p['cue']) if p['cue']['wait'] or index == len(points)-1 else 0)
        if duration:
            sample = dict(x=p['x'], y=p['y'], heading=math.radians(p['heading']), s=0, t=0, v=0, left=left, right=right)
            segments.append(dict(kind='hold', index=index, start=elapsed, duration=duration,
                                 direction=1, face=p['face'], samples=[sample, {**sample, 't': duration}]))
            beats.append(segments[-1])
            elapsed += duration

    def cue(p, index):
        event = dict(kind='cue', index=index, start=elapsed, face=p['face'], **p['cue'])
        event['mediaTime'] = p['cue']['duration']
        event['duration'] = cue_duration(p['cue'])
        cues.append(event)
        beats.append(event)

    def turn(p, start, end, index):
        angle = wrap(end-start)
        if abs(angle) < 1e-5:
            return
        count = max(20, math.ceil(abs(math.degrees(angle))))
        samples = [dict(x=p['x'], y=p['y'], s=abs(angle)*i/count, heading=start+angle*i/count, curvature=0)
                   for i in range(count+1)]
        timed(samples, [math.radians(g['angularSpeed'])]*len(samples), math.radians(g['angularSpeed'])*2)
        for p in samples:
            p['s'] = 0
        add('turn', samples, index)

    cue(points[0], 0)
    hold(points[0], 0)
    for i in range(1, len(points)):
        a, b = points[i-1], points[i]
        d = math.hypot(b['x']-a['x'], b['y']-a['y'])
        direction = -1 if b['direction'] == 'reverse' else 1
        if d < .01:
            turn(a, math.radians(a['heading']), math.radians(b['heading']), i)
        else:
            if b['path'] == 'line':
                h = math.atan2(b['y']-a['y'], b['x']-a['x'])+(math.pi if direction < 0 else 0)
                turn(a, math.radians(a['heading']), h, i)
                count = max(40, math.ceil(d*3))
                samples = [dict(x=a['x']+(b['x']-a['x'])*j/count, y=a['y']+(b['y']-a['y'])*j/count,
                                s=d*j/count, heading=h, curvature=0) for j in range(count+1)]
            else:
                samples = cubic_samples(a, b)
            tight = any(abs(p['curvature']) > 1/g['minRadius'] or abs(p['curvature'])*g['wheelBase']/2 >= .95 for p in samples)
            cusp = any(p.get('cusp') for p in samples)
            if tight or cusp:
                warnings.append(dict(index=i, severity='error', message=f"Waypoint {i+1}: {'path folds back on itself' if cusp else 'turn is too tight'}. Adjust its heading or use Turn & drive."))
            limits = [max(.3, min(b['speed'], g['maxWheelSpeed']/(1+abs(p['curvature'])*g['wheelBase']/2),
                                 math.radians(g['angularSpeed'])/abs(p['curvature']) if abs(p['curvature']) > 1e-8 else 1000)) for p in samples]
            add('move', timed(samples, limits, g['acceleration']), i, direction)
            distance += samples[-1]['s']
            if b['path'] == 'line':
                turn(b, samples[-1]['heading'], math.radians(b['heading']), i)
        arrivals.append(elapsed)
        cue(b, i)
        hold(b, i)
    radius = max(9, math.hypot((g['wheelBase']+.9)/2, max(6.4, g['wheelDiameter']/2)))
    unsafe = {s['index'] for s in segments if any(abs(p['x'])+radius > g['stageSize']/2 or abs(p['y'])+radius > g['stageSize']/2 for p in s['samples'])}
    unsafe.update(i for i, p in enumerate(points) if abs(p['x'])+radius > g['stageSize']/2 or abs(p['y'])+radius > g['stageSize']/2)
    warnings.extend(dict(index=i, severity='error', message=f"Waypoint {i+1}: Robbi's footprint crosses the stage edge.") for i in sorted(unsafe))
    if elapsed > 110:
        warnings.append(dict(index=0, severity='error', message='Robot playback is limited to 110 seconds. Shorten the animation or split it into clips.'))
    if any(s['kind'] == 'move' and s['samples'][-1]['s'] > 500 for s in segments):
        warnings.append(dict(index=0, severity='error', message='A single path is limited to 500 cm.'))
    return dict(project=project, segments=segments, beats=beats, cues=cues, arrivals=arrivals, duration=elapsed,
                distance=distance, left=left, right=right, warnings=warnings)


class MotionStudio:
    def __init__(self, root, data_dir, runtime=None):
        from .motion_media import MotionMedia
        self.root, self.directory, self.runtime = Path(root), Path(data_dir)/'motion_studio', runtime
        self.media = MotionMedia(root, data_dir)
        self._cue_player = None
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = dict(state='idle', completed=0)
        from .motion_capture import MotionCapture
        self.capture = MotionCapture(self)

    def _path(self, clip_id):
        if not isinstance(clip_id, str) or not re.fullmatch('[a-f0-9]{32}', clip_id):
            raise ValidationError('Invalid animation identifier')
        return self.directory / (clip_id+'.json')

    def library(self):
        clips = []
        with self._lock:
            for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                try:
                    p = self.get(path.stem)
                    clips.append(dict(id=path.stem, name=p['name'], waypoints=len(p['waypoints']), updated=path.stat().st_mtime))
                except (OSError, ValueError, ValidationError):
                    LOG.warning('Invalid saved motion clip: %s', path.name)
        return clips

    def get(self, clip_id):
        path = self._path(clip_id)
        if not path.exists():
            raise FileNotFoundError('Animation not found')
        if path.stat().st_size > 512*1024:
            raise ValidationError('Animation exceeds 512 KiB')
        return validate_project(json.loads(path.read_text(encoding='utf-8')))

    def save(self, project, clip_id=None):
        project = validate_project(project)
        with self._lock:
            if clip_id is None and len(list(self.directory.glob('*.json'))) >= 100:
                raise ValidationError('The device library is full (100 animations)')
            clip_id = clip_id or uuid.uuid4().hex
            atomic_write(self._path(clip_id), json.dumps(project, ensure_ascii=False, allow_nan=False, indent=2)+'\n')
        return dict(id=clip_id, name=project['name'])

    def delete(self, clip_id):
        with self._lock:
            self._path(clip_id).unlink(missing_ok=True)

    def _controller(self):
        if self.runtime is None:
            raise UnavailableError('Preview on this computer. Import this file in Motion Studio on Robbi to play it.')
        return self.runtime.controller()

    def _ready(self, controller):
        state = controller.motion.status()
        if not self.runtime.config.values.get('runtime', {}).get('calibration_confirmed'):
            return 'Confirm wheel and encoder calibration in the robot configuration first.', state
        if not state.get('available') or not state.get('encoder_enabled'):
            return 'Calibrated motors and encoders must be enabled on Robbi.', state
        obstacle = state.get('obstacle', {})
        if not obstacle.get('enabled') or obstacle.get('blocked') or not isinstance(obstacle.get('distance_cm'), (int, float)) or not math.isfinite(obstacle['distance_cm']):
            return 'Enable obstacle protection and provide a clear, fresh forward ToF reading.', state
        if getattr(controller.motion, 'handling_interlock', lambda: False)():
            return 'Place Robbi on a level surface before playback.', state
        return '', state

    def status(self):
        ready, reason, geometry = False, 'Preview on this computer. Import the animation in Motion Studio on Robbi for physical playback.', {}
        capabilities = dict(display=False, audio=False, speech=False, video=False)
        if self.runtime:
            try:
                controller = self._controller()
                reason, state = self._ready(controller)
                ready, geometry = not reason, state.get('config', {})
                capabilities = self._media_capabilities(controller)
            except (RuntimeError, UnavailableError, AttributeError) as error:
                reason = str(error)
        with self._lock:
            playback = copy.deepcopy(self._state)
        return dict(ready=ready, reason=reason, geometry=geometry, playback=playback,
                    capture=self.capture.status(), capture_capabilities=self.capture.capabilities(),
                    capabilities=capabilities,
                    device='Robbi' if self.runtime else 'This computer')

    @staticmethod
    def _media_capabilities(controller):
        health = controller.media.health() if hasattr(controller.media, 'health') else {}
        return dict(display=health.get('display', {}).get('available', callable(getattr(controller.media, 'show_face', None))),
                    audio=health.get('audio', {}).get('available', callable(getattr(controller.media, 'play_sound', None))),
                    speech=callable(getattr(getattr(controller, 'llm', None), 'synthesize', None)),
                    video=callable(getattr(controller.media, 'play_video', None)))

    def play(self, clip_id):
        plan = compile_project(self.get(clip_id))
        if plan['warnings']:
            raise ValidationError(plan['warnings'][0]['message'])
        controller = self._controller()
        with_motion = any(s['kind'] in ('move', 'turn') for s in plan['segments'])
        if with_motion:
            reason, state = self._ready(controller)
            if reason:
                raise UnavailableError(reason)
        else:
            state = {}
        self.media.validate_refs(plan['project'])
        caps = self._media_capabilities(controller)
        if not caps['display']:
            raise UnavailableError('Enable Robbi’s screen before playing screen cues')
        cues = [p['cue'] for p in plan['project']['waypoints']]
        if any(c['sound'] or c['speech'] for c in cues) and not caps['audio']:
            raise UnavailableError('Audio output is unavailable on Robbi')
        if any(c['speech'] for c in cues) and not caps['speech']:
            raise UnavailableError('Configure Robbi’s speech provider first')
        if any(c['screen'] == 'video' for c in cues) and not caps['video']:
            raise UnavailableError('Video playback is unavailable on Robbi')
        g = plan['project']['settings']
        for key, config_key in ((('wheelBase', 'wheel_base_cm'), ('wheelDiameter', 'wheel_diameter_cm')) if with_motion else ()):
            if abs(g[key]-float(state['config'][config_key])) > .1:
                raise ValidationError('The clip geometry differs from Robbi. Use Match robot in Robot setup, then save again.')
        with self._lock:
            if self.capture.active():
                raise BusyError('Stop recording before playing an animation')
            if self._thread and self._thread.is_alive():
                raise BusyError('Another motion animation is running')
            if self._cue_player and self._cue_player.alive():
                raise BusyError('The previous speech or video cue is still stopping')
            self._cancel = threading.Event()
            self._state = dict(state='running', id=clip_id, name=plan['project']['name'], completed=0, total=len(plan['beats']))
            self._thread = threading.Thread(target=self._run, args=(plan, controller, self._cancel), name='motion-studio', daemon=True)
            self._thread.start()
        return self.status()['playback']

    def stop(self):
        with self._lock:
            self._cancel.set()

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _run(self, plan, controller, cancelled):
        from .orchestration import Intent, MANUAL, intent_scope
        from .dynamics import advance
        from .motion_cues import StudioCuePlayer
        intent = Intent('motion-studio', MANUAL, cancelled=cancelled, deadline=time.monotonic()+115)
        g, pose = plan['project']['settings'], (0, 0, 0)
        first = plan['project']['waypoints'][0]
        h0 = math.radians(first['heading'])
        with_motion = any(s['kind'] in ('move', 'turn') for s in plan['segments'])
        with_audio = any(p['cue']['sound'] or p['cue']['speech'] for p in plan['project']['waypoints'])
        resources = ['display']+(['motion'] if with_motion else [])+(['audio'] if with_audio else [])
        try:
            with intent_scope(intent), controller.orchestrator.acquire(resources, ttl_s=115, cancel_event=intent) as lease:
                player = StudioCuePlayer(controller, self.media, self.root, intent, lease)
                self._cue_player = player
                try:
                    for index, segment in enumerate(plan['beats']):
                        if lease.is_set():
                            break
                        player.check()
                        if segment['kind'] == 'cue':
                            player.start(plan['project']['waypoints'][segment['index']])
                        elif segment['kind'] == 'hold':
                            until = time.monotonic()+segment['duration']
                            while not lease.is_set() and time.monotonic() < until:
                                player.check()
                                lease.wait(min(.02, max(0, until-time.monotonic())))
                            if plan['project']['waypoints'][segment['index']]['cue']['wait']:
                                player.finish()
                        else:
                            reason, state = self._ready(controller)
                            if reason:
                                raise UnavailableError(reason)
                            samples = segment['samples']
                            start, end = samples[0], samples[-1]
                            # Waypoint cm/s describes preview timing, not PWM.
                            # Use the chosen motor power directly to avoid stalls.
                            power = g['motorPower']
                            spec = dict(speed=power, duration=min(10, segment['duration']), require_encoders=True)
                            if segment['kind'] == 'turn':
                                spec.update(mode='turn', angle_deg=math.degrees(end['heading']-start['heading']),
                                            turn_rate_deg_s=g['angularSpeed'])
                            else:
                                h = start['heading']
                                path = [[p['s'], (p['x']-start['x'])*math.cos(h)+(p['y']-start['y'])*math.sin(h),
                                         -(p['x']-start['x'])*math.sin(h)+(p['y']-start['y'])*math.cos(h),
                                         p['heading']-h, p['curvature']] for p in samples]
                                spec.update(mode='studio_path', path=path, path_direction=segment['direction'])
                            future = controller.motion.drive(spec, wait=False, cancel_event=lease)
                            if future is None:
                                raise UnavailableError('Motion did not start')
                            while not future.done() and not lease.is_set():
                                player.check()
                                lease.wait(.02)
                            if lease.is_set():
                                break
                            receipt = future.result() or controller.motion.status().get('last_result', {})
                            while controller.motion.is_running() and not lease.is_set():
                                lease.wait(.01)
                            if lease.is_set():
                                break
                            if not receipt.get('ok'):
                                raise UnavailableError('Motion stopped: '+str(receipt.get('reason', receipt.get('error', 'unknown'))))
                            counts = receipt.get('final')
                            if not isinstance(counts, dict) or not all(k in counts for k in ('left', 'right')):
                                raise UnavailableError('Motion returned no measured wheel positions')
                            if segment['kind'] == 'move':
                                measured = receipt.get('odometry')
                                if not isinstance(measured, dict) or not all(k in measured for k in ('x_cm', 'y_cm', 'heading_rad')):
                                    raise UnavailableError('Motion returned no measured curve odometry')
                                x, y, h = pose
                                pose = (x+measured['x_cm']*math.cos(h)-measured['y_cm']*math.sin(h),
                                        y+measured['x_cm']*math.sin(h)+measured['y_cm']*math.cos(h), h+measured['heading_rad'])
                            else:
                                pose = advance(pose, counts['left']*controller.motion.cm_per_count(), counts['right']*controller.motion.cm_per_count(), g['wheelBase'])
                            dx, dy = end['x']-first['x'], end['y']-first['y']
                            expected = (dx*math.cos(h0)+dy*math.sin(h0), -dx*math.sin(h0)+dy*math.cos(h0))
                            error = math.hypot(pose[0]-expected[0], pose[1]-expected[1])
                            angle_error = abs(math.degrees(wrap(pose[2]-(end['heading']-h0))))
                            self._update(position_error_cm=round(error, 2), heading_error_deg=round(angle_error, 2))
                            if error > 4 or angle_error > 15:
                                raise UnavailableError('Measured motion drift exceeds 4 cm or 15°. Recalibrate before retrying.')
                        self._update(completed=index+1)
                    if not lease.is_set():
                        player.finish()
                    self._update(state='cancelled' if lease.is_set() else 'completed')
                finally:
                    player.stop()
                    if with_motion:
                        controller.motion.stop(owner=intent)
        except Exception as error:
            LOG.warning('Motion Studio playback: %s', error)
            self._update(state='cancelled' if intent.is_set() else 'failed', reason=str(error)[:300])
