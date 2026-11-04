"""Ten new six-second visual stories about boundaries, absurdity and perspective."""
from functools import lru_cache
from pathlib import Path
import math
import numpy as np
from PIL import Image,ImageDraw
from meme_encore_scenes import art,text,poly,c,S

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/dynamics_sources/meme-reflections-v1'
CREAM='#fff1d6'; GOLD='#ffd081'; CORAL='#ef998a'; SAGE='#c5d6a4'; INK='#292932'
GROUPS={'boundaries':'Peace, with conditions','absurdity':'A very normal situation','perspective':'Small things that matter'}
CLIPS={
 'meme-boundary-garden':dict(title='A prickly boundary',heading='PERSONAL SPACE',group='boundaries',
    setup='available does not mean unlimited.',quote='BOUNDARIES. NOT ATTITUDE.',voice='Boundaries. Not attitude.',
    when='A cactus raises a little fence. Incoming demands bounce off it.',
    sound='Message pops, a rising wooden fence, rubbery rebounds and a calm verdict.',
    sfx='A few tiny notification pops approach, a miniature wooden garden fence rattles up, two soft rubbery rebounds, a satisfying wooden latch click. Warm close-up cartoon foley, no voices or music.'),
 'meme-weather-candle':dict(title='Still here. Still lit.',heading='FORECAST: A LOT',group='perspective',
    setup='today brought its own weather.',quote='STILL HERE. STILL LIT.',voice='Still here. Still lit.',
    when='A tiny umbrella shelters a stubborn candle from an oversized raincloud.',
    sound='Soft rain, umbrella fabric opening, a small warm flame and a quiet punchline.',
    sfx='Gentle close-up rain patter, a tiny umbrella snaps open with a soft fabric swish, rain patters warmly on its canopy, one delicate flame flicker. Reassuring miniature foley, no voices, no thunder, no music.'),
 'meme-priority-octopus':dict(title='Everything is urgent',heading='ALL TOP PRIORITY',group='absurdity',
    setup='eight arms. zero clear instructions.',quote='PICK. ONE. THING.',voice='Pick. One. Thing.',
    when='An exhausted octopus juggles eight urgent flags, then keeps exactly one.',
    sound='Escalating flag flutters, little squeaks, a decisive pop and blunt advice.',
    sfx='Eight little cloth flags flap in quick escalating bursts, funny soft rubber squeaks overlap, then a single crisp pop and one calm wooden tick. Comedic dry foley, no voices, no words, no music.'),
 'meme-moving-goalposts':dict(title='Almost there, allegedly',heading='ALMOST THERE',group='absurdity',
    setup='success is just one step away.',quote='THE GOALPOSTS HAVE LEGS.',voice='The goalposts have legs.',
    when='A ball approaches the goal. The goal grows shoes and jogs away.',
    sound='A rolling ball, a springy surprise, hurried little footsteps and a dry explanation.',
    sfx='A small ball rolls across a smooth floor, a funny short spring boing, a pair of tiny shoes scampers away with brisk footsteps. Clean playful cartoon foley. No speech, no voices, no music.'),
 'meme-perfection-draft':dict(title='Draft number 802',heading='DRAFT NUMBER 802',group='absurdity',
    setup='polish it just one more time.',quote='DONE BEATS IMAGINARY.',voice='Done beats imaginary.',
    when='A pencil endlessly reworks a page until a GOOD ENOUGH stamp lets it go.',
    sound='Pencil scribbles, eraser rub, a crisp approval stamp and a sensible verdict.',
    sfx='A pencil scribbles in an increasingly fussy little loop, a soft rubber eraser rub, then one confident satisfying rubber stamp on paper. Warm tactile office foley, no voices, no music.'),
 'meme-invisible-progress':dict(title='Underground achievements',heading='NOTHING HAPPENING?',group='perspective',
    setup='the surface has no updates.',quote="ROOTS DON'T DO PR.",voice="Roots don't do PR.",
    when='A quiet patch of soil reveals a busy root system before one leaf appears.',
    sound='Earth crumbles, soft branching crackles, a sprout pluck and a wry observation.',
    sfx='Soft close-up earth crumbling, several delicate branching woody crackles spreading underneath, then a tiny bright organic sprout pop. Intimate miniature nature foley, no voices, no music.'),
 'meme-inner-critic':dict(title='Feedback received',heading='INNER CRITIC: LIVE',group='boundaries',
    setup='thank you for the unsolicited noise.',quote='FEEDBACK NOTED. MUTED.',voice='Feedback noted. Muted.',
    when='A peaceful frog sends an intrusive megaphone away and presses mute.',
    sound='A comically nasal kazoo blurt, switch click, a soft airy release and a calm response.',
    sfx='A very short annoying nasal kazoo-like blurt, a clean mute switch click cuts it off, followed by a gentle soft airy exhale. Funny but soothing foley, no speech, no words, no music.'),
 'meme-reply-later':dict(title='Asynchronous by nature',heading='SEEN. PROCESSING.',group='boundaries',
    setup='instant delivery. optional urgency.',quote='REACHABLE. EVENTUALLY.',voice='Reachable. Eventually.',
    when='A stack of messages becomes a letter in a bottle and floats away on a gentle tide.',
    sound='Inbox ticks, paper folding, a glass clink, cork pop and gentle water.',
    sfx='Three small inbox ticks, crisp paper folds, a tiny glass bottle clinks and a cork softly pops into place, a gentle water plop. Miniature whimsical foley, no voices, no music.'),
 'meme-comparison-clocks':dict(title='Different clocks',heading='THE COMPARISON GAME',group='perspective',
    setup='their timeline is not your timer.',quote='DIFFERENT CLOCK. SAME WORTH.',voice='Different clock. Same worth.',
    when='Two clocks run at different speeds, then receive the same little gold star.',
    sound='Two contrasting clock ticks settle into a shared soft chime and a reassuring line.',
    sfx='Two small clocks tick at different speeds, one quick and one slow, then both stop gently and two equal warm glass chimes ring together. Quiet tactile foley, no voices, no music.'),
 'meme-plot-twist-boat':dict(title='An unplanned view',heading='SO MUCH FOR THE PLAN',group='perspective',
    setup='route recalculating. dramatically.',quote='WRONG TURN. GREAT VIEW.',voice='Wrong turn. Great view.',
    when='A rejected plan folds into a little boat and sails toward a warm sunrise.',
    sound='Paper crumple and folds, a soft splash, a bright reveal and a contented conclusion.',
    sfx='A sheet of paper crumples briefly then crisply folds into a boat, a soft small splash, gentle rippling water and a delicate warm glass sparkle. Storybook foley, no speech, no voices, no music.'),
}


