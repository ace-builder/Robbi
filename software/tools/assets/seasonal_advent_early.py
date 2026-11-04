"""December 1–12: twelve object-led Advent stories with fixed source geometry."""
from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
import seasonal_common as c

A=c.art
SOURCE=c.source_root/'advent-early'


def detail(day,title,quote,cover,when,sound,cues):
    return dict(title=title,heading=f'ADVENT {day:02d}',heading_size=24,
                quote=quote,cover=cover,when=when,sound=sound,cues=cues,group='advent',day=day)


CLIPS={
 'advent-01':detail(1,'First little light','LET THE LITTLE LIGHT IN.', ['A LITTLE DOOR.','A LITTLE WONDER.'],
    'December 1: a tiny calendar door opens and lets the first star out.',
    'A little latch tick, a paper hinge, a golden ping, and a warm three-note chord.',[(.5,'door hinge'),(1.45,'first star'),(2.4,'little lights')]),
 'advent-02':detail(2,'Spark duty','SMALL SPARK. BIG GLOW.', ['SMALL SPARK.','BIG GLOW.'],
    'December 2: one wandering spark finds a candle wick and gets to work.',
    'A soft falling whistle, a little ignition pop, and a warm candle hum.',[(.45,'spark falls'),(1.25,'wick catches'),(2.1,'warm glow')]),
 'advent-03':detail(3,'Wrapped in warmth','EXTRA WARM WISHES.', ['WRAPPED IN','WARM WISHES.'],
    'December 3: a shivering present gets a very long scarf and settles down.',
    'Two tiny shiver ticks, soft wool swishes, and a warm finishing chime.',[(.5,'chilly present'),(1.1,'scarf wraps'),(2.25,'all tucked in')]),
 'advent-04':detail(4,'Branch delivery','FOUND MY SPOT.', ['A LITTLE BOUNCE.','A PERFECT SPOT.'],
    'December 4: a bauble bounces into view and hooks itself onto a fir branch.',
    'Two round little bounces, a hook tick, and a glassy ornament bell.',[(.55,'first bounce'),(1.15,'second bounce'),(2.15,'hook catches')]),
 'advent-05':detail(5,'Folded wishes','GOOD WISHES, ENCLOSED.', ['FOLDING UP','SOMETHING GOOD.'],
    'December 5: a written wish folds itself into an envelope and receives a heart seal.',
    'Paper folds, a seal pop, then a tiny delivery flourish.',[(.55,'letter folds'),(1.5,'envelope closes'),(2.3,'heart seal')]),
 'advent-06':detail(6,'Boot bonus','BOOT-IFUL SURPRISE.', ['A LITTLE BOOT.','A LOT OF JOY.'],
    'December 6: a waiting Saint Nicholas boot catches a clementine, a sweet, and a star.',
    'Three little gift plops followed by a soft celebratory jingle.',[(.65,'clementine lands'),(1.3,'sweet lands'),(2.05,'star lands')]),
 'advent-07':detail(7,'Icing on the day','A SWEET LITTLE UPGRADE.', ['TODAY GETS','A LITTLE ICING.'],
    'December 7: a plain gingerbread star gets a piped icing border and one sugar heart.',
    'A soft piping swish, six delicate sugar ticks, and a sweet final ping.',[(.55,'icing starts'),(1.8,'border complete'),(2.4,'sugar heart')]),
 'advent-08':detail(8,'One tiny snowflake','CAUGHT A LITTLE WONDER.', ['HOLD STILL.','LITTLE WONDER.'],
    'December 8: a woolly mitten follows a drifting snowflake and catches it gently.',
    'An airy falling chime, a soft wool catch, and a crystalline sparkle.',[(.5,'snowflake drifts'),(1.8,'gentle catch'),(2.5,'little wonder')]),
 'advent-09':detail(9,'Cocoa express','NEXT STOP: COZY.', ['SPECIAL DELIVERY.','NEXT STOP: COZY.'],
    'December 9: a toy train pulls in with a very important cargo of hot chocolate.',
    'Three tiny wheel beats, a warm toy whistle, and a ceramic arrival clink.',[(.4,'train arrives'),(1.4,'toy whistle'),(2.25,'cocoa delivery')]),
 'advent-10':detail(10,'Fold by fold','LOOK WHAT UNFOLDS.', ['FOLD BY FOLD.','A LITTLE MAGIC.'],
    'December 10: a folded paper wedge opens into a six-armed snowflake.',
    'Three papery unfolding swishes and a bright reveal chime.',[(.55,'first fold opens'),(1.25,'second fold opens'),(2.1,'snowflake revealed')]),
 'advent-11':detail(11,'Peel the magic','A ZEST FOR LITTLE JOYS.', ['A LITTLE ZEST.','A LITTLE MAGIC.'],
    'December 11: a clementine unwinds a curly peel ribbon that finishes in a star.',
    'A delicate peel rustle, a curling slide, and a sunny star ping.',[(.55,'peel begins'),(1.3,'ribbon curls'),(2.4,'zesty star')]),
 'advent-12':detail(12,'Cheers to small joys','CHEERS TO SMALL JOYS.', ['HALFWAY THERE.','HERE\'S TO COZY.'],
    'December 12: two cocoa mugs slide together, toast, and share a heart of steam.',
    'Two gentle mug slides, one ceramic toast, and a rising warm heart chord.',[(.55,'mugs approach'),(1.6,'little toast'),(2.5,'shared heart')]),
}


