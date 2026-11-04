"""Four fresh object-led celebrations, composed from fixed paper and felt art."""
from __future__ import annotations

from functools import lru_cache
import json
import math

from PIL import Image, ImageDraw
import seasonal_common as c

A=c.art
SOURCE=c.ROOT/'assets/dynamics_sources/seasonal-encore-v1/festive'
REVIEW=c.ROOT/'test-results/seasonal-encore-preview/festive-review'

CLIPS={
 'season-valentine':dict(title='Two hearts, one click',heading="VALENTINE'S DAY",heading_size=23,
    cover=('TWO LITTLE HEARTS.','ONE GOOD CLICK.'),quote='SOME THINGS JUST CLICK.',
    when='A Valentine greeting: two folded paper hearts take a shy orbit, then meet with a little click.',
    sound='Two shy paper plucks, an airy orbit, a soft clasp click, and a warm original chord.',
    group='celebrations',cues=[(.5,'shy little flutter'),(.8,'paper orbit'),(2.25,'hearts click'),(2.8,'tiny love flourish')]),
 'season-lunar-new-year':dict(title='Good fortune, tucked inside',heading='LUNAR NEW YEAR',heading_size=23,
    cover=('A LITTLE PACKET.','A BRIGHT BEGINNING.'),quote='GOOD THINGS INSIDE.',
    when='A red-packet-inspired Lunar New Year greeting: an original gold paper flourish springs up, followed by a little tangerine.',
    sound='A paper packet rustle, a springy original pluck, a little tangerine landing, and a bright gentle flourish.',
    group='celebrations',cues=[(.5,'packet anticipation'),(1.1,'packet opens'),(1.55,'gold flourish'),(2.5,'tangerine lands'),(2.9,'bright beginning')]),
 'season-carnival':dict(title='A very extra entrance',heading='CARNIVAL',heading_size=25,
    cover=('A TINY SHIMMY.','A GRAND ENTRANCE.'),quote='MASK ON. WORRIES OFF.',
    when='A Carnival greeting: a paper mask shimmies its feathers and ribbons, then takes a confetti bow.',
    sound='Soft ribbon swishes, two playful original plucks, a little bow beat, and scattered confetti ticks.',
    group='celebrations',cues=[(.5,'mask anticipation'),(1.1,'ribbon shimmy'),(1.55,'feather flourish'),(2.25,'confetti bow'),(3.,'grand entrance')]),
 'season-lucky-clover':dict(title='Three leaves, one soft landing',heading="ST PATRICK'S DAY",heading_size=22,
    cover=('THREE LITTLE LEAVES.','ONE LUCKY LANDING.'),quote='A LITTLE LUCK FOUND YOU.',
    when="A St Patrick's Day greeting: a green felt hat catches a folded three-leaf shamrock, which opens over its brim.",
    sound='An airy leaf whistle, a soft felt catch, a paper unfolding rustle, and a small original lucky pluck.',
    group='celebrations',cues=[(.5,'shamrock drifts'),(1.65,'felt catch'),(1.9,'three leaves unfold'),(2.5,'little lucky flourish')]),
}


def linear(t,a,b):return max(0.,min(1.,(t-a)/(b-a)))


def ease(t,a,b):
    p=linear(t,a,b)
    return p*p*p*(10+p*(-15+6*p))


def recoil(t,at,amount=1,decay=7,hz=15):
    q=t-at
    return amount*math.exp(-decay*q)*math.sin(hz*q) if 0<=q<1.1 else 0


def polygon(d,points,fill,outline=None,width=1):
    d.polygon([A.xy(p) for p in points],fill=fill)
    if outline:A.line(d,points+[points[0]],outline,width)


def bezier(a,b,cc,e,n=30):
    return [tuple((1-p)**3*a[k]+3*(1-p)**2*p*b[k]+3*(1-p)*p*p*cc[k]+p**3*e[k]
                  for k in (0,1)) for p in (j/n for j in range(n+1))]