def p(t,a,b):return c.phase(t,a,b)


@lru_cache(None)
def background(group):
    colors={'boundaries':((30,40,36),(25,33,21)), 'absurdity':((42,29,40),(35,20,26)), 'perspective':((30,34,47),(24,28,38))}
    base,lift=colors[group];y,x=np.mgrid[:720,:840]
    glow=np.exp(-((x-420)/340)**2-((y-380)/270)**2)
    grain=np.random.default_rng(741).normal(0,.38,(720,840))
    arr=np.stack([np.clip(np.rint(b+l*glow+grain),0,255).astype('uint8') for b,l in zip(base,lift)],axis=-1)
    return Image.fromarray(arr).convert('RGBA')


def stage(name):
    im=background(CLIPS[name]['group']).copy();d=ImageDraw.Draw(im)
    text(d,CLIPS[name]['heading'],140,23,21,CREAM)
    art.line(d,[(16,41),(264,41)],'#776d68',.5)
    art.ellipse(d,(44,181,236,194),'#1e222a')
    return im


@lru_cache(None)
def prop(name,w,h):return c.prop(str(SOURCE/(name+'.png')),w,h)


def pathline(d,points,progress,color=CREAM,width=2):
    count=(len(points)-1)*max(0,min(progress,1));n=int(count)
    if n>=len(points)-1:art.line(d,points,color,width);return
    if count<=0:return
    f=count-n;a=points[n];b=points[n+1]
    art.line(d,points[:n+1]+[(a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f)],color,width)


