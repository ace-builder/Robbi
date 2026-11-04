"""Twelve miniature object stories for December 13–24; no Robbi face."""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
import seasonal_common as c

SOURCE = c.source_root / 'advent-late'
A=c.art
S=c.S


def entry(day,title,quote,cover,when,sound,cues):
    return dict(title=title,heading=f'ADVENT {day}',heading_size=25,quote=quote,
                cover=cover,when=when,sound=sound,cues=cues,group='advent',day=day)


CLIPS={
 'advent-13':entry(13,'The biscuit bedtime','REST COUNTS AS MAGIC.',['REST COUNTS','AS MAGIC.'],
    'A sleepy gingerbread biscuit finds a matchbox bed and gets tucked in.',
    'A sliding matchbox, a soft pillow landing, a blanket rustle and a sleepy two-note hum.',
    [(.45,'matchbox opens'),(1.45,'biscuit lands on pillow'),(1.95,'blanket tucks in'),(2.85,'sleepy hum')]),
 'advent-14':entry(14,'The penguin pirouette','A LITTLE WINTER WIGGLE.',['A LITTLE','WINTER WIGGLE.'],
    'One tiny penguin turns a careful skate into an unexpectedly graceful spin.',
    'A soft skating swish, a rising spin chime and a pleased little ice bell.',
    [(.45,'skates glide'),(1.55,'tiny pirouette'),(2.65,'a proud landing')]),
 'advent-15':entry(15,'The helpful ribbon','TIED WITH LOVE.',['TIED WITH','LOVE.'],
    'A rather independent ribbon wraps its own present and ties a generous bow.',
    'A silky ribbon rustle, two soft loop pops and a golden knot chime.',
    [(.5,'ribbon wraps'),(1.35,'first bow loop'),(1.85,'second bow loop'),(2.65,'knot settles')]),
 'advent-16':entry(16,'A little world settles','LET IT SETTLE.',['LET IT','SETTLE.'],
    'A snow globe shakes out its worries, then watches every last snowflake settle.',
    'A glass tick, a gentle snow shuffle and a calm descending glass chime.',
    [(.5,'globe shakes'),(1.25,'snow dances'),(3.15,'little world grows quiet')]),
 'advent-17':entry(17,'The paper tree encore','ONE TINY ENCORE.',['ONE TINY','ENCORE.'],
    'A hand-cranked music box gives its paper tree a very small stage.',
    'Crank ticks, an original four-note music-box phrase, and a final warm bell.',
    [(.45,'crank turns'),(1,'paper tree dances'),(2.7,'one tiny bow')]),
 'advent-18':entry(18,'A lantern for a star','KEEP A LITTLE WONDER.',['KEEP A LITTLE','WONDER.'],
    'A wandering star finds the open door of a lantern and makes itself at home.',
    'A wandering star whistle, a lantern-door tick and a warm sparkling welcome.',
    [(.45,'star wanders'),(1.8,'star finds the lantern'),(2.6,'lantern glows')]),
 'advent-19':entry(19,'Pinecone party dress','FESTIVE ENOUGH.',['FESTIVE','ENOUGH.'],
    'A little pinecone receives a slightly oversized party hat. It is delighted.',
    'A falling felt swish, one soft hat plop and a happy three-note jingle.',
    [(.55,'hat drops'),(1.65,'hat lands'),(2.4,'festive little wobble')]),
 'advent-20':entry(20,'A heart with a sled','SMALL SLED. BIG LOVE.',['SMALL SLED.','BIG LOVE.'],
    'A tiny wooden sled somehow delivers a heart much larger than its cargo space.',
    'A wooden snow glide, a soft braking bump and a generous heart chime.',
    [(.45,'delivery glides in'),(1.8,'loaded sled suspension settles'),(2.45,'delivery complete')]),
 'advent-21':entry(21,'Reindeer snack break','SNACKS ARE IMPORTANT.',['SNACKS ARE','IMPORTANT.'],
    'A tiny reindeer drops its carrot, retrieves it, and declares a snack break.',
    'A carrot tumble, a soft snowy plop, a happy nibble and a satisfied little hum.',
    [(.6,'carrot tumbles'),(1.3,'carrot lands'),(2.35,'snack recovered'),(2.85,'happy snack break')]),
 'advent-22':entry(22,'Socks by the fireside','A LITTLE EXTRA WARMTH.',['A LITTLE','EXTRA WARMTH.'],
    'Two hanging socks sway into place above a small glowing fireplace.',
    'Two cloth swishes, tiny warm fire ticks, and a soft rising hearth hum.',
    [(.5,'first sock hangs'),(1.15,'second sock joins'),(2,'hearth glows'),(2.8,'warm little heart')]),
 'advent-23':entry(23,'The moon does tuck-in','SLEEP WELL, LITTLE WORLD.',['SLEEP WELL,','LITTLE WORLD.'],
    'The moon pulls a cloud blanket over a sleepy snowy village and turns down the stars.',
    'A soft night breeze, three window-light ticks and a sleepy descending lullaby chime.',
    [(.45,'village windows glow'),(1.3,'cloud blanket drifts in'),(3.1,'the last window dims')]),
 'advent-24':entry(24,'The Christmas Eve theatre','LIGHT FOR EVERYONE.',['A LITTLE LIGHT','FOR EVERYONE.'],
    'Paper theatre curtains open to a tree whose lights spread all the way to the audience.',
    'Velvet curtain rustles, an original rising light melody and a warm Christmas Eve chord.',
    [(.45,'curtains open'),(1.3,'tree lights climb'),(2.4,'star lights up'),(3.05,'light reaches everyone')]),
}


