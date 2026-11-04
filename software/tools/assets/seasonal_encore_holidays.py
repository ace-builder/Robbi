"""Four fresh seasonal object postcards, using fixed original art and native geometry."""
from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw
import seasonal_common as c

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/dynamics_sources/seasonal-encore-v1/holidays'
CLIPS={
 'season-april-fools':dict(title='A questionable little sprout',heading='APRIL FOOLS!',heading_size=26,
    quote='BOTANICALLY QUESTIONABLE.',cover=['BOTANICALLY','QUESTIONABLE.'],
    when='A terracotta flowerpot grows a rubber duck on a spring. The gardening manual is unavailable.',
    sound='A little soil rustle, a springy pluck, a soft duck-toy pop and a warm surprise chord.',
    cues=[(.55,'pot wiggles'),(1.3,'soil pops'),(1.45,'spring surprise'),(2.15,'duck settles'),(2.8,'questionable sparkle')],
    group='holidays',theme='april-fools',occasion='april-fools',sound_revision=2),
 'season-oktoberfest':dict(title='A little pretzel party',heading='OKTOBERFEST!',heading_size=26,
    quote='A LITTLE PRETZEL PARTY.',cover=['A LITTLE','PRETZEL PARTY.'],
    when='Two warm pretzels bounce in, share a cheerful little toast, then take one last dance step.',
    sound='Two soft bakery landings, a bright little toast, and a pair of playful dance plucks.',
    cues=[(.5,'first pretzel hops'),(.75,'second pretzel hops'),(1.35,'bakery landing'),(1.9,'pretzel toast'),(2.45,'left dance step'),(2.7,'right dance step'),(3.1,'together flourish')],
    group='holidays',theme='oktoberfest',occasion='oktoberfest',sound_revision=2),
 'season-diwali':dict(title='A little light, shared',heading='HAPPY DIWALI',heading_size=26,
    quote='LET A LITTLE LIGHT IN.',cover=['LET A LITTLE','LIGHT IN.'],
    when='A small clay oil lamp lights while an original geometric petal rangoli blooms softly around it.',
    sound='A gentle wick chime, soft petal plucks, and a warm universal three-note chord.',
    cues=[(.75,'diya lights'),(1.3,'first petals'),(1.7,'rangoli opens'),(2.15,'outer dots shimmer'),(2.85,'warm light flourish')],
    group='holidays',theme='diwali',occasion='diwali',sound_revision=2),
 'season-hanukkah':dict(title='So much little light',heading='HAPPY HANUKKAH',heading_size=25,
    quote='EIGHT NIGHTS. SO MUCH LIGHT.',cover=['EIGHT NIGHTS.','SO MUCH LIGHT.'],
    when='A raised central shamash shares its light with eight candles in a straight row, from left to right.',
    sound='A warm helper-light chime, eight individual lighting plucks, and a soft final chord.',
    cues=[(.5,'shamash lights')]+[(round(1.05+n*.2,2),'candle '+str(n+1)) for n in range(8)]+[(2.95,'nine lights together')],
    group='holidays',theme='hanukkah',occasion='hanukkah',sound_revision=2),
}
HANUKKAH_X=(45,69,93,117,163,187,211,235)
HANUKKAH_TIMES=tuple(round(1.05+n*.2,2) for n in range(8))
REVEALS={
 'season-april-fools':(2.8,'the seed packet was vague.',16),
 'season-oktoberfest':(3.1,'good company. extra twists.',18),
 'season-diwali':(2.85,'a small light, softly shared.',18),
 'season-hanukkah':(3.05,'a little light, shared.',15.7),
}


def prop(name,width,height):return c.prop(SOURCE/(name+'.png'),width,height)


def polygon(d,points,fill,outline=None,width=1.2):
    d.polygon([c.art.xy(p) for p in points],fill=fill)
    if outline:c.art.line(d,points+[points[0]],outline,width)


def pulse(t,start,duration):
    p=(t-start)/duration
    return math.sin(math.pi*p)**2 if 0<p<1 else 0


def overlay(im,layer,opacity=1):
    if opacity<=0:return
    if opacity<1:layer.putalpha(layer.getchannel('A').point(lambda a:round(a*opacity)))
    im.alpha_composite(layer)


