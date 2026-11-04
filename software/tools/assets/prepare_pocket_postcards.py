"""Six object-led LCD postcards. No Robbi face in any frame or loop bookend."""
from __future__ import annotations

from asset_catalog import catalog_group, catalog_path, read_catalog
import argparse
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import signal

from prepare_meme_lab import art, phase, env, tone, noise, pcm, encode, sha
from prepare_emotion_audio import loudness, metrics

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/dynamics_sources/pocket-postcards-v1'
OUT=ROOT/'test-results/pocket-postcards-preview'
S=3
RATE=22050
FPS=20
DURATION=4.5
BG='#241e2d'
INK='#392536'
CREAM='#fff1d6'
GOLD='#ffc579'
CORAL='#ff9683'
SAGE='#c4d3a2'
LILAC='#d2b9e2'
MUTED='#b7a3ad'
STARTS=[0,.1,.35,.5,.75,1,1.2,1.4,1.55,1.75,1.95,2.2,2.45,2.7,3,3.4,3.85,4.1,4.25,4.4]
TIMING=[round((b-a)*1000) for a,b in zip(STARTS,STARTS[1:]+[DURATION])]
CLIPS={
 'post-sunshine':dict(title='Pocket sunshine',heading='POCKET SUNSHINE',heading_size=24,
    quote='FOR YOUR CLOUDY DAYS.',cover=['FOR YOUR','CLOUDY DAYS.'],
    when='A little jar of sunshine, saved for the days that need it.',
    sound='A glass ping, a tiny lid pop, and a warm rising sun-chime.',
    cues=[(.45,'glass ping'),(.7,'lid pops'),(1.2,'sun rises'),(1.85,'lid settles'),(2.3,'warm sparkle')]),
 'post-peace-tea':dict(title='A cup of sorry',heading='A PEACE OFFERING',heading_size=23,
    quote='SORRY ABOUT THE CHAOS.',cover=['SORRY ABOUT','THE CHAOS.'],
    when='One apology, served warm. The tiny white flag is included.',
    sound='A gentle ceramic clink, a flag flutter, and a warm tea-kettle coo.',
    cues=[(.4,'cup slides'),(1.05,'ceramic clink'),(1.5,'peace flag'),(2.35,'heart-shaped steam')]),
 'post-slow-club':dict(title='Slow progress club',heading='SLOW PROGRESS CLUB',heading_size=21,
    quote='STILL COUNTS.',cover=['STILL','COUNTS.'],
    when='A very determined snail celebrates a very small distance.',
    sound='Soft little travel squeaks and a miniature victory fanfare.',
    cues=[(.5,'first tiny step'),(1.3,'still going'),(2.65,'party hat moment')]),
 'post-heart-mail':dict(title='Priority heart mail',heading='PRIORITY DELIVERY',heading_size=23,
    quote='YOU MATTER. A LOT.',cover=['YOU MATTER.','A LOT.'],
    when='An envelope carrying something rather bigger than a letter.',
    sound='Paper flutter, a soft seal pop, and a heart-sized three-note sparkle.',
    cues=[(.5,'paper flutter'),(1.25,'seal opens'),(2.25,'heart delivered')]),
 'post-return-guilt':dict(title='Return to sender',heading='SEND IT BACK.',heading_size=27,
    quote='NOT YOURS TO CARRY.',cover=['NOT YOURS','TO CARRY.'],
    when='Unnecessary guilt has been declined by the receiving department.',
    sound='Paper shuffles, one satisfyingly soft stamp, and a relieved little flourish.',
    cues=[(.55,'incoming paperwork'),(1.5,'return stamp'),(2.35,'request declined')]),
 'post-soft-landing':dict(title='A softer landing',heading='SOFT LANDING',heading_size=26,
    quote="IT'S OKAY TO RESTART.",cover=["IT'S OKAY",'TO RESTART.'],
    when='A paper plane misses the plan and finds a pillow. A perfectly valid landing.',
    sound='A paper-flight whistle, a plush poof, a tiny bounce and a reassuring bell.',
    cues=[(.4,'paper flight'),(1.65,'pillow landing'),(2.65,'soft reset')]),
}