def poly(d,points,fill):
    d.polygon([A.xy(p) for p in points],fill=fill)


def curve(d,pts,color,width=2):
    A.line(d,pts,color,width)


def bezier(p0,p1,p2,p3,n=40):
    return [tuple((1-u)**3*p0[k]+3*(1-u)**2*u*p1[k]+3*(1-u)*u*u*p2[k]+u**3*p3[k] for k in (0,1)) for u in [i/n for i in range(n+1)]]


def floor(d):
    A.ellipse(d,(50,181,230,191),'#392c3c')


def tree(d,x,y,h=77,w=61,color=c.SAGE):
    A.rounded(d,(x-4,y-6,x+4,y+11),2,'#b78772')
    for frac,half in [(.3,.5),(.58,.4),(.8,.28)]:
        top=y-h*frac-h*.25
        poly(d,[(x,top),(x-w*half,y-h*(frac-.23)),(x+w*half,y-h*(frac-.23))],color)
    curve(d,[(x,y-h),(x,y-9)],'#8ba58b',1)


def twinkle(d,x,y,r=4,color=c.GOLD):
    A.star(d,x,y,r,color)
    if r>4:
        A.line(d,[(x-r-3,y),(x-r-1,y)],color,1)
        A.line(d,[(x+r+1,y),(x+r+3,y)],color,1)


@lru_cache(None)
def sprite(name,w,h):
    return c.prop(str(SOURCE/(name+'.png')),w,h)


def note(d,x,y,color=c.GOLD):
    A.ellipse(d,(x-3,y-2,x+3,y+3),color)
    A.line(d,[(x+2,y),(x+2,y-13),(x+8,y-10)],color,1.5)


def caption(d,name,t,until,early):
    if t>=until:c.pill(d,CLIPS[name]['quote'],size=18)
    else:c.text(d,early,216,16,c.LILAC)


def smooth(t,a,b):
    u=max(0,min(1,(t-a)/(b-a)))
    return u*u*(3-2*u)


def settle(t,start,amplitude=1,speed=17,decay=7):
    q=max(0,t-start)
    return amplitude*math.sin(q*speed)*math.exp(-q*decay) if t>=start else 0


def tint(a,b,p):
    a=tuple(int(a[i:i+2],16) for i in (1,3,5));b=tuple(int(b[i:i+2],16) for i in (1,3,5))
    return tuple(round(x+(y-x)*max(0,min(1,p))) for x,y in zip(a,b))


@lru_cache(None)
def glow_sprite(radius,color):
    r=round(radius*S);im=Image.new('RGBA',(r*2+12*S,r*2+12*S))
    d=ImageDraw.Draw(im);rgb=tuple(int(color[i:i+2],16) for i in (1,3,5));cx=im.width/2
    for n in range(16,0,-1):
        rr=r*n/16
        d.ellipse((cx-rr,cx-rr,cx+rr,cx+rr),fill=rgb+(round(65*(1-n/17)**1.5),))
    return im.filter(ImageFilter.GaussianBlur(2*S))


def glow(im,x,y,r=24,color=c.GOLD,amount=1):
    if amount<=0:return
    p=glow_sprite(r,color)
    if amount<1:
        p=p.copy();p.putalpha(p.getchannel('A').point(lambda a:round(a*amount)))
    A.stamp(im,p,x,y)


def anchored(im,p,x,y,anchor=(.5,1),sx=1,sy=1,angle=0):
    """Deform a fixed prop while keeping its contact/pivot in native coordinates."""
    if sx<0:p=p.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    p=p.resize((max(1,round(p.width*abs(sx))),max(1,round(p.height*sy))),Image.Resampling.BICUBIC)
    dx=(anchor[0]-.5)*p.width/S;dy=(anchor[1]-.5)*p.height/S
    a=math.radians(angle)
    A.stamp(im,p,x-dx*math.cos(a)-dy*math.sin(a),y+dx*math.sin(a)-dy*math.cos(a),angle)


@lru_cache(None)
def paper_tree():
    p=Image.new('RGBA',(90*S,92*S));pd=ImageDraw.Draw(p)
    tree(pd,45,79,68,64)
    poly(pd,[(45,9),(45,75),(59,71),(49,55),(66,55),(51,35),(58,35)],'#a8bf94')
    twinkle(pd,45,12,5)
    return p


@lru_cache(None)
def party_hat():
    hat=Image.new('RGBA',(73*S,58*S));hd=ImageDraw.Draw(hat)
    poly(hd,[(10,50),(36,3),(64,50)],c.CORAL)
    poly(hd,[(36,3),(53,49),(64,50)],'#dc8279')
    A.rounded(hd,(7,45,66,55),5,c.CREAM)
    A.ellipse(hd,(30,0,42,12),c.CREAM);twinkle(hd,36,31,6,c.GOLD)
    return hat


