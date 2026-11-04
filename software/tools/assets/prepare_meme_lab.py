"""Seven original audiovisual LCD cartoons; deterministic art and sound design.

The built-in imagegen originals remain untouched in dynamics_sources/meme-lab-v1.
All sound is synthesized here; no recordings, voices or third-party samples.
"""
from __future__ import annotations

from asset_catalog import catalog_group, catalog_path, read_catalog
from functools import lru_cache
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import signal

import prepare_screen_gags as art
from prepare_emotion_audio import loudness, metrics

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'assets/dynamics_sources/meme-lab-v1'
OUT = ROOT / 'test-results/meme-lab-preview'
S = 3
RATE = 22050
FPS = 20
DURATION = 4.5
BG = '#14272d'
INK = '#10282c'
CREAM = '#fff1cf'
MINT = '#a8dfc7'
CORAL = '#ff9683'
GOLD = '#ffc579'
LILAC = '#c7b5f3'
STARTS = [0, .1, .3, .5, .75, 1, 1.2, 1.4, 1.55, 1.75, 1.95, 2.2, 2.45, 2.7, 3, 3.4, 3.85, 4.1, 4.25, 4.35]
TIMING = [round((b-a)*1000) for a, b in zip(STARTS, STARTS[1:]+[DURATION])]
CLIPS = {
    'meme-support-brick': dict(title='Emotional support brick', quote='IT GETS ME.',
        when='When the most reliable person in the room is a brick.',
        sound='Angelic sparkle, a very solid thud, and one deeply unhelpful squeak.',
        cues=[(.45, 'hopeful sparkle'), (1.5, 'brick lands'), (2.25, 'deadpan squeak')]),
    'meme-optimism-shredder': dict(title='Optimism.exe', quote='BACK TO REALITY.',
        when='For that brief, beautiful moment before checking the details.',
        sound='Cheerful startup, mechanical paper munching, and a descending error sigh.',
        cues=[(.45, 'optimistic boot'), (1.05, 'shredder starts'), (2.35, 'reality arrives')]),
    'meme-last-beep': dict(title='Per my last beep', quote='KIND REGARDS.',
        when='When a polite reminder has acquired legal representation.',
        sound='Inbox ping, frantic printer chatter, a rubber stamp, and two flat beeps.',
        cues=[(.5, 'mail arrives'), (1.05, 'printing'), (2.2, 'stamp lands'), (2.55, 'last beep')]),
    'meme-premium-empathy': dict(title='Empathy: premium', quote='FREE TRIAL ENDED.',
        when='Compassion is loading. Your subscription is not.',
        sound='Warm heart chimes abruptly interrupted by a lock and a dry payment rejection.',
        cues=[(.45, 'warm welcome'), (1.8, 'paywall locks'), (2.35, 'payment declined')]),
    'meme-overreaction': dict(title='Certified overreaction', quote='REASONABLE.',
        when='A microscopic inconvenience receives a full theatrical response.',
        sound='One tiny plink, an absurd original brass-like blast, and a deflating squeak.',
        cues=[(.7, 'tiny inconvenience'), (1.5, 'dramatic response'), (2.55, 'certification stamp')]),
    'meme-family-audacity': dict(title='Family-size audacity', quote='FAMILY SIZE.',
        when='That is an industrial quantity of confidence.',
        sound='Scanner chirps, a fizzy can pop, an elastic growth boing, and a smug honk.',
        cues=[(.5, 'audacity scan'), (1.5, 'can opens'), (1.75, 'audacity expands'), (2.5, 'smug honk')]),
    'meme-problem-compost': dict(title='Problem composter', quote='PERSONAL GROWTH.',
        when='The problem has been processed. Unfortunately, it is thriving.',
        sound='Paper crumple, a plop, digestive robot burbles, and a suspiciously cheerful sprout.',
        cues=[(.45, 'problem crumples'), (1.2, 'compost plop'), (1.65, 'processing'), (2.5, 'personal growth')]),
}


