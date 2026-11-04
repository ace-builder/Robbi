"""Small measured gestures, cached expressions, and IMU handling reactions."""
import logging
import math
from pathlib import Path
import random
import threading
import time

from .errors import BusyError, UnavailableError, ValidationError
from .face_animation import FaceAnimations
from .orchestration import BACKGROUND, INTERACTIVE, Intent, current_intent, intent_scope

LOG = logging.getLogger(__name__)
GESTURES = {
    'happy-wiggle': {'text': 'Quick ten-degree happy wiggle, returning to the original heading.', 'track': 'reaction_happy',
                     'face': 'happy-blink', 'lead_ms': 120, 'settle_ms': 180,
                     'steps': [('turn', 10), ('turn', -20), ('turn', 20), ('turn', -20), ('turn', 10)]},
    'tiny-no': {'text': 'A small left-right head shake.', 'track': None,
                'face': 'curious-glance', 'lead_ms': 120, 'settle_ms': 220,
                'steps': [('turn', 6), ('turn', -12), ('turn', 6)]},
    'tiny-nod': {'text': 'Two gentle 1.2 cm forward-and-back body nods.', 'track': None,
                 'face': 'soft-relief', 'speed': 100, 'lead_ms': 180, 'settle_ms': 250,
                 'pauses': {1: 160},
                 'steps': [('distance', 1.2), ('distance', -1.2), ('distance', 1.2), ('distance', -1.2)]},
    'curious-peek': {'text': 'Pause to look twelve degrees left and right, then face forward again.', 'track': None,
                     'face': 'curious-glance', 'turn_speed': 100, 'lead_ms': 180, 'settle_ms': 220,
                     'pauses': {0: 280, 1: 380}, 'steps': [('turn', -12), ('turn', 24), ('turn', -12)]},
    'happy-dance': {'text': 'Back and forth, side step, half circles and a wiggle within a nominal 16 cm radius.',
                    'face': 'happy-dance', 'lead_ms': 250, 'settle_ms': 350,
                    'pauses': {1: 180, 5: 180, 6: 120, 9: 180},
                    'track': 'dance_bots', 'steps': [('distance', 6), ('distance', -6), ('turn', 90),
                        ('distance', 6), ('distance', -6), ('turn', -90), ('arc', 180),
                        ('turn', 10), ('turn', -20), ('turn', 10), ('arc', 180)]},
    'orbit-dance': {'text': 'One small full circle, with an 8 cm turning radius, and a happy wiggle.',
                    'face': 'mischievous-grin', 'lead_ms': 250, 'settle_ms': 350, 'pauses': {0: 200},
                    'track': 'dance_garden', 'steps': [('arc', 360), ('turn', 10), ('turn', -20), ('turn', 10)]},
    'shuffle': {'text': 'Short forward/backward steps with playful left and right turns.',
                 'face': 'happy-dance', 'lead_ms': 200, 'settle_ms': 300, 'pauses': {1: 200, 4: 140},
                 'track': 'dance_funk', 'steps': [('distance', 6), ('distance', -6), ('turn', 10),
                     ('turn', -20), ('turn', 10), ('distance', 4), ('distance', -4)]},
}


def advance(pose, left, right, wheel_base):
    """Differential-drive exponential map, distances in cm and heading in radians."""
    x, y, heading = pose
    angle, distance = (right-left) / wheel_base, (left+right) / 2
    if abs(angle) < 1e-9:
        return x + distance*math.cos(heading), y + distance*math.sin(heading), heading
    radius = distance / angle
    return (x + radius*(math.sin(heading+angle)-math.sin(heading)),
            y - radius*(math.cos(heading+angle)-math.cos(heading)), heading+angle)


def wheel_distances(step, wheel_base):
    mode = step['mode']
    if mode == 'distance':
        return step['distance_cm'], step['distance_cm']
    angle = math.radians(step['angle_deg'])
    radius = step.get('radius_cm', 0)
    return angle * (radius-wheel_base/2), angle * (radius+wheel_base/2)