def poly(d,points,fill):d.polygon([A.xy(p) for p in points],fill=fill)


def prop(name,w,h):return c.prop(SOURCE/(name+'.png'),w,h)


def sparkle(d,x,y,r=4):
    A.line(d,[(x-r,y),(x+r,y)],c.GOLD,1.4)
    A.line(d,[(x,y-r),(x,y+r)],c.GOLD,1.4)


def snowflake(d,x,y,r,color=c.CREAM,angle=0):
    for n in range(6):
        a=angle+math.tau*n/6
        end=(x+math.cos(a)*r,y+math.sin(a)*r)
        A.line(d,[(x,y),end],color,1.6)
        for q in (.52,.77):
            px=x+math.cos(a)*r*q;py=y+math.sin(a)*r*q
            for b in (-.7,.7):
                A.line(d,[(px,py),(px-math.cos(a+b)*r*.23,py-math.sin(a+b)*r*.23)],color,1.3)


def steam(d,x,y,t,width=1.6):
    for offset in (-8,8):
        A.line(d,[(x+offset+math.sin(j*.65+t*2+offset)*2.5,y-j*3.5) for j in range(7)],c.MUTED,width)


def gift(d,x,y):
    A.rounded(d,(x-36,y-29,x+36,y+35),5,c.CORAL,c.INK,1.3)
    A.rounded(d,(x-40,y-36,x+40,y-23),3,'#ffc2a1',c.INK,1.2)
    A.rounded(d,(x-5,y-36,x+5,y+35),1,c.GOLD)
    A.ellipse(d,(x-22,y-51,x,y-34),None,c.GOLD,3)
    A.ellipse(d,(x,y-51,x+22,y-34),None,c.GOLD,3)


def branch(d):
    A.line(d,[(67,66),(221,87)],'#b48766',5)
    for x in range(72,222,14):
        y=66+(x-67)*21/154
        for dy in (-1,1):
            A.line(d,[(x,y),(x-20,y+dy*17)],c.SAGE,3)
            A.line(d,[(x+5,y+1),(x-9,y+dy*20)],'#91ac85',2)


def envelope(d,x,y,closed=1):
    A.rounded(d,(x-57,y-27,x+57,y+29),4,c.CREAM,c.INK,1.2)
    A.line(d,[(x-56,y+27),(x,y-8),(x+56,y+27)],'#c49f91',1.2)
    poly(d,[(x-57,y-27),(x,y-27+36*closed),(x+57,y-27)],'#eed6b9')
    A.line(d,[(x-57,y-27),(x,y-27+36*closed),(x+57,y-27)],'#c49f91',1.2)


@lru_cache(None)
def folded_flake():
    im=Image.new('RGBA',(110*c.S,110*c.S));d=ImageDraw.Draw(im)
    snowflake(d,55,55,47,c.CREAM)
    return im


def progress(t,a,b):return max(0.,min(1.,(t-a)/(b-a)))


def smooth(t,a,b):
    p=progress(t,a,b)
    return p*p*p*(10+p*(-15+6*p))


def recoil(t,at,amount=1,decay=6,hz=14):
    q=max(0,t-at)
    return 0 if t<at or q>1.1 else amount*math.exp(-decay*q)*math.sin(hz*q)


def bezier(a,b,c_,e,n=28):
    return [tuple((1-p)**3*a[j]+3*(1-p)**2*p*b[j]+3*(1-p)*p*p*c_[j]+p**3*e[j]
                  for j in (0,1)) for p in (i/n for i in range(n+1))]


def path_slice(points,start=0,end=1):
    """Cut a polyline by arc length, so a tool tip follows its visible work."""
    lengths=[math.dist(a,b) for a,b in zip(points,points[1:])]
    total=sum(lengths);lo=start*total;hi=end*total;travel=0;out=[]
    for a,b,length in zip(points,points[1:],lengths):
        if length and travel<=hi and travel+length>=lo:
            for distance in (max(lo,travel),min(hi,travel+length)):
                p=(distance-travel)/length
                out.append(tuple(a[j]+(b[j]-a[j])*p for j in (0,1)))
        travel+=length
    return out or [points[0],points[0]]


@lru_cache(None)
def warm_halo(rx,ry):
    layer=Image.new('RGBA',(rx*2*c.S,ry*2*c.S));d=ImageDraw.Draw(layer)
    for n in range(28,0,-1):
        p=n/28
        A.ellipse(d,(rx-rx*p,ry-ry*p,rx+rx*p,ry+ry*p),(255,177,93,round(1+65*(1-p)**1.5)))
    return layer.filter(ImageFilter.GaussianBlur(4*c.S))