def phase(t, start, end):
    return art.ease((t-start)/(end-start))


def label(draw, value, x, y, size=20, color=CREAM, bold=True):
    font = art.font(size, bold)
    box = draw.textbbox((0, 0), value, font=font)
    if box[2]-box[0] > 258*S:
        raise ValueError('Caption too wide: '+value)
    draw.text((round(x*S), round(y*S)), value, anchor='mm', font=font, fill=color)


def text(draw, value, y, size=22, color=CREAM):
    label(draw, value, 140, y, size, color)


def pill(draw, value, y, size=23, color=INK, fill=GOLD):
    box = draw.textbbox((0, 0), value, font=art.font(size))
    width = (box[2]-box[0])/S+20
    art.rounded(draw, (140-width/2, y-18, 140+width/2, y+18), 9, fill)
    text(draw, value, y, size, color)


@lru_cache(None)
def prop(name, width, height):
    with Image.open(SOURCE/(name+'.png')) as source:
        source = source.convert('RGBA')
        bbox = source.getchannel('A').point(lambda a: 255 if a > 24 else 0).getbbox()
        if not bbox:
            raise ValueError('Empty prop: '+name)
        sprite = source.crop(bbox)
        sprite.thumbnail((round(width*S), round(height*S)), Image.Resampling.LANCZOS)
        return sprite


def sparks(draw, t, cx=140, cy=125, count=8, radius=75, color=GOLD):
    for n in range(count):
        a = n*math.tau/count + .12*math.sin(t*2)
        r = radius+4*math.sin(n+t*4)
        art.star(draw, cx+math.cos(a)*r, cy+math.sin(a)*r*.66,
                 2.7+1.3*math.sin(t*5+n)**2, color, a)


def paper(draw, x, y, width=68, height=76, content='HOPE'):
    art.rounded(draw, (x-width/2, y-height/2, x+width/2, y+height/2), 5, CREAM)
    art.line(draw, [(x-width/2+9, y+height/2-12), (x+width/2-9, y+height/2-12)], '#c1baa1', 2)
    label(draw, content, x, y+3, 17, INK)