def envelope(d,x,y,w=40,h=25,color=CREAM):
    art.rounded(d,(x-w/2,y-h/2,x+w/2,y+h/2),3,color)
    art.line(d,[(x-w/2+1,y-h/2+1),(x,y+3),(x+w/2-1,y-h/2+1)],CORAL,1)


def leaf(d,x,y,angle=0,scale=1,color=SAGE):
    a=angle;points=[]
    for n in range(25):
        q=math.tau*n/24;xx=12*math.cos(q)*scale;yy=5*math.sin(q)*scale
        points.append((x+xx*math.cos(a)-yy*math.sin(a),y+xx*math.sin(a)+yy*math.cos(a)))
    poly(d,points,color)
    art.line(d,[(x-9*scale*math.cos(a),y-9*scale*math.sin(a)),(x+9*scale*math.cos(a),y+9*scale*math.sin(a))],'#73936f',.7)


def wave(d,t,y=176,color='#7eaeac',amp=3):
    points=[(x,y+amp*math.sin(x/22+t*2)) for x in range(20,262,3)]
    art.line(d,points,color,2)


def stamp(value,w=154):
    im=Image.new('RGBA',(round(w*S),45*S));d=ImageDraw.Draw(im)
    art.rounded(d,(2,2,w-2,43),6,SAGE,CREAM,1)
    text(d,value,w/2,23,20,INK)
    return im