def halo(im,x,y,rx,ry,strength=1):
    sprite=warm_halo(rx,ry).copy()
    sprite.putalpha(sprite.getchannel('A').point(lambda a:round(a*strength)))
    A.stamp(im,sprite,x,y)


@lru_cache(None)
def paper_arm(shade):
    """One rigid die-cut sector; transparent holes reveal the actual stage."""
    im=Image.new('RGBA',(120*c.S,120*c.S));d=ImageDraw.Draw(im)
    pts=[(60,60)]
    for off,r in [(0,19),(.18,42),(.35,35),(.52,50),(.69,35),(.87,42),(math.pi/3,19)]:
        a=-math.pi/2+off;pts.append((60+r*math.cos(a),60+r*math.sin(a)))
    poly(d,pts,shade)
    A.line(d,[pts[0],pts[1]],'#d5bcb4',.8)
    for rr,size in [(29,4),(41,2.2)]:
        a=-math.pi/2+.52;x=60+rr*math.cos(a);y=60+rr*math.sin(a)
        poly(d,[(x,y-size),(x+size,y),(x,y+size),(x-size,y)],(0,0,0,0))
    return im


def scene(name,t):
    info=CLIPS[name];im=c.stage(info);d=ImageDraw.Draw(im)
    A.ellipse(d,(56,182,224,192),'#392c3c')
    day=info['day'];done=2.78
    if day==1:
        p=smooth(t,.63,1.48);rise=smooth(t,1.45,2.4)
        latch=recoil(t,.5,1.8,12,31)
        halo(im,143,130,56,61,.3+.7*p)
        A.rounded(d,(86,63,194,189),7,'#98624f',c.GOLD,1.5)
        A.rounded(d,(91,68,189,185),4,'#e2b48a')
        A.rounded(d,(96,73,184,181),3,'#382738')
        A.line(d,[(97,179),(183,179)],'#76544c',2)
        A.ellipse(d,(106-24*p,180,178+24*p,187), '#6b4b42')
        # The star belongs inside the little room until the door exposes it.
        sx=140+10*rise;sy=133-43*rise
        halo(im,sx,sy,27,28,p*.7)
        A.star(d,sx,sy,9+12*rise,c.GOLD,angle=.2*(1-rise))
        A.star(d,sx-2,sy-3,3+3*rise,c.CREAM,angle=.2*(1-rise))
        angle=1.35*p;edge=96+88*math.cos(angle)+latch
        skew=12*math.sin(angle)
        face=[(96,73),(edge,73+skew),(edge,181-skew),(96,181)]
        poly(d,[(edge,73+skew),(edge+4*p,72+skew),(edge+4*p,182-skew),(edge,181-skew)],'#98624f')
        poly(d,face,'#d6a47a')
        A.line(d,face+[face[0]],'#f5ce9f',1.3)
        if edge>105:
            A.line(d,[(101,80),(edge-5,79+skew),(edge-5,174-skew),(101,174)],'#b27d5b',1)
        for yy in (94,158):A.rounded(d,(93,yy,99,yy+9),1,c.GOLD)
        A.ellipse(d,(edge-10,124,edge-5,130),c.GOLD,c.INK,.7)
        if rise>0:
            for n,(x,y) in enumerate([(211,98),(72,117),(204,151)]):
                sparkle(d,x,y,4*smooth(t,1.9+n*.13,2.35+n*.13))
        before='one tiny door to wonder.'
    elif day==2:
        fall=progress(t,.45,1.25);lit=smooth(t,1.25,1.62)
        if t<1.25:
            sx=111+29*fall+8*math.sin(math.pi*fall);sy=56+43*fall*fall
            A.line(d,[(sx-6,sy-10),(sx-3,sy-5)],c.MUTED,1)
            A.star(d,sx,sy,3.5,c.GOLD,angle=fall*2)
        glow=lit*(.9+.1*math.sin(min(t,3.6)*3))
        halo(im,140,109,69,57,glow)
        A.ellipse(d,(95,174,185,188),'#bd8052',c.INK,1)
        A.ellipse(d,(99,174,181,183),c.GOLD)
        A.rounded(d,(118,107,162,179),5,'#e9cca5')
        A.rounded(d,(120,107,157,178),4,c.CREAM)
        A.rounded(d,(153,111,160,155),3,'#ebcfac')
        A.rounded(d,(123,110,128,145),2,'#fff8e6')
        A.rounded(d,(130,107,136,130),3,'#f8e8ca')
        A.ellipse(d,(118,101,162,113),'#f4d4af')
        A.ellipse(d,(130,104,150,110),'#d8ae88')
        A.line(d,[(140,107),(140,98)],c.INK,1.8)
        if lit:
            flick=math.sin(min(t,3.6)*8)
            tipx=140+1.2*flick;tipy=103-29*lit+.8*flick
            poly(d,[(tipx,tipy),(130,91+10*(1-lit)),(131,101),(137,107),(145,106),(150,99),(146,88+12*(1-lit))],c.GOLD)
            poly(d,[(140,89+11*(1-lit)),(136,101),(140,106),(145,101)],c.CREAM)
            A.ellipse(d,(137,103,144,108),'#fff8e9')
        before='a little spark found its job.'
    elif day==3:
        wrap=smooth(t,1.1,2.25);x=140+math.sin(t*35)*1.4*(1-smooth(t,.9,1.5))
        # The back tail peeks around the package; the front is one continuous ribbon.
        back=smooth(t,1.95,2.25)
        if back>0:A.line(d,[(177,125),(187,128),(190,128+37*back)],'#91ac85',12)
        gift(d,x,143+recoil(t,2.25,1.1,8,12))
        if t<1.5:
            fade=1-smooth(t,1.05,1.5)
            for xx in (85,195):A.line(d,[(xx,114),(xx-3*fade,123),(xx+3*fade,133),(xx,142)],c.LILAC,1.3)
        scarf=bezier((60,125),(97,120),(136,127),(174,126),36)+bezier((174,126),(185,125),(184,150),(178,173),20)[1:]
        visible=path_slice(scarf,.29*wrap,wrap)
        if wrap>0:
            A.line(d,visible,'#88a778',15)
            A.line(d,[(xx,yy-1) for xx,yy in visible],c.SAGE,12)
            for point in path_slice(scarf,.3,wrap)[::4] if wrap>.3 else []:
                xx,yy=point;A.line(d,[(xx-1,yy-4),(xx+1,yy+4)],'#a8be91',.9)
            xx,yy=visible[-1]
            for dx in (-4,0,4):A.line(d,[(xx+dx,yy),(xx+dx-1,yy+5)],c.SAGE,1.2)
        if wrap==1:
            A.heart(d,211,97,8*smooth(t,2.25,2.55),c.CORAL)
        before='this present could use a scarf.'
    elif day==4:
        branch(d)
        if t<2.15:
            q=progress(t,.55,2.15)
            if q<.375:
                f=q/.375;x=38+68*f;y=158-40*math.sin(math.pi*f)
            else:
                f=(q-.375)/.625;x=106+75*smooth(f,0,1);y=158-25*f-43*math.sin(math.pi*f)
            angle=0
        else:
            angle=.34*math.exp(-3.4*(t-2.15))*math.sin((t-2.15)*10)
            x=181+51*math.sin(angle);y=82+51*math.cos(angle)
        squash=abs(recoil(t,.55,.075,13,20))+abs(recoil(t,1.15,.075,13,20))
        if t>=2.15:
            A.line(d,[(181,82),(x,y-23)],c.GOLD,1.5)
            A.ellipse(d,(177,77,185,85),None,c.GOLD,1.3)
        else:
            A.line(d,[(x,y-24),(x,y-50)],c.GOLD,1.4)
            A.ellipse(d,(x-4,y-55,x+4,y-47),None,c.GOLD,1.3)
        rx=23*(1+squash);ry=23*(1-squash)
        A.rounded(d,(x-5,y-ry-6,x+5,y-ry+1),1,c.GOLD)
        A.ellipse(d,(x-rx,y-ry,x+rx,y+ry),'#df776e',c.GOLD,1.2)
        A.ellipse(d,(x-rx+2,y-ry+1,x+rx-4,y+ry-5),c.CORAL)
        A.ellipse(d,(x-14,y-17,x-4,y-9),'#ffe4c2')
        A.line(d,[(x-20,y+7),(x-10,y+13),(x,y+7),(x+10,y+13),(x+20,y+7)],c.GOLD,2)
        A.star(d,x+9,y-6,4,c.CREAM,angle=angle)
        if t>=2.15:sparkle(d,220,137,5*smooth(t,2.15,2.5))
        before='just looking for the right branch.'
    elif day==5:
        fold=smooth(t,.55,1.5);close=smooth(t,1.5,2.05);seal=smooth(t,2.12,2.3)
        # The same sheet, pocket and flap stay present through every fold.
        A.rounded(d,(83,119,197,179),4,'#d8b99f',c.INK,1)
        apex=76+80*close
        poly(d,[(83,119),(140,apex),(197,119)],'#edcdb0' if close<.5 else '#eed6b9')
        A.line(d,[(83,119),(140,apex),(197,119)],'#c59a83',1)
        letter_top=88+41*fold
        A.rounded(d,(91,letter_top,189,171),2,c.CREAM)
        for offset in (15,26,37):
            yy=letter_top+offset
            if yy<166:A.line(d,[(102,yy),(167,yy)],'#bea49e',1)
        if letter_top+17<164:A.heart(d,177,letter_top+17,5,c.CORAL)
        # Side gussets and the rising bottom fold occlude the letter naturally.
        poly(d,[(83,120),(83+48*fold,148),(83,179)],'#f5e2c6')
        poly(d,[(197,120),(197-48*fold,148),(197,179)],'#f5e2c6')
        poly(d,[(83,179),(140,178-35*fold),(197,179)],c.CREAM)
        if fold>0:A.line(d,[(83,179),(140,178-35*fold),(197,179)],'#c49f91',1)
        if close>0:
            poly(d,[(83,119),(140,apex),(197,119)],'#eed6b9')
            A.line(d,[(83,119),(140,apex),(197,119)],'#c49f91',1)
        if t>=2.12:
            yy=151-18*(1-seal)+recoil(t,2.3,2,10,20)
            A.ellipse(d,(128,yy-11,152,yy+11),c.CORAL)
            A.ellipse(d,(130,yy-9,150,yy+9),None,'#ffbaa0',1)
            A.heart(d,140,yy,5.5,c.CREAM)
        if t>=2.5:sparkle(d,216,109,4*smooth(t,2.5,2.7))
        before='packing something good for you.'
    elif day==6:
        impacts=[.65,1.3,2.05]
        bounce=sum(recoil(t,at,2.6,11,20) for at in impacts)
        # Front cuff remains the actual artwork, in front of all three falling treats.
        for start,land,kind,x0,x1 in [(.35,.65,'orange',76,111),(1.,1.3,'sweet',194,138),(1.75,2.05,'star',195,159)]:
            if t<start:continue
            q=progress(t,start,land);xx=x0+(x1-x0)*q
            yy=56+35*q*q+recoil(t,land,-4,8,18)
            if q==1:xx=x1
            if kind=='orange':c.deform_stamp(im,prop('clementine',28,28),xx,yy,angle=-25*(1-q))
            elif kind=='sweet':
                poly(d,[(xx-13,yy-7),(xx-7,yy),(xx-13,yy+7)],c.GOLD)
                poly(d,[(xx+13,yy-7),(xx+7,yy),(xx+13,yy+7)],c.GOLD)
                A.rounded(d,(xx-8,yy-6,xx+8,yy+6),3,c.CORAL)
                A.line(d,[(xx-4,yy-5),(xx+2,yy+5)],c.CREAM,2)
            else:A.star(d,xx,yy,12,c.GOLD,angle=-.8*(1-q))
        c.deform_stamp(im,prop('nicholas-boot',132,104),145,141+bounce*.35,sx=1+abs(bounce)*.003,sy=1-abs(bounce)*.005)
        for at in impacts:
            q=progress(t,at,at+.3)
            if 0<q<1:
                for sign in (-1,1):A.line(d,[(102+sign*33,101-q*4),(102+sign*(37+q*5),97-q*6)],c.GOLD,1)
        if t>=2.25:
            for x,y in [(68,112),(210,91),(213,150)]:sparkle(d,x,y,4*smooth(t,2.25,2.6))
        before='a very important boot inspection.'
    elif day==7:
        A.stamp(im,prop('ginger-star',126,126),140,124)
        p=smooth(t,.55,1.8)
        points=[]
        for n in range(13):
            a=-math.pi/2+math.tau*n/12;r=45 if n%2==0 else 28
            points.append((140+math.cos(a)*r,124+math.sin(a)*r))
        drawn=path_slice(points,0,p)
        if p>0:
            A.line(d,[(x+.7,y+1) for x,y in drawn],'#cb986d',4.2)
            A.line(d,drawn,c.CREAM,3.2)
            A.line(d,[(x-.4,y-.4) for x,y in drawn],'#fff8e9',.9)
        if .45<t<2.05:
            x,y=drawn[-1];leave=smooth(t,1.8,2.05);x+=21*leave;y=max(73,y-13*leave)
            squeeze=1+.06*math.sin(p*math.tau*6)
            poly(d,[(x+5,y-5),(x+15*squeeze,y-27),(x+30*squeeze,y-16)],c.CORAL)
            A.line(d,[(x+15*squeeze,y-27),(x+30*squeeze,y-16)],'#ffd0b2',2)
            poly(d,[(x,y),(x+5,y-8),(x+11,y-4)],'#e3d7cd')
            A.line(d,[(x,y),(x+3,y-4)],c.CREAM,1)
        if 1.8<=t<2.35:
            q=progress(t,1.8,2.35);sparkle(d,140,79,6*math.sin(math.pi*q))
        if t>=2.15:
            q=smooth(t,2.15,2.4);yy=95+29*q+recoil(t,2.4,-2,10,19)
            A.heart(d,140,yy,10,c.CORAL)
        if t>=2.4:
            for x,y in [(60,99),(218,143)]:sparkle(d,x,y,4*smooth(t,2.4,2.7))
        before='today deserves a little icing.'
    elif day==8:
        p=smooth(t,.5,1.8);bump=recoil(t,1.8,4.5,6,14)
        x=113+27*p+recoil(t,1.8,1.4,6,14)
        angle=-11*(1-p)+recoil(t,1.8,4,6,14)
        c.deform_stamp(im,prop('sage-mitten',93,107),x,144+bump,angle=angle,sy=1-abs(bump)*.004)
        fx=140+20*(1-p)*math.sin(p*5);fy=57+43*p+(bump if t>=1.8 else 0)
        snowflake(d,fx,fy,12,c.CREAM,(1-p)*.8)
        if 1.8<t<2.3:
            q=progress(t,1.8,2.3)
            for sign in (-1,1):A.line(d,[(140+sign*(18+q*5),102-q*7),(140+sign*(22+q*6),99-q*7)],c.GOLD,1)
        if t>=2.5:
            for xx,yy in [(67,107),(210,84),(201,156)]:sparkle(d,xx,yy,4*smooth(t,2.5,2.8))
        before='hold still. this one is special.'
    elif day==9:
        p=smooth(t,.4,2.25);x=-108+221*p+recoil(t,2.25,4,6,16)
        A.line(d,[(16,179),(264,179)],'#8d7486',2)
        for xx in range(24,265,15):A.line(d,[(xx,176),(xx,183)],'#8d7486',2)
        A.line(d,[(x+15,161),(x+39,161)],c.GOLD,3)
        A.rounded(d,(x-53,148,x+18,166),3,c.SAGE,c.INK,1.2)
        A.line(d,[(x-49,152),(x+14,152)],'#e2e3b7',1.2)
        cargo=recoil(t,2.25,4,5.5,13)
        tilt=-3*math.sin(math.pi*p)+recoil(t,2.25,7,5.5,13)
        c.deform_stamp(im,prop('cocoa-mug',64,58),x-16+cargo,121-abs(cargo)*.35,angle=tilt)
        steam(d,x-21+cargo,95,min(t,3.6),1.1)
        A.rounded(d,(x+35,130,x+100,165),5,'#cf726b',c.INK,1.2)
        A.rounded(d,(x+37,130,x+97,159),4,c.CORAL)
        A.rounded(d,(x+67,110,x+101,154),3,c.CORAL,c.INK,1.2)
        A.rounded(d,(x+73,117,x+94,133),2,c.CREAM)
        A.line(d,[(x+83,118),(x+83,132)],'#d9b798',1.1)
        A.rounded(d,(x+45,112,x+56,132),2,c.GOLD,c.INK,1)
        A.rounded(d,(x+42,109,x+59,115),2,c.GOLD)
        A.rounded(d,(x+64,105,x+104,113),2,c.GOLD,c.INK,1)
        A.line(d,[(x+38,154),(x+64,154)],c.GOLD,2)
        for xx in (x-38,x+4,x+48,x+88):
            A.ellipse(d,(xx-9,158,xx+9,176),c.INK,c.GOLD,2)
            angle=-221*p/9
            for n in range(3):
                a=angle+n*math.tau/3
                A.line(d,[(xx,167),(xx+6*math.cos(a),167+6*math.sin(a))],c.CREAM,1)
            A.ellipse(d,(xx-2,165,xx+2,169),c.CORAL)
        if 1.4<t<2.15:
            q=progress(t,1.4,2.15)
            A.ellipse(d,(x+48-q*15,103-q*19,x+54+q*2,109-q*14),'#b7a3ad')
        if t>=2.25:sparkle(d,238,104,4*smooth(t,2.25,2.65))
        before='precious cargo coming through.'
    elif day==10:
        p=smooth(t,.55,2.1)
        # Six identical rigid cut-paper wedges fan out from a single folded stack.
        paper=Image.new('RGBA',im.size)
        spin=recoil(t,2.1,3.4,5,11)
        for n in range(5,-1,-1):
            turn=n*60*p+spin
            shade=c.CREAM if n%2 else '#eedad0'
            A.stamp(paper,paper_arm(shade),140,127,angle=-turn)
        mask=ImageDraw.Draw(paper)
        A.ellipse(mask,(135,122,145,132),(0,0,0,0))
        im.alpha_composite(paper)
        if t>=2.1:
            for xx,yy in [(67,91),(212,143),(207,84)]:sparkle(d,xx,yy,4*smooth(t,2.1,2.5))
        before='good things unfold in their time.'
    elif day==11:
        p=smooth(t,.55,2.4)
        A.stamp(im,prop('clementine',87,87),97,144)
        # Wide, two-sided peel starts on the fruit skin and curls continuously.
        lead=bezier((119,130),(135,143),(144,113),(165,110),30)
        lead+=bezier((165,110),(174,106),(183,105),(188,108),15)[1:]
        spiral=[]
        for n in range(101):
            q=n/100;a=-math.pi/2+q*math.tau*1.5;r=25-17*q
            spiral.append((188+r*math.cos(a),133+r*math.sin(a)))
        full=lead+spiral[1:];visible=path_slice(full,0,p)
        if p>0:
            A.line(d,visible,'#df7c35',8)
            A.line(d,[(x,y-1.3) for x,y in visible],c.GOLD,4.8)
            A.line(d,[(x,y+2) for x,y in visible],'#f2a24e',1.1)
            # The root has a curved peel cut, visibly attached to the orange.
            root=bezier((109,121),(118,119),(123,126),(119,132),16)
            A.line(d,root,'#bc652c',1.5)
        if t>=2.4:
            h=c.spring(t,2.4,2.83,overshoot=1.5)
            A.star(d,188,133,10*h,c.GOLD,angle=(1-h)*.4)
            for xx,yy in [(218,91),(222,158)]:sparkle(d,xx,yy,4*smooth(t,2.4,2.75))
        before='a small, freshly peeled delight.'
    elif day==12:
        p=smooth(t,.55,1.6);settle=smooth(t,1.65,2.2)
        recoil_x=recoil(t,1.6,2.2,7,18)
        x1=68+27*p-5*settle-recoil_x;x2=212-27*p+5*settle+recoil_x
        lift=8*p*(1-settle);tilt=-4*math.sin(p*math.pi)+recoil(t,1.6,2.5,7,18)
        # Inward-facing handles touch precisely once before the mugs settle apart.
        c.deform_stamp(im,prop('cocoa-mug',90,81),x1,143-lift,angle=tilt)
        c.deform_stamp(im,prop('cocoa-mug',90,81).transpose(Image.Transpose.FLIP_LEFT_RIGHT),x2,143-lift,angle=-tilt)
        steam(d,x1-8,101-lift,min(t,3.6),1.3);steam(d,x2+8,101-lift,min(t,3.6),1.3)
        if 1.6<=t<2.1:
            q=progress(t,1.6,2.1);sparkle(d,140,133,7*math.sin(math.pi*q))
            A.line(d,[(136-q*3,123-q*4),(133-q*3,117-q*4)],c.GOLD,1.3)
            A.line(d,[(144+q*3,123-q*4),(147+q*3,117-q*4)],c.GOLD,1.3)
        if t>=2.5:
            h=c.spring(t,2.5,2.95,overshoot=1.3);A.heart(d,140,89-8*min(h,1),11*h,c.CORAL)
            sparkle(d,220,77,3*smooth(t,2.5,2.85))
        before='two cups. one very cozy plan.'
    else:raise ValueError(name)
    c.reveal_caption(im,info,t,done,before,size=19)
    return im.convert('RGB')


