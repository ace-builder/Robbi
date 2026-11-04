"""Four fresh fixed-artwork seasonal stories and original nonverbal sound cues."""
from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw
import seasonal_common as c

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/dynamics_sources/seasonal-encore-v1/nature'
REVIEW=ROOT/'test-results/seasonal-encore-preview/nature-review'
A=c.art;S=c.S

CLIPS={
 'season-earth-day':dict(title='A little care for Earth',heading='EARTH DAY',heading_size=25,
    cover=['A LITTLE CARE.','A WORLD OF GOOD.'],quote='SMALL CARE. BIG WORLD.',
    when='A little Earth globe receives a leafy mend and grows a hopeful new sprout.',
    sound='A gentle globe creak, leafy paper rustle, two mending plucks and a warm growing chord.',
    group='seasons',theme='earth-day',occasion='earth-day',
    cues=[(.5,'globe gently rocks'),(1.1,'leaf patch arrives'),(1.65,'leafy mend settles'),(2.3,'new sprout grows'),(3.,'a hopeful little sparkle')]),
 'season-summer-picnic':dict(title='The strawberry heist',heading='SUMMER PICNIC',heading_size=24,
    cover=['PRECIOUS CARGO.','VERY SMALL CREW.'],quote='PICNIC SECURITY: ZERO.',
    when='Three determined ants carry one extremely ambitious strawberry off the picnic cloth.',
    sound='Three light ant footsteps, a tiny lifting strain, a soft marching rhythm and a strawberry-sized flourish.',
    group='seasons',theme='summer-picnic',
    cues=[(.5,'the tiny crew arrives'),(1.25,'prepare to lift'),(1.65,'the strawberry lifts'),(2.15,'precious cargo marches'),(3.1,'the heist is complete')]),
 'season-autumn-harvest':dict(title='The bonus mushroom',heading='AUTUMN HARVEST',heading_size=23,
    cover=['SMALL HARVEST.','BIG JOY.'],quote='SMALL HARVEST. BIG JOY.',
    when='A woven basket catches two small mushrooms, then one very generous bonus mushroom.',
    sound='Two soft mushroom plops, a falling whoosh, a squashy basket thump and a contented harvest chord.',
    group='seasons',theme='autumn-harvest',
    cues=[(.55,'first mushroom drops'),(.95,'first soft landing'),(1.5,'second soft landing'),(1.9,'the bonus mushroom drops'),(2.45,'a generously sized landing'),(3.05,'harvest joy')]),
 'season-winter-solstice':dict(title='A little more daylight',heading='WINTER SOLSTICE',heading_size=23,
    cover=['THE LONGEST NIGHT.','A LITTLE MORE LIGHT.'],quote='BRIGHTER DAYS AHEAD.',
    when='The long night parts like a paper curtain and a little sun rises over snowy hills.',
    sound='A quiet night hum, two soft curtain swishes, a rising sunrise motif and a warm daylight chord.',
    group='seasons',theme='winter-solstice',occasion='winter-solstice',
    cues=[(.5,'the longest night'),(1.05,'night curtains begin to part'),(1.75,'first little sunlight'),(2.4,'sun clears the snowy hill'),(3.1,'a little more daylight')]),
}


def poly(d,points,fill):d.polygon([A.xy(p) for p in points],fill=fill)


def smooth(t,a,b):
    p=max(0,min(1,(t-a)/(b-a)))
    return p*p*(3-2*p)


def settle(t,start,amplitude=1,speed=18,decay=6):
    p=t-start
    return amplitude*math.sin(p*speed)*math.exp(-p*decay) if p>=0 else 0


def bezier(a,b,c_,d,n=40):
    return [tuple((1-u)**3*a[k]+3*(1-u)**2*u*b[k]+3*(1-u)*u*u*c_[k]+u**3*d[k] for k in (0,1)) for u in [i/n for i in range(n+1)]]


def blend(a,b,p):
    aa=tuple(int(a[k:k+2],16) for k in (1,3,5));bb=tuple(int(b[k:k+2],16) for k in (1,3,5))
    return tuple(round(x+(y-x)*p) for x,y in zip(aa,bb))


def glow(im,x,y,r,color=c.GOLD,amount=1):
    if amount<=0:return
    layer=Image.new('RGBA',im.size);d=ImageDraw.Draw(layer)
    rgb=tuple(int(color[k:k+2],16) for k in (1,3,5))
    for n in range(20,0,-1):
        rr=r*n/20;A.ellipse(d,(x-rr,y-rr,x+rr,y+rr),rgb+(round(55*amount*(1-n/21)**1.6),))
    im.alpha_composite(layer)