def label(draw,value,x,y,size=20,color=CREAM,bold=True):
    font=art.font(size,bold);box=draw.textbbox((0,0),value,font=font)
    if box[2]-box[0]>258*S:raise ValueError('Caption too wide: '+value)
    draw.text((round(x*S),round(y*S)),value,anchor='mm',font=font,fill=color)


def text(draw,value,y,size=22,color=CREAM):
    label(draw,value,140,y,size,color)


def pill(draw,value,y=215,size=21,fill=SAGE):
    box=draw.textbbox((0,0),value,font=art.font(size));w=(box[2]-box[0])/S+18
    while w>266 and size>18:
        size-=1;box=draw.textbbox((0,0),value,font=art.font(size));w=(box[2]-box[0])/S+18
    assert w<=266
    art.rounded(draw,(140-w/2,y-17,140+w/2,y+17),9,fill)
    text(draw,value,y,size,INK)


def stage(name):
    im=Image.new('RGBA',(280*S,240*S),BG);d=ImageDraw.Draw(im)
    detail=CLIPS[name]
    text(d,detail['heading'],23,detail['heading_size'])
    art.line(d,[(18,41),(262,41)],'#443344',1)
    return im


@lru_cache(None)
def cover(name):
    """A typography card is also the exact first and last frame of the loop."""
    im=stage(name);d=ImageDraw.Draw(im)
    art.star(d,140,79,8,GOLD)
    a,b=CLIPS[name]['cover']
    text(d,a,119,27,CREAM);text(d,b,152,29,CORAL)
    art.line(d,[(108,187),(172,187)],MUTED,1)
    return im.convert('RGB')


@lru_cache(None)
def prop(name,width,height):
    with Image.open(SOURCE/(name+'.png')) as original:
        original=original.convert('RGBA')
        box=original.getchannel('A').point(lambda a:255 if a>24 else 0).getbbox()
        if not box:raise ValueError('Empty prop '+name)
        sprite=original.crop(box)
        sprite.thumbnail((round(width*S),round(height*S)),Image.Resampling.LANCZOS)
        return sprite


def heart_float(d,t,start,x,y,size=6,color=CORAL):
    if t<start:return
    p=phase(t,start,start+.35)
    art.heart(d,x+2*math.sin((t-start)*2),y-min(17,(t-start)*10),size*p,color)


def sun(d,x,y,r,t):
    for n in range(10):
        a=math.tau*n/10+.06*math.sin(t*2)
        a1=r+5;a2=r+11
        art.line(d,[(x+math.cos(a)*a1,y+math.sin(a)*a1),
                    (x+math.cos(a)*a2,y+math.sin(a)*a2)],GOLD,2.5)
    art.ellipse(d,(x-r,y-r,x+r,y+r),GOLD)
    art.ellipse(d,(x-r*.55,y-r*.64,x-r*.1,y-r*.35),'#ffe8ae')


@lru_cache(None)
def return_stamp():
    im=Image.new('RGBA',(214*S,78*S));d=ImageDraw.Draw(im)
    art.rounded(d,(2,2,212,76),8,CREAM,CORAL,3)
    art.rounded(d,(7,7,207,71),5,None,CORAL,1)
    label(d,'RETURN TO',107,25,19,INK)
    label(d,'SENDER',107,53,31,'#b94748')
    return im


@lru_cache(None)
def plane():
    im=Image.new('RGBA',(78*S,40*S));d=ImageDraw.Draw(im)
    d.polygon([art.xy(p) for p in [(3,4),(75,17),(23,35),(29,21)]],fill=CREAM)
    d.polygon([art.xy(p) for p in [(3,4),(29,21),(75,17)]],fill='#f4bc9b')
    d.polygon([art.xy(p) for p in [(29,21),(23,35),(41,25)]],fill='#bb8b91')
    art.line(d,[(3,4),(75,17),(23,35),(29,21),(3,4)],INK,1.2)
    art.line(d,[(29,21),(75,17)],INK,1)
    return im