def anchored(im,sprite,x,base,sx=1,sy=1,angle=0):
    c.deform_stamp(im,sprite,x,base-sprite.height/c.S*sy/2,sx,sy,angle)


def glow(im,x,y,radius,amount=1):
    if amount<=0:return
    layer=Image.new('RGBA',im.size);d=ImageDraw.Draw(layer)
    for n in range(16,0,-1):
        r=radius*n/16
        c.art.ellipse(d,(x-r,y-r,x+r,y+r),(255,194,112,round(amount*(2+(16-n)*1.2))))
    overlay(im,layer)


def flame(d,x,wick,t,amount=1,size=1):
    if amount<=0:return
    h=13*amount*size;w=3.7*amount*size;lean=.7*math.sin(t*7+x*.1)*amount
    polygon(d,[(x,wick),(x-w,wick-3*amount),(x-w*.85,wick-h*.48),(x+lean,wick-h),
               (x+w*.75,wick-h*.5),(x+w,wick-3*amount)],c.GOLD)
    c.art.ellipse(d,(x-1.4*amount,wick-6*amount*size,x+1.4*amount,wick-.5*amount),c.CREAM)


def sparkle(d,x,y,r,t,amount=1,color=c.GOLD):
    if amount>0:c.art.star(d,x,y,r*amount*(.94+.06*math.sin(t*4+x)),color)


def april_fools(im,d,t):
    wiggle=2.3*math.sin((t-.55)*19)*c.phase(t,.55,.8)*(1-c.phase(t,1.12,1.3))
    rise=c.spring(t,1.3,2.15,1.0)
    age=max(0,t-2.15);settle=3*math.sin(age*13)*math.exp(-age*5) if t>2.15 else 0
    duck_y=171-85*rise+settle
    duck=Image.new('RGBA',im.size);dd=ImageDraw.Draw(duck)
    spring_top=duck_y+29
    if spring_top<145:
        points=[]
        for n in range(81):
            u=n/80;y=145+(spring_top-145)*u
            points.append((140+7.5*math.sin(u*math.pi*10)*math.sin(math.pi*u)**.35,y))
        c.art.line(dd,points,c.INK,3.1);c.art.line(dd,points,c.LILAC,1.8)
    duck.paste((0,0,0,0),c.art.xy((0,184,280,240)))
    overlay(im,duck)
    # A single foreground pot permanently hides the spring and duck below its rim.
    polygon(d,[(99+wiggle,143),(181+wiggle,143),(171+wiggle,187),(109+wiggle,187)],'#c98267',c.INK,1.5)
    polygon(d,[(103+wiggle,148),(116+wiggle,148),(123+wiggle,183),(112+wiggle,183)],'#e0a083')
    c.art.rounded(d,(94+wiggle,135,186+wiggle,151),4,'#df9a78',c.INK,1.5)
    c.art.ellipse(d,(99+wiggle,136,181+wiggle,146),'#70504c',c.INK,1)
    # Hide only the rim's back half under the emerging object, preserving its source.
    if rise>0:
        top=Image.new('RGBA',im.size)
        c.deform_stamp(top,prop('rubber-duck',76,58),140,duck_y,1+.035*pulse(t,1.45,.4),1-.035*pulse(t,1.45,.4),-4*min(1,rise)-settle)
        top.paste((0,0,0,0),c.art.xy((0,143,280,240)))
        overlay(im,top)
    c.art.rounded(d,(128+wiggle,158,152+wiggle,179),4,c.CREAM)
    c.label(d,'?',140+wiggle,169,19,c.INK)
    if t>=1.3:
        a=t-1.3
        for vx,vy in [(-48,-31),(45,-38),(-62,-14),(62,-18)]:
            land=(-vy+math.sqrt(vy*vy+280*48))/140
            age=min(a,land);x=140+vx*age;y=139+vy*age+70*age*age
            c.art.ellipse(d,(x-1.6,y-1.4,x+1.6,y+1.4),'#ac7a62')
    if t>=2.8:
        for x,y in [(85,95),(205,103)]:sparkle(d,x,y,3.5,t,c.phase(t,2.8,3.1))