def leaf(d,x,y,w,h,angle=0,color=c.SAGE):
    # A symmetric pointed leaf with a fine central vein.
    a=math.radians(angle)
    def pt(v):return (x+v[0]*math.cos(a)+v[1]*math.sin(a),y-v[0]*math.sin(a)+v[1]*math.cos(a))
    pts=bezier((0,0),(-w,-h*.2),(-w*.6,-h*.85),(0,-h),20)+bezier((0,-h),(w*.6,-h*.85),(w,-h*.2),(0,0),20)
    poly(d,[pt(p) for p in pts],color)
    A.line(d,[pt((0,0)),pt((0,-h*.85))],'#7f9f7f',.8)


@lru_cache(None)
def prop(name,w,h):return c.prop(SOURCE/(name+'.png'),w,h)


def prop_base(im,name,x,y,w,h):
    p=prop(name,w,h);A.stamp(im,p,x,y-p.height/S/2)


def ant(d,x,y,t,moving=True,load=False):
    step=math.sin(t*18+x)*2 if moving else 0
    for n in (-1,0,1):
        xx=x+n*3
        A.line(d,[(xx,y-3),(xx-3,y-1),(xx-5+step*(1 if n%2 else -1),y+3)],'#5f454b',1)
        A.line(d,[(xx,y-3),(xx+3,y),(xx+5-step*(1 if n%2 else -1),y+3)],'#5f454b',1)
    for dx,r in [(-4,2.8),(0,2),(4,2.5)]:A.ellipse(d,(x+dx-r,y-5-r,x+dx+r,y-5+r),'#5f454b')
    A.line(d,[(x+4,y-7),(x+6,y-11),(x+8,y-10)],'#5f454b',.8)
    if load:A.line(d,[(x,y-7),(x+1,y-13)],'#5f454b',1)


def basket(d,sy=0,squash=0,front=False):
    if not front:
        d.arc(A.xy((82,111+sy,198,187+sy)),180,360,fill='#c99873',width=5*S)
        d.arc(A.xy((88,116+sy,192,183+sy)),180,360,fill='#e5ba87',width=2*S)
        A.ellipse(d,(68,143+sy,212,169+sy),'#6e4950',c.GOLD,2)
    else:
        rim=151+sy+squash
        poly(d,[(70,rim),(210,rim),(198,189+sy),(82,189+sy)],'#b58266')
        for yy in (158,167,176,185):A.line(d,[(77,yy+sy+squash*.3),(203,yy+sy+squash*.3)],'#e6b786',1.2)
        for xx in range(85,201,14):A.line(d,[(xx,rim+4),(140+(xx-140)*.85,187+sy)],'#8b6056',1)
        A.ellipse(d,(69,rim-4,211,rim+7),'#dcad7e',c.INK,1)
        A.line(d,[(79,rim+2),(201,rim+2)],'#f6d4a0',1.1)