def heart_points(cx,cy,r,angle=0):
    out=[]
    for n in range(81):
        t=n/80*math.tau
        x=r*math.sin(t)**3;y=-r*(13*math.cos(t)-5*math.cos(2*t)-2*math.cos(3*t)-math.cos(4*t))/16
        out.append((cx+x*math.cos(angle)-y*math.sin(angle),cy+x*math.sin(angle)+y*math.cos(angle)))
    return out


def composite(im,layer,opacity=1):
    if opacity<=0:return
    if opacity<1:
        layer=layer.copy();layer.putalpha(layer.getchannel('A').point(lambda a:round(a*opacity)))
    im.alpha_composite(layer)


def spark(d,x,y,r=4,color=c.GOLD):
    A.line(d,[(x-r,y),(x+r,y)],color,1.2)
    A.line(d,[(x,y-r),(x,y+r)],color,1.2)


@lru_cache(None)
def paper_heart(color,shade):
    im=Image.new('RGBA',(76*c.S,78*c.S));d=ImageDraw.Draw(im)
    points=heart_points(38,36,30)
    polygon(d,[(x,y+1.5) for x,y in points],'#a36975')
    polygon(d,points,color)
    # The right half is a second paper plane, bounded by the same contour.
    right=[point for point in points if point[0]>=38]
    polygon(d,right,shade)
    A.line(d,[(38,27),(38,66)],'#f9cbbd',.9)
    A.line(d,[(25,46),(38,64),(52,45)],'#fff1d650',.7)
    A.line(d,[(19,27),(22,22),(27,20)],c.CREAM,1.1)
    return im


def valentine(im,d,t):
    shy=3*math.sin(math.pi*linear(t,.5,.8)) if .5<=t<.8 else 0
    orbit=ease(t,.8,1.75)
    meet=ease(t,1.75,2.25)
    angle=math.pi+math.pi*orbit
    ax=140+49*math.cos(angle)+shy;ay=126+29*math.sin(angle)
    bx=280-ax;by=252-ay
    ax=ax*(1-meet)+156*meet;bx=bx*(1-meet)+124*meet
    ay=ay*(1-meet)+132*meet;by=by*(1-meet)+132*meet
    bump=recoil(t,2.25,3.7,8,17)
    ax+=bump;bx-=bump
    aa=-9+18*orbit-9*meet+recoil(t,2.25,-4,8,17)
    ba=9-18*orbit+9*meet-recoil(t,2.25,-4,8,17)
    A.ellipse(d,(65,180,215,190),'#392c3c')
    if .8<t<1.75:
        for n in range(1,5):
            trail=max(0,orbit-.045*n);a=math.pi+math.pi*trail
            for sign in (-1,1):
                x=140+sign*49*math.cos(a);y=126+sign*29*math.sin(a)
                A.ellipse(d,(x-1,y-1,x+1,y+1),'#82687e')
    left=paper_heart(c.CORAL,'#e78279');right=paper_heart(c.LILAC,'#b49bc8')
    # Draw the more distant heart first as they pass each other on the orbit.
    items=[(ay,left,ax,aa),(by,right,bx,ba)]
    for yy,sprite,xx,turn in sorted(items,key=lambda item:item[0]):
        c.deform_stamp(im,sprite,xx,yy,angle=turn,sy=1-.035*abs(bump))
    if t>=2.25:
        p=ease(t,2.25,2.6)
        for n in range(3):
            if p>n/3:
                y=126+n*8
                A.line(d,[(136,y-2),(144,y+2)],c.GOLD,2)
                A.line(d,[(136,y),(144,y+4)],'#ffd9a7',.8)
        if 2.25<t<2.65:
            q=linear(t,2.25,2.65);spark(d,140,105,7*math.sin(q*math.pi))
    if t>=2.8:
        p=c.spring(t,2.8,3.2,1.3)
        A.heart(d,140,80-4*min(1,p),8*p,c.CORAL)
        for x,y in [(80,110),(207,116)]:spark(d,x,y,3*ease(t,2.8,3.2))
    c.reveal_caption(im,CLIPS['season-valentine'],t,3.03,'a tiny orbit. a little courage.',19)