def event(at,kind,hz=660,duration=.3,gain=.13):
    return dict(at=at,kind=kind,hz=hz,duration=duration,gain=gain)


def audio_events(name):
    day=CLIPS[name]['day']
    return {
        1:[event(.5,'tick',620,.12,.08),event(.63,'rustle',660,.5,.10),event(1.45,'bell',880,.45,.12),event(2.4,'chord',[523.25,659.25,783.99],.65,.12)],
        2:[event(.45,'whoosh',[1300,600],.65,.075),event(1.25,'pop',460,.16,.12),event(1.42,'tick',1800,.07,.035),event(2.1,'chord',[220,330,440],.85,.085)],
        3:[event(.5,'tick',800,.08,.08),event(.8,'tick',900,.08,.08),event(1.1,'rustle',500,.85,.10),event(2.25,'pluck',587,.48,.12)],
        4:[event(.55,'pluck',330,.18,.13),event(1.15,'pluck',440,.17,.12),event(2.15,'tick',1300,.08,.09),event(2.22,'bell',1175,.6,.13)],
        5:[event(.55,'rustle',500,.55,.10),event(1.5,'rustle',600,.5,.10),event(2.3,'pop',700,.15,.13),event(2.5,'pluck',880,.38,.10)],
        6:[event(.65,'thud',380,.16,.11),event(1.3,'pop',580,.13),event(2.05,'bell',1047,.4),event(2.25,'jingle',[880,1175,1397],.55,.11)],
        7:[event(.55,'rustle',800,1.15,.034)]+[event(.65+n*.18,'tick',1000+n*80,.055,.055) for n in range(6)]+[event(1.8,'pluck',1568,.22,.095),event(2.4,'bell',1319,.5,.12)],
        8:[event(.5,'chime',[1760,1175],.7,.09),event(1.8,'thud',380,.15,.08),event(2.5,'sparkle',[1397,1760,2093],.6,.12)],
        9:[event(at,'thud',340,.085,.065) for at in (.4,.74,1.0,1.24,1.51,1.83,2.15)]+[event(1.4,'hum',[660,700,660],.4,.105),event(2.25,'bell',920,.4,.12)],
        10:[event(.55,'rustle',600,.34,.10),event(1.25,'rustle',650,.4,.10),event(2.1,'rustle',700,.24,.08),event(2.35,'chord',[698,1047,1397],.6,.12)],
        11:[event(.55,'rustle',600,.6,.09),event(1.3,'whoosh',[600,1000],.55,.08),event(2.4,'pluck',1319,.45,.13)],
        12:[event(.55,'whoosh',450,.65,.07),event(1.6,'bell',1047,.35,.15),event(1.63,'bell',1568,.22,.035),event(2.5,'chord',[440,554.37,659.25],.8,.12)],
    }[day]