def scene(name,t):
    base=c.stage(CLIPS[name]);im=Image.new('RGBA',base.size);d=ImageDraw.Draw(im)
    ready=3.15;before='a little seasonal surprise.'
    A.ellipse(d,(43,182,237,192),'#392c3c')
    if name=='season-earth-day':
        wob=settle(t,.5,2.5,10,3.8);cx=140+wob;cy=126
        glow(im,cx,cy,76,'#a6c8b2',.45)
        A.ellipse(d,(cx-50,cy-50,cx+50,cy+50),'#7da9ad',c.INK,1.5)
        A.ellipse(d,(cx-45,cy-46,cx+44,cy+43),None,'#a6cbcb',.8)
        # Stylized Atlantic-facing continents; the globe has no face.
        for pts in [[(-32,-32),(-17,-41),(-8,-34),(-14,-24),(-7,-17),(-18,-10),(-23,-17),(-34,-18)],
                    [(-20,-6),(-8,-5),(-4,4),(-12,16),(-15,29),(-23,20),(-21,9),(-28,0)],
                    [(10,-38),(24,-40),(32,-30),(23,-24),(26,-17),(14,-16),(8,-25)],
                    [(11,-12),(25,-12),(33,0),(22,20),(13,14),(5,-1)]]:
            poly(d,[(cx+x,cy+y) for x,y in pts],c.SAGE)
        d.arc(A.xy((cx-57,cy-58,cx+57,cy+55)),285,110,fill=c.GOLD,width=2*S)
        A.line(d,[(cx+16,cy+53),(140,181)],c.GOLD,3)
        A.rounded(d,(113,181,168,188),4,c.GOLD)
        patch=smooth(t,1.1,1.65)
        if patch<1:A.line(d,[(cx+16,cy+7),(cx+22,cy+12),(cx+17,cy+17),(cx+25,cy+22)],'#526f7d',1.5)
        px=218-61*patch;py=107+39*patch
        if patch>0:
            leaf(d,px,py,12,29,angle=-40+12*patch)
            for n in range(3):
                q=smooth(t,1.65+n*.09,1.8+n*.09)
                if q>0:A.line(d,[(153+n*3,134+n*3),(157+n*3,133+n*3)],c.CREAM,q)
        grow=smooth(t,2.3,2.95)
        if grow>0:
            stem=bezier((140,77),(140,70),(136,67),(141,65),25)[:max(2,round(26*grow))]
            A.line(d,stem,'#8fb18c',2)
            foliage=smooth(t,2.55,2.95);tx,ty=stem[-1]
            leaf(d,tx,ty,7*foliage,15*foliage,-48,color=c.SAGE)
            leaf(d,tx,ty,7*foliage,16*foliage,49,color='#a4c496')
        if t>=3:
            for x,y in [(85,95),(204,112),(103,65)]:A.star(d,x,y,3*smooth(t,3,3.3),c.GOLD)
        before='a little care goes a long way.';ready=3.15
    elif name=='season-summer-picnic':
        # The cloth remains stationary while its overly ambitious cargo departs.
        A.rounded(d,(37,164,240,190),4,c.CREAM,c.INK,1)
        for x in range(41,239,14):A.line(d,[(x,165),(x,189)],'#e7b1a0',6)
        for y in (169,183):A.line(d,[(40,y),(237,y)],'#e7b1a0',6)
        for x in range(41,239,14):
            for y in (169,183):A.rounded(d,(x-3,y-3,x+3,y+3),.5,'#c88983')
        # A little untouched lemonade makes the missing strawberry funnier.
        A.rounded(d,(51,127,76,164),3,'#e9c28b',c.CREAM,1)
        A.ellipse(d,(51,123,76,131),'#f5d49a',c.CREAM,1)
        A.line(d,[(66,143),(72,113),(80,113)],c.CORAL,2)
        arrive=smooth(t,.5,1.25);lift=smooth(t,1.4,1.65);carry=smooth(t,1.8,3.1)
        x=139+76*carry;walk=1.8<t<3.1
        bob=math.sin((t-1.8)*16)*.7 if walk else settle(t,3.1,.8,17,7)
        fruit_base=171-5*lift+bob
        for n,dx in enumerate((-24,0,24)):
            ax=(34+n*15)*(1-arrive)+(x+dx)*arrive
            ant(d,ax,177,t+n,arrive<1 or walk,load=lift>.2)
        if lift>0:
            A.ellipse(d,(x-31,fruit_base-1,x+31,fruit_base+3),c.SAGE)
            A.line(d,[(x-27,fruit_base+1),(x+27,fruit_base+1)],'#88a184',.7)
        prop_base(im,'strawberry',x,fruit_base,84,105)
        if 1.25<t<1.65:
            for xx in (113,165):A.line(d,[(xx,164),(xx-3,160)],c.GOLD,1)
        if t>=3.1:A.star(d,244,113,4*smooth(t,3.1,3.35),c.GOLD)
        before='the crew has ambitious plans.';ready=3.2
    elif name=='season-autumn-harvest':
        impact=settle(t,.95,1.2)+settle(t,1.5,1.3)+settle(t,2.45,3.1,18,5.5)
        basket(d,sy=impact*.25)
        for x,start,end in [(103,.55,.95),(179,1.05,1.5)]:
            if t>=start:
                p=smooth(t,start,end);bottom=75+89*p+settle(t,end,2,18,7)
                prop_base(im,'woodland-mushroom',x,bottom,45,49)
        if t>=1.9:
            p=smooth(t,1.9,2.45);bottom=49+118*p+impact
            prop_base(im,'woodland-mushroom',140,bottom,112,111)
        basket(d,sy=impact*.25,squash=impact,front=True)
        if t>=3.05:
            for x,y in [(61,122),(223,131)]:A.star(d,x,y,4*smooth(t,3.05,3.3),c.GOLD)
            A.heart(d,140,86,5*smooth(t,3.05,3.3),c.CORAL)
        before='a basket with very good luck.';ready=3.2
    elif name=='season-winter-solstice':
        opening=smooth(t,1.05,2.7);rise=smooth(t,1.5,2.65)
        # A paper diorama: night panels physically gather at the edges.
        A.rounded(d,(33,57,247,190),5,'#694d68',c.INK,1)
        A.rounded(d,(39,63,241,184),2,blend('#665471','#d7ab98',rise))
        suny=178-70*rise
        sunlight=Image.new('RGBA',im.size);sun_d=ImageDraw.Draw(sunlight)
        glow(sunlight,140,suny,61,c.GOLD,rise*.8)
        for n in range(10):
            a=n*math.tau/10
            A.line(sun_d,[(140+math.cos(a)*24,suny+math.sin(a)*24),(140+math.cos(a)*29,suny+math.sin(a)*29)],c.GOLD,1.3)
        A.ellipse(sun_d,(121,suny-19,159,suny+19),c.GOLD)
        A.ellipse(sun_d,(126,suny-14,136,suny-8),'#ffe8b8')
        im.alpha_composite(sunlight.crop((39*S,63*S,241*S,184*S)),(39*S,63*S))
        for pts,color in [([(39,175),(73,142),(109,166),(151,139),(198,163),(241,148),(241,184),(39,184)],'#b6bec9'),
                          ([(39,181),(83,159),(126,176),(171,153),(215,174),(241,163),(241,184),(39,184)],'#e0e4de'),
                          ([(39,185),(81,177),(122,183),(165,171),(205,180),(241,177),(241,187),(39,187)],c.CREAM)]:poly(d,pts,color)
        width=101-92*opening
        for side in (-1,1):
            outer=39 if side==-1 else 241;inner=outer-side*width
            poly(d,[(outer,63),(inner,63),(inner+side*3,179),(outer,184)],'#3e354f')
            for n in range(4):
                x=outer-side*(n+.6)*width/4
                A.line(d,bezier((x,64),(x+side*2,105),(x+side*3,149),(x+side*3,179),25),'#534360',max(.6,width/25))
            if width>22:
                for q in (.28,.65):A.star(d,outer-side*width*q,84+31*q,2,c.LILAC)
        A.rounded(d,(29,54,251,64),4,'#b8938b')
        if t>=3.1:
            for x,y in [(89,113),(193,123)]:A.star(d,x,y,3*smooth(t,3.1,3.35),c.GOLD)
        before='even the longest night turns.';ready=3.2
    base.alpha_composite(im.crop((0,48*S,280*S,196*S)),(0,48*S))
    c.reveal_caption(base,CLIPS[name],t,ready,before,size=18)
    return base


