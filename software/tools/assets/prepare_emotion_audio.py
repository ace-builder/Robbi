"""Build Robbi's CC0, nonverbal emotion audition library (offline after fetch).

The authored synthesis recipes and resulting new audio are dedicated to CC0-1.0.
Kenney accents retain their original CC0-1.0 provenance. No credentials/API calls.
Requires the existing development numpy/scipy/Pillow dependencies and FFmpeg.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import wave
import zipfile

import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "assets/dynamics_sources/emotion-audio-v1"
LIBRARY = ROOT / "assets/emotion_audio"
RATE = 22050
LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"

# A is a rhythmic motif, B a continuous elastic gesture, C a resonant hum with
# a separately sourced CC0 electronic accent. Semitones are relative to base.
# Each gesture is authored, not a random pitch-shift of another variant.
MOODS = [
 # id, face, family, base MIDI, A notes, B pitch contour, C chord, CC0 accent,
 # three user-facing names, emotional direction
 ("happy", "happy-blink", "bright", 74, [0,4,7,12,7], [0,8,5,12,9], [0,4,7], "digital:highUp", ["Pocket sunshine", "Bouncy squeal", "Sunny little hum"], "Light upward motion with a friendly, resolved landing."),
 ("excited", "excited-grin", "bright", 77, [0,7,4,7,12,16,12], [0,12,4,16,7,19], [0,7,12], "digital:powerUp2", ["Happy beeping", "Sparkle trill", "Excited motor hum"], "Quick rising bursts and a little flutter of anticipation."),
 ("affectionate", "affectionate-smile", "gentle", 67, [0,7,4,0], [0,3,5,3,0], [0,4,9], "interface:glass_002", ["Heart bloops", "Nuzzling squeak", "Warm electric purr"], "Soft rounded sounds that approach and settle gently."),
 ("proud", "proud-smile", "bright", 69, [0,4,7,12], [0,4,7,7,5], [0,4,7], "interface:confirmation_002", ["Tiny achievement", "Satisfied chirrup", "Polished little chime"], "A measured upward flourish with a confident finish."),
 ("shy", "shy-smile", "gentle", 73, [0,3,0,-2], [-4,1,4,0,-3], [0,7,14], "interface:pluck_001", ["Bashful peeps", "Peeking squeak", "Hidden little shimmer"], "A tentative rise, a pause, and a soft retreat."),
 ("amazed", "amazed-gasp", "bright", 72, [0,12,7,16], [-7,5,17,12,14], [0,7,14], "digital:phaseJump1", ["Wide-eyed bloops", "Wonder sweep", "Discovery shimmer"], "An expanding rise that hangs briefly in wonder."),
 ("curious", "curious-glance", "inquisitive", 72, [0,5,2,9], [0,6,2,10], [0,5,14], "interface:question_001", ["Question beeps", "Little antenna", "Investigating hum"], "Short exploratory sounds with a rising question at the end."),
 ("thoughtful", "thoughtful-look", "inquisitive", 65, [0,4,2,5,4], [0,2,-1,3,2], [0,7,9], "digital:lowThreeTone", ["Thinking dots", "Pondering wobble", "Quiet processor"], "Spaced, unhurried tones with a small unresolved turn."),
 ("skeptical", "skeptical-look", "inquisitive", 67, [0,1,-2], [0,4,1,-3,-2], [0,7,10], "interface:question_003", ["Really bloops", "Side-eye squeak", "Doubtful resonance"], "A short raised question followed by a dry downward answer."),
 ("confused", "confused-look", "inquisitive", 70, [0,6,2,5,-1], [0,7,-3,6,1], [0,5,11], "digital:lowRandom", ["Mixed-up beeps", "Puzzled spring", "Wonky little circuit"], "Small direction changes that leave the phrase unresolved."),
 ("disappointed", "sad-sigh", "low", 68, [7,4,0,-5], [5,2,-3,-7,-9], [0,3,7], "digital:highDown", ["Deflated bloops", "Drooping squeak", "Fading hope hum"], "A clear downward letdown, short and gentle."),
 ("sad", "sad-sigh", "low", 62, [3,0,-2,-5], [3,1,-2,-5,-7], [0,3,7], "digital:lowDown", ["Small blue beeps", "Sad little squeak", "Rainy circuit hum"], "Slower falling notes with a fragile tremble and soft tail."),
 ("frustrated", "frustrated-frown", "tense", 65, [0,0,1,0,-3], [0,3,-1,4,-4], [0,5,7], "interface:glitch_001", ["Grumbly stutter", "Scrunched squeak", "Flustered motor"], "Compact repeated protests with restrained electronic roughness."),
 ("worried", "pickup-startle", "tense", 70, [0,1,0,3,1], [0,2,-1,4,1], [0,7,13], "interface:question_002", ["Nervous peeps", "Wavering query", "Uneasy flutter"], "A hesitant repetition that never becomes an alarm."),
 ("startled", "pickup-startle", "tense", 75, [0,12,5], [-5,15,3,-2], [0,7,12], "digital:pepSound2", ["Oh bloop", "Tiny startle zip", "Springy surprise"], "A quick upward hop followed by a calmer settling tail."),
 ("relieved", "soft-relief", "gentle", 67, [7,4,2,0], [7,4,1,0,0], [0,4,7], "interface:confirmation_001", ["All-clear bloops", "Settling squeak", "Unwinding hum"], "Tension gently falls away into a stable, warm note."),
 ("content", "sleepy-breath", "gentle", 64, [0,4,7,4,0], [0,2,3,2,0], [0,4,7], "interface:bong_001", ["Cozy pebbles", "Mellow warble", "Contented purr"], "A slow, rounded arc with no urgency."),
 ("sleepy", "sleepy-yawn", "low", 61, [4,2,0,-3], [0,5,3,-3,-5], [0,7,12], "interface:minimize_002", ["Drowsy dots", "Sleepy glide", "Dozing circuit"], "A lazy lift that drifts downward and fades out."),
 ("playful", "cheeky-wink", "bright", 76, [0,7,3,10,5,12], [0,10,2,12,4,7], [0,4,9], "digital:pepSound4", ["Skipping beeps", "Rubber ducklet", "Bubble machine"], "A light bouncing rhythm with a mischievous final flick."),
 ("mischievous", "mischievous-grin", "bright", 71, [0,3,7,6,9], [0,5,3,9,6], [0,3,9], "interface:select_003", ["Sneaky blips", "Cheeky slide", "Secret circuit"], "A conspiratorial pause and a quick upward wink."),
 ("bored", "sleepy-yawn", "low", 63, [0,0,-2,0,-4], [0,2,0,-2,-5], [0,7,10], "interface:minimize_004", ["Idle dots", "Droopy spring", "Waiting motor"], "Spaced repetitions sink slowly into an unhurried tail."),
 ("determined", "proud-smile", "bright", 66, [0,0,5,7,12], [-2,0,5,7,12], [0,7,12], "interface:confirmation_004", ["Can-do pulses", "Ready-set zip", "Steady power hum"], "Firm, even pulses build toward a positive finish."),
 ("greeting", "happy-blink", "social", 73, [0,4,7,4,9], [0,7,4,9,7], [0,4,7], "interface:open_002", ["Hello bloops", "Friendly chirrup", "Welcome shimmer"], "A compact cheerful call that leaves room for a reply."),
 ("farewell", "soft-relief", "social", 70, [7,4,0,4,0], [7,3,5,0,-2], [0,4,7], "interface:close_002", ["Little wave", "Goodnight squeak", "See-you chime"], "A warm downward wave with an easy, resolved ending."),
]


def db(value):
    return float(20 * np.log10(max(float(value), 1e-12)))


def hz(midi):
    return 440 * 2 ** ((np.asarray(midi) - 69) / 12)


def envelope(count, attack=.025, release=.075):
    env = np.ones(count)
    a, r = min(round(attack*RATE), count//2), min(round(release*RATE), count//2)
    if a: env[:a] = np.sin(np.linspace(0, math.pi/2, a))**2
    if r: env[-r:] = np.sin(np.linspace(math.pi/2, 0, r))**2
    return env


def voice(frequencies, timbre, *, flutter=0, vibrato=0):
    frequencies = np.asarray(frequencies, dtype=float)
    t = np.arange(len(frequencies))/RATE
    frequencies = frequencies * 2**(vibrato*np.sin(2*math.pi*5.2*t)/12)
    phase = 2*math.pi*np.cumsum(frequencies)/RATE
    if timbre == 'beep':
        x = np.sin(phase) + .13*np.sin(2*phase) + .035*np.sin(3*phase)
    elif timbre == 'squeak':
        depth = .32 + .26*np.sin(math.pi*np.linspace(0,1,len(t)))**2
        x = np.sin(phase + depth*np.sin(2*phase)) + .07*np.sin(3*phase)
    else:
        x = np.sin(phase) + .18*np.sin(2*phase+.13*np.sin(2*math.pi*2*t)) + .09*np.sin(3*phase)
    if flutter: x *= .82 + .18*np.sin(2*math.pi*flutter*t)
    return x


def smooth_curve(values, count):
    grid = np.linspace(0, len(values)-1, count)
    index = np.minimum(grid.astype(int),len(values)-2)
    u = grid-index
    u = u*u*(3-2*u)
    return np.array(values)[index]*(1-u)+np.array(values)[index+1]*u


def beep_phrase(mood):
    name, _, family, base, notes, *_ = mood
    length = {'bright':.14,'gentle':.23,'inquisitive':.19,'low':.24,'tense':.14,'social':.16}[family]
    gap = {'bright':.055,'gentle':.095,'inquisitive':.12,'low':.13,'tense':.06,'social':.065}[family]
    chunks=[]
    for i,note in enumerate(notes):
        duration=length*(1.65 if i==len(notes)-1 else 1)
        if name=='excited': duration*=.78
        if name in ('shy','mischievous','thoughtful') and i==1:
            chunks.append(np.zeros(round(.11*RATE)))
        count=round(duration*RATE)
        pitch=hz(base+note + .5*np.exp(-np.linspace(0,6,count)))
        x=voice(pitch,'beep',vibrato=.04 if family!='low' else .12)
        x*=envelope(count,.012,.055)
        x*=1-.18*i/max(1,len(notes)-1) if family in ('low','gentle') else .8+.2*i/len(notes)
        chunks.extend((x,np.zeros(round(gap*RATE))))
    return np.concatenate(chunks)


def squeak_phrase(mood):
    name, _, family, base, _, curve, *_ = mood
    duration={'bright':1.06,'gentle':1.45,'inquisitive':1.18,'low':1.7,'tense':1.02,'social':1.13}[family]
    if name=='startled': duration=.74
    count=round(duration*RATE)
    pitch=hz(base+3+smooth_curve(curve,count))
    x=voice(pitch,'squeak',flutter=12 if name in ('excited','worried','frustrated') else 0,
            vibrato=.28 if family in ('low','tense') else .12)
    # A shaped body, not a dry sustained whistle. All changes are continuous.
    t=np.linspace(0,1,count)
    body=(.78+.22*np.sin(math.pi*t))*(1-.28*t if family=='low' else 1)
    return x*body*envelope(count,.045,.2 if family in ('low','gentle') else .1)


_SOURCE_CACHE = {}


def source_clip(reference):
    if reference in _SOURCE_CACHE: return _SOURCE_CACHE[reference].copy()
    pack,name=reference.split(':')
    archive='kenney-'+('digital-audio' if pack=='digital' else 'interface-sounds')+'.zip'
    with zipfile.ZipFile(SOURCES/archive) as z:
        data=z.read('Audio/'+name+'.ogg')
    decoded=subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-ac','1','-ar',str(RATE),
                            '-f','f32le','pipe:1'],input=data,capture_output=True,check=True).stdout
    x=np.frombuffer(decoded,dtype='<f4').astype(float)
    live=np.flatnonzero(abs(x)>max(abs(x).max()*.015,1e-5))
    if not len(live): raise ValueError('Silent source: '+reference)
    x=x[max(0,live[0]-round(.012*RATE)):min(len(x),live[-1]+round(.06*RATE))]
    # Retain a small accent, not a loud laser or interface error blast.
    x=signal.sosfilt(signal.butter(3,[250,2600],fs=RATE,btype='bandpass',output='sos'),x)
    x=x/max(abs(x).max(),1e-9)*envelope(len(x),.02,.06)
    _SOURCE_CACHE[reference]=x
    return x.copy()


def hum_phrase(mood):
    name,_,family,base,_,curve,chord,reference,*_=mood
    duration={'bright':1.55,'gentle':1.9,'inquisitive':1.65,'low':2.1,'tense':1.38,'social':1.55}[family]
    count=round(duration*RATE)
    contour=smooth_curve(curve,count)*.3
    # Keep the fundamental audible on a small enclosure speaker. Low emotions
    # derive their character from slow falling contours, not inaudible bass.
    root=max(57,base-12)
    x=np.zeros(count)
    for interval,gain in zip(chord,[1,.25,.16]):
        x+=gain*voice(hz(root+interval+contour),'hum',flutter=8 if name=='excited' else 2.3,
                      vibrato=.1 if family!='tense' else .2)
    x*=envelope(count,.09,.36)
    accent=source_clip(reference)
    # Fit the accent to the first half of the gesture with a gentle pitch change.
    speed={'bright':1.15,'gentle':.85,'inquisitive':1.,'low':.7,'tense':.9,'social':1.05}[family]
    accent=signal.resample_poly(accent,100,round(speed*100))
    limit=min(len(accent),round(.7*duration*RATE))
    accent=accent[:limit]*envelope(limit,.025,.08)
    offset=round(.065*RATE)
    x[offset:offset+limit]+=.29*accent
    if name in ('playful','mischievous','excited'):
        # A delayed quiet echo adds a playful answer without a reverb wash.
        late=round(.6*RATE)
        n=min(limit,len(x)-late)
        x[late:late+n]+=.12*accent[:n]*envelope(n,.02,.1)
    return x


def pcm_bytes(x):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(RATE)
        output.writeframes(np.round(np.clip(x,-1,1)*32767).astype('<i2').tobytes())
    return buffer.getvalue()


def loudness(data):
    result=subprocess.run(['ffmpeg','-hide_banner','-nostats','-i','pipe:0','-af','ebur128=peak=true',
                           '-f','null','-'],input=data,capture_output=True,check=True)
    summary=result.stderr.decode(errors='replace').rsplit('Summary:',1)[-1]
    integrated=re.search(r'I:\s+(-?[\d.]+) LUFS',summary)
    peak=re.search(r'Peak:\s+(-?[\d.]+) dBFS',summary)
    if not integrated or not peak: raise ValueError('FFmpeg loudness measurement missing')
    return float(integrated.group(1)),float(peak.group(1))


def master(x, family):
    x=signal.sosfilt(signal.butter(3,[180,4600],fs=RATE,btype='bandpass',output='sos'),x)
    x-=x.mean()
    x*=envelope(len(x),.012,.06)
    x=np.pad(x,(round(.025*RATE),round(.055*RATE)))
    x*=.3/max(abs(x).max(),1e-9)
    target={'bright':-23.,'gentle':-25.,'inquisitive':-24.,'low':-25.5,'tense':-24.,'social':-23.5}[family]
    measured,peak=loudness(pcm_bytes(x))
    gain=min(target-measured,-9.1-peak)
    x*=10**(gain/20)
    return pcm_bytes(x),target


def metrics(data):
    with wave.open(io.BytesIO(data),'rb') as w:
        assert (w.getnchannels(),w.getsampwidth(),w.getframerate())==(1,2,RATE)
        x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float)/32768
    level,true_peak=loudness(data)
    power=np.abs(np.fft.rfft(x))**2
    freq=np.fft.rfftfreq(len(x),1/RATE)
    live=np.flatnonzero(abs(x)>.001)
    edge=round(.015*RATE)
    blocks=[np.sqrt(np.mean(b*b)) for b in np.array_split(x,max(1,len(x)//220))]
    return dict(duration_s=round(len(x)/RATE,4),peak_dbfs=round(db(abs(x).max()),2),
                true_peak_dbfs=true_peak,lufs=level,rms_dbfs=round(db(np.sqrt(np.mean(x*x))),2),
                dc_offset=round(float(x.mean()),8),clipped_samples=int(np.sum(abs(x)>=.999)),
                leading_silence_ms=round(live[0]/RATE*1000,1),
                trailing_silence_ms=round((len(x)-1-live[-1])/RATE*1000,1),
                edge_peak=round(float(max(abs(x[:edge]).max(),abs(x[-edge:]).max())),8),
                sub_150hz_energy=round(float(power[freq<150].sum()/power.sum()),7),
                above_6khz_energy=round(float(power[freq>6000].sum()/power.sum()),7),
                waveform=[round(float(v)/max(blocks),3) for v in blocks])


def build():
    LIBRARY.mkdir(parents=True,exist_ok=True)
    sounds=ROOT/'assets/sounds'
    sounds.mkdir(parents=True,exist_ok=True)
    manifest={'version':1,'title':'Robbi emotion sounds','license':'CC0-1.0','license_url':LICENSE_URL,
              'sample_rate':RATE,'channels':1,'bits':16,'selected_variants':{},'emotions':[]}
    reports=[]
    for mood in MOODS:
        name,face,family,*_=mood
        entry={'id':name,'label':name.capitalize(),'face':face,'family':family,'direction':mood[-1],'variants':[]}
        for index,render in enumerate((beep_phrase,squeak_phrase,hum_phrase)):
            variant='abc'[index]
            identifier=f'emo_{name}_{variant}'
            data,target=master(render(mood),family)
            path=sounds/(identifier+'.wav')
            path.write_bytes(data)
            measured=metrics(data)
            sources=[]
            if index==2:
                pack,clip=mood[7].split(':')
                sources=[{'pack':'kenney-'+('digital-audio' if pack=='digital' else 'interface-sounds'),
                          'member':'Audio/'+clip+'.ogg','processing':'Trimmed, band-limited, resampled, faded and mixed quietly into an original synthesized hum.'}]
            record={'id':identifier,'variant':variant.upper(),'title':mood[8][index],
                    'character':['Rounded rhythmic beeps','Elastic pitch glides and squeaks','Warm layered hum with a CC0 electronic accent'][index],
                    'file':'assets/sounds/'+path.name,'license':'CC0-1.0','sources':sources,
                    'sha256':hashlib.sha256(data).hexdigest(),'target_lufs':target,**measured}
            entry['variants'].append(record)
            reports.append({'id':identifier,**measured})
            print(identifier,measured['duration_s'],'s',measured['lufs'],'LUFS',flush=True)
        manifest['emotions'].append(entry)
    (LIBRARY/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    (LIBRARY/'qa-metrics.json').write_text(json.dumps(reports,indent=2)+'\n',encoding='utf-8')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    build()
