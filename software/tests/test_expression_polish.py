"""Deterministic checks of authored timing and context-aware expression behavior."""
from concurrent.futures import Future
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from robbi import dynamics as dynamics_module, face_animation, orchestration
from robbi.app_runtime.host import AppHost
from robbi.errors import ValidationError
from robbi.expression_catalog import FACE_EXPRESSIONS, SCREEN_GAGS, ALL_MEME_ANIMATIONS, SWEET_ANIMATIONS, POSTCARD_ANIMATIONS, ALL_SEASONAL_ANIMATIONS, ADVENT_ANIMATIONS, local_expression_request
from robbi.face_animation import FaceAnimations
from robbi.orchestration import INTERACTIVE, Intent, intent_scope
from robot import behavior as behavior_module
from robot.domains.conversation import ConversationDomain
from robot.domains import tools as tools_module
from test_app_conversation import Harness
from test_dynamic_features import dynamic_controller

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0, seconds)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    for module in (face_animation, dynamics_module, orchestration):
        monkeypatch.setattr(module, 'time', clock)

    def wait(lease, seconds):
        if not lease.is_set():
            clock.advance(seconds)
        return lease.is_set()

    monkeypatch.setattr(orchestration.Lease, 'wait', wait)
    return clock


def player(clock, *, write_cost=0, cancel_after=None):
    writes, cancel = [], threading.Event()

    def write(image, **kwargs):
        writes.append((clock.now, image))
        clock.advance(write_cost)
        if len(writes) == cancel_after:
            cancel.set()
        return True

    display = SimpleNamespace(orchestrator=orchestration.Orchestrator(), show_pil_frame=write)
    return FaceAnimations(ROOT/'assets/face_animations', display), writes, cancel


def test_all_expressions_load_and_share_bounded_decoded_atlases():
    faces = FaceAnimations(ROOT/'assets/face_animations', None)
    assert set(faces.catalog()) >= set(FACE_EXPRESSIONS) | set(SCREEN_GAGS) | set(ALL_MEME_ANIMATIONS) | set(SWEET_ANIMATIONS) | set(POSTCARD_ANIMATIONS) | set(ALL_SEASONAL_ANIMATIONS) | set(ADVENT_ANIMATIONS)
    neutral = faces.clip('natural-blink').frames[0]
    assert faces.clip('curious-glance').frames[0] is neutral
    for name in FACE_EXPRESSIONS:
        clip = faces.clip(name)
        assert .5 < clip.duration() <= 4
        assert clip.duration(10) == clip.duration(30)
        assert all(frame.size == (280, 240) and frame.mode == 'RGB' for frame in clip.frames)
    assert len(faces._cache) == 3 and len(faces._sheets) == 2


@pytest.mark.parametrize('fps', [10, 30])
def test_frame_rate_changes_sampling_not_expression_duration(clock, fps):
    faces, writes, cancel = player(clock)
    result = faces.play('cheeky-wink', fps=fps, loops=2, cancel_event=cancel)
    assert result['ok'] and result['displayed']
    assert result['duration_s'] == result['elapsed_s'] == 2.72
    assert result['frames'] == (28 if fps == 10 else 82)
    assert writes[-1][1] is faces.frames('cheeky-wink')[-1]
    assert result['recommended_fps'] == fps


def test_slow_spi_drops_late_frames_and_finishes_on_rest_pose(clock):
    faces, writes, cancel = player(clock, write_cost=.065)
    result = faces.play('curious-glance', fps=30, cancel_event=cancel)
    assert result['ok'] and result['skipped'] > 20
    assert result['elapsed_s'] <= result['duration_s'] + .13
    assert 14 < result['actual_fps'] < 16 and result['recommended_fps'] == 10
    assert writes[-1][1] is faces.frames('curious-glance')[-1]
    assert all(b[0]-a[0] >= .064 for a, b in zip(writes, writes[1:]))


def test_stop_during_transition_has_no_final_repaint_or_held_lease(clock):
    faces, writes, cancel = player(clock, cancel_after=9)
    result = faces.play('cheeky-wink', fps=30, cancel_event=cancel)
    assert result['state'] == 'cancelled' and len(writes) == 9
    assert faces.display.orchestrator.snapshot()['active_leases'] == 0
    assert writes[-1][1].tobytes() != faces.frames('cheeky-wink')[-1].tobytes()


@pytest.mark.parametrize('fields', [
    {'durations_ms': [100]}, {'durations_ms': [100, True]},
    {'durations_ms': [100, float('nan')]}, {'durations_ms': [100, 100], 'blend_ms': [0, 101]},
    {'durations_ms': [100, 100], 'blend_ms': [0]}, {'blend_ms': 20},
])
def test_invalid_timelines_are_rejected_before_output(tmp_path, fields):
    (tmp_path/'sheet.png').write_bytes((ROOT/'assets/face_animations/expression-atlas-v2.png').read_bytes())
    spec = {'sheet': 'sheet.png', 'columns': 4, 'rows': 5, 'order': [0, 1], **fields}
    (tmp_path/'bad.json').write_text(json.dumps(spec))
    with pytest.raises(ValidationError):
        FaceAnimations(tmp_path, None).clip('bad')


