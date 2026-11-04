"""Ten original object-led meme stories, authored at 840 x 720. No robot faces."""
from functools import lru_cache
from pathlib import Path
import math
import numpy as np
from PIL import Image, ImageDraw
import seasonal_common as c

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'assets/dynamics_sources/meme-encore-v1'
art = c.art
S = 3
CREAM, GOLD, CORAL, SAGE, INK = '#fff1d6', '#ffc579', '#ff9683', '#c4d3a2', '#272330'

CLIPS = {
 'meme-dumpster-promotion': dict(title='Failing upward',heading='FAILING UPWARD',group='work',
    quote='MANAGEMENT MATERIAL.',voice='Management material.',setup='a small operational issue.',
    when='The dumpster is on fire. Naturally, it gets promoted.',
    sound='Metal rattle, tiny fire crackles, a coronation ding and a deadpan verdict.',
    sfx='Close-up cartoon metal dumpster rattles, tiny harmless fire crackles, then a comically grand short brass coronation flourish. Dry clean cinematic foley. No speech, no voices, no background music.'),
 'meme-meeting-escape': dict(title='This meeting has wings',heading='QUICK SYNC?',group='work',
    quote="COULD'VE BEEN AN EMAIL.",voice="Could've been an email.",setup='just a quick sixty minutes.',
    when='A calendar invitation sprouts paper wings and escapes its own meeting.',
    sound='Calendar ping, fluttering paper, flight whoosh and one pointed observation.',
    sfx='A single pleasant calendar notification ping, crisp paper unfolding into wings, a brisk fluttering paper flight whoosh. Funny miniature foley. No speech, no voices, no music.'),
 'meme-brain-tabs': dict(title='Too many tabs',heading='BRAIN: 47 TABS',group='brain',
    quote='ONE TAB HAS MUSIC.',voice='One tab is playing music.',setup='which one? excellent question.',
    when='Browser windows multiply until a rubber duck is revealed as the DJ.',
    sound='Accelerating computer clicks, a digital wobble, a tiny duck squeak and voice.',
    sfx='Playful accelerating sequence of little computer mouse clicks and soft window opening pops, ending in a bewildered rubber duck squeak. Dry cartoon sound design, no speech, no voices, no music.'),
 'meme-delulu-launch': dict(title='Delusion propulsion',heading='ZERO QUALIFICATIONS',group='brain',
    quote='POWERED BY DELUSION.',voice='Powered by delusion.',setup='unlimited confidence, though.',
    when='A toaster launches its bread into orbit, entirely on self-belief.',
    sound='Toaster lever, escalating mechanical rattle, rocket rush and a confident verdict.',
    sfx='A retro toaster lever clicks down, tense spring rattles building to a ridiculous tiny rocket ignition and fast upward whoosh. Premium funny foley, no speech, no voices, no music.'),
 'meme-last-spoon': dict(title='My last spoon',heading='ONE MORE THING?',group='mood',
    quote='ABSOLUTELY NOT.',voice='Absolutely not.',setup='remaining capacity: one spoon.',
    when='A raccoon watches its final spoon get collected. The answer is no.',
    sound='Delicate silver spoon clink, squeaky winch, a wooden refusal stamp and voice.',
    sfx='One silver spoon delicately clinks, a little squeaky pulley lifts it away, followed by one dry wooden stamp thump. Funny understated tactile foley. No speech, no voices, no music.'),
 'meme-tiny-deadline': dict(title='Deadline energy',heading='MAKING GREAT TIME',group='work',
    quote='DUE YESTERDAY.',voice='Due yesterday.',setup='my pace is a lifestyle.',
    when='A determined snail makes three centimetres of progress. The deadline disagrees.',
    sound='Tiny sticky footsteps, ticking tension, a large rubber stamp and the bad news.',
    sfx='Several tiny funny sticky snail footsteps, a short accelerating clock tick, then a heavy rubber office stamp thud with a little spring wobble. No speech, no voices, no music.'),
 'meme-vibe-check': dict(title='Vibe security',heading='VIBE CHECK',group='mood',
    quote='NO THOUGHTS. APPROVED.',voice='No thoughts. Approved.',setup='please empty your brain.',
    when='A rubber duck clears the scanner with absolutely nothing to declare.',
    sound='Conveyor motor, electronic scan sweep, two approval pings and a dry announcement.',
    sfx='Tiny airport conveyor motor hum, a clean electronic scanner sweep, two satisfying soft approval chimes, a very small rubber duck squeak. No speech, no voices, no music.'),
 'meme-budget-luxury': dict(title='Luxury budget',heading='MY SAVINGS',group='mood',
    quote='FINANCIALLY DECORATIVE.',voice='Financially decorative.',setup='please do not touch the exhibit.',
    when='One coin receives the luxury museum treatment. Still one coin.',
    sound='Silky curtain pull, a delicate coin spin, sparkling reveal and a luxury-style verdict.',
    sfx='A luxurious velvet curtain swishes open, one tiny coin spins and settles with a delicate metallic clink, an elegant shimmering glass chime. Dry premium foley. No speech, no voices, no music.'),
 'meme-overthinking': dict(title='Investigation complete',heading='OVERTHINKING DEPT.',group='brain',
    quote='I CONNECTED NOTHING.',voice='I connected nothing.',setup='the evidence is mostly vibes.',
    when='Red string takes over the evidence board. The conclusion remains a question mark.',
    sound='Pinboard taps, rapid thread twangs, a deflating little pluck and a confession.',
    sfx='Quick corkboard pushpin taps and increasingly frantic little plucked string twangs, ending in one deflating low rubber boing. Dry playful miniature detective foley. No speech, no voices, no music.'),
 'meme-couch-quest': dict(title='Side quest accepted',heading='MAIN QUEST: EXIST',group='mood',
    quote='SIDE QUEST: NAP.',voice='Side quest. Nap.',setup='a hero must choose wisely.',
    when='A quest scroll arrives. A cat chooses the cushion and earns a sleep achievement.',
    sound='Paper scroll unfurls, cushion poof, a tiny achievement chime and a sleepy verdict.',
    sfx='A fantasy parchment scroll quickly unfurls, a soft plush cushion poofs, followed by a tiny magical achievement chime and gentle sleepy exhale. No speech, no words, no music.'),
}
GROUPS = {'work':'Workplace incidents', 'brain':'Brain activity', 'mood':'Current mood'}


