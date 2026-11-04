"""Shared drawing and original sound synthesis for the seasonal postcard collection."""
from functools import lru_cache
import hashlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import signal

from prepare_pocket_postcards import (
    art, phase, label, text, pill, env, tone, noise, pcm, loudness, metrics,
    S, RATE, FPS, DURATION, BG, INK, CREAM, GOLD, CORAL, SAGE, LILAC, MUTED,
)

ROOT = Path(__file__).resolve().parents[2]
source_root = ROOT / 'assets/dynamics_sources/seasonal-postcards-v1'


def spring(t, start, end, overshoot=1.4):
    """A bounded settle curve: exact endpoints and a small authored overshoot."""
    if t <= start:
        return 0.0
    if t >= end:
        return 1.0
    p = (t-start)/(end-start)-1
    return 1+(overshoot+1)*p**3+overshoot*p**2


def deform_stamp(im, sprite, x, y, sx=1, sy=1, angle=0):
    """Transform the same prop, retaining its source geometry between frames."""
    if sx < 0:
        sprite = ImageOps.mirror(sprite)
    if sy < 0:
        sprite = ImageOps.flip(sprite)
    size = (max(1, round(sprite.width*abs(sx))), max(1, round(sprite.height*abs(sy))))
    art.stamp(im, sprite.resize(size, Image.Resampling.LANCZOS), x, y, angle)


def reveal_caption(im, detail, t, ready, before, size=18):
    """Fade setup out before payoff appears, keeping both labels readable."""
    setup = 1-phase(t, ready-.18, ready-.02)
    payoff = phase(t, ready+.02, ready+.18)
    if setup > 0:
        layer = Image.new('RGBA', im.size)
        text(ImageDraw.Draw(layer), before, 215, 16, LILAC)
        layer.putalpha(layer.getchannel('A').point(lambda a: round(a*setup)))
        im.alpha_composite(layer)
    if payoff > 0:
        layer = Image.new('RGBA', im.size)
        pill(ImageDraw.Draw(layer), detail['quote'], size=size)
        layer.putalpha(layer.getchannel('A').point(lambda a: round(a*payoff)))
        im.alpha_composite(layer)


@lru_cache(maxsize=1)
def stage_background():
    # Fixed, very soft stage lighting adds depth without moving any pixels.
    y, x = np.mgrid[0:240*S, 0:280*S]
    light = np.exp(-((x-140*S)/(110*S))**2-((y-142*S)/(82*S))**2)
    light[:44*S] = 0
    rgb = np.zeros((240*S, 280*S, 3), dtype=np.uint8)
    for channel, (base, lift) in enumerate(zip((36, 30, 45), (10, 6, 7))):
        rgb[:, :, channel] = np.rint(base+lift*light).astype(np.uint8)
    return Image.fromarray(rgb).convert('RGBA')


def stage(detail):
    im = stage_background().copy()
    d = ImageDraw.Draw(im)
    text(d, detail['heading'], 23, detail.get('heading_size', 24))
    art.line(d, [(18, 41), (262, 41)], '#443344', 1)
    return im


def cover(detail):
    im = stage(detail)
    d = ImageDraw.Draw(im)
    art.star(d, 140, 79, 8, GOLD)
    for value, y, size, color in zip(detail['cover'], (119, 152), (27, 29), (CREAM, CORAL)):
        while d.textbbox((0, 0), value, font=art.font(size))[2] > 252*S and size > 19:
            size -= 1
        text(d, value, y, size, color)
    art.line(d, [(108, 187), (172, 187)], MUTED, 1)
    return im.convert('RGB')


@lru_cache(maxsize=128)
def prop(path, width, height):
    with Image.open(Path(path)) as original:
        sprite = original.convert('RGBA')
        box = sprite.getchannel('A').point(lambda a: 255 if a > 24 else 0).getbbox()
        if not box:
            raise ValueError('Empty prop: ' + str(path))
        sprite = sprite.crop(box)
        sprite.thumbnail((round(width*S), round(height*S)), Image.Resampling.LANCZOS)
        return sprite


KINDS = {'bell', 'chime', 'pop', 'rustle', 'whoosh', 'hum', 'thud', 'jingle', 'sparkle', 'tick', 'chord', 'pluck'}


def synthesize(name, events):
    """Synthesize short nonverbal Foley and musical gestures, with no samples."""
    mix = np.zeros(round(DURATION*RATE))
    seed = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    for n, event in enumerate(events):
        at = float(event['at'])
        kind = event['kind']
        duration = float(event.get('duration', .4))
        gain = float(event.get('gain', .13))
        hz = event.get('hz', 660)
        assert kind in KINDS and .35 <= at and at+duration <= 3.96, (name, event)
        assert .03 <= duration <= 1.8 and 0 < gain <= .6, (name, event)
        freq = [float(hz), float(hz)] if isinstance(hz, (int, float)) else list(hz)
        assert freq and all(160 <= f <= 3600 for f in freq), (name, event)
        if kind == 'chord':
            # Lists here are simultaneous notes, unlike the intentional pitch
            # glides used by hum/chime. Normalize for the number of notes.
            wave = sum(tone(duration, [f, f], harmonics=(1, .10, .025), decay=4)
                       for f in freq) / np.sqrt(len(freq))
            wave *= env(round(duration*RATE), .018, .10)
        elif kind == 'pluck':
            wave = tone(duration, freq, harmonics=(1, .20, .055), decay=13)
        elif kind in {'bell', 'chime', 'sparkle'}:
            wave = tone(duration, freq, harmonics=(1, .06, .018), decay=7)
            wave += .045*tone(duration, [min(4200, f*2.73) for f in freq], harmonics=(1,), decay=16)
        elif kind == 'hum':
            wave = tone(duration, freq, harmonics=(1, .12, .02), vibrato=.006)*env(round(duration*RATE), .04, .09)
        elif kind in {'pop', 'thud'}:
            wave = tone(duration, [freq[0], max(170, freq[-1]*.58)], harmonics=(1, .13, .025), decay=11)
            if kind == 'thud':
                wave += .35*noise(duration, seed+n, 250, 1800)
        elif kind in {'rustle', 'whoosh'}:
            wave = noise(duration, seed+n, 650 if kind == 'rustle' else 350, 3400)
            wave *= env(round(duration*RATE), .04, .08)
        elif kind == 'tick':
            wave = tone(duration, freq, harmonics=(1, .08), decay=25)
        else:  # Original shaker-like jingle, not a quotation of a song.
            wave = tone(duration, freq, harmonics=(1, .14, .04), decay=8)
            wave += .2*noise(duration, seed+n, 1700, 4300)
        start = round(at*RATE)
        mix[start:start+len(wave)] += gain*wave
    mix = signal.sosfilt(signal.butter(3, [165, 4400], btype='bandpass', fs=RATE, output='sos'), mix)
    mix *= env(len(mix), .02, .15)
    measured, peak = loudness(pcm(mix))
    mix *= 10**(min(-25-measured, -9.1-peak)/20)
    data = pcm(mix)
    report = metrics(data)
    assert report['clipped_samples'] == 0 and report['true_peak_dbfs'] <= -9
    assert report['edge_peak'] < .0001 and abs(report['dc_offset']) < .0001
    assert -33 < report['lufs'] <= -24.5, (name, report['lufs'])
    return data, report