def scene(name,t):
    base=c.stage(CLIPS[name]);im=Image.new('RGBA',base.size);d=ImageDraw.Draw(im)
    floor(d);day=CLIPS[name]['day'];ready=2.8;before='a little winter magic.'
    if day==13:
        opened=smooth(t,.45,1.1);land=smooth(t,.9,1.45);tuck=smooth(t,1.95,2.65)
        bed_y=settle(t,1.45,2.4)
        A.rounded(d,(56-12*opened,131+bed_y,205-12*opened,186+bed_y),5,'#b86f61',c.INK,1.5)
        A.rounded(d,(66,145+bed_y,221,185+bed_y),4,c.GOLD,c.INK,1.5)
        A.rounded(d,(72,144+bed_y,215,174+bed_y),3,'#b58c80')
        pillow_squash=1-.13*math.exp(-max(0,t-1.45)*7) if t>=1.45 else 1
        A.rounded(d,(75,162-24*pillow_squash+bed_y,116,166+bed_y),7,c.CREAM)
        breathe=math.sin((t-2.7)*2.6)*.45*smooth(t,2.7,3.3)
        A.stamp(im,sprite('sleepy-gingerbread',137,74),141,102+37*land+bed_y+breathe,angle=-5*(1-land))
        # The blanket begins as a folded strip resting at the foot of the bed.
        left=199-82*tuck;top=159-24*tuck+bed_y
        A.rounded(d,(left,top,214,173+bed_y),5,c.SAGE,c.INK,1)
        A.line(d,[(left+6,top+4),(left+6,170+bed_y)],c.CREAM,2)
        for k in range(3):
            yy=top+5+k*(168+bed_y-top)/3
            A.line(d,[(left+11,yy),(209,yy)],'#a0b887',.8)
        if t>=2.85:
            for j in range(2):
                q=smooth(t,2.85+j*.22,3.2+j*.22)
                c.label(d,'z',78-9*j,105-15*j-4*q,12+3*j,tint(c.BG,c.LILAC,q))
        ready=2.75;before='even biscuits need a bedtime.'
    elif day==14:
        glide=smooth(t,.45,1.45);turn=smooth(t,1.55,2.55)
        x=57+94*glide+8*math.sin(math.pi*turn)
        A.ellipse(d,(27,158,253,193),'#9db5ba');A.ellipse(d,(33,162,247,189),'#c7d9d3')
        for yy in (173,182):A.line(d,[(45,yy),(232,yy-5)],c.CREAM,.7)
        curve(d,bezier((42,181),(85,189),(113,173),(x,178)), '#78959c',1)
        if turn>0:A.ellipse(d,(x-23,173,x+23,184),None,'#9db5ba',1)
        spin=math.cos(turn*math.tau)
        sx=(.42+.58*abs(spin))*(1 if spin>=0 else -1)
        hop=5*math.sin(math.pi*smooth(t,2.15,2.65)) if 2.15<t<2.65 else 0
        anticipation=2*math.sin(math.pi*smooth(t,1.3,1.55)) if 1.3<t<1.55 else 0
        squash=settle(t,2.65,.08,20,8)
        anchored(im,sprite('skating-penguin',91,112),x,178-hop,sx=sx*(1+squash),sy=1-squash-.035*anticipation,angle=3*math.sin(turn*math.tau))
        if t>=2.65:
            for xx,yy in [(101,94),(206,103),(217,151)]:twinkle(d,xx,yy,4*smooth(t,2.65,2.95))
        ready=2.85;before='a very serious skating lesson.'
    elif day==15:
        wrap=smooth(t,.5,1.35);tie=smooth(t,2.4,2.75)
        A.rounded(d,(84,112,196,181),5,c.SAGE,c.INK,1.5)
        A.rounded(d,(79,106,201,122),3,'#d5dfb9',c.INK,1)
        for xx in range(96,194,22):twinkle(d,xx,151 if xx%3 else 171,2,'#92aa84')
        if wrap>0:
            A.line(d,[(140,181),(140,181-72*wrap)],c.CORAL,8)
            A.line(d,[(85,144),(85+111*wrap,144)],c.CORAL,8)
        # Trace full, non-degenerate paths with two long free ends; pull into the knot.
        for sign,begin in [(-1,1.35),(1,1.85)]:
            p=smooth(t,begin,begin+.62)
            pts=bezier((140,109),(140+sign*68,63),(140+sign*66,120),(140,109),60)
            count=round(p*(len(pts)-1))+1
            if count>1:curve(d,pts[:count],c.CORAL,5)
            tail=smooth(t,begin+.32,begin+.82)
            if tail>0:
                tail_pts=bezier((140,110),(140+sign*7,119),(140+sign*19,143),(140+sign*(26-3*tie),138))
                tail_count=round(tail*(len(tail_pts)-1))+1
                if tail_count>1:curve(d,tail_pts[:tail_count],c.CORAL,4)
        if tie>0:
            A.ellipse(d,(140-7*tie,109-6*tie,140+7*tie,109+6*tie),c.CORAL)
            A.line(d,[(137,105),(142,113)],'#ffd0b2',1)
        for xx,yy in [(65,94),(219,128),(207,77)]:
            if t>=2.75:twinkle(d,xx,yy,4*smooth(t,2.75,3))
        ready=2.85;before='this ribbon knows what to do.'
    elif day==16:
        dx=4*math.sin((t-.5)*24)*(1-smooth(t,.5,1.25)) if t>.5 else 0;cx=140+dx
        A.ellipse(d,(cx-61,54,cx+61,176),'#47434e',c.LILAC,1.5)
        A.ellipse(d,(cx-47,149,cx+47,168),c.CREAM)
        tree(d,cx,154,65,55);twinkle(d,cx,86,5)
        snow=Image.new('RGBA',im.size);sd=ImageDraw.Draw(snow)
        for n in range(43):
            q=smooth(t,1.05+(n%7)*.045,2.65+(n%11)*.07)
            angle=n*2.399;radius=12+(n*17)%40
            xx=math.cos(angle)*radius;sy=115+math.sin(angle)*radius
            swirl=(1-q)*math.sin(t*5+n)*5
            ex=xx*.92;ey=151+(n*7)%12-3*(xx/48)**2
            yy=sy*(1-q)+ey*q
            px=cx+xx*(1-q)+ex*q+swirl
            rr=.65+(n%3)*.18
            A.ellipse(sd,(px-rr,yy-rr,px+rr,yy+rr),c.CREAM)
        mask=Image.new('L',im.size);md=ImageDraw.Draw(mask);A.ellipse(md,(cx-56,59,cx+56,171),255)
        snow.putalpha(Image.composite(snow.getchannel('A'),Image.new('L',im.size),mask));im.alpha_composite(snow)
        curve(d,bezier((cx-44,89),(cx-40,70),(cx-27,65),(cx-21,64)),c.CREAM,3)
        A.rounded(d,(cx-51,166,cx+51,188),7,'#a8756e',c.INK,1)
        A.rounded(d,(cx-37,172,cx+37,183),4,c.GOLD);c.label(d,'peace, eventually.',cx,177,11,c.INK)
        ready=3.2;before='a little swirl. a little stillness.'
    elif day==17:
        turn=smooth(t,.45,2.75);a=turn*math.tau*1.5
        A.rounded(d,(75,147,205,186),5,'#b68478',c.INK,1)
        poly(d,[(75,147),(95,135),(224,135),(205,147)],'#dbb092')
        poly(d,[(205,147),(224,135),(224,174),(205,186)],'#8f6264')
        A.rounded(d,(94,158,185,176),3,c.CREAM);c.label(d,'tiny encore',140,167,13,c.INK)
        crankx=238+4*math.cos(a*2);cranky=154+7*math.sin(a*2)
        curve(d,[(224,154),(233,154),(crankx,cranky),(crankx+9,cranky)],c.GOLD,2)
        A.ellipse(d,(crankx+6,cranky-3,crankx+14,cranky+3),c.CORAL)
        A.ellipse(d,(126,130,172,140),'#e4c4a4')
        # A fixed folded-paper tree gently sways on its spindle; its colour stays constant.
        bow=5*math.sin(math.pi*smooth(t,2.75,3.2)) if 2.75<t<3.2 else 0
        anchored(im,paper_tree(),149,140,anchor=(.5,.99),sx=.76+.24*math.cos(a)**2,angle=6*math.sin(a)+bow)
        for n in range(3):
            if t>=1+n*.32:
                q=smooth(t,1+n*.32,1.3+n*.32);note(d,83+n*48,112-18*q-2*math.sin(t*2+n),tint(c.BG,c.GOLD if n%2 else c.LILAC,q))
        ready=3.2;before='a whole concert. pocket-sized.'
    elif day==18:
        fly=smooth(t,.45,2.1);warm=smooth(t,2.1,2.85)
        glow(im,181,133,72,c.GOLD,warm)
        A.rounded(d,(147,95,215,174),5,'#a37164',c.INK,1.5)
        A.rounded(d,(154,101,208,166),3,tint('#594047','#ac7656',warm))
        poly(d,[(141,95),(181,74),(221,95)],c.GOLD)
        A.ellipse(d,(172,62,190,80),None,c.GOLD,2)
        A.line(d,[(151,117),(210,117)],'#cda17d',2);A.line(d,[(180,99),(180,168)],'#d7aa7e',2)
        A.rounded(d,(143,170,219,180),3,c.GOLD)
        x=45+136*fly;y=92+40*fly-30*math.sin(math.pi*fly)+settle(t,2.1,2,11,5)
        for j in range(5):
            if fly<1:
                q=max(0,fly-j*.028);tx=45+136*q;ty=92+40*q-30*math.sin(math.pi*q)
                A.ellipse(d,(tx-1,ty-1,tx+1,ty+1),tint(c.BG,c.GOLD,1-j*.15))
        glow(im,x,y,23,c.GOLD,.65);twinkle(d,x,y,9+warm,c.GOLD)
        twinkle(d,x-2,y-2,3,c.CREAM)
        for xx,yy in [(131,94),(229,117),(129,160)]:
            twinkle(d,xx,yy,4*warm)
        ready=2.9;before='a place for a wandering star.'
    elif day==19:
        drop=smooth(t,.55,1.65);wobble=settle(t,1.65,6.5,15,4.8);squash=settle(t,1.65,.055,18,7)
        ensemble=Image.new('RGBA',im.size)
        anchored(ensemble,sprite('festive-pinecone',84,104),140,190,sx=1+squash,sy=1-squash,angle=wobble)
        if t<1.65:
            anchored(ensemble,party_hat(),146-6*drop,58+51*drop,anchor=(.5,.92),angle=-17*(1-drop))
        else:
            # The brim shares the pinecone's contact-pivot rotation after landing.
            a=math.radians(wobble);oy=-81*(1-squash)
            anchored(ensemble,party_hat(),140+oy*math.sin(a),190+oy*math.cos(a),anchor=(.5,.92),angle=wobble,sx=1+squash,sy=1-squash)
        im.alpha_composite(ensemble)
        for xx,yy in [(75,117),(206,98),(211,155)]:
            if t>=2.4:twinkle(d,xx,yy,4*smooth(t,2.4,2.7))
        ready=2.75;before='formalwear for forest friends.'
    elif day==20:
        travel=smooth(t,.45,1.8);x=17+130*travel;spring=settle(t,1.8,4,15,4.5)
        A.line(d,[(22,186),(252,186)],c.CREAM,1)
        for xx in range(26,260,29):A.ellipse(d,(xx,190,xx+2,192),c.LILAC)
        # Runners stay planted; the loaded deck and heart share a small suspension settle.
        curve(d,[(x-38,175),(x-35,186),(x+34,186),(x+45,179)],'#b18477',3)
        deck=162+spring
        for xx in (-29,17):curve(d,[(x+xx,deck+6),(x+xx+2,178),(x+xx,184)],'#b18477',3)
        A.rounded(d,(x-43,deck,x+35,deck+7),3,c.GOLD)
        curve(d,[(x-40,deck),(x-39,deck+17),(x+33,deck+17),(x+43,deck+11)],c.GOLD,2)
        heart_y=deck-43
        A.heart(d,x-3,heart_y,44,c.CORAL);A.heart(d,x-17,heart_y-12,8,'#ffd2b2')
        # The package is secured to its deck with a fine golden ribbon.
        curve(d,[(x-22,deck),(x-3,deck-7),(x+18,deck)],'#e7ac78',1.2)
        curve(d,[(x+35,deck+3),(x+61,deck-2),(x+74,deck+2)],c.CREAM,1.3)
        if 1.7<t<2.4:
            q=smooth(t,1.7,2.4)
            for n in range(5):A.ellipse(d,(x-41-n*5-12*q,181-9*math.sin(q*math.pi+n*.1),x-39-n*5-12*q,183-9*math.sin(q*math.pi+n*.1)),c.CREAM)
        if t>=2.45:twinkle(d,x+60,108,5*smooth(t,2.45,2.75))
        ready=2.8;before='the delivery is mostly heart.'
    elif day==21:
        fall=smooth(t,.6,1.3);recover=smooth(t,1.65,2.35)
        bend=6*math.sin(recover*math.pi);munch=smooth(t,2.5,3.3)
        nod=math.sin((t-2.5)*21)*.65*smooth(t,2.5,2.65)*(1-smooth(t,3.15,3.5)) if t>=2.5 else 0
        anchored(im,sprite('snack-reindeer',109,129),174,187,sy=1-.006*nod,angle=1.5*math.sin(recover*math.pi)+nod)
        # The tip ends at the sprite's fixed muzzle; successive bites shorten the free end.
        mouth=(147,124+nod*.5)
        cx=mouth[0]-56*fall+56*recover;cy=mouth[1]+30*fall-30*recover+bend
        carrot=Image.new('RGBA',(42*S,27*S));cd=ImageDraw.Draw(carrot)
        shorten=8*munch
        poly(cd,[(5+shorten,9),(35,10),(10+shorten,23)],'#f4a16c')
        curve(cd,[(10+shorten,11),(3+shorten,2)],c.SAGE,2.3);curve(cd,[(10+shorten,11),(10+shorten,0)],c.SAGE,2.3)
        curve(cd,[(20+shorten*.4,14),(23+shorten*.4,16)],'#b86f61',1)
        # Rightmost carrot tip is registered to the mouth once retrieved.
        anchored(im,carrot,cx,cy,anchor=(35/42,10/27),angle=-8*(1-fall)+100*fall*(1-recover)+nod)
        if t>=2.6:
            for n in range(3):
                q=smooth(t,2.6+n*.12,2.95+n*.12)
                if 0<q<1:A.ellipse(d,(140-9*q+n*3,128+14*q,141-9*q+n*3,129+14*q),'#f4a16c')
        if t>=2.85:A.heart(d,110,98-3*smooth(t,2.85,3.3),7*smooth(t,2.85,3.1),c.CORAL)
        ready=3.25;before='important reindeer business.'
    elif day==22:
        warm=smooth(t,1.8,2.8);glow(im,140,163,71,c.GOLD,.3+.7*warm)
        A.rounded(d,(72,105,208,188),3,'#9f756f',c.INK,1)
        for yy in (122,142,162,182):
            A.line(d,[(74,yy),(206,yy)],'#b98a7b',1)
            for xx in range(85+(10 if yy%4 else 0),207,26):A.line(d,[(xx,yy-17),(xx,yy)],'#b98a7b',1)
        A.rounded(d,(108,134,173,188),12,'#3a2931');glow(im,141,166,34,c.GOLD,.5+.5*warm)
        for n in range(3):
            x=122+n*13;hh=18+11*warm+2.5*math.sin(t*7+n*1.8)
            pts=bezier((x-7,181),(x-10,170),(x+4,169-hh),(x,181-hh),15)+bezier((x,181-hh),(x+9,166),(x+9,176),(x+7,181),15)
            poly(d,pts,c.CORAL if n%2==0 else c.GOLD)
        A.line(d,[(116,183),(166,178)],'#a87b68',4)
        A.rounded(d,(63,100,217,110),3,c.CREAM)
        for n,x in enumerate((106,172)):
            arrival=smooth(t,.5+n*.65,1+n*.65);angle=settle(t,1+n*.65,9,8,3.4)
            sock=Image.new('RGBA',(43*S,53*S));sd=ImageDraw.Draw(sock)
            A.rounded(sd,(9,3,30,43),5,c.CORAL if n==0 else c.SAGE);A.rounded(sd,(10,31,42,48),8,c.CORAL if n==0 else c.SAGE)
            A.rounded(sd,(7,2,32,13),3,c.CREAM);twinkle(sd,20,26,4,c.CREAM)
            # Upper cuff pivots on a visible hook rather than translating while swinging.
            A.ellipse(d,(x-2,96,x+2,100),c.GOLD)
            anchored(im,sock,x,98-27*(1-arrival),anchor=(20/43,4/53),angle=angle-8*(1-arrival))
        if t>=2.8:A.heart(d,140,124,5*smooth(t,2.8,3.1),c.GOLD)
        ready=3.1;before='a toastier sort of together.'
    elif day==23:
        drift=smooth(t,1.3,2.9)
        for xx,yy in [(38,73),(113,64),(182,76),(244,93)]:twinkle(d,xx,yy,2*(1-.45*drift),c.LILAC)
        glow(im,218,77,32,c.GOLD,.5)
        moon=Image.new('RGBA',im.size);moon_d=ImageDraw.Draw(moon)
        A.ellipse(moon_d,(200,57,239,96),c.GOLD);A.ellipse(moon_d,(214,51,246,85),(0,0,0,0));im.alpha_composite(moon)
        for n,(x,y,h) in enumerate([(60,153,29),(105,145,40),(150,157,27),(194,149,35)]):
            A.rounded(d,(x-18,y,x+18,y+h),2,'#9d7c82');poly(d,[(x-24,y),(x,y-20),(x+24,y)],c.CREAM)
            awake=smooth(t,.45+n*.12,.65+n*.12)*(1-smooth(t,2.25+n*.16,2.65+n*.16))
            glow(im,x,y+17,14,c.GOLD,awake*.6)
            A.rounded(d,(x-5,y+12,x+5,y+23),1,tint('#59414f',c.GOLD,awake));A.line(d,[(x,y+12),(x,y+23)],'#9d7c82',1)
        A.ellipse(d,(31,177,234,194),c.CREAM)
        cx=252-111*drift;cy=101+42*drift
        for dx,dy,rx,ry in [(-48,8,29,12),(-24,0,29,18),(5,1,30,20),(35,8,28,13)]:
            A.ellipse(d,(cx+dx-rx,cy+dy-ry,cx+dx+rx,cy+dy+ry),'#d4c4d7')
        curve(d,bezier((cx-65,cy+16),(cx-20,cy+26),(cx+37,cy+15),(cx+57,cy+17)),'#b2a2bf',1)
        if t>=2.9:
            q=smooth(t,2.9,3.3);c.label(d,'z',225,121-3*q,14,tint(c.BG,c.GOLD,q));c.label(d,'z',236,110-3*q,10,tint(c.BG,c.LILAC,q))
        ready=3.3;before='the moon takes the late shift.'
    elif day==24:
        open_=smooth(t,.45,1.35);spread=smooth(t,3.05,3.65)
        A.rounded(d,(35,57,245,190),5,'#b58674',c.INK,1);A.rounded(d,(44,65,236,182),2,'#352a3c')
        A.rounded(d,(80,169,200,179),3,'#bd9384');tree(d,140,159,83,78)
        for n,(xx,yy) in enumerate([(120,145),(153,143),(137,128),(158,119),(128,110),(143,96)]):
            q=smooth(t,1.3+n*.16,1.56+n*.16)
            glow(im,xx,yy,13,c.GOLD,q*.8)
            A.ellipse(d,(xx-2,yy-2,xx+2,yy+2),tint('#71886e',c.GOLD if n%2 else c.CORAL,q))
        star=smooth(t,2.4,2.75);glow(im,140,76,34,c.GOLD,star)
        twinkle(d,140,76,7,tint('#877867',c.GOLD,star))
        # Curved hems and continuous soft fold bands gather as the curtains open.
        width=96-82*open_;wave=2*math.sin(t*5)*(1-open_)
        for sign in (-1,1):
            outer=44 if sign==-1 else 236;inner=outer-sign*width
            pts=[(outer,65),(inner,65)]+bezier((inner,65),(inner+sign*wave,112),(inner+sign*5,151),(inner+sign*7,173),30)+bezier((inner+sign*7,173),(inner,177),(outer-sign*5,183),(outer,180),20)
            poly(d,pts,'#b9656c')
            for n in range(7):
                fx=outer-sign*width*(n+.5)/7
                curve(d,bezier((fx,66),(fx+sign*2,112),(fx+sign*3,148),(fx+sign*5,177),25),'#d58080' if n%2==0 else '#9c535f',max(.65,width/24))
        A.rounded(d,(30,53,250,66),4,c.GOLD)
        for n,xx in enumerate((128,152,105,175,80,200,55,225)):
            q=smooth(t,3.05+n*.055,3.25+n*.055)
            if q>0:glow(im,xx,191,11,c.GOLD,q);twinkle(d,xx,191,3*q,c.GOLD)
        for xx,yy in [(65,88),(213,97),(207,145)]:
            if star>0:twinkle(d,xx,yy,3*star)
        ready=3.33;before='the little theatre is ready.'
    base.alpha_composite(im.crop((0,48*S,280*S,198*S)),(0,48*S))
    if hasattr(c,'reveal_caption'):c.reveal_caption(base,CLIPS[name],t,ready,before,size=18)
    else:caption(ImageDraw.Draw(base),name,t,ready,before)
    return base