def text(d, value, x, y, size=18, color=CREAM):
    while d.textbbox((0,0),value,font=art.font(size))[2] > 258*S:
        size -= .5
    d.text((round(x*S),round(y*S)),value,font=art.font(size),fill=color,anchor='mm')


def poly(d, points, fill):
    d.polygon([art.xy(p) for p in points],fill=fill)


@lru_cache(None)
def background(group):
    colors={'work':((28,34,40),(31,25,20)), 'brain':((34,27,46),(33,20,37)), 'mood':((25,38,38),(22,35,22))}
    base,lift=colors[group]
    y,x=np.mgrid[:720,:840]
    glow=np.exp(-((x-420)/310)**2-((y-390)/255)**2)
    rgb=np.stack([np.rint(b+l*glow).astype('uint8') for b,l in zip(base,lift)],axis=-1)
    return Image.fromarray(rgb).convert('RGBA')


def stage(name):
    detail=CLIPS[name]
    im=background(detail['group']).copy();d=ImageDraw.Draw(im)
    text(d,detail['heading'],140,22,22)
    art.line(d,[(16,41),(264,41)],'#73665b',.5)
    art.ellipse(d,(43,181,237,195),'#191d25')
    return im


@lru_cache(None)
def prop(name,width,height):
    paths={'duck':ROOT/'assets/dynamics_sources/seasonal-encore-v1/holidays/rubber-duck.png',
           'snail':ROOT/'assets/dynamics_sources/pocket-postcards-v1/slow-snail.png'}
    return c.prop(str(paths.get(name,SOURCE/(name+'.png'))),width,height)


def bob(t, amount=1):return math.sin(t*3.3)*amount
def p(t,a,b):return c.phase(t,a,b)


def label_card(d, value, x,y,w=80,color=CREAM,size=14):
    art.rounded(d,(x-w/2,y-13,x+w/2,y+13),5,color)
    text(d,value,x,y,size,INK)


def sparkles(d,t,cx=140,cy=118):
    for n in range(9):
        a=n*math.tau/9
        r=48+12*math.sin(n*1.9)
        art.star(d,cx+math.cos(a)*r,cy+math.sin(a)*r,2+math.sin(t*4+n),GOLD,a+t*.2)