def scene(name,t):
    im=stage(name);d=ImageDraw.Draw(im)
    art.ellipse(d,(51,181,229,192),'#392c3c')
    if name=='post-sunshine':
        rise=phase(t,.8,1.85);lid=phase(t,.7,1.15)
        sun(d,140,146-64*rise,12+10*rise,t)
        art.stamp(im,prop('sunshine-jar',98,90),140,149)
        art.heart(d,140,157,9,CORAL)
        lid_y=-29*lid+111*phase(t,1.15,1.85)
        art.ellipse(d,(108+65*lid,99+lid_y,172+65*lid,108+lid_y),GOLD,INK,1.3)
        art.line(d,[(116+65*lid,102+lid_y),(164+65*lid,102+lid_y)],CREAM,1.1)
        if t>=2.3:
            for x,y,r in [(73,93,4),(213,128,3),(82,158,3)]:
                art.star(d,x,y,r*(.9+.1*math.sin(t*3+x)),GOLD)
            pill(d,CLIPS[name]['quote'],size=20)
        else:text(d,'saved a little light for you.',216,16,LILAC)
    elif name=='post-peace-tea':
        arrive=phase(t,.4,1.05);x=210-70*arrive
        flag=phase(t,1.5,2.05)
        if flag>0:
            fy=143-65*flag
            art.line(d,[(211,166),(211,fy)],CREAM,2)
            points=[(212,fy+3),(239,fy+5+2*math.sin(t*5)),(235,fy+22),(212,fy+19)]
            d.polygon([art.xy(p) for p in points],fill=CREAM)
            art.heart(d,224,fy+12,4,CORAL)
        art.stamp(im,prop('peace-tea',160,99),x,144)
        for n in range(2):
            points=[(x-26+n*24+math.sin(j*.6+t*2+n)*3,103-j*4) for j in range(7)]
            art.line(d,points,'#baa6ad',1.3)
        heart_float(d,t,2.35,129,86,10)
        if t>=2.35:pill(d,CLIPS[name]['quote'],size=19)
        else:text(d,'one apology. served warm.',216,16,LILAC)
    elif name=='post-slow-club':
        travel=phase(t,.5,2.65);x=76+74*travel
        for dx in range(29,252,12):art.line(d,[(dx,180),(dx+5,180)],'#8c7489',1)
        art.line(d,[(244,180),(244,126)],CREAM,2)
        for row in range(3):
            for col in range(4):
                d.rectangle(art.xy((244+col*5,126+row*5,249+col*5,131+row*5)),
                            fill=CREAM if (row+col)%2==0 else INK)
        bob=math.sin(t*5)*.7 if .5<t<2.65 else 0
        art.stamp(im,prop('slow-snail',139,90),x,138+bob)
        hat=phase(t,2.65,2.95)
        if hat>0:
            hy=93-11*hat
            d.polygon([art.xy(p) for p in [(x-28,hy+22),(x-17,hy),(x-5,hy+22)]],fill=CORAL)
            art.ellipse(d,(x-21,hy-4,x-13,hy+4),GOLD)
            art.star(d,x-16,hy+14,3,CREAM)
            for dx,dy in [(-48,-27),(47,-33),(73,-8)]:
                art.star(d,x+dx,126+dy,3*hat,GOLD)
        if t>=2.65:pill(d,CLIPS[name]['quote'],size=24)
        else:text(d,'your pace is a pace.',216,17,LILAC)
    elif name=='post-heart-mail':
        opened=phase(t,.65,1.25);gift=phase(t,1.25,2.25)
        art.rounded(d,(73,116,207,184),7,'#d8b9a4',INK,1.5)
        d.polygon([art.xy(p) for p in [(74,119),(140,157-88*opened),(206,119)]],fill='#e8cfaf')
        art.line(d,[(74,119),(140,157-88*opened),(206,119)],GOLD,1.5)
        art.heart(d,140,149-55*gift,12+22*gift,CORAL)
        art.heart(d,131,139-55*gift,3+2*gift,'#ffd0b2')
        # The envelope front occludes the emerging heart until it rises out.
        d.polygon([art.xy(p) for p in [(74,119),(140,157),(206,119),(206,183),(74,183)]],fill=CREAM)
        art.line(d,[(74,183),(126,145)],'#bb8b91',1)
        art.line(d,[(206,183),(154,145)],'#bb8b91',1)
        art.ellipse(d,(129,148,151,170),CORAL)
        art.heart(d,140,159,5,CREAM)
        art.rounded(d,(179,124,198,144),2,SAGE)
        art.heart(d,188,133,5,'#836480')
        heart_float(d,t,2.25,62,105,6)
        heart_float(d,t,2.45,222,100,7,GOLD)
        if t>=2.25:pill(d,CLIPS[name]['quote'],size=22)
        else:text(d,'special delivery, just for you.',216,15,LILAC)
    elif name=='post-return-guilt':
        arrive=phase(t,.55,1.1);x=82+58*arrive
        art.rounded(d,(x-87,91,x+87,183),5,CREAM)
        label(d,'UNNECESSARY',x,116,18,'#b9a7a2')
        label(d,'GUILT',x,148,31,'#b9a7a2')
        if t>=1.25:
            land=phase(t,1.25,1.5)
            bounce=0 if t<1.5 else -3*math.exp(-(t-1.5)*10)*abs(math.sin((t-1.5)*18))
            stamp=return_stamp();scale=.85+.15*land
            stamp=stamp.resize((round(stamp.width*scale),round(stamp.height*scale)),Image.Resampling.LANCZOS)
            art.stamp(im,stamp,140,100+37*land+bounce,-9)
        if t>=2.35:
            art.star(d,235,80,5,GOLD);art.star(d,43,84,3,GOLD)
            pill(d,CLIPS[name]['quote'],size=21)
        else:text(d,'receiving department says...',216,15,LILAC)
    elif name=='post-soft-landing':
        fly=phase(t,.4,1.65)
        bounce=0 if t<1.65 else -9*math.exp(-(t-1.65)*6)*abs(math.sin((t-1.65)*14))
        squash=0 if t<1.65 else .13*math.exp(-(t-1.65)*8)*math.sin((t-1.65)*14)**2
        pillow=prop('soft-pillow',172,78)
        pillow=pillow.resize((pillow.width,round(pillow.height*(1-squash))),Image.Resampling.LANCZOS)
        art.stamp(im,pillow,140,191-pillow.height/S/2)
        px=38+165*fly-63*fly*fly;py=71+56*fly*fly+bounce
        art.stamp(im,plane(),px,py,12-22*fly)
        if 1.65<t<2.65:
            q=(t-1.65)
            for n in range(6):
                a=math.pi*(.15+n*.14)
                xx=140+math.cos(a)*(38+q*35);yy=149-math.sin(a)*(14+q*23)
                art.ellipse(d,(xx-2,yy-1,xx+2,yy+1),CREAM)
        if t>=2.65:pill(d,CLIPS[name]['quote'],size=20)
        else:text(d,'a different kind of success.',216,16,LILAC)
    else:raise ValueError(name)
    return im.convert('RGB')