def tangerine(d,x,y,r=15,angle=0):
    A.ellipse(d,(x-r,y-r*.86,x+r,y+r*.86),'#df873b',c.INK,.7)
    A.ellipse(d,(x-r+1,y-r*.85,x+r-3,y+r*.65),'#f6a64e')
    A.ellipse(d,(x-r*.57,y-r*.6,x-r*.13,y-r*.36),'#ffd290')
    for n in range(7):
        a=n*2.1;rr=r*(.45+.24*(n%2))
        A.ellipse(d,(x+rr*math.cos(a)-.65,y+rr*.7*math.sin(a)-.65,x+rr*math.cos(a)+.65,y+rr*.7*math.sin(a)+.65),'#da853a')
    A.line(d,[(x,y-r*.75),(x+2,y-r-3)],'#786849',1.2)
    polygon(d,[(x+1,y-r-1),(x+9,y-r-6),(x+12,y-r),(x+5,y-r+1)],c.SAGE)
    A.line(d,[(x+3,y-r),(x+9,y-r-3)],'#91a16d',.8)


@lru_cache(None)
def gold_flourish():
    im=Image.new('RGBA',(88*c.S,88*c.S));d=ImageDraw.Draw(im)
    # An original folded-paper rosette, deliberately free of letters or zodiac imagery.
    for n in range(8):
        a=n*math.tau/8
        def pt(rr,off=0):return (44+rr*math.cos(a+off),44+rr*math.sin(a+off))
        polygon(d,[pt(9),pt(25,-.2),pt(35),pt(25,.2)],c.GOLD,'#cf954f',.65)
        A.line(d,[pt(10),pt(31)],'#ffe2a5',.9)
    A.ellipse(d,(30,30,58,58),'#d9984c')
    A.ellipse(d,(33,32,55,54),c.GOLD)
    A.star(d,44,43,7,c.CREAM)
    return im


def lunar_new_year(im,d,t):
    wiggle=2.0*math.sin((t-.5)*22)*ease(t,.5,.65)*(1-ease(t,.85,1.1))
    lid=ease(t,1.1,1.5);rise=c.spring(t,1.3,1.95,1.35)
    bump=recoil(t,1.1,2.2,8,18)+recoil(t,1.75,1.6,9,18)
    A.ellipse(d,(77,181,224,192),'#392c3c')
    A.rounded(d,(105+wiggle,103,175+wiggle,185),5,'#a8494d',c.GOLD,1.2)
    polygon(d,[(106+wiggle,106),(140+wiggle,139-58*lid),(174+wiggle,106)],'#e8816d',c.GOLD,1)
    if rise>0:
        # Rosette and orange exist behind the front pocket throughout emergence.
        c.deform_stamp(im,gold_flourish(),140,155-63*rise,sy=.85+.15*min(1,rise),angle=-8*(1-min(1,rise)))
        if t>=1.75:
            q=linear(t,1.75,2.5);xx=140+67*ease(q,0,1)
            yy=122+49*q-43*math.sin(math.pi*q)+recoil(t,2.5,-3,9,18)
            tangerine(d,xx,yy)
    A.rounded(d,(105+wiggle,112+bump,175+wiggle,185),4,'#bd5352',c.INK,1)
    A.rounded(d,(109+wiggle,115+bump,171+wiggle,181),3,'#d96558')
    A.line(d,[(111+wiggle,116+bump),(169+wiggle,116+bump)],'#f6a175',1)
    A.rounded(d,(113+wiggle,120+bump,167+wiggle,177),2,None,c.GOLD,.8)
    A.ellipse(d,(128+wiggle,137+bump,152+wiggle,161+bump),'#c24f4d',c.GOLD,1)
    A.star(d,140+wiggle,149+bump,7,c.GOLD)
    if t>=2.9:
        p=ease(t,2.9,3.2)
        for x,y,r in [(87,100,4),(194,91,4),(221,132,3)]:spark(d,x,y,r*p)
    c.reveal_caption(im,CLIPS['season-lunar-new-year'],t,3.08,'a little packet of bright beginnings.',19)