def crown(d,x,y,s=1):
    poly(d,[(x-23*s,y+12*s),(x-27*s,y-9*s),(x-12*s,y-1*s),(x,y-18*s),(x+12*s,y-1*s),(x+27*s,y-9*s),(x+23*s,y+12*s)],GOLD)
    art.line(d,[(x-21*s,y+8*s),(x+21*s,y+8*s)],'#b47743',2)
    art.ellipse(d,(x-3*s,y-2*s,x+3*s,y+4*s),CORAL)


def flames(d,t,x,y,scale=1):
    for n in range(5):
        cx=x+(n-2)*16*scale; h=(24+12*math.sin(t*10+n*1.8))*scale
        poly(d,[(cx-13*scale,y),(cx-8*scale,y-h*.7),(cx+3*scale,y-h-10*scale),(cx+4*scale,y-h*.4),(cx+14*scale,y)],CORAL)
        poly(d,[(cx-7*scale,y),(cx+2*scale,y-h*.75),(cx+8*scale,y)],GOLD)


@lru_cache(None)
def calendar():
    im=Image.new('RGBA',(102*S,94*S));d=ImageDraw.Draw(im)
    art.rounded(d,(3,3,99,91),9,CREAM)
    art.rounded(d,(3,3,99,29),9,CORAL)
    for x in (24,77):art.rounded(d,(x-2,0,x+2,12),2,GOLD)
    text(d,'QUICK SYNC',51,18,12,INK);text(d,'60',51,53,32,INK);text(d,'MINUTES',51,77,11,INK)
    return im


def browser_window(d,x,y,n):
    art.rounded(d,(x,y,x+123,y+71),7,'#f2e9d8',INK,1)
    art.rounded(d,(x,y,x+123,y+17),7,('#e59d86','#bcacd3','#b5c9aa')[n%3])
    for j in range(3):art.ellipse(d,(x+7+j*8,y+6,x+10+j*8,y+9),INK)
    for j,w in enumerate((79,53,68)):art.rounded(d,(x+12,y+26+j*12,x+12+w,y+29+j*12),1,'#acaaa1')