def render(name,t):
    card=cover(name)
    if t<.1 or t>=4.35:return card.copy()
    frame=scene(name,t)
    if t<.35:return Image.blend(card,frame,phase(t,.1,.35))
    if t>4.0:return Image.blend(frame,card,phase(t,4.0,4.35))
    return frame


def soundtrack(name):
    x=np.zeros(round(DURATION*RATE))
    seed=int(hashlib.sha256(name.encode()).hexdigest()[:8],16)
    def add(at,sound,gain=1):
        start=round(at*RATE);n=min(len(sound),len(x)-start)
        x[start:start+n]+=gain*sound[:n]
    def bell(at,hz,duration=.5,gain=.13):
        add(at,tone(duration,[hz,hz],harmonics=(1,.06,.018),decay=7),gain)
        add(at,tone(duration*.6,[hz*2.73,hz*2.73],harmonics=(1,),decay=16),gain*.045)
    def hum(at,duration,hz,gain=.12):
        add(at,tone(duration,hz,harmonics=(1,.12,.02),vibrato=.005)
            *env(round(duration*RATE),.055,.10),gain)
    if name=='post-sunshine':
        bell(.45,1396.91,.36,.12)
        add(.7,tone(.14,[510,280],harmonics=(1,.12),decay=10),.18)
        for n,hz in enumerate((523.25,659.25,783.99)):bell(1.2+n*.15,hz,.7,.14)
        bell(1.85,1174.66,.16,.07)
        bell(2.3,1046.5,.6,.12);bell(2.55,1318.51,.65,.10)
        hum(3.05,.65,[523,523,494],.065)
    elif name=='post-peace-tea':
        add(.4,noise(.35,seed+2,700,2500),.09)
        bell(1.05,1320,.25,.13);bell(1.07,1760,.20,.045)
        add(1.05,noise(.10,seed,800,3300),.07)
        add(1.5,noise(.3,seed+1,600,2300)*env(round(.3*RATE),.035,.06),.12)
        hum(2.35,.64,[510,660,580],.13)
        bell(3.1,783.99,.65,.09)
    elif name=='post-slow-club':
        for n,at in enumerate((.5,.9,1.3,1.7,2.1)):
            add(at,tone(.18,[380+n*25,440+n*25,360+n*25],harmonics=(1,.09),vibrato=.009),.11)
        for n,hz in enumerate((523.25,659.25,783.99,1046.5)):
            bell(2.65+n*.14,hz,.5,.13)
    elif name=='post-heart-mail':
        add(.5,noise(.42,seed,700,3300)*env(round(.42*RATE),.06,.1),.13)
        add(1.25,tone(.18,[500,730,530],harmonics=(1,.10),decay=7),.17)
        for n,hz in enumerate((659.25,783.99,987.77)):
            bell(2.25+n*.16,hz,.65,.14)
        hum(3.0,.6,[392,440,392],.07)
    elif name=='post-return-guilt':
        for n in range(3):add(.55+n*.12,noise(.12,seed+n,800,3600),.12)
        add(1.5,tone(.18,[410,220],harmonics=(1,.18,.035),decay=12),.26)
        add(1.5,noise(.12,seed+4,500,3000),.18)
        hum(2.35,.55,[510,620,740],.14)
        bell(3.0,987.77,.6,.08)
    elif name=='post-soft-landing':
        hum(.4,1.18,[990,1120,760,490],.11)
        add(.4,noise(1.18,seed,1000,3600)*env(round(1.18*RATE),.12,.16),.04)
        add(1.65,noise(.26,seed+1,220,1700),.23)
        add(1.65,tone(.35,[320,240,330,260],harmonics=(1,.14),decay=4),.19)
        bell(2.65,659.25,.6,.13);bell(2.93,523.25,.7,.11)
    else:raise ValueError(name)
    x=signal.sosfilt(signal.butter(3,[165,4400],btype='bandpass',fs=RATE,output='sos'),x)
    x*=env(len(x),.02,.15)
    measured,peak=loudness(pcm(x));x*=10**(min(-25-measured,-9.1-peak)/20)
    data=pcm(x);report=metrics(data)
    assert report['clipped_samples']==0 and report['true_peak_dbfs']<=-9
    assert report['edge_peak']<.0001 and abs(report['dc_offset'])<.0001
    assert -32<report['lufs']<=-24.5
    return data,report