@lru_cache(None)
def feather(color):
    im=Image.new('RGBA',(32*c.S,65*c.S));d=ImageDraw.Draw(im)
    pts=bezier((16,62),(2,49),(1,20),(15,3),20)+bezier((15,3),(30,15),(29,43),(16,62),20)[1:]
    polygon(d,pts,color,'#8c737f',.65)
    A.line(d,bezier((16,62),(13,45),(17,22),(15,7)),c.CREAM,.9)
    for yy in range(16,54,7):
        A.line(d,[(16,yy+8),(7 if yy>26 else 10,yy)],'#e7d9be',.65)
        A.line(d,[(16,yy+8),(25 if yy>26 else 22,yy)],'#e7d9be',.65)
    return im


def carnival(im,d,t):
    antic=-7*math.sin(math.pi*linear(t,.5,1.1)) if .5<t<1.1 else 0
    q=max(0,t-1.1)
    shimmy=11*math.sin(q*15)*math.exp(-1.9*q) if 1.1<=t<2.2 else 0
    bow=math.sin(math.pi*linear(t,2.25,2.8)) if 2.25<t<2.8 else 0
    angle=antic+shimmy;yy=134+8*bow
    A.ellipse(d,(59,181,221,192),'#392c3c')
    def transform(x,y):
        a=math.radians(-angle);dx=x-140;dy=y-134
        return (140+dx*math.cos(a)-dy*math.sin(a),yy+dx*math.sin(a)+dy*math.cos(a))
    # Independent silk ribbons trail behind the mask with a slight phase lag.
    for side in (-1,1):
        wave=3*math.sin(t*7+side)*ease(t,.5,.8)*(1-ease(t,2.4,3.0))
        pts=bezier((140+side*61,137),(140+side*(96+wave),131),(140+side*104,174),(140+side*72,182),35)
        pts=[transform(x,y) for x,y in pts]
        A.line(d,pts,'#aa7e9d',5.5)
        A.line(d,[(x-.6,y-.7) for x,y in pts],c.LILAC,2.4)
    for n,(x,y,turn,color) in enumerate([(106,99,26,c.CREAM),(115,94,2,c.SAGE),(126,99,-23,c.LILAC)]):
        px,py=transform(x,y)
        flutter=3*math.sin(t*8+n)*ease(t,1.1,1.3)*(1-ease(t,2.2,2.8))
        c.deform_stamp(im,feather(color),px,py,angle=turn+angle+flutter)
    mask=c.prop(SOURCE/'carnival-mask.png',143,83)
    c.deform_stamp(im,mask,140,yy,sy=1-.08*bow,angle=angle)
    if t>=2.25:
        age=t-2.25
        for n in range(18):
            side=-1 if n%2 else 1;vx=side*(20+(n*11)%40);vy=-50-(n*7)%21
            startx=140+side*47;starty=128
            landing=(-vy+math.sqrt(vy*vy+4*100*(187-starty)))/200
            a=min(age,landing);xx=startx+vx*(1-math.exp(-1.3*a))/1.3;cy=starty+vy*a+100*a*a
            if age>landing:cy=187-2*abs(recoil(age,landing,1,10,20))
            if not 18<xx<262 or not 49<cy<192:continue
            color=[c.CORAL,c.GOLD,c.SAGE,c.LILAC][n%4]
            if n%3==0:A.star(d,xx,cy,2.8,color,angle=a*4+n)
            else:
                b=n+a*5
                A.line(d,[(xx-2*math.cos(b),cy-2*math.sin(b)),(xx+2*math.cos(b),cy+2*math.sin(b))],color,2)
    if t>=3.0:
        for x,y in [(64,95),(218,92)]:spark(d,x,y,4*ease(t,3.,3.3))
    c.reveal_caption(im,CLIPS['season-carnival'],t,3.05,'a little shimmy. a lot of entrance.',19)