def scene(name,t,caption=True):
    im=stage(name);d=ImageDraw.Draw(im)
    if name=='meme-dumpster-promotion':
        sh=math.sin(t*31)*p(t,.45,.9)*(1-p(t,2.3,2.8))*1.6
        flames(d,t,140+sh,137,p(t,.5,1.1))
        for x in (95,181):art.ellipse(d,(x-8,171,x+8,187),'#13171d',GOLD,1)
        poly(d,[(79+sh,126),(201+sh,126),(192+sh,177),(88+sh,177)],'#648e76')
        art.rounded(d,(72+sh,120,208+sh,133),4,'#91b399')
        for x in (103,139,175):art.line(d,[(x+sh,139),(x+sh,168)],'#416657',2)
        text(d,'TEAM LEAD',140+sh,151,12,CREAM)
        rise=c.spring(t,2.2,2.85)
        if rise>0:crown(d,140,48+38*rise,.85)
        if t>2.85:sparkles(d,t,140,130)
    elif name=='meme-meeting-escape':
        fly=p(t,1.7,3.1);x=140+300*fly;y=126-65*fly
        wing=p(t,.9,1.6)
        if wing:
            flap=math.sin(t*16)*18*wing
            poly(d,[(x-40,y),(x-95,y-27+flap),(x-73,y+17),(x-43,y+16)],'#d1c6ae')
            poly(d,[(x+40,y),(x+95,y-27+flap),(x+73,y+17),(x+43,y+16)],'#fff7e7')
            art.line(d,[(x-43,y+2),(x-91,y-23+flap)],GOLD,1)
        art.stamp(im,calendar(),x,y,-14*fly)
        if t>2.4:
            e=p(t,2.4,3.0);w=65*e
            art.rounded(d,(140-w,108,140+w,163),5,CREAM)
            art.line(d,[(140-w,109),(140,143),(140+w,109)],CORAL,2)
            label_card(d,'SENT',140,177,47,SAGE,11)
    elif name=='meme-brain-tabs':
        for n in range(7):
            appear=p(t,.3+n*.17,.6+n*.17);scatter=p(t,2.05,2.75)
            if not appear:continue
            x=48+n*9 + (n-3)*90*scatter;y=71+n*9+(n%2*2-1)*145*scatter
            if scatter<1:browser_window(d,x,y+18*(1-appear),n)
        reveal=c.spring(t,2.1,2.8)
        if reveal>0:
            c.deform_stamp(im,prop('duck',115,107),140,137,1,reveal)
            for x in (97,167):art.rounded(d,(x,99,x+17,128),5,INK,GOLD,2)
            d.arc(art.xy((98,77,182,150)),185,355,fill=GOLD,width=5*S)
            for n in range(3):
                nx=202+n*13;ny=94+n*20+bob(t+n,6)
                art.ellipse(d,(nx-4,ny+8,nx+3,ny+13),GOLD)
                art.line(d,[(nx+2,ny+10),(nx+2,ny-6),(nx+8,ny-2)],GOLD,2)
    elif name=='meme-delulu-launch':
        rise=p(t,1.65,2.85);shake=math.sin(t*40)*2*p(t,.7,1)*(1-rise)
        if rise>0:
            for n in range(7):
                xx=140+(n-3)*14;yy=170+math.sin(n*2+t*8)*6
                art.ellipse(d,(xx-17,yy-9,xx+17,yy+9),'#cab8af')
        ty=129-190*rise
        if rise<1:flames(d,t,140,ty+44,.7*rise)
        toast=Image.new('RGBA',(58*S,62*S));td=ImageDraw.Draw(toast)
        art.rounded(td,(5,3,53,58),13,'#bb793c',CREAM,2)
        art.rounded(td,(10,8,48,53),10,'#ffda91')
        text(td,'CEO',29,29,14,INK)
        art.stamp(im,toast,140+shake,ty,-15*rise)
        art.stamp(im,prop('toaster',146,101),140+shake,153+6*math.sin(rise*math.pi))
        if rise>=1:
            for x,y in ((78,76),(208,81),(215,120)):art.star(d,x,y,4,GOLD,t)
            label_card(d,'ORBIT ACHIEVED',140,72,137,SAGE,12)
    elif name=='meme-last-spoon':
        art.stamp(im,prop('raccoon',142,133),118,125+bob(t,.5))
        lift=p(t,1.1,2.5);sy=140-120*lift
        if lift<1:
            art.line(d,[(214,45),(214,sy-24)],'#aa9a8e',1)
            art.rounded(d,(210,sy-12,216,sy+28),3,'#b4bec4')
            art.ellipse(d,(201,sy-31,225,sy-8),'#e1e4de',INK,1)
            art.ellipse(d,(205,sy-27,220,sy-13),'#8e9a9f')
        if t>=2.6:
            stamp=Image.new('RGBA',(79*S,44*S));sd=ImageDraw.Draw(stamp)
            art.rounded(sd,(2,2,77,42),5,None,CORAL,3);text(sd,'NOPE',39,22,25,CORAL)
            art.stamp(im,stamp,206,143+12*(1-c.spring(t,2.6,2.9)),-14)
    elif name=='meme-tiny-deadline':
        travel=p(t,.4,2.3);x=77+45*travel
        for xx in range(28,250,14):art.line(d,[(xx,181),(xx+6,181)],'#9a9784',1)
        art.stamp(im,prop('snail',132,87),x,144+bob(t,.6))
        art.line(d,[(242,180),(242,93)],GOLD,2)
        poly(d,[(243,94),(261,104),(243,114)],CORAL)
        if t>2.4:
            stamp=Image.new('RGBA',(191*S,48*S));sd=ImageDraw.Draw(stamp)
            art.rounded(sd,(3,3,188,45),5,CORAL);text(sd,'YESTERDAY',96,25,25,INK)
            art.stamp(im,stamp,140,81-75*(1-c.spring(t,2.4,2.85)),-8)
    elif name=='meme-vibe-check':
        art.rounded(d,(30,165,250,186),8,'#5e6f69')
        for n in range(11):
            xx=36+((n*20+t*20)%204);art.ellipse(d,(xx,171,xx+8,179),'#a9b9a7')
        x=54+134*p(t,.4,2.6)
        art.stamp(im,prop('duck',87,83),x,125+bob(t,.7))
        art.rounded(d,(119,66,171,167),9,None,SAGE,6)
        scan=74+78*((t*.8)%1)
        art.line(d,[(125,scan),(165,scan)],GOLD,2)
        if t>2.7:
            art.ellipse(d,(192,74,225,107),SAGE)
            art.line(d,[(200,90),(207,97),(217,82)],INK,3)
    elif name=='meme-budget-luxury':
        # A velvet pedestal and orbiting single coin; the two curtains part independently.
        art.rounded(d,(95,143,185,186),5,'#956179')
        art.ellipse(d,(95,136,185,153),'#c08aa1')
        spin=1 if t>2.5 else .23+.77*abs(math.cos(t*8))
        art.ellipse(d,(140-22*spin,90,140+22*spin,134),'#b7873b')
        art.ellipse(d,(140-18*spin,93,140+18*spin,131),GOLD)
        if spin>.65:text(d,'1',140,113,25,'#9d632b')
        open_=p(t,.4,1.45)
        for side in (-1,1):
            cx=140+side*91*open_
            art.rounded(d,(cx-47,53,cx+47,181),3,'#733e57')
            for offset in (-29,-9,11,31):art.line(d,[(cx+offset,54),(cx+offset+4,181)],'#99566b',4)
        if t>2.5:sparkles(d,t,140,112)
    elif name=='meme-overthinking':
        art.rounded(d,(34,56,246,188),7,'#a5805c',GOLD,2)
        nodes=[(65,85),(140,81),(211,92),(81,153),(177,150)]
        for n,(x,y) in enumerate(nodes):
            art.rounded(d,(x-19,y-15,x+19,y+21),2,CREAM)
            text(d,['?','!','?','...','?'][n],x,y+3,20,INK)
        for n in range(18):
            q=p(t,.4+n*.1,.7+n*.1)
            if q:
                a=nodes[n%5];b=nodes[(n*3+2)%5]
                bend=(math.sin(n*9)*12,math.cos(n*7)*10)
                end=(a[0]+(b[0]-a[0])*q,a[1]+(b[1]-a[1])*q)
                art.line(d,[(a[0]+bend[0],a[1]),end],CORAL,1)
        for x,y in nodes:art.ellipse(d,(x-3,y-14,x+3,y-8),'#8e3946')
        if t>2.7:
            art.ellipse(d,(111,98,169,156),INK,GOLD,2);text(d,'?',140,128,39,GOLD)
    elif name=='meme-couch-quest':
        breath=1+.012*math.sin(t*2.4)
        art.ellipse(d,(55,164,226,192),'#9c7caa')
        art.ellipse(d,(62,156,218,184),'#ceadd0')
        drop=20*(1-c.spring(t,.7,1.4))
        c.deform_stamp(im,prop('cat',147,121),137,128-drop,1,breath)
        scroll=p(t,.3,.9)*(1-p(t,1.8,2.3))
        if scroll>0:
            art.rounded(d,(186,55,257,55+76*scroll),6,CREAM)
            if scroll>.8:text(d,'QUEST',221,77,12,INK);text(d,'!',221,105,25,INK)
        if t>2.3:
            for n in range(3):text(d,'z',205+n*10,128-n*22+bob(t+n,3),14+n*3,GOLD)
            crown(d,125,66,.55*p(t,2.3,2.8))
    if caption:
        detail=CLIPS[name]
        if t<2.75:
            alpha=1-p(t,2.45,2.7)
            layer=Image.new('RGBA',im.size);text(ImageDraw.Draw(layer),detail['setup'],140,216,15,'#cbbeb4')
        else:
            alpha=p(t,2.75,2.95)
            layer=Image.new('RGBA',im.size);ld=ImageDraw.Draw(layer)
            art.rounded(ld,(9,201,271,233),8,SAGE)
            text(ld,detail['quote'],140,217,19,INK)
        layer.putalpha(layer.getchannel('A').point(lambda v:round(v*alpha)));im.alpha_composite(layer)
    return im


@lru_cache(None)
def cover(name):
    # The final visual joke remains on screen; no robot face is loaded at either end.
    return scene(name,4.65).convert('RGB')


def render(name,t):
    card=cover(name)
    if t<.1 or t>=5.8:return card.copy()
    im=scene(name,t).convert('RGB')
    # Flight props stay inside the stage; titles never move or get occluded.
    im.paste(card.crop((0,0,840,44*S)),(0,0))
    if t<.3:return Image.blend(card,im,p(t,.1,.3))
    if t>5.55:return Image.blend(im,card,p(t,5.55,5.8))
    return im
