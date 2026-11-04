"""Deterministic microphone simulation. No providers, credentials or robot outputs.

Requires the development/test NumPy and SciPy extras, not a new runtime service.
Use --manifest <json> --out <private-directory>. Manifest entries contain id,
path, text and reply. Audio and transcripts stay in the chosen output folder.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

RATE = 16000
PROFILES = ('clean', 'room', 'noisy', 'muffled', 'clipped', 'far')


def read_audio(path):
    with wave.open(str(path), 'rb') as source:
        if source.getsampwidth() != 2:
            raise ValueError('Use PCM16 WAV input')
        audio = np.frombuffer(source.readframes(source.getnframes()), '<i2').astype(float)
        audio = audio.reshape(-1, source.getnchannels()).mean(axis=1) / 32768
        rate = source.getframerate()
    if rate != RATE:
        divisor = math.gcd(rate, RATE)
        audio = resample_poly(audio, RATE // divisor, rate // divisor)
    if not 0 < len(audio) <= RATE * 15:
        raise ValueError('Use a short test utterance (up to 15 seconds)')
    return audio


def wav_bytes(audio):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as target:
        target.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        target.writeframes(np.clip(audio * 32767, -32768, 32767).astype('<i2').tobytes())
    return stream.getvalue()


def simulate(audio, profile, seed=42):
    """Approximate a small mono mic: room reflections, rumble, hiss, bandwidth/gain."""
    if profile not in PROFILES:
        raise ValueError('Unknown acoustic profile')
    rng = np.random.default_rng(seed)
    audio = audio / max(.01, np.max(np.abs(audio))) * .48
    if profile == 'clean':
        speech = audio
    else:
        low, high = (350, 2300) if profile == 'muffled' else (150, 5800)
        speech = sosfilt(butter(3, [low, high], btype='bandpass', fs=RATE, output='sos'), audio)
        dry = speech.copy()
        for delay, gain in [(0.037, .23), (.081, .14), (.143, .08), (.233, .04)]:
            offset = int(delay * RATE)
            speech[offset:] += dry[:-offset] * gain
    if profile == 'clipped':
        speech = np.clip(speech * 3., -.35, .35)
    if profile == 'far':
        speech *= .18
    # Noise continues before/after the utterance; silence is not an artificial
    # clean boundary which would hide a stuck capture in a noisy room.
    signal = np.pad(speech, (int(.5 * RATE), int(1.2 * RATE)))
    if profile != 'clean':
        snr = 6 if profile == 'noisy' else 12 if profile == 'far' else 18
        scale = np.sqrt(np.mean(speech**2)) / (10 ** (snr / 20))
        noise = rng.normal(0, 1, len(signal))
        rumble = np.sin(np.arange(len(signal)) * 2 * np.pi * 100 / RATE)
        signal += scale * (.85 * noise + .35 * rumble)
    return np.clip(signal, -.99, .99)


def negatives(seed=42):
    rng = np.random.default_rng(seed)
    t = np.arange(RATE * 3) / RATE
    clicks = np.zeros(len(t))
    for start in (6000, 19000, 36000):
        clicks[start:start+50] = rng.normal(0, .5, 50)
    varying_noise = rng.normal(0, 1, len(t))
    breath = sosfilt(butter(3, 1300, fs=RATE, output='sos'), varying_noise)
    return {'silence': np.zeros(len(t)), 'fan': rng.normal(0, .04, len(t)),
            'hum': .09 * np.sin(2*np.pi*100*t), 'clicks': clicks,
            'dc_fault': np.full(len(t), .07),
            'motor_ramp': (.015+.025*t) * (varying_noise+.5*np.sin(2*np.pi*120*t)),
            'rustling': varying_noise*.1*np.sin(2*np.pi*1.3*t)**6,
            'breathing': breath*.16*np.sin(np.pi*.8*t)**4,
            'electronic_tones': .1*np.sin(2*np.pi*(240*t+30*t*t))*np.sin(np.pi*1.5*t)**2}


def replay(audio, *, reply='Would you like to continue?', root=None):
    """Feed actual PCM state machine; collect admission candidates, never execute."""
    if root:
        sys.path.insert(0, str(Path(root) / 'src'))
    from robbi.configuration.settings import settings
    from robbi.engine import microphone
    saved = settings.voice_enabled, settings.conversation_followup_enabled
    old_time = microphone.time
    clock = [100.]
    microphone.time = SimpleNamespace(monotonic=lambda: clock[0])
    settings.voice_enabled = settings.conversation_followup_enabled = True
    captures = []
    try:
        mic = microphone.MicrophoneService()
        mic._running = True
        mic._effective_wake_engine = lambda: 'disabled'
        def submitted(callback, wav, name, **options):
            captures.append({'wav': wav, 'context': options.get('followup')})
            mic._mode = 'wake'
        mic._submit_transcription = submitted
        mic.request_followup(5., reply=reply)
        mic._apply_pending_followup(clock[0])
        payload = np.clip(audio*32767, -32768, 32767).astype('<i2').tobytes()
        # Enough quiet tail to observe capture ending even after adverse input.
        payload += b'\0' * int(RATE * 2 * 3)
        for offset in range(0, len(payload), mic._chunk_bytes):
            clock[0] += mic._chunk_ms / 1000
            chunk = payload[offset:offset+mic._chunk_bytes].ljust(mic._chunk_bytes, b'\0')
            mic._handle_chunk(chunk)
        return {'captures': captures, 'mode': mic._mode,
                'pending': mic._pending_followup is not None,
                'last_rejection': getattr(mic, '_last_followup_rejection', '')}
    finally:
        settings.voice_enabled, settings.conversation_followup_enabled = saved
        microphone.time = old_time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    entries = json.loads(args.manifest.read_text(encoding='utf-8'))
    report = []
    cases = []
    for entry in entries:
        audio = read_audio(entry['path'])
        for profile in PROFILES:
            cases.append((entry['id']+'-'+profile, simulate(audio, profile), entry))
    for name, audio in negatives().items():
        cases.append((name, audio, {'reply': 'Would you like to continue?', 'text': ''}))
    for name, audio, entry in cases:
        if not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in name):
            raise ValueError('Case IDs must be simple filenames')
        source = args.out / (name+'.wav')
        source.write_bytes(wav_bytes(audio))
        result = replay(audio, reply=entry['reply'], root=Path(__file__).resolve().parents[2])
        captures = result.pop('captures')
        paths = []
        for i, capture in enumerate(captures):
            target = args.out / (name+f'-capture-{i}.wav')
            target.write_bytes(capture['wav'])
            paths.append(str(target.resolve()))
        report.append(dict(id=name, source=str(source.resolve()), captures=paths,
                           text=entry['text'], reply=entry['reply'], **result))
    (args.out/'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'cases':len(report), 'captured':sum(bool(r['captures']) for r in report),
                      'noise_captured':[r['id'] for r in report if not r['text'] and r['captures']]}))


if __name__ == '__main__':
    main()