def felt_hat(d,x,y,bounce=0):
    A.ellipse(d,(x-59,y+23+bounce,x+59,y+41+bounce),'#2c5749',c.INK,1.1)
    A.ellipse(d,(x-57,y+23+bounce,x+57,y+35+bounce),'#618e6a')
    polygon(d,[(x-37,y+29+bounce),(x-30,y-39+bounce),(x+30,y-39+bounce),(x+37,y+29+bounce)],'#487b59',c.INK,1.1)
    polygon(d,[(x-30,y-36+bounce),(x-25,y+25+bounce),(x-34,y+27+bounce)],'#6b9870')
    A.ellipse(d,(x-30,y-43+bounce,x+30,y-33+bounce),'#7ca77b',c.INK,.9)
    A.line(d,[(x-23,y-37+bounce),(x+22,y-37+bounce)],'#a8c696',1)
    polygon(d,[(x-35,y+9+bounce),(x+35,y+9+bounce),(x+37,y+27+bounce),(x-37,y+27+bounce)],'#244b3e')
    A.rounded(d,(x-9,y+10+bounce,x+9,y+26+bounce),2,c.GOLD)
    A.rounded(d,(x-5,y+13+bounce,x+5,y+23+bounce),1,'#244b3e')


def shamrock(d,x,y,opening,angle=0):
    A.line(d,bezier((x,y-1),(x+3,y+7),(x-1,y+13),(x+5,y+20),20),'#91ae7f',2.4)
    # Exactly three fixed heart-shaped leaflets open from one folded stack.
    for n in (0,2,1):
        a=angle+math.radians((n-1)*74*opening)
        points=heart_points(0,-12.5,12)
        points=[(x+px*math.cos(a)-py*math.sin(a),y+px*math.sin(a)+py*math.cos(a)) for px,py in points]
        polygon(d,points,[c.SAGE,'#dbe4b6','#b3ca94'][n],'#7e9b6a',.7)
        A.line(d,[(x,y),(x+18*math.sin(a),y-18*math.cos(a))],'#789668',.8)


def lucky_clover(im,d,t):
    p=ease(t,.5,1.65)
    x=215-72*p+15*(1-p)*math.sin(math.pi*p*2)
    y=73+74*p-13*math.sin(math.pi*p)
    bump=recoil(t,1.65,3.6,7,16)
    opening=ease(t,1.9,2.5)
    A.ellipse(d,(65,180,215,192),'#392c3c')
    felt_hat(d,140,143,bump*.55)
    if t>=1.65:
        x=143+recoil(t,1.65,-1.2,7,16);y=147+bump
    shamrock(d,x,y,opening,angle=(1-p)*.65+recoil(t,1.65,.12,7,16))
    if 1.65<t<2.05:
        q=linear(t,1.65,2.05)
        for side in (-1,1):A.line(d,[(143+side*(21+q*4),162-q*5),(143+side*(26+q*4),158-q*5)],c.GOLD,1)
    if t>=2.5:
        p=ease(t,2.5,2.85)
        for xx,yy,r in [(77,97,4),(204,110,4),(203,154,3)]:spark(d,xx,yy,r*p)
    c.reveal_caption(im,CLIPS['season-lucky-clover'],t,2.98,'three little leaves. one soft landing.',18)


SCENES={
 'season-valentine':valentine,'season-lunar-new-year':lunar_new_year,
 'season-carnival':carnival,'season-lucky-clover':lucky_clover,
}


def scene(name,t):
    im=c.stage(CLIPS[name]);d=ImageDraw.Draw(im)
    SCENES[name](im,d,t)
    return im


def event(at,kind,hz=660,duration=.3,gain=.12):
    return dict(at=at,kind=kind,hz=hz,duration=duration,gain=gain)


def audio_events(name):
    return {
      'season-valentine':[event(.5,'pluck',523,.22,.10),event(.65,'pluck',659,.2,.08),
        event(.8,'rustle',750,.7,.065),event(2.25,'tick',1300,.12,.13),event(2.3,'pop',660,.15,.075),
        event(2.8,'chord',[440,554,659],.65,.12)],
      'season-lunar-new-year':[event(.5,'rustle',650,.38,.08),event(1.1,'pop',420,.17,.1),
        event(1.2,'rustle',600,.35,.075),event(1.55,'pluck',[660,988],.45,.14),
        event(2.5,'thud',380,.2,.1),event(2.9,'bell',1319,.5,.12)],
      'season-carnival':[event(.5,'rustle',700,.42,.085),event(1.1,'whoosh',600,.4,.08),
        event(1.2,'pluck',698,.19,.09),event(1.55,'pluck',932,.25,.11),
        event(2.25,'thud',360,.16,.09),event(2.3,'jingle',[1047,1397],.4,.075),
        event(2.65,'tick',1600,.08,.07),event(3.,'chord',[349,523,698],.6,.105)],
      'season-lucky-clover':[event(.5,'whoosh',[1100,650],.7,.075),
        event(1.65,'thud',340,.17,.1),event(1.9,'rustle',700,.5,.08),
        event(2.5,'pluck',784,.48,.14),event(2.6,'pluck',1175,.35,.055)],
    }[name]