def review():
    """Native-size polish review; parent performs final media exports."""
    import numpy as np
    out=c.ROOT/'test-results/seasonal-postcards-preview/polish-advent-early'
    out.mkdir(parents=True,exist_ok=True)
    sheet=Image.new('RGB',(840,12*267),c.BG);sd=ImageDraw.Draw(sheet)
    qa={}
    poses=[(.7,1.65,3.55),(.7,1.35,3.55),(1.1,1.75,3.55),(.8,2.3,3.55),
           (.65,1.7,3.55),(.46,1.15,3.55),(.75,1.3,3.55),(.65,1.9,3.55),
           (.9,1.7,3.55),(.7,1.3,3.55),(.8,1.65,3.55),(.9,1.6,3.55)]
    for row,(name,info) in enumerate(CLIPS.items()):
        for col,t in enumerate(poses[row]):
            frame=scene(name,t).resize((280,240),Image.Resampling.LANCZOS)
            sheet.paste(frame,(col*280,row*267))
        sd.text((8,row*267+244),f'{name}: {info["title"]}',fill=c.CREAM)
        data,report=c.synthesize(name,audio_events(name))
        samples=np.frombuffer(data,dtype='<i2').astype(float)/32768
        energies={str(at):float(np.sqrt(np.mean(samples[round(at*c.RATE):round((at+.1)*c.RATE)]**2))) for at,label in info['cues']}
        assert min(energies.values())>.001,(name,energies)
        qa[name]=dict(report,frames_verified=90,review_times=poses[row],cue_rms=energies)
        # No effect or moving prop may enter the exact 38-pixel header.
        header=np.asarray(c.stage(info).convert('RGB'))[:38*c.S]
        for frame in range(90):
            image=scene(name,frame/20)
            assert np.array_equal(np.asarray(image)[:38*c.S],header),(name,frame,'header')
        c.cover(info)
        sheet.crop((0,row*267,840,(row+1)*267)).save(out/f'{name}-poses.png')
    sheet.save(out/'contact-sheet.png')
    for block in range(4):
        sheet.crop((0,block*801,840,(block+1)*801)).save(out/f'contact-days-{block*3+1:02d}-{block*3+3:02d}.png')
    (out/'qa.json').write_text(json.dumps(qa,indent=2),encoding='utf-8')
    notes={
      'advent-01':'Anticipatory latch recoil, perspective door thickness and inset molding, fixed hinge hardware, warm room and star light, eased star emergence.',
      'advent-02':'Accelerating curved spark, smooth ignition, layered warm breathing halo, shaded wax/drips and holder, three-part flame and small fire tick.',
      'advent-03':'Continuous curved scarf with arc-length reveal and trailing trim, ribbed wool/fringe, delayed rear tail, diminishing shiver and package settle.',
      'advent-04':'Two authored ball arcs, impact squash, precise hook landing, anchored damped pendulum and richer glass highlights.',
      'advent-05':'Persistent sheet, pocket, gussets and flap throughout, letter slides inside while bottom folds, closing flap crosses hinge continuously, wax seal lands with recoil.',
      'advent-06':'Accelerating diagonal treat falls, fixed cuff occlusion, individual rebound and boot squash, impact marks and visible landed cargo.',
      'advent-07':'Arc-length nozzle tracking, pulsing piping bag and metal nozzle, dimensional icing line, tool withdrawal, border-complete sparkle and audible pluck, falling sugar heart.',
      'advent-08':'Eased drift, tracking mitten tilt, shared catch recoil and squash, snowflake rides mitten after contact, outward catch accents.',
      'advent-09':'Quintic accelerate/brake, distance-driven three-spoke wheels, decelerating wheel sound beats, cab detailing and chimney puff, delayed cargo tilt/recoil and warm steam.',
      'advent-10':'Six fixed die-cut transparent paper sectors fan rigidly from a folded stack, genuine alpha holes reveal stage light, alternating paper shading and settling rotation.',
      'advent-11':'Curved broad two-sided peel rooted on orange skin, visible rind cut, arc-length reveal into continuous spiral, spring star flourish.',
      'advent-12':'Inward handles meet once at cue, lift and anticipatory tilt, damped separation and shared steam/heart spring; simultaneous warm final chord.',
    }
    (out/'change-notes.json').write_text(json.dumps(dict(changes=notes,frames=1080,native_size=[280,240],poses_per_clip=3,header_check='All 90 frames per clip exactly match fixed first 38 pixels.',sound_check='Every declared cue has RMS above .001 in following 100 ms; synthesis metrics passed.',source_assets='Five fixed existing generated props; no replacement generation.'),indent=2),encoding='utf-8')
    return out


if __name__=='__main__':print(review())