def scene(name, t):
    image = Image.new('RGBA', (280*S, 240*S), BG)
    draw = ImageDraw.Draw(image)
    # A quiet fixed stage line grounds the objects; it never drifts between poses.
    art.ellipse(draw, (52, 174, 228, 190), '#203b3c')
    if name == 'meme-support-brick':
        text(draw, 'EMOTIONAL SUPPORT', 23, 20)
        text(draw, 'NOW AVAILABLE', 47, 14, MINT)
        arrival = phase(t, .7, 1.5)
        bounce = 0 if t < 1.5 else -8*math.exp(-(t-1.5)*7)*abs(math.sin((t-1.5)*19))
        y = -70+210*arrival+bounce
        if t > 1.42:
            sparks(draw, t, cy=133, radius=94)
            art.ellipse(draw, (70, 171, 210, 187), '#34504b')
        art.stamp(image, prop('brick', 162, 105), 140, y)
        # Keep the falling brick inside its stage, clear of the title.
        draw.rectangle(art.xy((0, 0, 280, 63)), fill=BG)
        text(draw, 'EMOTIONAL SUPPORT', 23, 20)
        text(draw, 'NOW AVAILABLE', 47, 14, MINT)
        if t >= 2.1:
            pill(draw, 'IT GETS ME.', 213, 24)
        else:
            text(draw, 'please remain hopeful.', 215, 16, MINT)
    elif name == 'meme-optimism-shredder':
        text(draw, 'OPTIMISM.EXE', 23, 25)
        feed = phase(t, 1.05, 2.25)
        # The fixed machine occludes the paper as it feeds through the top slot.
        if feed < 1:
            paper(draw, 140, 68+72*feed, 66, 58)
            art.heart(draw, 140, 55+72*feed, 7, CORAL)
        shake = math.sin(t*65)*1.0 if 1.05 < t < 2.25 else 0
        art.stamp(image, prop('shredder', 134, 114), 140+shake, 140)
        if 1.3 < t < 2.5:
            for n in range(7):
                u=(t*1.7+n*.14)%1
                art.line(draw, [(98+n*14, 182+u*13), (100+n*14, 188+u*13)], CORAL, 2)
        if t >= 2.35:
            pill(draw, 'BACK TO REALITY.', 217, 21)
        else:
            text(draw, 'new day. new hope.', 218, 19, MINT)
    elif name == 'meme-last-beep':
        text(draw, 'PER MY LAST BEEP', 24, 21)
        expansion = phase(t, 1.05, 1.9)
        # A coherent receipt unfolds out of a little robot's deadpan mail slot.
        for n in range(4):
            y=112-n*16*expansion
            art.rounded(draw, (73+n*2, y, 207-n*2, y+56), 4, '#d4d4b9' if n%2 else CREAM)
        art.rounded(draw, (66, 132, 214, 184), 13, MINT, INK, 2)
        art.line(draw, [(81, 144), (199, 144)], INK, 5)
        for x in (97, 179):
            art.line(draw, [(x-8, 160), (x+8, 160)], INK, 4)
        if t > 1.5:
            label(draw, 'BEEP.', 140, 88, 34, INK)
        if t >= 2.2:
            # Stamp lands once, then remains pinned rather than vibrating.
            drop=8*(1-phase(t, 2.2, 2.35))
            art.rounded(draw, (68, 110-drop, 212, 137-drop), 3, CORAL, INK, 2)
            label(draw, 'AS PREVIOUSLY BEEPED', 140, 124-drop, 11, INK)
            pill(draw, 'KIND REGARDS.', 216, 24)
        else:
            text(draw, 'circling back aggressively.', 216, 16, MINT)
    elif name == 'meme-premium-empathy':
        text(draw, 'EMPATHY', 23, 27)
        text(draw, 'A PREMIUM EXPERIENCE', 48, 13, MINT)
        size=39+3*math.sin(t*5) if t < 1.8 else 39
        art.heart(draw, 140, 119, size, CORAL)
        if t < 1.8:
            sparks(draw, t, cy=120, radius=69, color=MINT)
        else:
            lock=phase(t, 1.8, 2.0)
            art.rounded(draw, (78, 73, 202, 175), 13, None, '#58706b', 2)
            art.rounded(draw, (120, 112-10*(1-lock), 160, 150), 18, None, GOLD, 7)
            art.rounded(draw, (112, 133, 168, 170), 8, GOLD, INK, 2)
            art.ellipse(draw, (136, 142, 144, 150), INK)
            art.line(draw, [(140, 147), (140, 158)], INK, 3)
        if t >= 2.3:
            pill(draw, 'FREE TRIAL ENDED.', 212, 20)
        else:
            text(draw, 'your feelings matter*', 213, 20, MINT)
    elif name == 'meme-overreaction':
        text(draw, 'MINOR INCONVENIENCE', 23, 18)
        if t < 1.5:
            art.head(image, 140, 127, 118, 'skeptical-look', 4)
            art.ellipse(draw, (199, 78, 208, 87), CORAL)
            label(draw, '!', 203.5, 82.5, 9, INK)
        else:
            boom=phase(t, 1.5, 1.68)
            shake=2.5*math.exp(-(t-1.5)*2.8)*math.sin(t*59)
            for n in range(18):
                a=n*math.tau/18
                art.line(draw, [(140+math.cos(a)*89, 123+math.sin(a)*73),
                                (140+math.cos(a)*125, 123+math.sin(a)*82)], '#4d6262', 2)
            art.head(image, 140+shake, 125, 118+88*boom, 'amazed-gasp', 4)
        if t >= 2.55:
            pill(draw, 'REASONABLE.', 211, 26)
        elif t >= 1.5:
            pill(draw, 'TOTALLY NORMAL.', 211, 22, fill=MINT)
        else:
            text(draw, 'handling it normally.', 214, 18, MINT)
    elif name == 'meme-family-audacity':
        text(draw, 'AUDACITY DETECTED', 23, 21)
        growth=phase(t, 1.5, 2.15)
        # The same illustration and label share the same continuous transform.
        width=62+65*growth
        height=93+70*growth
        center_y=146-18*growth
        tilt=0 if t < 1.5 else 4*math.exp(-(t-1.5)*3)*math.sin((t-1.5)*23)
        can=prop('can', round(width), round(height)).copy()
        # Type is drawn on the prop, so it cannot slide independently of the can.
        cd=ImageDraw.Draw(can)
        f=ImageFont.truetype(str(art.SOURCE/'fonts/Lato-Bold.ttf'), max(12, round(can.width*.135)))
        cd.text((can.width/2, can.height*.50), 'AUDACITY', font=f, fill=INK, anchor='mm')
        art.stamp(image, can, 140, center_y, tilt)
        if .5 < t < 1.45:
            y=90+70*((t-.5)/.95)
            art.line(draw, [(87, y), (193, y)], MINT, 1)
        if t > 1.5:
            sparks(draw, t, cy=110, radius=98, color=CORAL)
        if t >= 2.3:
            pill(draw, 'FAMILY SIZE.', 216, 26)
        else:
            text(draw, 'that is quite a lot.', 215, 19, MINT)
    elif name == 'meme-problem-compost':
        text(draw, 'PROCESSING YOUR ISSUE', 23, 18)
        if t < 1.25:
            y=67+67*phase(t, .7, 1.25)
            size=20-7*phase(t, .7, 1.25)
            # Irregular paper silhouette is fixed; only its position/scale moves.
            points=[(140+math.cos(n*math.tau/9)*size*(1 if n%2 else .8),
                     y+math.sin(n*math.tau/9)*size) for n in range(9)]
            draw.polygon([art.xy(p) for p in points], fill=CORAL)
            label(draw, '!', 140, y, 23, INK)
        sprout=phase(t, 2.2, 2.85)
        if sprout:
            tip=132-65*sprout
            art.line(draw, [(140, 145), (140, tip)], MINT, 5)
            art.ellipse(draw, (140, tip+9, 140+30*sprout, tip+9+16*sprout), MINT)
            art.ellipse(draw, (140-28*sprout, tip+24, 140, tip+24+15*sprout), '#72b897')
        shake=1.6*math.sin(t*24) if 1.5 < t < 2.2 else 0
        art.stamp(image, prop('bin', 133, 104), 140+shake, 149)
        if t >= 2.5:
            pill(draw, 'PERSONAL GROWTH.', 207, 21)
            text(draw, 'unfortunately.', 234, 12, CORAL)
        else:
            text(draw, 'we take this very seriously.', 215, 15, MINT)
    else:
        raise ValueError(name)
    return image.convert('RGB')