def test_legacy_sheets_keep_frame_based_timing(tmp_path):
    (tmp_path/'sheet.png').write_bytes((ROOT/'assets/face_animations/happy-blink.png').read_bytes())
    (tmp_path/'legacy.json').write_text(json.dumps({'sheet': 'sheet.png', 'columns': 2, 'rows': 5}))
    clip = FaceAnimations(tmp_path, None).clip('legacy')
    assert clip.duration(10) == 1 and clip.duration(30) == pytest.approx(1/3)
    assert clip.frame_at(.15) is clip.frames[1]


def test_preview_of_silent_gesture_does_not_touch_motors_or_preempt_audio(clock):
    dynamics, motion, audio = dynamic_controller()
    motion.status = lambda: pytest.fail('Preview must not query absent motors')
    speaker = Intent('existing-speaker', INTERACTIVE)
    with intent_scope(speaker):
        lease = dynamics.controller.orchestrator.acquire(('audio',))
    try:
        result = dynamics.play('tiny-nod', with_motion=False)
        assert result['ok'] and result['displayed'] and not audio and not motion.stops
        assert not lease.is_set()
    finally:
        lease.release()


def reaction_controller(clock, *, quiet=False, muted=False, busy_audio=False):
    dynamics, motion, _ = dynamic_controller()
    controller, writes, sounds, stops = dynamics.controller, [], [], []
    controller._filming_mode = controller._voice_processing = False
    controller.autonomy_policy = SimpleNamespace(is_paused=lambda: False)
    controller._action_token = lambda: (1, threading.Event())
    controller.behavior = SimpleNamespace(snapshot=lambda: SimpleNamespace(muted=muted))
    controller.day_cycle = SimpleNamespace(snapshot=lambda: {'quiet': quiet})
    controller.media.is_audio_playing = lambda: busy_audio
    controller.media.display.show_pil_frame = lambda frame, **kwargs: writes.append(frame) or True
    controller.media.play_sound = lambda path, **kwargs: sounds.append(path.stem) or SimpleNamespace(wait=lambda seconds: 0)
    controller.media.stop_audio = lambda **kwargs: stops.append(kwargs)
    dynamics.pickup.picked_up = True
    return dynamics, writes, sounds, stops


@pytest.mark.parametrize('setting', ['quiet', 'muted', 'busy_audio'])
def test_quiet_or_occupied_pickup_is_visual_only(clock, setting):
    dynamics, writes, sounds, stops = reaction_controller(clock, **{setting: True})
    dynamics._react('picked_up', clock.now)
    assert len(writes) >= 10 and not sounds and not stops
    assert writes[-1] is dynamics.faces.frames('pickup-startle')[-1]


def test_pickup_speech_alternates_and_repeated_handling_stays_quiet(clock):
    dynamics, writes, sounds, stops = reaction_controller(clock)
    dynamics._react('picked_up', clock.now)
    first = sounds[0]
    dynamics.pickup.picked_up = False
    dynamics._react('put_down', clock.now)
    assert sounds[-1] == 'pickup_landed'
    dynamics.pickup.picked_up = True
    dynamics._react('picked_up', clock.now)
    assert len(sounds) == 2  # A quick re-lift still gets a face, without another line.
    clock.advance(21)
    dynamics._react('picked_up', clock.now)
    assert sounds[-1] != first and len(sounds) == len(stops) == 3
    assert all(stop['owner'].owner == 'handling-reaction' for stop in stops)


def test_landing_cancels_airborne_face_and_only_its_own_audio(clock):
    dynamics, writes, sounds, stops = reaction_controller(clock)

    def land(frame, **kwargs):
        writes.append(frame)
        dynamics.pickup.picked_up = False
        return True

    dynamics.controller.media.display.show_pil_frame = land
    dynamics._react('picked_up', clock.now)
    assert len(writes) == len(sounds) == len(stops) == 1
    assert dynamics.controller.orchestrator.snapshot()['active_leases'] == 0


def test_monitor_keeps_landing_until_previous_reaction_finishes(clock):
    dynamics, _, _, _ = reaction_controller(clock)
    previous = dynamics._reaction_future = Future()
    events, submitted = iter(('put_down', None)), []
    waits = iter((False, False, True))
    dynamics._stop = SimpleNamespace(wait=lambda seconds: next(waits))

    def update(*args, **kwargs):
        event = next(events)
        if event is None:
            previous.set_result(None)
        return event

    dynamics.pickup.update = update
    dynamics.controller._effect_executor = object()
    dynamics.controller._submit_guarded = lambda executor, callback, *args: submitted.append(args) or Future()
    dynamics._monitor()
    assert submitted == [('put_down', clock.now)] and dynamics._pending_reaction is None