def oktoberfest(im,d,t):
    first=c.phase(t,.5,1.15);second=c.phase(t,.75,1.35)
    toast=c.phase(t,1.5,1.9)*(1-c.phase(t,1.98,2.3))
    contact=pulse(t,1.9,.3)
    for target in (95,185):c.art.ellipse(d,(target-35,185,target+35,190),'#392c3c')
    for n,(travel,target,entry) in enumerate([(first,95,20),(second,185,260)]):
        side=1 if n==0 else -1
        x=entry+(target-entry)*travel+side*7*toast
        hop=34*math.sin(math.pi*travel)+14*toast+9*pulse(t,2.45+n*.25,.37)
        squash=.055*contact
        anchored(im,prop('warm-pretzel',83,77),x,188-hop,1+squash,1-squash,side*(-5+14*toast))
    if t>=1.9:
        fade=1-c.phase(t,2.1,2.4)
        sparkle(d,140,123,7,t,fade)
    if t>=3.1:
        u=c.phase(t,3.1,3.35)
        for x,y in [(73,87),(207,88)]:sparkle(d,x,y,4,t,u)


def rangoli(im,t):
    layer=Image.new('RGBA',im.size);d=ImageDraw.Draw(layer)
    for n in range(8):
        a=n*math.tau/8;u=c.spring(t,1.3+(n%4)*.12,2.3+(n%4)*.06,.75)
        if u<=0:continue
        radius=27+38*min(1,u);cx=140+math.cos(a)*radius;cy=155+math.sin(a)*radius*.42
        def point(rad,tan):
            return (cx+(rad*math.cos(a)-tan*math.sin(a))*u,cy+(rad*math.sin(a)+tan*math.cos(a))*.42*u)
        color=[c.CORAL,c.SAGE,c.LILAC,c.GOLD][n%4]
        polygon(d,[point(-14,0),point(0,-10),point(14,0),point(0,10)],color)
        polygon(d,[point(-7,0),point(0,-4.5),point(7,0),point(0,4.5)],c.CREAM)
    dots=c.phase(t,2.15,2.6)
    if dots>0:
        for n in range(24):
            a=n*math.tau/24;x=140+92*math.cos(a);y=155+38*math.sin(a)
            r=1.45*dots;c.art.ellipse(d,(x-r,y-r*.7,x+r,y+r*.7),c.GOLD if n%2 else c.CORAL)
    overlay(im,layer,.9)


def diwali(im,d,t):
    amount=c.phase(t,.75,1.1)
    rangoli(im,t)
    glow(im,181,124,31,.7*amount)
    # Original small clay bowl, oil surface, and one cotton wick.
    polygon(d,[(96,142),(105,164),(123,176),(148,179),(170,169),(185,144)],'#bc775f',c.INK,1.5)
    polygon(d,[(106,150),(118,168),(143,174),(135,158)],'#d99172')
    c.art.ellipse(d,(94,130,188,156),'#e2a17b',c.INK,1.5)
    c.art.ellipse(d,(101,134,182,151),'#79504a',c.INK,1)
    c.art.line(d,[(110,139),(135,136),(158,139)],'#e5bd83',1.3)
    c.art.line(d,[(172,143),(182,134),(184,128)],c.CREAM,3.5)
    c.art.line(d,[(184,130),(184,126)],c.INK,1.2)
    flame(d,184,127,t,amount,1.6)
    if t>=2.85:
        for x,y in [(90,105),(212,118)]:sparkle(d,x,y,3,t,c.phase(t,2.85,3.2),c.GOLD)


def bezier(points,u):
    a,b,cc,d=points;v=1-u
    return tuple(v**3*a[k]+3*v*v*u*b[k]+3*v*u*u*cc[k]+u**3*d[k] for k in (0,1))


def lit_count(t):
    return (1 if t>=.5 else 0)+sum(t>=at for at in HANUKKAH_TIMES)


def helper_light(t):
    if t<.7 or t>=2.95:return None
    stops=[(.7,140,81)]+[(at,x,107) for at,x in zip(HANUKKAH_TIMES,HANUKKAH_X)]+[(2.95,140,81)]
    for (ta,xa,ya),(tb,xb,yb) in zip(stops,stops[1:]):
        if ta<=t<=tb:
            u=c.phase(t,ta,tb)
            return xa+(xb-xa)*u,ya+(yb-ya)*u-8*math.sin(math.pi*u)
    return None