def render(name, t):
    neutral=art.face().convert('RGB').resize((280*S, 240*S), Image.Resampling.LANCZOS)
    if t < .1 or t >= 4.3:
        return neutral
    frame=scene(name, t)
    fade=min(phase(t, .1, .3), 1-phase(t, 4.1, 4.3))
    return Image.blend(neutral, frame, fade) if fade < 1 else frame


def env(n, attack=.008, release=.04):
    result=np.ones(n)
    a=min(n//2, round(attack*RATE));r=min(n//2, round(release*RATE))
    if a:result[:a]=np.sin(np.linspace(0, math.pi/2, a))**2
    if r:result[-r:]=np.cos(np.linspace(0, math.pi/2, r))**2
    return result


def tone(duration, frequencies, harmonics=(1, .13, .035), vibrato=0, decay=0):
    n=round(duration*RATE);t=np.arange(n)/RATE
    f=np.interp(t, np.linspace(0, duration, len(frequencies)), frequencies)
    f*=1+vibrato*np.sin(math.tau*6*t)
    p=math.tau*np.cumsum(f)/RATE
    value=sum(a*np.sin((i+1)*p) for i,a in enumerate(harmonics))
    return value*env(n)*np.exp(-decay*t)


def noise(duration, seed, low=350, high=4000):
    n=round(duration*RATE)
    value=np.random.default_rng(seed).normal(0, 1, n)
    value=signal.sosfilt(signal.butter(2, [low, high], btype='bandpass', fs=RATE, output='sos'), value)
    return value*env(n, .008, .045)*.4


def pcm(x):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(RATE)
        w.writeframes(np.round(np.clip(x,-1,1)*32767).astype('<i2').tobytes())
    return buffer.getvalue()


def soundtrack(name):
    x=np.zeros(round(DURATION*RATE))
    seed=int(hashlib.sha256(name.encode()).hexdigest()[:8],16)
    def add(t, sound, gain=1):
        start=round(t*RATE);n=min(len(sound),len(x)-start)
        x[start:start+n]+=gain*sound[:n]
    def note(t, hz, duration=.18, gain=.2):
        add(t,tone(duration,[hz,hz],decay=3),gain)
    def sparkle(t, descending=False):
        notes=[659.25,830.61,987.77,1318.51]
        for i,hz in enumerate(notes[::-1] if descending else notes):
            add(t+i*.09,tone(.36,[hz,hz],harmonics=(1,.08),decay=7),.17)
    def stamp(t):
        add(t,tone(.15,[490,240],harmonics=(1,.18,.07),decay=16),.35)
        add(t,noise(.10,seed+1,800,4500),.3)
    if name=='meme-support-brick':
        sparkle(.45)
        add(1.5,tone(.34,[300,170],harmonics=(1,.4,.15),decay=11),.52)
        add(1.5,noise(.13,seed,200,2400),.38)
        for i,hz in enumerate((700,920,1110)):
            add(1.53,tone(.24,[hz,hz],harmonics=(1,),decay=18),.055)
        add(2.25,tone(.31,[610,810,540],harmonics=(1,.26,.05),vibrato=.02),.20)
    elif name=='meme-optimism-shredder':
        sparkle(.45)
        n=round(1.17*RATE);t=np.arange(n)/RATE
        motor=tone(1.17,[230,260,220],harmonics=(1,.4,.15),vibrato=.012)
        add(1.05,motor*(.6+.4*np.sin(math.tau*23*t)**2),.16)
        add(1.05,noise(1.17,seed,500,3600)*(.4+.6*np.sin(math.tau*31*t)**2),.19)
        add(2.35,tone(.68,[650,430,280],harmonics=(1,.23,.06),vibrato=.012),.23)
        note(3.2,280,.14,.14)
    elif name=='meme-last-beep':
        note(.5,1046.5,.25,.24);note(.62,1318.5,.20,.12)
        for i in range(11):
            add(1.05+i*.077,noise(.035,seed+i,900,4000),.17)
            add(1.05+i*.077,tone(.04,[480,410],harmonics=(1,.1)),.10)
        stamp(2.2)
        note(2.55,530,.21,.24);note(2.96,530,.30,.24)
    elif name=='meme-premium-empathy':
        for i,hz in enumerate((523.25,659.25,783.99)):
            add(.45+i*.13,tone(.65,[hz,hz],harmonics=(1,.1),decay=5),.15)
        stamp(1.8)
        add(1.83,tone(.2,[1200,730],harmonics=(1,.18),decay=15),.13)
        add(2.35,tone(.22,[310,310],harmonics=(1,.35,.14)),.21)
        add(2.7,tone(.36,[310,230],harmonics=(1,.35,.14)),.21)
    elif name=='meme-overreaction':
        note(.7,1380,.09,.11)
        # Original short dissonant horn gesture; not a sampled meme sting.
        add(1.5,tone(.78,[293,306,260],harmonics=(1,.48,.24,.1),vibrato=.007),.25)
        add(1.5,tone(.78,[415,430,370],harmonics=(1,.32,.12),vibrato=.008),.16)
        add(1.5,noise(.17,seed,400,4200),.22)
        stamp(2.55)
        add(2.79,tone(.53,[820,590,260],harmonics=(1,.18,.06),vibrato=.025),.18)
    elif name=='meme-family-audacity':
        for t,hz in ((.5,800),(.73,1000),(.96,1250)):note(t,hz,.09,.15)
        stamp(1.5);add(1.53,noise(.27,seed,1600,4800),.16)
        add(1.75,tone(.55,[230,1050,540,770,500],harmonics=(1,.25,.08)),.25)
        add(2.5,tone(.34,[360,340,360],harmonics=(1,.5,.18,.08)),.21)
        add(2.92,tone(.48,[360,390,250],harmonics=(1,.5,.18,.08)),.21)
    elif name=='meme-problem-compost':
        for i in range(4):add(.45+i*.06,noise(.09,seed+i,700,3700),.16)
        add(1.2,tone(.24,[520,210],harmonics=(1,.2,.07),decay=6),.3)
        for i,hz in enumerate((260,340,280,410,300)):
            add(1.65+i*.10,tone(.13,[hz,hz*1.7,hz*.8],harmonics=(1,.22)),.16)
        sparkle(2.5)
        add(3.05,tone(.56,[720,680,420],harmonics=(1,.16,.05),vibrato=.018),.18)
    else:raise ValueError(name)
    x=signal.sosfilt(signal.butter(3,[165,4800],btype='bandpass',fs=RATE,output='sos'),x)
    x*=env(len(x),.02,.12)
    measured,peak=loudness(pcm(x))
    gain=min(-23-measured,-8.1-peak)
    x*=10**(gain/20)
    data=pcm(x)
    report=metrics(data)
    assert report['clipped_samples']==0 and report['true_peak_dbfs']<=-8
    assert report['edge_peak']<.0001 and abs(report['dc_offset'])<.0001
    assert -31<report['lufs']<=-22.5
    return data,report


def encode(frames, size, audio, output):
    subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pixel_format','rgb24',
        '-video_size',f'{size[0]}x{size[1]}','-framerate',str(FPS),'-i','pipe:0','-i',str(audio),
        '-map','0:v:0','-map','1:a:0','-c:v','libx264','-preset','medium','-crf','17',
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','96k','-movflags','+faststart',
        '-t',str(DURATION),str(output)],input=b''.join(f.tobytes() for f in frames),check=True)


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',choices=list(CLIPS),help='Rebuild one clip in an existing collection')
    args=parser.parse_args()
    for folder in (OUT,OUT/'gifs',OUT/'share',ROOT/'assets'):
        folder.mkdir(parents=True,exist_ok=True)
    records=[];audio_reports=[]
    existing={}
    if args.only:
        existing={r['id']:r for r in json.loads((catalog_group(ROOT, 'meme_lab')).read_text(encoding='utf-8'))['clips']}
    overview=Image.new('RGB',(1200,590),BG);od=ImageDraw.Draw(overview)
    for index,(name,detail) in enumerate(CLIPS.items()):
        if args.only and name!=args.only:
            records.append(existing[name])
            audio_reports.append({'id':name,**existing[name]['audio_metrics']})
            continue
        wav=ROOT/'assets/sounds'/(name+'.wav')
        audio,report=soundtrack(name);wav.write_bytes(audio)
        sidecar=ROOT/'assets/videos'/(name+'_audio.wav');sidecar.write_bytes(audio)
        audio_reports.append({'id':name,**report})
        masters=[render(name,i/FPS) for i in range(round(DURATION*FPS))]
        frames=[f.resize((280,240),Image.Resampling.LANCZOS) for f in masters]
        neutral=art.face().convert('RGB')
        # Native endpoints are exact, bypassing up/down-sampling of approved art.
        for i in (0,1,86,87,88,89):frames[i]=neutral.copy()
        video=ROOT/'assets/videos'/(name+'.mp4')
        encode(frames,(280,240),wav,video)
        share=OUT/'share'/(name+'.mp4');encode(masters,(840,720),wav,share)
        keys=[neutral.copy() if t<.1 or t>=4.3 else render(name,t).resize((280,240),Image.Resampling.LANCZOS) for t in STARTS]
        atlas=Image.new('RGB',(1120,1200))
        for n,f in enumerate(keys):atlas.paste(f,((n%4)*280,(n//4)*240))
        sheet=ROOT/'assets/face_animations'/(name+'.png');atlas.save(sheet)
        assert sheet.stat().st_size<=4*1024*1024
        spec=dict(sheet=sheet.name,columns=4,rows=5,order=list(range(20)),durations_ms=TIMING,blend_ms=0)
        manifest=sheet.with_suffix('.json');manifest.write_text(json.dumps(spec)+'\n')
        palette=atlas.quantize(colors=256,method=Image.Quantize.MEDIANCUT)
        gif_frames=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames[::2]]
        gif=OUT/'gifs'/(name+'.gif')
        gif_frames[0].save(gif,save_all=True,append_images=gif_frames[1:],duration=100,loop=0,optimize=False,disposal=2)
        poster=frames[61];poster.save(OUT/(name+'-poster.png'))
        cues=[dict(time_s=t,label=label,video_frame=round(t*FPS)) for t,label in detail['cues']]
        record={**{k:v for k,v in detail.items() if k!='cues'},'id':name,'duration_s':DURATION,'fps':FPS,
            'frames':len(frames),'native_size':[280,240],'share_size':[840,720],'sprite_frames':20,'cues':cues,
            'video':video.relative_to(ROOT).as_posix(),'audio':wav.relative_to(ROOT).as_posix(),
            'sidecar':sidecar.relative_to(ROOT).as_posix(),'sheet':sheet.relative_to(ROOT).as_posix(),
            'manifest':manifest.relative_to(ROOT).as_posix(),'gif':gif.relative_to(OUT).as_posix(),
            'share':share.relative_to(OUT).as_posix(),'poster':name+'-poster.png',
            'sound_default':True,'audio_sha256':sha(wav),'video_sha256':sha(video),
            'sheet_sha256':sha(sheet),'audio_metrics':report}
        records.append(record)
        print(name, f'{len(frames)} frames; {report["lufs"]} LUFS; peak {report["true_peak_dbfs"]} dBFS',flush=True)
    for index,record in enumerate(records):
        col,row=index%4,index//4
        with Image.open(OUT/record['poster']) as poster:overview.paste(poster,(col*300+10,row*295+8))
        od.text((col*300+10,row*295+259),record['title'],font=ImageFont.truetype(str(art.SOURCE/'fonts/Lato-Bold.ttf'),15),fill=CREAM)
    od.text((940,350),'ROBBIE',font=ImageFont.truetype(str(art.SOURCE/'fonts/Lato-Bold.ttf'),32),fill=CORAL)
    od.text((940,400),'AFTER HOURS',font=ImageFont.truetype(str(art.SOURCE/'fonts/Lato-Bold.ttf'),23),fill=CREAM)
    od.text((940,442),'7 original audiovisual jokes',font=ImageFont.truetype(str(art.SOURCE/'fonts/Lato-Regular.ttf'),16),fill=MINT)
    overview.save(OUT/'overview.png')
    catalog={'version':1,'title':'Robbi after hours','duration_s':DURATION,'clips':records,
        'sound_provenance':'Original deterministic synthesis. No voices, music recordings or third-party samples.'}
    (catalog_group(ROOT, 'meme_lab')).write_text(json.dumps(catalog,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (OUT/'audio-checks.json').write_text(json.dumps(audio_reports,indent=2)+'\n')


if __name__=='__main__':main()