def test_idle_expressions_have_cooldowns_yawn_spacing_and_no_body_or_audio(monkeypatch):
    monkeypatch.setattr(behavior_module.BehaviorEngine, '_load', lambda self: behavior_module.BehaviorSnapshot())
    monkeypatch.setattr(behavior_module.random, 'choice', lambda choices: choices[-1])
    engine = behavior_module.BehaviorEngine()
    engine._next_expression_at = 0
    engine._state.mood = 'tired'
    assert engine.passive_presentation(now=100) == {'face_animation': 'sleepy-yawn'}
    assert engine.passive_presentation(now=101) is None
    assert engine.passive_presentation(now=130) == {'face_animation': 'sleepy-breath'}
    assert engine.passive_presentation(now=200) == {'face_animation': 'sleepy-yawn'}
    engine._state.awake = False
    assert engine.passive_presentation(now=250) == {'face_animation': 'sleepy-breath'}
    assert engine._next_expression_at >= 285


@pytest.mark.parametrize('guard', ['paused', 'voice', 'audio', 'thinking'])
def test_idle_does_not_interrupt_stop_voice_or_thinking(guard):
    subject = Harness()
    waits = iter((False, True))
    subject._running, subject._filming_mode = True, False
    subject._lifecycle_stop = SimpleNamespace(wait=lambda timeout: next(waits))
    subject.focus_timer = SimpleNamespace(is_active=lambda: False)
    subject.autonomy_policy = SimpleNamespace(is_paused=lambda: guard == 'paused')
    subject._voice_processing = guard == 'voice'
    subject.media = SimpleNamespace(is_audio_playing=lambda: guard == 'audio')
    subject._presentation_state = {'thinking': guard == 'thinking'}
    subject.behavior = SimpleNamespace(passive_presentation=lambda: pytest.fail('Idle must stay quiet'))
    subject._ambient_loop()


@pytest.mark.parametrize('text,name,tool', [
    ('Robbi, please wink at me!', 'cheeky-wink', 'animate'),
    ('Robbi, schau nach links, bitte.', 'look-left', 'animate'),
    ('Do a happy dance', 'happy-dance', 'perform'),
    ('preview curious peek', 'curious-peek', 'perform'),
])
def test_narrow_local_expression_shortcuts(text, name, tool):
    host = SimpleNamespace(packages=SimpleNamespace(projects={'expressions': None}), enabled=lambda app: True)
    request = local_expression_request(host, text)
    assert request['tool'] == tool and request['arguments']['name'] == name
    if text.startswith('preview'):
        assert request['arguments']['with_motion'] is False
    assert local_expression_request(host, 'Why do people wink at me?') is None
    host.enabled = lambda app: False
    assert local_expression_request(host, text) is None


def test_spoken_expression_uses_real_app_without_model_and_temporarily_keeps_pose(tmp_path, monkeypatch):
    class Conversation(ConversationDomain, Harness):
        pass

    subject = Conversation()
    subject.behavior = SimpleNamespace(note_interaction=lambda: None)
    subject._touch_activity = lambda: None
    subject.parse_debug_command = lambda text: None
    executed, animated = [], []
    subject.execute_action = lambda action, **kwargs: executed.append(action) or action
    subject.memory = SimpleNamespace(add_user_message=lambda *a, **k: None, add_assistant_message=lambda *a, **k: None)
    robot = SimpleNamespace(capture_token=lambda: None,
        play_video=lambda name, *a, **k: animated.append(name) or {'ok': True, 'state': 'completed', 'frames_displayed': 41, 'audio_state': 'completed'})
    host = AppHost(ROOT, tmp_path, robot, permissions_enabled=False)
    host.scan()
    subject.app_host = host
    try:
        result = subject.think_and_respond('wink at me', source='speech_input')
        assert result['app_result']['result']['ok'] and animated == ['cheeky-wink']
        assert subject.provider_calls == [] and executed == [result]
        assert subject._app_preserves_display()
        expiry = subject._app_display_until
        monkeypatch.setattr(tools_module, 'time', SimpleNamespace(monotonic=lambda: expiry+.1))
        assert not subject._app_preserves_display()
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def test_voice_shortcut_still_requires_display_permission(tmp_path):
    robot = SimpleNamespace(capture_token=lambda: None,
        face_animation=lambda *a, **k: pytest.fail('Permission must precede display'))
    host = AppHost(ROOT, tmp_path, robot)
    host.scan()
    try:
        request = local_expression_request(host, 'blink')
        result = host.call(request['app'], request['tool'], request['arguments'])
        assert result['permission_required']
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)