def plan(name):
    if name not in GESTURES:
        raise ValidationError('Unknown gesture; choose ' + ', '.join(GESTURES))
    result = []
    routine = GESTURES[name]
    for group, (mode, amount) in enumerate(routine['steps']):
        # Short substeps let us check the measured pose and next swept path.
        count = math.ceil(abs(amount) / (15 if mode == 'arc' else 3 if mode == 'distance' else 20))
        for segment in range(count):
            step = {'mode': mode, 'speed': routine.get('turn_speed', 100) if mode == 'turn' else routine.get('speed', 100), 'duration': .3,
                    'require_encoders': True}
            step['distance_cm' if mode == 'distance' else 'angle_deg'] = amount / count
            if mode == 'arc':
                step['radius_cm'] = 8
            if segment == count-1 and group in routine.get('pauses', {}):
                step['pause_ms'] = routine['pauses'][group]
            result.append(step)
    return result


def inside_envelope(pose, steps, wheel_base, limit=18):
    for step in steps:
        left, right = wheel_distances(step, wheel_base)
        for _ in range(12):
            pose = advance(pose, left/12, right/12, wheel_base)
            if math.hypot(*pose[:2]) > limit:
                return False
    return True


class PickupDetector:
    """Gravity-relative tilt/lift heuristic; a stationary level lift is unobservable."""
    def __init__(self):
        self.baseline = None
        self.ready_at = None
        self.trigger_at = self.settle_at = None
        self.last_sample = None
        self.picked_up = False
        self.angle = 0.0
        self.reference_source = 'startup_pose'

    def update(self, sample, *, moving=False, now=None):
        now = time.monotonic() if now is None else now
        values = sample.get('acceleration_m_s2')
        if not sample.get('fresh') or not isinstance(values, (list, tuple)) or len(values) != 3 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            self.ready_at = self.trigger_at = self.settle_at = None
            return None
        sample_id = sample.get('sample_count', sample.get('timestamp'))
        if sample_id is not None and sample_id == self.last_sample:
            return None
        self.last_sample = sample_id
        magnitude = math.sqrt(sum(v*v for v in values))
        gyro = sample.get('gyro_deg_s') or [0, 0, 0]
        bias = sample.get('gyro_bias_deg_s') or [0, 0, 0]
        gyro = [v-b for v,b in zip(gyro,bias, strict=True)]
        spin = math.sqrt(sum(v*v for v in gyro))
        stable = abs(magnitude - 9.80665) < 1.1 and spin < 8 and not moving
        reference = sample.get('upright_reference') or {}
        up = reference.get('gravity_unit')
        if (isinstance(up, (list, tuple)) and len(up) == 3
                and all(type(v) in (int,float) and math.isfinite(v) for v in up)
                and abs(sum(v*v for v in up)-1) < .01):
            if self.baseline != list(up):
                self.baseline = list(up)
                self.ready_at = self.trigger_at = self.settle_at = None
            self.reference_source = reference.get('source', 'saved_pose')
        if self.baseline is None:
            if stable and magnitude > 1:
                self.ready_at = now if self.ready_at is None else self.ready_at
                if now - self.ready_at >= 2:
                    self.baseline = [v/magnitude for v in values]
            else:
                self.ready_at = None
            return None
        if magnitude > 1:
            self.angle = math.degrees(math.acos(max(-1, min(1, sum(a*b/magnitude for a, b in zip(self.baseline, values, strict=True))))))
        suspected = self.angle > 28 or magnitude < 3 or (not moving and abs(magnitude-9.80665) > 4 and spin > 35)
        if not self.picked_up:
            if suspected:
                self.trigger_at = now if self.trigger_at is None else self.trigger_at
                if now - self.trigger_at >= .12:
                    self.picked_up = True
                    self.settle_at = None
                    return 'picked_up'
            else:
                self.trigger_at = None
        elif stable and self.angle < 12:
            self.settle_at = now if self.settle_at is None else self.settle_at
            if now - self.settle_at >= 2:
                self.picked_up = False
                self.trigger_at = self.settle_at = None
                return 'put_down'
        else:
            self.settle_at = None
        return None