def export_gif(frames,path):
    # Include the crossfade colors, which the twenty-pose atlas omits.
    # One palette for the entire loop prevents palette changes between frames.
    swatches=Image.new('RGB',(280*10,240*9))
    for n,frame in enumerate(frames):swatches.paste(frame,((n%10)*280,(n//10)*240))
    palette=swatches.quantize(colors=256,method=Image.Quantize.MEDIANCUT)
    gif_frames=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]
    gif_frames[0].save(path,save_all=True,append_images=gif_frames[1:],duration=50,loop=0,optimize=False,disposal=2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',choices=list(CLIPS),help='Rebuild one clip in an existing collection')
    args=parser.parse_args()
    for folder in (OUT,OUT/'gifs',OUT/'share',ROOT/'assets'):
        folder.mkdir(parents=True,exist_ok=True)
    records=[];audio_reports=[]
    existing={}
    if args.only:
        existing={r['id']:r for r in json.loads((catalog_group(ROOT, 'pocket_postcards')).read_text(encoding='utf-8'))['clips']}
    overview=Image.new('RGB',(900,590),BG);od=ImageDraw.Draw(overview)
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
        bookend=cover(name).resize((280,240),Image.Resampling.LANCZOS)
        # Typography cards are the exact loop endpoints; no robot face is loaded.
        for i in (0,1,87,88,89):frames[i]=bookend.copy()
        video=ROOT/'assets/videos'/(name+'.mp4')
        encode(frames,(280,240),wav,video)
        share=OUT/'share'/(name+'.mp4');encode(masters,(840,720),wav,share)
        keys=[bookend.copy() if t<.1 or t>=4.35 else render(name,t).resize((280,240),Image.Resampling.LANCZOS) for t in STARTS]
        atlas=Image.new('RGB',(1120,1200))
        for n,f in enumerate(keys):atlas.paste(f,((n%4)*280,(n//4)*240))
        sheet=ROOT/'assets/face_animations'/(name+'.png');atlas.save(sheet)
        assert sheet.stat().st_size<=4*1024*1024
        spec=dict(sheet=sheet.name,columns=4,rows=5,order=list(range(20)),durations_ms=TIMING,blend_ms=0)
        manifest=sheet.with_suffix('.json');manifest.write_text(json.dumps(spec)+'\n')
        gif=OUT/'gifs'/(name+'.gif')
        export_gif(frames,gif)
        poster=frames[61];poster.save(OUT/(name+'-poster.png'))
        cues=[dict(time_s=t,label=label,video_frame=round(t*FPS)) for t,label in detail['cues']]
        record={**{k:v for k,v in detail.items() if k!='cues'},'id':name,'duration_s':DURATION,'fps':FPS,
            'frames':len(frames),'native_size':[280,240],'share_size':[840,720],'sprite_frames':20,'cues':cues,
            'video':video.relative_to(ROOT).as_posix(),'audio':wav.relative_to(ROOT).as_posix(),
            'sidecar':sidecar.relative_to(ROOT).as_posix(),'sheet':sheet.relative_to(ROOT).as_posix(),
            'manifest':manifest.relative_to(ROOT).as_posix(),'gif':gif.relative_to(OUT).as_posix(),
            'share':share.relative_to(OUT).as_posix(),'poster':name+'-poster.png',
            'contains_robbi_face':False,'bookend':'typographic-postcard','sound_default':True,'audio_sha256':sha(wav),'video_sha256':sha(video),
            'sheet_sha256':sha(sheet),'audio_metrics':report}
        records.append(record)
        print(name, f'{len(frames)} frames; {report["lufs"]} LUFS; peak {report["true_peak_dbfs"]} dBFS',flush=True)
    for index,record in enumerate(records):
        col,row=index%3,index//3
        with Image.open(OUT/record['poster']) as poster:overview.paste(poster,(col*300+10,row*295+8))
        od.text((col*300+10,row*295+259),record['title'],font=ImageFont.truetype(str(art.SOURCE/'fonts/Lato-Bold.ttf'),15),fill=CREAM)
    overview.save(OUT/'overview.png')
    catalog={'version':1,'title':'Pocket postcards','duration_s':DURATION,'clips':records,
        'sound_provenance':'Original deterministic synthesis. No voices, music recordings or third-party samples.'}
    (catalog_group(ROOT, 'pocket_postcards')).write_text(json.dumps(catalog,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (OUT/'audio-checks.json').write_text(json.dumps(audio_reports,indent=2)+'\n')


if __name__=='__main__':main()