def event(at,kind,hz=660,duration=.3,gain=.13):
    return dict(at=at,kind=kind,hz=hz,duration=duration,gain=gain)


def audio_events(name):
    day=CLIPS[name]['day']
    return {
      13:[event(.45,'rustle',duration=.55,gain=.075),event(1.45,'thud',220,.21,.11),event(1.55,'pluck',330,.22,.05),event(1.95,'rustle',duration=.6,gain=.075),event(2.85,'hum',[392,330],.7,.1),event(3.12,'chord',[262,330,392],.55,.055)],
      14:[event(.45,'whoosh',duration=.65,gain=.075),event(1.55,'chime',[587,784,988],.7,.1),event(2.2,'whoosh',duration=.25,gain=.045),event(2.65,'tick',660,.12,.09),event(2.72,'chord',[587,784,1175],.6,.11)],
      15:[event(.5,'rustle',duration=.65,gain=.08),event(1.35,'pluck',660,.28,.09),event(1.85,'pluck',784,.28,.09),event(2.4,'rustle',duration=.22,gain=.045),event(2.65,'chord',[659,784,988],.65,.11)],
      16:[event(.5,'tick',1047,.12,.08),event(.65,'rustle',duration=.6,gain=.065),event(1.25,'jingle',[1175,988],.5,.065),event(2.15,'bell',784,.4,.045),event(3.15,'chord',[392,494,587],.65,.1)],
      17:[event(.45,'tick',440,.1,.075),event(.7,'tick',440,.1,.075),event(1,'pluck',659,.45,.11),event(1.35,'pluck',784,.45,.11),event(1.7,'pluck',988,.45,.11),event(2.05,'pluck',880,.45,.11),event(2.4,'pluck',784,.35,.08),event(2.7,'chord',[523,659,784],.7,.1)],
      18:[event(.45,'hum',[523,784],.8,.075),event(1.25,'bell',988,.4,.055),event(1.8,'tick',660,.13,.085),event(2.15,'hum',[392,523],.6,.055),event(2.6,'chord',[659,988,1319],.7,.11),event(3.1,'sparkle',1568,.4,.055)],
      19:[event(.55,'whoosh',duration=.55,gain=.065),event(1.65,'thud',330,.17,.1),event(1.82,'pluck',440,.2,.055),event(2.4,'jingle',[659,784,988],.65,.1),event(2.8,'chord',[523,659,784],.6,.07)],
      20:[event(.45,'whoosh',duration=.75,gain=.075),event(1.8,'thud',240,.2,.09),event(1.97,'pluck',330,.2,.06),event(2.17,'pluck',392,.2,.045),event(2.45,'chord',[523,659,784],.8,.11)],
      21:[event(.6,'rustle',duration=.35,gain=.075),event(1.3,'pop',330,.17,.09),event(1.7,'whoosh',duration=.35,gain=.045),event(2.35,'tick',660,.11,.08),event(2.58,'tick',784,.1,.06),event(2.85,'hum',[392,523],.65,.09),event(2.94,'tick',659,.1,.045),event(3.15,'chord',[392,494,587],.55,.055)],
      22:[event(.5,'rustle',duration=.4,gain=.065),event(1.15,'rustle',duration=.4,gain=.065),event(1.85,'tick',587,.12,.06),event(2,'tick',659,.12,.07),event(2.3,'tick',784,.1,.055),event(2.8,'chord',[330,440,523],.8,.1)],
      23:[event(.45,'tick',784,.1,.06),event(.62,'tick',659,.1,.06),event(.78,'tick',587,.1,.06),event(1.3,'whoosh',duration=.8,gain=.055),event(2.3,'pluck',659,.25,.055),event(2.55,'pluck',523,.25,.055),event(2.8,'pluck',440,.25,.05),event(3.1,'chord',[262,330,392],.7,.09)],
      24:[event(.45,'rustle',duration=.8,gain=.075),event(1.3,'pluck',523,.4,.08),event(1.62,'pluck',659,.4,.08),event(1.94,'pluck',784,.4,.08),event(2.4,'sparkle',1047,.6,.1),event(3.05,'chord',[523,659,784,1047],.8,.12),event(3.35,'sparkle',[1319,1568],.4,.055)],
    }[day]