def review():
    import numpy as np
    REVIEW.mkdir(parents=True,exist_ok=True)
    overview=Image.new('RGB',(560,534),c.BG);od=ImageDraw.Draw(overview)
    qa={}
    times=(.45,.9,1.35,1.8,2.25,2.5,2.9,3.55)
    for index,(name,info) in enumerate(CLIPS.items()):
        poses=Image.new('RGB',(840,266),c.BG);pd=ImageDraw.Draw(poses)
        for n,t in enumerate((.7,1.65,3.55)):
            poses.paste(scene(name,t).convert('RGB').resize((280,240),Image.Resampling.LANCZOS),(n*280,0))
            pd.text((n*280+8,245),f'{name} / {t:.2f}s',fill=c.CREAM)
        poses.save(REVIEW/f'{name}-poses.png')
        motion=Image.new('RGB',(1120,528),c.BG);md=ImageDraw.Draw(motion)
        for n,t in enumerate(times):
            xx=(n%4)*280;yy=(n//4)*264
            motion.paste(scene(name,t).convert('RGB').resize((280,240),Image.Resampling.LANCZOS),(xx,yy))
            md.text((xx+8,yy+244),f'{t:.2f}s',fill=c.CREAM)
        motion.save(REVIEW/f'{name}-motion.png')
        xx=(index%2)*280;yy=(index//2)*267
        overview.paste(scene(name,3.55).convert('RGB').resize((280,240),Image.Resampling.LANCZOS),(xx,yy))
        od.text((xx+8,yy+244),info['title'],fill=c.CREAM)
        header=np.asarray(c.stage(info))[:38*c.S]
        for n in range(90):
            frame=scene(name,n/20)
            assert frame.size==(840,720) and frame.mode=='RGBA'
            assert np.array_equal(np.asarray(frame)[:38*c.S],header),(name,n,'header')
        cover=c.cover(info)
        cover.save(REVIEW/f'{name}-cover.png')
        data,report=c.synthesize(name,audio_events(name))
        samples=np.frombuffer(data,dtype='<i2').astype(float)/32768
        cue_rms={str(at):float(np.sqrt(np.mean(samples[round(at*c.RATE):round((at+.1)*c.RATE)]**2))) for at,label in info['cues']}
        assert min(cue_rms.values())>.001,(name,cue_rms)
        qa[name]=dict(report,frames_verified=90,header_fixed=True,cue_rms=cue_rms,
                      mode='RGBA',native_size=[280,240],pose_times=[.7,1.65,3.55])
    overview.save(REVIEW/'overview.png')
    (REVIEW/'qa.json').write_text(json.dumps(qa,indent=2),encoding='utf-8')
    (REVIEW/'review-notes.json').write_text(json.dumps(dict(
        purpose='Four fresh object-led greetings with fixed prop geometry and no Robbi face.',
        illustration='One original generated carnival mask, unchanged PNG; other objects are deterministic native paper/felt illustrations.',
        cultural_scope='Red-packet-inspired Lunar New Year greeting, no invented script, year, or zodiac; original sound gestures. Exactly three leaflets in the St Patrick shamrock.',
        motion=['Paper hearts hesitate, orbit, meet, recoil, and clasp.',
                'Packet anticipates, flap pivots, gold rosette springs, tangerine arcs and lands behind pocket as needed.',
                'Mask anticipates, feathers and ribbons shimmy, bow triggers falling confetti.',
                'Folded shamrock drifts, lands on felt hat with shared recoil, then opens three rigid leaflets.'],
        review='Pending visual inspection of native contacts and motion strips.',issues=[]),indent=2),encoding='utf-8')
    return REVIEW


if __name__=='__main__':print(review())
