"""Provider-independent movement contract, emitted even with custom TOOLS.md.

Examples are exercised through the sanitizer, executor and real path planner
in test_motion_contract.py. They describe target motion, not exact odometry.
"""
import json


DRIVE_EXAMPLES = (
    ('forward 10 cm', {'mode': 'distance', 'distance_cm': 10}),
    ('backward 10 cm', {'mode': 'distance', 'distance_cm': -10}),
    ('turn left 90 degrees', {'mode': 'turn', 'angle_deg': 90}),
    ('turn right 90 degrees', {'mode': 'turn', 'angle_deg': -90}),
    ('left arc, radius 15 cm, angle 90 degrees', {'mode': 'arc', 'radius_cm': 15, 'angle_deg': 90}),
    ('right arc, radius 15 cm, angle 90 degrees', {'mode': 'arc', 'radius_cm': 15, 'angle_deg': -90}),
    ('drive a sine wave 30 cm forward', {'mode': 'sine', 'distance_cm': 30, 'cycles': 1}),
    ('wiggle in place twice', {'mode': 'wiggle', 'angle_deg': 15, 'cycles': 2}),
    ('stop moving', {'mode': 'stop'}),
)

DRIVE_SCHEMA = {
    'type': 'object',
    'properties': {
        'mode': {'type': 'string', 'enum': ['distance', 'turn', 'arc', 'sine', 'wiggle', 'stop']},
        'distance_cm': {'type': 'number', 'minimum': -100, 'maximum': 100},
        'angle_deg': {'type': 'number', 'minimum': -360, 'maximum': 360},
        'radius_cm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 50},
        'amplitude_cm': {'type': 'number', 'minimum': 0, 'maximum': 25},
        'cycles': {'type': 'integer', 'minimum': 1, 'maximum': 8},
        'speed': {'type': 'integer', 'minimum': 0, 'maximum': 100},
    },
    'required': ['mode'],
    'additionalProperties': False,
}


def motion_prompt_catalog():
    # Read gesture names from the implementation, not a second hand-kept list.
    from .dynamics import GESTURES

    lines = [
        'Runtime movement contract (authoritative API; supplements custom tool notes):',
        'Robbi supports encoder-targeted distances in centimeters, turns in degrees, arcs, '
        'forward sine/wave paths, in-place wiggles, and registered body gestures. '
        'It is NOT limited to speed and time. Use these calls when movement is requested; '
        'a capability question alone does not request motion.',
        'Emit a top-level `drive` object (or a `drive` in an ordered sequence step). '
        'Parameters: ' + json.dumps(DRIVE_SCHEMA, separators=(',', ':')),
        'For distance use signed distance_cm: positive forward, negative backward. '
        'For turn/arc use signed angle_deg: positive left, negative right. '
        'Do not use direction to reverse mode=distance or mode=turn; their signed values determine direction.',
        'For sine (also called wave/sine_forward), distance_cm is positive forward extent, '
        'cycles is 1–6; radius_cm/angle_deg constrain curvature/heading. Leave amplitude_cm '
        'unset for the feasible automatic amplitude. Explicit amplitude_cm is the sine '
        'coefficient (peak lateral excursion is twice this); infeasible geometry is rejected. '
        'Sine moves forward; wiggle stays in place, swings angle_deg 5–60 each side, '
        'cycles 1–8, and returns to its starting heading.',
        'speed is optional PWM percent (default 100), not cm/s. duration is a legacy timed-drive '
        'parameter, not a substitute for distance or angle. For requested centimeters/degrees '
        'use the measured modes directly; do not offer a guessed timed burst. '
        'Targets are approximate physical motion, subject to encoder calibration and wheel slip. '
        'Hardware/interlocks may refuse or stop an action; never override them or claim '
        'completion before a result. A missing sensor does not mean this API lacks distance support.',
        'Exact action examples:',
    ]
    lines.extend('- ' + text + ': ' + json.dumps({'drive': spec}, separators=(',', ':'))
                 for text, spec in DRIVE_EXAMPLES)
    lines.append('Use sequence with wait_for_completion:true on each drive step for ordered '
                 'moves, such as forward 10 cm then turn right. A short speak may acknowledge '
                 'the request; do not say it is already done.')
    lines.append('Registered `gesture` string values (wheel/body movements, not arms):')
    lines.extend('- ' + json.dumps({'gesture': name}, separators=(',', ':')) + ': ' + item['text']
                 for name, item in GESTURES.items())
    lines.append('For a greeting wave use happy-wiggle; for a requested driving wave use sine. '
                 'A gesture owns motion/presentation: keep other drive or animation effects '
                 'in separate sequence steps. Older conversation replies or custom notes '
                 'that describe only direction/speed/duration are incomplete; use this contract.')
    return '\n'.join(lines)