def hanukkah(im,d,t):
    # Eight equal-height holders in one straight row, plus a separate raised shamash.
    for x in HANUKKAH_X:
        points=[bezier([(140,178),(x,178),(x,169),(x,150)],j/24) for j in range(25)]
        c.art.line(d,points,c.INK,5.5);c.art.line(d,points,c.GOLD,3.1)
    c.art.line(d,[(140,128),(140,183)],c.INK,6)
    c.art.line(d,[(140,128),(140,183)],c.GOLD,3.7)
    c.art.rounded(d,(116,181,164,189),4,c.GOLD,c.INK,1.3)
    c.art.line(d,[(122,183),(158,183)],c.CREAM,1)
    for n,x in enumerate(HANUKKAH_X):
        c.art.rounded(d,(x-4,110,x+4,150),1,c.CREAM,c.INK,.8)
        c.art.line(d,[(x+2,113),(x+2,147)],'#e5bda5',1.2)
        c.art.rounded(d,(x-6,148,x+6,154),2,c.GOLD,c.INK,1)
        c.art.line(d,[(x,110),(x,107)],c.INK,1)
        amount=c.phase(t,HANUKKAH_TIMES[n],HANUKKAH_TIMES[n]+.14)
        if amount>0:
            glow(im,x,101,12,.42*amount)
            flame(d,x,107,t,amount)
    c.art.rounded(d,(136,84,144,129),1,c.CORAL,c.INK,.8)
    c.art.line(d,[(142,87),(142,125)],'#ffd4b0',1)
    c.art.rounded(d,(134,127,146,133),2,c.GOLD,c.INK,1)
    c.art.line(d,[(140,84),(140,81)],c.INK,1)
    lit=c.phase(t,.5,.72)
    glow(im,140,75,15,.5*lit);flame(d,140,81,t,lit)
    shared=helper_light(t)
    if shared:
        x,y=shared;trail=Image.new('RGBA',im.size);td=ImageDraw.Draw(trail)
        points=[]
        for n in range(7):
            pos=helper_light(max(.7,t-.012*n))
            if pos:points.append(pos)
        if len(points)>1:c.art.line(td,points,c.GOLD,1.1)
        c.art.ellipse(td,(x-1.8,y-1.8,x+1.8,y+1.8),c.CREAM)
        overlay(im,trail,1-c.phase(t,2.72,2.95))
    if t>=2.95:
        for x,y in [(69,77),(212,76)]:sparkle(d,x,y,2.7,t,c.phase(t,2.95,3.2))


SCENES={'season-april-fools':april_fools,'season-oktoberfest':oktoberfest,'season-diwali':diwali,'season-hanukkah':hanukkah}


def scene(name,t):
    im=c.stage(CLIPS[name]);d=ImageDraw.Draw(im)
    c.art.ellipse(d,(53,182,227,191),'#392c3c')
    SCENES[name](im,d,t)
    ready,before,size=REVEALS[name]
    c.reveal_caption(im,CLIPS[name],t,ready,before,size=size)
    return im


def audio_events(name):
    def e(at,kind,hz,duration=.2,gain=.12):return dict(at=at,kind=kind,hz=hz,duration=duration,gain=gain)
    return {
      'season-april-fools':[e(.55,'rustle',400,.35,.08),e(1.3,'pop',330,.13,.12),e(1.45,'pluck',[460,720],.35,.14),e(2.15,'pluck',590,.2,.1),e(2.8,'chord',[523,659,784],.55,.13)],
      'season-oktoberfest':[e(.5,'whoosh',480,.25,.075),e(.75,'whoosh',570,.25,.075),e(1.35,'thud',270,.15,.08),e(1.9,'chord',[659,784,988],.35,.13),e(2.45,'pluck',659,.2,.11),e(2.7,'pluck',784,.2,.11),e(3.1,'chord',[523,659,784],.5,.13)],
      'season-diwali':[e(.75,'chime',784,.45,.1),e(1.3,'pluck',523,.25,.1),e(1.7,'pluck',659,.3,.1),e(2.15,'sparkle',988,.35,.085),e(2.85,'chord',[523,659,784],.65,.13)],
      'season-hanukkah':[e(.5,'chime',659,.35,.1)]+[e(at,'pluck',hz,.18,.085) for at,hz in zip(HANUKKAH_TIMES,(523,587,659,698,784,880,988,1047))]+[e(2.95,'chord',[523,659,784],.65,.13)],
    }[name]
