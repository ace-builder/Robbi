"""Small JSONL contract and pure timing plan; no hardware or provider imports."""
import copy
import json
import math
import re

from ..errors import ValidationError
from .timed_motion import TIMED_MODES, validate as timed_move

MAX_SHOT_MS = 600_000
CHANNELS = {'speak': {'audio'}, 'sound': {'audio'}, 'move': {'motion'},
            'face': {'display'}, 'face_animation': {'display'},
            'gesture': {'motion', 'audio', 'display'}, 'pause': set(),
            'wait_for_wake': {'microphone'}}


def output_channels(cue):
    channels = CHANNELS[cue['action']]
    if cue['action'] == 'speak' and cue.get('face_pattern'):
        return channels | {'display'}
    return channels


def number(value, name, low, high, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
        raise ValidationError(f'{name} must be {"an integer" if integer else "a number"} between {low} and {high}')
    return int(value) if integer else value


def text(value, name, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValidationError(f'{name} must contain 1–{limit} characters')
    return value.strip()


def asset(value):
    value = text(value, 'Asset name')
    if '/' in value or '\\' in value or '..' in value:
        raise ValidationError('Use a registered asset name, not a file path')
    return value


def voice(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValidationError('Use a valid ElevenLabs voice ID')
    return value


def shot(value, default_voice):
    if not isinstance(value, dict) or set(value) - {'name', 'description', 'voice_id', 'record_video', 'actions'}:
        raise ValidationError('A shot contains name, optional description/voice_id/record_video, and actions')
    name = text(value.get('name'), 'Shot name')
    description = value.get('description', '')
    if not isinstance(description, str) or len(description) > 1000:
        raise ValidationError('Description must be text up to 1000 characters')
    result = {'name': name, 'description': description, 'voice_id': voice(value.get('voice_id', default_voice)), 'actions': []}
    if type(value.get('record_video', False)) is not bool:
        raise ValidationError('record_video must be true or false')
    result['record_video'] = value.get('record_video', False)
    actions = value.get('actions')
    if not isinstance(actions, list) or not 1 <= len(actions) <= 64:
        raise ValidationError('A shot needs between 1 and 64 actions')
    seen = {}
    fields = {'speak': {'text', 'voice_id', 'face_pattern'}, 'move': {'move'}, 'pause': {'duration_ms'},
              'face': {'name'}, 'face_animation': {'name'}, 'gesture': {'name'}, 'sound': {'name'},
              'wait_for_wake': set()}
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict) or raw.get('action') not in CHANNELS:
            raise ValidationError(f'Action {index+1}: choose '+', '.join(CHANNELS))
        kind = raw['action']
        if set(raw) - {'id', 'action', 'start', 'timeout_ms'} - fields[kind]:
            raise ValidationError(f'Action {index+1}: unknown fields for {kind}')
        identifier = raw.get('id', f'a{index+1}')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', identifier) or identifier in seen:
            raise ValidationError(f'Action {index+1}: use a unique ID, starting with a letter')
        cue = {'id': identifier, 'action': kind, 'timeout_ms': number(raw.get('timeout_ms', 120000), 'timeout_ms', 100, 120000, True)}
        start = copy.deepcopy(raw.get('start', {'after': next(reversed(seen)), 'event': 'end'} if seen else {'at_ms': 0}))
        if not isinstance(start, dict):
            raise ValidationError(f'{identifier}: start must be an object')
        if 'at_ms' in start:
            if set(start) != {'at_ms'}:
                raise ValidationError(f'{identifier}: use at_ms OR after/event/offset_ms')
            start['at_ms'] = number(start['at_ms'], 'at_ms', 0, MAX_SHOT_MS, True)
        else:
            if set(start) - {'after', 'event', 'offset_ms'} or start.get('after') not in seen:
                raise ValidationError(f'{identifier}: after must reference an earlier action ID')
            start.setdefault('event', 'end')
            if start['event'] not in {'start', 'end'}:
                raise ValidationError('Timing event must be start or end')
            start['offset_ms'] = number(start.get('offset_ms', 0), 'offset_ms', -110000, MAX_SHOT_MS, True)
            if start['offset_ms'] < 0 and (start['event'] != 'end' or seen[start['after']]['action'] not in {'speak', 'sound', 'pause'}):
                raise ValidationError('Negative offsets require the end of prepared speech, sound or a pause')
        cue['start'] = start
        if kind == 'speak':
            cue['text'] = text(raw.get('text'), f'{identifier} speech', 10000)
            if 'voice_id' in raw: cue['voice_id'] = voice(raw['voice_id'])
            if 'face_pattern' in raw:
                pattern = raw['face_pattern']
                if not isinstance(pattern, list) or not 2 <= len(pattern) <= 6:
                    raise ValidationError('face_pattern needs 2–6 registered face names')
                cue['face_pattern'] = [asset(name) for name in pattern]
        elif kind == 'pause':
            cue['duration_ms'] = number(raw.get('duration_ms'), 'duration_ms', 0, 110000, True)
        elif kind == 'wait_for_wake':
            pass  # Actual detection anchors subsequent end-linked cues.
        elif kind == 'move':
            move = raw.get('move')
            if isinstance(move, dict) and move.get('mode') in TIMED_MODES:
                cue['move'] = timed_move(move)
                if cue['timeout_ms'] < cue['move']['duration_ms'] + 1000:
                    raise ValidationError('timeout_ms must allow the timed motion plus 1000 ms')
                seen[identifier] = cue
                result['actions'].append(cue)
                continue
            if not isinstance(move, dict) or set(move) - {'mode', 'distance_cm', 'angle_deg', 'radius_cm', 'amplitude_cm', 'cycles', 'speed'}:
                raise ValidationError('move must use supported movement fields; safety overrides are not accepted')
            mode = move.get('mode')
            required = {'distance': {'distance_cm'}, 'turn': {'angle_deg'}, 'arc': {'radius_cm', 'angle_deg'},
                        'sine': {'distance_cm', 'cycles'}, 'wiggle': {'angle_deg', 'cycles'}, 'stop': set()}
            if mode not in required or not required[mode] <= move.keys():
                raise ValidationError('move requires distance/distance_cm, turn/angle_deg, arc/radius_cm/angle_deg, sine/distance_cm/cycles, wiggle/angle_deg/cycles, or stop')
            limits = {'distance_cm': (-500 if mode == 'distance' else -100, 500 if mode == 'distance' else 100), 'angle_deg': (-360, 360), 'radius_cm': (.1, 50),
                      'amplitude_cm': (0, 25), 'cycles': (1, 6 if mode == 'sine' else 8), 'speed': (0, 100)}
            cue['move'] = {'mode': mode, **{key: number(v, key, *limits[key], integer=key in {'cycles', 'speed'}) for key, v in move.items() if key != 'mode'}}
            if mode == 'sine' and move['distance_cm'] <= 0: raise ValidationError('Sine forward distance must be positive')
            if mode == 'wiggle' and not 5 <= move['angle_deg'] <= 60: raise ValidationError('Wiggle angle must be 5–60 degrees')
        else:
            cue['name'] = asset(raw.get('name'))
        seen[identifier] = cue
        result['actions'].append(cue)
    return result


def jsonl(source, default_voice):
    if not isinstance(source, str) or len(source.encode('utf-8')) > 1024*1024:
        raise ValidationError('Upload JSONL text up to 1 MiB')
    result, names = [], set()
    for line_no, line in enumerate(source.lstrip('\ufeff').splitlines(), 1):
        if not line.strip(): continue
        try:
            value = shot(json.loads(line), default_voice)
        except (ValueError, TypeError, RecursionError, ValidationError) as error:
            raise ValidationError(f'Line {line_no}: {error}') from None
        if value['name'] in names: raise ValidationError(f'Line {line_no}: duplicate shot name {value["name"]}')
        result.append(value); names.add(value['name'])
        if len(result) > 100: raise ValidationError('Import at most 100 shots at once')
    if not result: raise ValidationError('The JSONL file contains no shots')
    return result


def planned_times(definition, durations):
    """Preview known times; motion completion remains unknown until executed."""
    times = {}
    for cue in definition['actions']:
        timing = cue['start']; start = timing.get('at_ms')
        if start is None:
            ref = times[timing['after']]
            anchor = ref[timing['event']]
            start = None if anchor is None else anchor + timing.get('offset_ms', 0)
            if timing.get('offset_ms', 0) < 0:
                duration = durations.get(timing['after'])
                if duration is None or duration + timing['offset_ms'] < 0:
                    raise ValidationError(f'{cue["id"]}: before-end offset exceeds the prepared clip duration')
        duration = durations.get(cue['id'])
        end = None if start is None or duration is None else start+duration
        if (start is not None and start < 0) or (end is not None and end > MAX_SHOT_MS) or (start is not None and start > MAX_SHOT_MS):
            raise ValidationError('Shot timing exceeds the 10-minute limit')
        times[cue['id']] = {'start': start, 'end': end}
    entries = definition['actions']
    for index, cue in enumerate(entries):
        a = times[cue['id']]
        if a['start'] is None or a['end'] is None: continue
        for other in entries[:index]:
            b = times[other['id']]
            if output_channels(cue) & output_channels(other) and b['start'] is not None and b['end'] is not None and max(a['start'], b['start']) < min(a['end'], b['end'])-.01:
                raise ValidationError(f'{other["id"]} and {cue["id"]} overlap on the same output; adjust their timing')
    return times
