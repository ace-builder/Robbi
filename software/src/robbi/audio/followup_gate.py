"""Cheap rejection of obvious non-speech before follow-up STT.

This is an acoustic sanity check, not speaker recognition or a speech model.
The semantic gate remains mandatory. No NumPy/SciPy/model dependency.
"""
from __future__ import annotations

import math
import sys
from array import array


def rejection(pcm: bytes, sample_rate: int, minimum_rms: float = 260) -> str:
    if not pcm or len(pcm) % 2 or not 8000 <= sample_rate <= 48000:
        return 'invalid_audio'
    # Bound work even if a caller supplies a corrupt or unusually long capture.
    if len(pcm) > sample_rate * 2 * 30:
        return 'audio_too_long'
    samples = array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    # Frame energy is sufficient here; decimation keeps the check cheap on Pi.
    stride = max(1, sample_rate // 4000)
    samples = samples[::stride]
    frame_size = max(1, round(sample_rate / stride * .02))
    levels = []
    for offset in range(0, len(samples) - frame_size + 1, frame_size):
        frame = samples[offset:offset + frame_size]
        mean = sum(frame) / frame_size
        variance = sum(value * value for value in frame) / frame_size - mean * mean
        levels.append(math.sqrt(max(0., variance)))
    if not levels:
        return 'too_short'
    threshold = max(minimum_rms, max(levels) * .12)
    active = sorted(level for level in levels if level >= threshold)
    if len(active) < 8:  # At least 160 ms of non-DC energy, including pre-roll.
        return 'too_little_speech'
    low = active[int((len(active) - 1) * .10)]
    high = active[int((len(active) - 1) * .90)]
    if high / max(1., low) < 1.6:
        return 'stationary_noise'
    return ''