def scene(name,t):
    im=stage(name);d=ImageDraw.Draw(im)
    if name=='meme-boundary-garden':
        art.stamp(im,prop('cactus',102,133),143,120+math.sin(t*2)*.5)
        rise=p(t,1.1,1.8);fy=211-57*rise
        for x in range(64,224,20):
            poly(d,[(x,fy+34),(x,fy),(x+6,fy-7),(x+12,fy),(x+12,fy+34)],CREAM)
        art.line(d,[(64,fy+11),(216,fy+11)],'#d4bda1',4)
        for n in range(3):
            q=p(t,.45+n*.48,1.45+n*.48)
            x=27+61*math.sin(q*math.pi);y=89+n*28-12*math.sin(q*math.pi)
            if q<1:
                art.rounded(d,(x-15,y-11,x+15,y+11),5,CORAL);text(d,'!',x,y,18,INK)
        if t>2.5:
            art.ellipse(d,(209,68,234,93),SAGE)
            art.line(d,[(215,80),(220,85),(229,73)],INK,2)
    elif name=='meme-weather-candle':
        # The opening cloud is deliberately far larger than the candle.
        for x,y,r in [(79,66,15),(100,60,20),(125,68,16),(148,63,17),(168,71,13)]:art.ellipse(d,(x-r,y-r,x+r,y+r),'#9fa5b8')
        umbrella=p(t,1.05,1.8)
        for n in range(16):
            x=50+(n*37)%173;y=78+((t*59+n*21)%101)
            if umbrella>.6 and 88<x<193 and y>103:continue
            art.line(d,[(x,y),(x-3,y+7)],'#99b5ce',1)
        art.rounded(d,(120,145,156,182),5,CREAM)
        art.ellipse(d,(120,140,156,151),'#ecd0a8')
        fy=132+math.sin(t*9)*1.2
        poly(d,[(138,fy-13),(131,fy+1),(135,fy+8),(144,fy+6),(146,fy)],GOLD)
        art.line(d,[(138,141),(138,148)],INK,1)
        if umbrella:
            ux=141;uy=113;w=64*umbrella
            canopy=[(ux-w,uy)]+[(ux+w*math.cos(a),uy-35*umbrella*math.sin(a)) for a in np.linspace(math.pi,0,32)]+[(ux+w,uy)]
            poly(d,canopy,CORAL)
            for u in (-.5,0,.5):art.line(d,[(ux,uy-35*umbrella),(ux+w*u,uy)],'#bd7168',1)
            art.line(d,[(ux,uy),(ux,130),(ux+7,136),(ux+12,131)],GOLD,2)
    elif name=='meme-priority-octopus':
        spots=[(41,101),(64,68),(100,57),(136,52),(174,57),(211,71),(239,105),(229,145)]
        for n,(x,y) in enumerate(spots):
            growth=p(t,.35+n*.10,.7+n*.10);depart=p(t,2.05+n*.055,2.5+n*.055)
            if growth and depart<1:
                x+=(x-140)*depart*2;y-=80*depart
                art.line(d,[(140+(x-140)*.75,157),(x,y+32)],GOLD,1.2)
                poly(d,[(x,y),(x+21,y+4+math.sin(t*12+n)*3),(x+18,y+23),(x,y+20)],CORAL)
                text(d,'!',x+9,y+12,15,INK)
        squash=1+.018*math.sin(t*13)*(1-p(t,2.4,3))
        c.deform_stamp(im,prop('octopus',166,112),140,140,1/squash,squash)
        if t>2.55:
            up=c.spring(t,2.55,3.0)
            art.line(d,[(201,164),(201,97-38*up)],GOLD,2)
            art.rounded(d,(201,62,247,86),4,SAGE);text(d,'ONE',224,74,13,INK)
    elif name=='meme-moving-goalposts':
        bx=40+138*p(t,.4,2.4);gx=180+54*p(t,1.1,2.5)
        walk=p(t,1.1,1.4);bounce=abs(math.sin(t*10))*5*walk
        gy=164-bounce
        for x in (gx-23,gx+23):
            art.line(d,[(x,gy-68),(x,gy)],CREAM,3)
            art.line(d,[(x,gy),(x+5*math.sin(t*10),181)],CORAL,3)
            art.ellipse(d,(x-7+4*math.sin(t*10),179,x+9+4*math.sin(t*10),185),GOLD)
        art.line(d,[(gx-23,gy-68),(gx+23,gy-68)],CREAM,3)
        for n in range(1,5):art.line(d,[(gx-23+n*9,gy-66),(gx-23+n*9,gy)],'#7a8796',.7)
        for n in range(1,7):art.line(d,[(gx-22,gy-68+n*9),(gx+22,gy-68+n*9)],'#7a8796',.7)
        art.ellipse(d,(bx-14,154,bx+14,182),CREAM)
        art.star(d,bx,168,7,INK,t*3)
        for n in range(3):art.line(d,[(bx-27-n*9,164+n*5),(bx-20-n*9,164+n*5)],GOLD,1)
        if t>2.7:text(d,'AGAIN?',123,84,19,CORAL)
    elif name=='meme-perfection-draft':
        art.rounded(d,(76,58,205,187),5,CREAM)
        for y in (88,110,132,154):art.line(d,[(89,y),(191,y)],'#d4c8b6',1)
        for n in range(3):
            pts=[(95+j*2.5,89+n*22+math.sin(j*1.1+t*3)*3) for j in range(35)]
            pathline(d,pts,p(t,.35+n*.3,.9+n*.3),'#687780',1.3)
        if t<2.35:
            x=104+64*(.5+.5*math.sin(t*8));y=112+14*math.sin(t*4)
            art.line(d,[(x,y),(x+37,y-44)],GOLD,9)
            poly(d,[(x-4,y-4),(x+5,y+3),(x-7,y+11)],'#b69270')
            art.line(d,[(x+34,y-41),(x+40,y-48)],CORAL,10)
        if t>2.3:
            q=c.spring(t,2.3,2.8)
            art.stamp(im,stamp('GOOD ENOUGH',174),140,121-66*(1-q),-11)
            for n in range(5):art.star(d,61+n*39,66+7*math.sin(n+t*3),2+q,GOLD,t+n)
    elif name=='meme-invisible-progress':
        art.rounded(d,(45,92,235,186),8,'#8c6550',GOLD,1)
        art.line(d,[(47,95),(232,95)],'#b79164',5)
        for n in range(28):
            x=53+(n*41)%176;y=109+(n*19)%69
            art.ellipse(d,(x,y,x+2,y+2),'#ac8968')
        growth=p(t,.4,2.65)
        for n in range(7):
            xx=140+(n-3)*23
            pts=[(140+(xx-140)*u**.85+3*math.sin(u*11+n)*u,
                  97+(78-abs(n-3)*5)*u) for u in np.linspace(0,1,26)]
            pathline(d,pts,max(0,min(1,growth*1.4-n*.07)),CREAM,1.6)
            if growth>.4:
                for side in (-1,1):pathline(d,[(xx,142),(xx+side*13,153),(xx+side*18,160)],p(t,1.2+n*.1,2.4+n*.05),GOLD,.8)
        sprout=p(t,2.05,2.9)
        art.line(d,[(140,94),(140,94-31*sprout)],SAGE,2)
        if sprout>0:leaf(d,132,78,-.45,sprout);leaf(d,149,68,.45,sprout)
    elif name=='meme-inner-critic':
        art.ellipse(d,(83,169,230,189),'#667c68')
        c.deform_stamp(im,prop('frog',128,130),164,120,1,1+.012*math.sin(t*2.5))
        leave=p(t,1.7,2.65);x=61-100*leave;y=111
        poly(d,[(x-17,y-13),(x+14,y-29),(x+14,y+29),(x-17,y+13)],CORAL)
        art.rounded(d,(x-28,y-10,x-15,y+10),3,GOLD)
        art.line(d,[(x-13,y+10),(x-18,y+30)],GOLD,5)
        if leave<.5:
            for n in range(3):d.arc(art.xy((x+12,y-15-n*6,x+31+n*9,y+15+n*6)),280,80,fill=GOLD,width=3)
        if t>2.2:
            art.ellipse(d,(39,115,85,161),SAGE)
            poly(d,[(48,135),(54,135),(61,129),(61,148),(54,142),(48,142)],INK)
            art.line(d,[(65,132),(75,144)],INK,2);art.line(d,[(75,132),(65,144)],INK,2)
            for n in range(3):leaf(d,102+n*52,73+8*math.sin(t+n),-.5+n*.4,.4)
    elif name=='meme-reply-later':
        bottle=p(t,1.65,2.65);x=140+42*p(t,2.7,5.4);y=130+math.sin(t*2)*2
        for n in range(3):
            q=p(t,.35+n*.25,1.8+n*.15)
            if q<1:envelope(d,82+n*49,86+28*math.sin(q*math.pi),36*(1-q)+4,22*(1-q)+4)
        if bottle>0:
            # A fixed glass outline, paper message and cork travel as one object.
            glass=Image.new('RGBA',(82*S,112*S));gd=ImageDraw.Draw(glass)
            art.rounded(gd,(8,26,74,109),13,'#8cb6af',CREAM,2)
            art.rounded(gd,(27,5,55,36),4,'#8cb6af',CREAM,2)
            art.rounded(gd,(29,0,53,15),3,'#c89565')
            envelope(gd,41,66,42,28)
            art.line(gd,[(18,42),(18,87)],'#c3d9c8',2)
            c.deform_stamp(im,glass,x,y,bottle*.72,bottle*.72,angle=12*math.sin(t*1.4))
        wave(d,t,169);wave(d,t+1,182,'#597e87',2)
        if t>2.8:text(d,'NO RUSH',62,150,13,GOLD)
    elif name=='meme-comparison-clocks':
        for n,cx in enumerate((80,200)):
            art.ellipse(d,(cx-43,77,cx+43,163),GOLD)
            art.ellipse(d,(cx-37,83,cx+37,157),CREAM)
            for j in range(12):
                a=j*math.tau/12
                art.line(d,[(cx+31*math.sin(a),120-31*math.cos(a)),(cx+34*math.sin(a),120-34*math.cos(a))],INK,1)
            angle=min(t,2.5)*(5 if n==0 else .65)
            art.line(d,[(cx,120),(cx+26*math.sin(angle),120-26*math.cos(angle))],INK,2)
            art.line(d,[(cx,120),(cx+17*math.sin(angle/3+.8),120-17*math.cos(angle/3+.8))],CORAL,3)
            text(d,('THEM','ME')[n],cx,176,13,CREAM)
            if t>2.5:art.star(d,cx,66,12*p(t,2.5,2.95),GOLD)
        if t>2.7:text(d,'=',140,123,28,SAGE)
    elif name=='meme-plot-twist-boat':
        fold=p(t,.75,2.25);travel=p(t,2.3,5.3);cx=112+42*travel;cy=144+math.sin(t*2)*2
        sunrise=p(t,1.7,3.0)
        art.ellipse(d,(193,125-58*sunrise,235,167-58*sunrise),GOLD)
        # Corners of the same paper converge into a small origami hull.
        start=[(-51,-69),(51,-69),(51,30),(-51,30)]
        finish=[(-62,0),(62,0),(35,25),(-35,25)]
        hull=[(cx+a[0]*(1-fold)+b[0]*fold,cy+a[1]*(1-fold)+b[1]*fold) for a,b in zip(start,finish)]
        poly(d,hull,CREAM)
        if fold<.7:text(d,'THE PLAN',cx,cy-31,16,INK)
        if fold>.35:
            sail=p(t,1.2,2.25)
            poly(d,[(cx-28,cy),(cx+10,cy-57*sail),(cx+10,cy)],'#e9b99e')
            poly(d,[(cx+10,cy),(cx+10,cy-57*sail),(cx+44,cy)],CREAM)
            art.line(d,[(cx-60,cy),(cx+60,cy)],CORAL,1)
        wave(d,t,172);wave(d,t+.7,185,'#608998',2)
        if t>2.8:
            for n in range(3):art.star(d,202+n*17,87+17*math.sin(n+t),2.5,GOLD)
    # Keep all moving pieces within the scene, clear of headings and subtitles.
    base=stage(name)
    im.paste(base.crop((0,0,840,44*S)),(0,0))
    im.paste(background(CLIPS[name]['group']).crop((0,198*S,840,720)),(0,198*S))
    layer=Image.new('RGBA',im.size);ld=ImageDraw.Draw(layer)
    if t<2.75:
        alpha=1-p(t,2.45,2.7);text(ld,CLIPS[name]['setup'],140,216,14,'#d0c3b5')
    else:
        alpha=p(t,2.75,2.95);art.rounded(ld,(8,201,272,233),8,SAGE)
        text(ld,CLIPS[name]['quote'],140,217,18.5,INK)
    layer.putalpha(layer.getchannel('A').point(lambda a:round(a*alpha)));im.alpha_composite(layer)
    return im


@lru_cache(None)
def cover(name):return scene(name,4.65).convert('RGB')


def render(name,t):
    card=cover(name)
    if t<.1 or t>=5.8:return card.copy()
    frame=scene(name,t).convert('RGB')
    if t<.3:return Image.blend(card,frame,p(t,.1,.3))
    if t>5.55:return Image.blend(frame,card,p(t,5.55,5.8))
    return frame