def e(at,kind,hz=660,duration=.3,gain=.1):return dict(at=at,kind=kind,hz=hz,duration=duration,gain=gain)


def audio_events(name):
    return {
      'season-earth-day':[e(.5,'hum',[262,294],.5,.06),e(1.1,'rustle',duration=.5,gain=.065),e(1.65,'pluck',523,.25,.09),e(1.85,'pluck',659,.25,.08),e(2.3,'chime',[523,784],.6,.09),e(3.,'chord',[392,523,659],.7,.11)],
      'season-summer-picnic':[e(.5,'tick',440,.1,.055),e(.7,'tick',494,.1,.055),e(.9,'tick',523,.1,.055),e(1.25,'hum',[220,294],.35,.07),e(1.65,'pop',330,.18,.075),e(1.85,'pluck',392,.22,.075),e(2.15,'pluck',440,.22,.075),e(2.45,'pluck',494,.22,.075),e(2.75,'pluck',523,.22,.075),e(3.1,'chord',[392,494,587],.65,.11)],
      'season-autumn-harvest':[e(.55,'whoosh',duration=.3,gain=.05),e(.95,'pop',330,.18,.09),e(1.5,'pop',392,.18,.09),e(1.9,'whoosh',duration=.45,gain=.065),e(2.45,'thud',220,.23,.11),e(2.65,'pluck',330,.2,.05),e(3.05,'chord',[330,440,554],.7,.11)],
      'season-winter-solstice':[e(.5,'hum',[220,262],.65,.07),e(1.05,'rustle',duration=.65,gain=.055),e(1.75,'pluck',392,.45,.075),e(2.05,'pluck',494,.45,.075),e(2.4,'bell',587,.5,.085),e(3.1,'chord',[392,494,587,784],.7,.11)],
    }[name]