class Signal:
    def __init__(self, external, stop, *, deadline, extra=lambda: False):
        self.external, self.stop, self.deadline, self.extra = external, stop, deadline, extra
    def is_set(self):
        return self.stop.is_set() or time.monotonic() >= self.deadline or self.extra() or bool(self.external and self.external.is_set())
    def wait(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while not self.is_set() and time.monotonic() < end:
            self.stop.wait(min(.02, max(0, end-time.monotonic())))
        return self.is_set()


class DynamicBehavior:
    def __init__(self, controller, assets_dir, *, pickup_reactions=True):
        self.controller, self.assets_dir = controller, Path(assets_dir)
        self.faces = FaceAnimations(self.assets_dir / 'face_animations', controller.media.display)
        self.pickup = PickupDetector()
        self.pickup_reactions = pickup_reactions
        self._stop = threading.Event()
        self._thread = None
        self._play_lock = threading.Lock()
        self._last_reaction = -100.0
        self._reaction_future = None
        self._pending_reaction = None
        self._last_pickup_sound = None
        self._pickup_spoke = False
        self.last_result = {}

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._monitor, name='handling-reactions', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def status(self):
        return {'pickup_reactions_enabled': self.pickup_reactions, 'pickup_detected': self.pickup.picked_up,
                'pickup_calibrated': self.pickup.baseline is not None, 'tilt_deg': round(self.pickup.angle, 1),
                'upright_reference_source': self.pickup.reference_source,
                'last_gesture': dict(self.last_result), 'last_animation': dict(self.faces.last_result)}

    def motion_blocked(self):
        return self.pickup.picked_up

    def _motion_ready(self):
        state = self.controller.motion.status()
        if not state.get('available') or not state.get('encoder_enabled'):
            return 'Calibrated encoder motion must be enabled before dancing.'
        if not self.controller.imu.status().get('fresh') or self.pickup.baseline is None:
            return 'A fresh, settled IMU is required for dance motion.'
        if self.pickup.picked_up or self.pickup.angle > 20:
            return 'Put Robbi down on a level surface before dancing.'
        intent = current_intent()
        filming = intent is not None and intent.owner == 'filming-shot'
        if not filming and (state.get('obstacle', {}).get('blocked') or not state.get('obstacle', {}).get('enabled')):
            return 'Dance motion needs enabled obstacle protection and clear fresh forward ToF.'
        return ''

    def play(self, name, *, with_motion=True, with_audio=True, cancel_event=None):
        if type(with_motion) is not bool or type(with_audio) is not bool:
            raise ValidationError('Gesture output switches must be booleans')
        result = self._play(name, with_motion=with_motion, with_audio=with_audio, cancel_event=cancel_event)
        self.last_result = dict(result)
        return result

    def _play(self, name, *, with_motion=True, with_audio=True, cancel_event=None):
        steps = plan(name)
        routine = GESTURES[name]
        if not self._play_lock.acquire(blocking=False):
            raise BusyError('Another gesture is still running')
        try:
            if with_motion:
                reason = self._motion_ready()
                if reason:
                    return {'ok': False, 'state': 'unavailable', 'reason': reason}
            motion = self.controller.motion
            # A screen/audio preview works even when the motor adapter is absent.
            base = float(motion.status().get('config', {}).get('wheel_base_cm', 10.2)) if with_motion else 10.2
            for step in steps:
                if step['mode'] == 'arc':
                    step['radius_cm'] = max(step['radius_cm'], base/2 + .5)
            if not inside_envelope((0, 0, 0), steps, base):
                raise ValidationError('Gesture exceeds the 18 cm planning envelope')
            signal = Signal(cancel_event, self._stop, deadline=time.monotonic()+90,
                            extra=lambda: with_motion and (self.pickup.picked_up or not self.controller.imu.status().get('fresh')))
            intent = current_intent() or Intent('gesture:' + name, INTERACTIVE, cancelled=signal)
            behavior = getattr(self.controller, 'behavior', None)
            audible = with_audio and routine['track'] and not (behavior and behavior.snapshot().muted)
            resources = ['display'] + (['motion'] if with_motion else []) + (['audio'] if audible else [])
            completed, pose, handle, displayed = 0, (0, 0, 0), None, False
            clip = self.faces.clip(routine['face'])
            started, next_frame = time.monotonic(), 0.0
            peak_offset = 0.0
            with intent_scope(intent), self.controller.orchestrator.acquire(resources, cancel_event=signal, ttl_s=90) as lease:
                def frame(final=False):
                    nonlocal next_frame, displayed
                    now = time.monotonic()
                    if lease.is_set():
                        return False
                    if not final and now < next_frame:
                        return True
                    picture = clip.frames[-1] if final else clip.frame_at(now-started)
                    next_frame = now + .1
                    written = self.controller.media.display.show_pil_frame(picture, cancel_event=lease)
                    displayed = displayed or bool(written)
                    return written

                def pause(milliseconds):
                    until = time.monotonic() + milliseconds / 1000
                    while not lease.is_set() and time.monotonic() < until:
                        frame()
                        lease.wait(min(.02, max(0, until-time.monotonic())))
                    return not lease.is_set()
                try:
                    if audible:
                        sound = self.assets_dir / 'sounds' / (routine['track']+'.wav')
                        if sound.is_file():
                            handle = self.controller.media.play_sound(sound, wait=False, cancel_event=lease)
                    if with_motion:
                        frame()
                        if not pause(routine['lead_ms']):
                            return {'ok': False, 'state': 'cancelled', 'completed_steps': 0}
                        for index, step in enumerate(steps):
                            if lease.is_set():
                                return {'ok': False, 'state': 'cancelled', 'completed_steps': completed}
                            if not inside_envelope(pose, steps[index:], base):
                                return {'ok': False, 'state': 'interrupted', 'reason': 'Measured drift would exceed the dance envelope', 'completed_steps': completed}
                            future = motion.drive({key: value for key, value in step.items() if key != 'pause_ms'}, wait=False, cancel_event=lease)
                            if future is None:
                                return {'ok': False, 'state': 'unavailable', 'reason': 'Motion did not start'}
                            while not future.done() and not lease.is_set():
                                frame()
                                lease.wait(.02)
                            if lease.is_set():
                                return {'ok': False, 'state': 'cancelled', 'completed_steps': completed}
                            result = future.result() or motion.status().get('last_result', {})
                            # Futures are completed before native motor cleanup releases its lease.
                            while motion.is_running() and not lease.is_set():
                                frame()
                                lease.wait(.01)
                            if lease.is_set():
                                return {'ok': False, 'state': 'cancelled', 'completed_steps': completed}
                            if not result.get('ok'):
                                return {'ok': False, 'state': 'interrupted', 'reason': result.get('reason', 'Motion failed'), 'completed_steps': completed}
                            counts = result.get('final')
                            if not isinstance(counts, dict) or not all(k in counts for k in ('left', 'right')):
                                return {'ok': False, 'state': 'interrupted', 'reason': 'Motion returned no measured wheel position'}
                            pose = advance(pose, counts['left']*motion.cm_per_count(), counts['right']*motion.cm_per_count(), base)
                            completed += 1
                            peak_offset = max(peak_offset, math.hypot(*pose[:2]))
                            if math.hypot(*pose[:2]) > 18:
                                return {'ok': False, 'state': 'interrupted', 'reason': 'Dance position guard reached'}
                            if not pause(step.get('pause_ms', 0)):
                                return {'ok': False, 'state': 'cancelled', 'completed_steps': completed}
                        frame(final=True)
                        # Rest on the final pose, rather than restart the loop.
                        if lease.wait(routine['settle_ms']/1000):
                            return {'ok': False, 'state': 'cancelled', 'completed_steps': completed}
                    else:
                        receipt = self.faces.play(routine['face'], loops=3 if routine['track'] and name.endswith('dance') else 1,
                                                  cancel_event=lease, hold_s=0)
                        if not receipt.get('ok'):
                            return receipt
                        displayed = receipt.get('displayed', False)
                    self.last_result = {'ok': True, 'state': 'completed', 'name': name, 'motion': with_motion,
                                        'completed_steps': completed, 'estimated_offset_cm': round(math.hypot(*pose[:2]), 2),
                                        'peak_measured_offset_cm': round(peak_offset, 2),
                                        'heading_error_deg': round(math.degrees(math.remainder(pose[2], 2*math.pi)), 2),
                                        'face_animation': routine['face'], 'elapsed_s': round(time.monotonic()-started, 3),
                                        'audio_started': handle is not None, 'displayed': displayed}
                    return dict(self.last_result)
                finally:
                    if with_motion:
                        motion.stop(owner=intent)
                    if handle is not None:
                        self.controller.media.stop_audio(owner=intent)
        finally:
            self._play_lock.release()

    def _monitor(self):
        while not self._stop.wait(.05):
            try:
                event = self.pickup.update(self.controller.imu.status(), moving=self.controller.motion.is_running())
                if event == 'picked_up':
                    self.controller.motion.stop()
                    invalidate = getattr(getattr(self.controller, 'slam', None), 'invalidate_pose', None)
                    if callable(invalidate):
                        invalidate('Robbi was picked up. Put it down, then relocalize on the map.')
                if event and self.pickup_reactions:
                    self._pending_reaction = (event, time.monotonic())
                # Keep just the newest handling transition. A landing can
                # replace a pickup cue instead of disappearing behind it.
                if self._pending_reaction and (self._reaction_future is None or self._reaction_future.done()):
                    pending, self._pending_reaction = self._pending_reaction, None
                    self._reaction_future = self.controller._submit_guarded(self.controller._effect_executor, self._react, *pending)
            except Exception:
                LOG.debug('Handling monitor sample unavailable', exc_info=True)

    def _react(self, event, observed_at):
        controller = self.controller
        if time.monotonic() - observed_at > 2 or (event == 'picked_up') != self.pickup.picked_up:
            return
        if self._stop.is_set() or controller._filming_mode:
            return
        if controller.autonomy_policy.is_paused() or controller._voice_processing:
            return
        now = time.monotonic()
        quiet = controller.behavior.snapshot().muted or controller.day_cycle.snapshot().get('quiet')
        audible = not quiet and not controller.media.is_audio_playing()
        if event == 'picked_up':
            audible = audible and now - self._last_reaction >= 20
            self._pickup_spoke = False
        else:
            audible = audible and self._pickup_spoke and now - self._last_reaction < 60
            self._pickup_spoke = False
        signal = Signal(controller._action_token()[1], self._stop, deadline=now+8,
                        extra=lambda: (event == 'picked_up') != self.pickup.picked_up)
        intent = Intent('handling-reaction', BACKGROUND, cancelled=signal)
        clip = self.faces.clip('pickup-startle' if event == 'picked_up' else 'soft-relief')
        with intent_scope(intent):
            handle = None
            try:
                resources = ('display', 'audio') if audible else ('display',)
                with controller.orchestrator.acquire(resources, cancel_event=signal, wait_s=0, ttl_s=8) as lease:
                    if audible:
                        choices = [name for name in ('pickup_put_me_down', 'pickup_whee') if name != self._last_pickup_sound]
                        name = random.choice(choices) if event == 'picked_up' else 'pickup_landed'
                        path = self.assets_dir / 'sounds' / (name+'.wav')
                        if not path.is_file():
                            path = self.assets_dir / 'sounds/reaction_surprise.wav'
                        handle = controller.media.play_sound(path, wait=False, cancel_event=lease)
                        if handle is not None and event == 'picked_up':
                            self._last_reaction = now
                            self._last_pickup_sound = name
                            self._pickup_spoke = True
                    started, next_frame, final_shown = time.monotonic(), 0.0, False
                    while not lease.is_set():
                        elapsed = time.monotonic() - started
                        if not final_shown and (elapsed >= next_frame or elapsed >= clip.duration()):
                            final_shown = elapsed >= clip.duration()
                            picture = clip.frames[-1] if final_shown else clip.frame_at(elapsed)
                            if not controller.media.display.show_pil_frame(picture, cancel_event=lease):
                                break
                            next_frame = elapsed + .1
                        if elapsed >= clip.duration() and (handle is None or handle.wait(0) is not None):
                            break
                        lease.wait(.025)
            except (BusyError, UnavailableError):
                pass
            finally:
                if handle is not None:
                    controller.media.stop_audio(owner=intent)