def review():
    """Render review artifacts and validate all 1,080 frames; never export production assets."""
    import json
    import numpy as np
    out=Path(__file__).resolve().parents[2]/'test-results/seasonal-postcards-preview/polish-advent-late'
    out.mkdir(parents=True,exist_ok=True)
    notes={
      13:'Matchbox settles under the biscuit; pillow compresses; blanket unfolds from a strip resting on the bed; small breathing and sleepy-letter reveal.',
      14:'Upright horizontal perspective turn with mirrored orientation; skate soles remain on ice; shallow hop, anchored landing squash and ice-ring trace.',
      15:'Ribbon draws along full continuous Bezier loops, then tails pull in and a knot tightens; no degenerate loops or premature knot.',
      16:'One consistent tree; 43 deterministic particles clipped to a circular glass interior; staggered snow settling onto a varied bank.',
      17:'Fixed folded-paper tree texture, constant colour and spindle contact; smooth perspective sway, final bow and rotating crank.',
      18:'Smooth travelling star with trailing points; star and cream centre rendered in front of window bars; radial lantern and star illumination.',
      19:'Hat and cone share one base pivot, impact squash and damped wobble after contact; shaded hat and bounded entrance.',
      20:'Planted runners, loaded deck suspension and heart share a damped braking settle; fine securing ribbon, slack tow rope and snow spray.',
      21:'Carrot tip registered to the mouth; floor contact corrected for rotated geometry; returned snack shortens during munch, tiny crumbs and head nods.',
      22:'Stockings swing from fixed cuff hooks; anchored curved flames, warm hearth glow and soft cloth arrivals.',
      23:'Transparent crescent cutout; cloud blanket lowers onto roofs; four windows dim in sequence and stars soften before sleep letters.',
      24:'Curved curtain hems and seven soft fold bands; progressively illuminated lamps, star glow and centre-out audience sparkles.',
    }
    report={'revision':'polish-v2','native_resolution':[280,240],'render_resolution':[840,720],
            'duration_seconds':4.5,'fps':20,'clips':{}}
    overview=Image.new('RGB',(840,1080),c.BG)
    times=(.45,.9,1.35,1.8,2.15,2.5,2.9,3.55)
    for n,(name,detail) in enumerate(CLIPS.items()):
        header=np.asarray(c.stage(detail))[:48*S]
        frames=[]
        for k in range(90):
            frame=scene(name,k/20)
            assert frame.size==(840,720),(name,k,'size')
            assert np.array_equal(np.asarray(frame)[:48*S],header),(name,k,'header')
            frames.append(frame.convert('RGB').resize((280,240),Image.Resampling.LANCZOS))
        c.cover(detail)
        poses=Image.new('RGB',(840,240),c.BG)
        for col,t in enumerate((.65,1.8,3.5)):poses.paste(frames[round(t*20)],(col*280,0))
        poses.save(out/(name+'-poses.png'))
        motion=Image.new('RGB',(1120,510),c.BG)
        md=ImageDraw.Draw(motion)
        for k,t in enumerate(times):
            x=k%4*280;y=k//4*255;motion.paste(frames[round(t*20)],(x,y));md.text((x+8,y+242),f'{t:.2f}s',fill=c.CREAM)
        motion.save(out/(name+'-motion.png'))
        overview.paste(frames[71],((n%3)*280,(n//3)*270))
        ImageDraw.Draw(overview).text(((n%3)*280+8,(n//3)*270+244),detail['title'],fill=c.CREAM)
        events=audio_events(name);_,metrics=c.synthesize(name,events)
        for at,label in detail['cues']:
            assert any(e['at']-.08<=at<=e['at']+e['duration']+.08 for e in events),(name,label,'silent cue')
        report['clips'][name]={'frames_checked':90,'header_clear_to_y':48,'cover_fits':True,
                              'cue_windows_audible':True,'audio_metrics':metrics,'change_notes':notes[detail['day']]}
        print(name,'90 frames, header, cover, cues and audio OK',flush=True)
    overview.save(out/'overview.png')
    (out/'qa-change-notes.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    review()