def review():
    import io
    import wave
    import numpy as np
    REVIEW.mkdir(parents=True,exist_ok=True)
    overview=Image.new('RGB',(560,540),c.BG)
    report={'pack':'seasonal-encore-v1','module':'nature','native_resolution':[280,240],
            'cue_energy_threshold_rms':.001,'clips':{},
            'visual_review':{'native_contact_poses_inspected':12,'native_motion_poses_inspected':32,
                             'fixed_issues':['Clipped solstice sunlight to the paper theatre.',
                                             'Attached sprout leaves to the growing stem endpoint.',
                                             'Joined all three ant carriers with a leaf stretcher.'],
                             'remaining_uncertainties':[]}}
    notes={
      'season-earth-day':'No face on globe. Geographic silhouettes remain fixed; leafy mend covers the scar, golden stitches follow, and the sprout grows from the north edge.',
      'season-summer-picnic':'Three native ants lift and transport one unchanged generated strawberry. Feet stay on the cloth; carrying gait and strawberry bob share the same load.',
      'season-autumn-harvest':'One unchanged mushroom artwork appears at two small sizes and one oversized scale. Basket rim consistently occludes stems; all three landings settle against fixed ground.',
      'season-winter-solstice':'Layered paper hills occlude the rising sun; the night panels gather with their attached stars; warm daylight and caption settle before 3.55 seconds.',
    }
    for n,(name,detail) in enumerate(CLIPS.items()):
        header=np.asarray(c.stage(detail))[:48*S];frames=[]
        for k in range(90):
            frame=scene(name,k/20);assert frame.size==(840,720)
            assert np.array_equal(np.asarray(frame)[:48*S],header),(name,k,'header intrusion')
            frames.append(frame.convert('RGB').resize((280,240),Image.Resampling.LANCZOS))
        c.cover(detail)
        for text,size in [(detail['quote'],18),(detail['heading'],detail['heading_size'])]:
            box=ImageDraw.Draw(c.stage(detail)).textbbox((0,0),text,font=A.font(size))
            assert box[2]-box[0]<=258*S,(name,text,'caption width')
        sheet=Image.new('RGB',(840,240),c.BG)
        for col,t in enumerate((.65,1.8,3.55)):sheet.paste(frames[round(t*20)],(col*280,0))
        sheet.save(REVIEW/(name+'-poses.png'))
        motion=Image.new('RGB',(1120,510),c.BG);md=ImageDraw.Draw(motion)
        for k,t in enumerate((.45,.85,1.25,1.65,2.05,2.45,2.9,3.55)):
            x=k%4*280;y=k//4*255;motion.paste(frames[round(t*20)],(x,y));md.text((x+8,y+242),f'{t:.2f}s',fill=c.CREAM)
        motion.save(REVIEW/(name+'-motion.png'))
        overview.paste(frames[71],((n%2)*280,(n//2)*270))
        ImageDraw.Draw(overview).text(((n%2)*280+8,(n//2)*270+244),detail['title'],fill=c.CREAM)
        events=audio_events(name);data,metrics=c.synthesize(name,events)
        with wave.open(io.BytesIO(data),'rb') as wav:
            samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)/32768
        if samples.ndim>1:samples=samples.mean(axis=1)
        cue_reports=[]
        for at,label in detail['cues']:
            assert any(ev['at']-.08<=at<=ev['at']+ev['duration']+.08 for ev in events),(name,label,'no event')
            window=samples[round(at*c.RATE):round((at+.16)*c.RATE)]
            rms=float(np.sqrt(np.mean(window**2)));assert rms>.001,(name,label,'quiet cue')
            cue_reports.append(dict(at=at,label=label,energy_rms=rms))
        report['clips'][name]=dict(frames_checked=90,header_clear_through_y=48,cover_fits=True,caption_fits=True,audio_metrics=metrics,cues=cue_reports,change_notes=notes[name])
        print(name,'90 frames, bounds, covers and audio cue energy OK',flush=True)
    overview.save(REVIEW/'overview.png')
    (REVIEW/'qa-change-notes.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':review()
