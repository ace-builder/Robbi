"""Ten new miniature stories: boundaries, absurd bureaucracy and small recoveries."""
from functools import lru_cache
from pathlib import Path
import math
import numpy as np
from PIL import Image, ImageDraw
import seasonal_common as c

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/dynamics_sources/after-hours-v1'
art=c.art;S=3
CREAM='#fff0d8';INK='#283039';GOLD='#f4c57d';CORAL='#ed9887';SAGE='#bbd5b1';LILAC='#cdb9e5';BLUE='#91c4ca'
GROUPS={'boundaries':'Respectfully, no','absurd':'Highly questionable','gentle':'Still figuring it out'}
CLIPS={
 'meme-feature-cactus':dict(title='Working as designed',heading='QUALITY CONTROL',group='boundaries',quote='FEATURE, NOT A BUG.',voice='Working as designed.',setup='have you tried being smoother?',when='A cactus passes an inspection without sanding down a single spine.',sound='A measuring-tool slide, a soft glass inspection rub and a decisive approval stamp.',sfx='A miniature quality inspector slides a small metal measuring tool, gently rubs a magnifying glass, then presses one neat rubber approval stamp onto paper. Dry close-up tactile foley, no speech, no music, no digital beeps.'),
 'meme-urgent-laundry':dict(title='Delicate cycle',heading='URGENT: WASH SEPARATELY',group='boundaries',quote='LOOKS LESS URGENT NOW.',voice='Looks less urgent now.',setup='check the emotional care label.',when='An urgent memo takes a miniature wash cycle and comes out reading can wait.',sound='A little washing-machine latch, bubbly water swishing and a soft finished fabric flap.',sfx='A miniature washing machine door clicks shut, a brief tiny gentle motor turns with bubbly water swishing, then stops with a soft cloth flap and one little water droplet. Comically calm laundry foley. No voices, music or beeps.'),
 'meme-expectation-luggage':dict(title='Travel light',heading='GREAT EXPECTATIONS',group='gentle',quote='EXPECTATIONS: CABIN SIZE.',voice='Travel light.',setup='that will not fit overhead.',when='An enormous cloud of shoulds compresses into a tiny suitcase that rolls away.',sound='A soft balloon deflation, leather suitcase creak, clasp click and tiny rolling wheels.',sfx='A large soft air cushion slowly deflates with an amusing airy whoosh, a small leather case creaks closed with a neat metal clasp click, then two tiny wheels roll briefly. No speech, music or beeps.'),
 'meme-tomorrow-vending':dict(title='Freshly postponed',heading='DO IT NOW',group='absurd',quote='FRESHLY POSTPONED.',voice='Freshly postponed.',setup='press here for motivation.',when='Pressing NOW on a vending machine delivers three cans of tomorrow.',sound='A mechanical button, three distinct hollow can clunks and an unapologetic punchline.',sfx='A miniature vending machine button clicks, its little motor turns, then three small aluminum cans drop separately into a tray: clunk, clink, clunk, with light rattling decay. Funny tactile mechanism. No speech, no music, no electronic tones.'),
 'meme-plot-armor':dict(title='Somehow, still here',heading='PLOT ARMOR',group='absurd',quote='SOMEHOW, STILL HERE.',voice='Somehow, still here.',setup='defense budget: cardboard.',when='A cardboard shield protects a teacup from a meteor that becomes a sugar cube.',sound='A tiny meteor whoosh, a cardboard thunk, porcelain clink and a sugar plop.',sfx='A small descending airy meteor whistle, a surprisingly soft hollow cardboard shield thunk, followed by a delicate sugar cube dropping into a teacup with a porcelain clink and tiny plop. Dry cartoon foley, gentle, no voice or music.'),
 'meme-still-mending':dict(title='Some assembly required',heading='STILL MENDING',group='gentle',quote='SOME ASSEMBLY REQUIRED.',voice='Still a work in progress.',setup='the instructions were vague.',when='Two halves of a cracked mug reunite along a golden seam, then hold a small flower.',sound='Two fragile ceramic touches, a warm glass shimmer and one quiet botanical rustle.',sfx='Two delicate ceramic pieces gently touch and settle together, followed by a small warm rubbed-glass shimmer and a soft leaf unfurling rustle. Tender and hopeful, close-up foley, no voices, no melody.'),
 'meme-shrimp-authority':dict(title='The court has crustaceans',heading='OFFICIAL RULING',group='boundaries',quote='YOU MAY LOG OFF.',voice='You may log off.',setup='the court has heard enough.',when='A tiny shrimp judge bangs a gavel and stamps an official permission to log off.',sound='A tiny courtroom paper shuffle, one grounded wooden gavel knock and a legal ruling.',sfx='A very small wooden courtroom: brief paper shuffle, a tiny gavel lifts with a sleeve rustle then one clear warm wooden knock near the middle, followed by a rubber stamp tap. Dry close-up foley, no crowd, no speech, no music.'),
 'meme-capypause':dict(title='Unavailable, on purpose',heading='DEEP WORK',group='boundaries',quote='UNAVAILABLE. ON PURPOSE.',voice='Unavailable. On purpose.',setup='currently processing: nothing.',when='A serene capybara lowers its parasol while incoming mail bounces harmlessly away.',sound='Bath water, a parasol snap, papery deflections and a contented exhale.',sfx='Tiny peaceful bath water lap, a little parasol opening with a soft fabric fwip, then three light paper envelopes patter off the fabric canopy. End with a soft nonverbal airy puff. No human speech, no music, no notification beeps.'),
 'meme-hot-take-freezer':dict(title='Cool story',heading='HOT TAKE',group='absurd',quote='COOL STORY.',voice='Cool story.',setup='temperature: unnecessary.',when='A flaming speech bubble enters a freezer and comes back as one harmless ice cube.',sound='A little flame crackle, freezer latch, a short frosty breath and an ice clink.',sfx='A small harmless flame crackles briefly, a miniature freezer door softly clicks shut, a cool airy frosty whoosh, and one small ice cube clinks into a dish. Crisp but not harsh, no speech or music.'),
 'meme-control-tea':dict(title='One manageable thing',heading='CONTROL PANEL',group='gentle',quote='I CAN CONTROL THE TEA.',voice='I can control the tea.',setup='everything is a lot of knobs.',when='An impossible control panel folds down to reveal one calm, manageable cup of tea.',sound='Confused mechanical dial chatter, one decisive switch, kettle breath and a cup tap.',sfx='Several tiny mechanical knobs rattle and click uncertainly, one firm gentle toggle switch cuts the chatter off, then a small kettle exhales warm steam and a ceramic cup settles with a soft tap. No speech, no music, no beeps.'),
}
CLIPS['meme-shrimp-authority']['accent']='Exactly one miniature wooden judge gavel striking a small wooden sound block. A single dry warm wooden knock, gentle and authoritative, with a short natural decay. No repeats, no voices, no background sound, no music.'
CLIPS['meme-tomorrow-vending']['accent']='Exactly one small sealed aluminum soda can landing in a miniature vending machine tray. One hollow metal clunk and a short natural rattling decay. Isolated, close-up, gentle. No repeats, no voices, no music.'
CLIPS['meme-hot-take-freezer']['accent']='Exactly one small ice cube landing gently in a small ceramic dish. One delicate bright glassy clink with a tiny short settling rattle. Isolated dry recording, no other objects, no voices, no music.'

def p(t,a,b):return c.phase(t,a,b)
def poly(d,points,color,outline=None,width=1):
    d.polygon([art.xy(q) for q in points],fill=color)
    if outline:art.line(d,[*points,points[0]],outline,width)
def text(d,value,x,y,size=20,color=CREAM,max_width=250):
    while d.textbbox((0,0),value,font=art.font(size))[2]>max_width*S and size>9:size-=.5
    d.text((round(x*S),round(y*S)),value,font=art.font(size),fill=color,anchor='mm')

@lru_cache(None)
def background(group):
    bases={'boundaries':((23,39,40),(14,24,17)),'absurd':((35,28,46),(21,10,18)),'gentle':((37,34,33),(20,16,9))}
    base,lift=bases[group];y,x=np.mgrid[:720,:840];glow=np.exp(-((x-420)/330)**2-((y-370)/245)**2)
    return Image.fromarray(np.stack([np.rint(a+b*glow).astype('uint8') for a,b in zip(base,lift)],axis=-1)).convert('RGBA')

def stage(name):
    im=background(CLIPS[name]['group']).copy();d=ImageDraw.Draw(im)
    text(d,CLIPS[name]['heading'],140,22,22)
    art.line(d,[(16,41),(264,41)],'#796f69',.6)
    art.ellipse(d,(49,180,231,193),'#18272b')
    return im

@lru_cache(None)
def prop(name,w,h):return c.prop(str(SOURCE/(name+'.png')),w,h)

def bubble(d,x,y,w=65,label='MORE?',color=CREAM):
    art.rounded(d,(x-w/2,y-15,x+w/2,y+15),8,color)
    poly(d,[(x-10,y+10),(x-18,y+23),(x+4,y+12)],color)
    text(d,label,x,y,12,INK,w-6)

def glove(d,x,y,side=1):
    art.rounded(d,(x-12,y-8,x+11,y+10),7,CORAL,INK,1.3)
    art.ellipse(d,(x-13,y-14,x+13,y+9),CORAL,INK,1.3)
    art.ellipse(d,(x+side*8-5,y+1,x+side*8+5,y+11),'#ffbbaa',INK,1)
    art.line(d,[(x-6,y-7),(x+3,y-9)],'#ffccb0',1.4)

def boat(d,x,y,s=1):
    poly(d,[(x-48*s,y-8*s),(x+49*s,y-8*s),(x+25*s,y+17*s),(x-24*s,y+17*s)],CREAM,INK,1)
    poly(d,[(x-37*s,y-9*s),(x+8*s,y-42*s),(x+5*s,y-9*s)],'#e6d5bd',INK,1)
    poly(d,[(x+8*s,y-42*s),(x+36*s,y-9*s),(x+5*s,y-9*s)],'#fff9e7',INK,1)
    art.line(d,[(x-24*s,y+17*s),(x+5*s,y-8*s),(x+25*s,y+17*s)],'#bc9c83',1)

def cup_layer(color=CREAM):
    im=Image.new('RGBA',(120*S,112*S));d=ImageDraw.Draw(im)
    d.arc(art.xy((75,32,111,78)),-90,90,fill=color,width=9*S)
    art.rounded(d,(15,24,88,90),15,color,INK,2)
    art.ellipse(d,(15,18,88,38),color,INK,2)
    art.ellipse(d,(22,23,82,32),'#80614c')
    art.line(d,[(25,48),(25,71)],'#ffffff',2)
    return im

def steam(d,x,y,t):
    for n in range(3):
        dx=(n-1)*15; a=math.sin(t*2+n)*3
        art.line(d,[(x+dx,y),(x+dx+a,y-8),(x+dx-a,y-17)],'#b6c8bd',1.3)

def scene(name,t,caption=True):
    im=stage(name);d=ImageDraw.Draw(im)
    if name=='meme-feature-cactus':
        inspect=p(t,.55,1.65);approve=p(t,2.05,2.65)
        art.stamp(im,prop('boundary-cactus',112,137),147,119,1.3*math.sin(t*3)*(1-approve))
        lx=78+24*inspect;ly=96+74*approve
        art.line(d,[(lx-12,ly+16),(lx-26,ly+35)],GOLD,6)
        art.ellipse(d,(lx-22,ly-22,lx+22,ly+22),None,CREAM,4)
        art.line(d,[(lx-9,ly-13),(lx+2,ly-17)],BLUE,2)
        for n in range(3):
            if .9+n*.3<t<1.25+n*.3:art.star(d,132+n*16,74+n*14,4,GOLD)
        if approve:
            y=167-9*(1-approve)
            art.rounded(d,(144,y,247,y+24),4,GOLD,INK,1)
            text(d,'AS DESIGNED',195,y+12,12,INK,95)
    elif name=='meme-urgent-laundry':
        art.rounded(d,(115,55,239,188),11,'#a0c1bb',INK,2)
        art.rounded(d,(124,64,229,84),4,CREAM)
        text(d,'GENTLE',165,74,10,INK,64)
        art.ellipse(d,(208,68,220,80),GOLD,INK,1)
        art.ellipse(d,(128,90,226,181),None,CREAM,5)
        art.ellipse(d,(134,96,220,175),'#305562',INK,2)
        enter=p(t,.45,1.05);wash=p(t,1.05,2.5);leave=p(t,2.5,3.15)
        cx=62+113*enter-111*leave;cy=119+23*leave
        card=Image.new('RGBA',(72*S,53*S));cd=ImageDraw.Draw(card)
        art.rounded(cd,(1,1,71,52),4,CREAM,INK,1)
        if wash<.9:text(cd,'URGENT',36,26,13,'#a44f48',65)
        else:text(cd,'CAN WAIT',36,26,12,INK,65)
        scale=1-.38*enter+.38*leave
        c.deform_stamp(im,card,cx,cy,scale,scale,360*2*wash*(1-leave))
        if .9<t<2.6:
            for j in range(6):
                a=t*6+j*math.tau/6;x=177+33*math.cos(a);y=135+29*math.sin(a)
                art.ellipse(d,(x-3,y-3,x+3,y+3),None,BLUE,1)
        if leave>.9:
            for x in (42,84):art.rounded(d,(x-2,113,x+2,126),1,GOLD)
            art.line(d,[(25,118),(99,118)],CREAM,1)
    elif name=='meme-expectation-luggage':
        pack=p(t,.7,2.35);travel=p(t,3.25,4.6);cx=140+46*travel
        art.rounded(d,(cx-38,147,cx+38,184),7,CORAL,INK,2)
        for x in (cx-25,cx+25):art.ellipse(d,(x-4,181,x+4,189),INK,GOLD,1)
        # The oversized cloud visibly shrinks into the case, never teleports.
        if pack<1:
            sc=1-.91*pack;x=140;y=91+68*pack
            for dx,dy,r in ((-44,7,24),(-19,-8,31),(20,-5,30),(48,10,22),(0,15,37)):
                art.ellipse(d,(x+(dx-r)*sc,y+(dy-r)*sc,x+(dx+r)*sc,y+(dy+r)*sc),CREAM)
            if pack<.55:text(d,'SHOULD',x,y+4,19*(1-.4*pack),INK,126)
        lid=p(t,2.2,2.65)
        poly(d,[(cx-37,147),(cx-29,128+17*lid),(cx+29,128+17*lid),(cx+37,147)],'#f8bca1',INK,1.5)
        art.rounded(d,(cx-9,140,cx+9,148),3,None,GOLD,2)
        art.rounded(d,(cx-4,158,cx+4,168),2,GOLD)
        if lid>.9:
            art.star(d,cx+49,147,4*(1+.1*math.sin(t*4)),GOLD)
            text(d,'LIGHT',cx,176,8,INK,50)
    elif name=='meme-tomorrow-vending':
        art.rounded(d,(66,52,214,187),12,'#8eb9b0',INK,2)
        art.rounded(d,(78,65,164,148),5,'#263d43',CREAM,1)
        text(d,'MOTIVATION',121,76,11,CREAM,77)
        for j in range(3):
            x=94+j*27;art.rounded(d,(x-9,91,x+9,125),3,('#f1bb7a','#bdb5d9','#ed9985')[j]);text(d,'?',x,109,15,INK)
        pulse=math.exp(-((t-.7)/.16)**2)
        art.ellipse(d,(174-2*pulse,90-2*pulse,202+2*pulse,118+2*pulse),GOLD,INK,1)
        text(d,'NOW',188,104,10,INK,26)
        art.rounded(d,(81,151,201,180),4,INK)
        for j,start in enumerate((.85,1.5,2.15)):
            q=p(t,start,start+.45)
            if q>0:
                x=109+j*32;cy=136+26*q-7*math.sin(math.pi*q)
                art.rounded(d,(x-12,cy-15,x+12,cy+15),4,('#f1bb7a','#bdb5d9','#ed9985')[j],INK,1)
                art.line(d,[(x-8,cy-11),(x+8,cy-11)],CREAM,1)
                text(d,'LATER',x,cy,6.5,INK,23)
        for x in (76,204):art.rounded(d,(x-4,185,x+4,191),2,INK)
    elif name=='meme-plot-armor':
        art.stamp(im,cup_layer(),132,145)
        shield=p(t,.4,1.1);sx=76;sy=132
        poly(d,[(sx-24,sy-28*shield),(sx+23,sy-28*shield),(sx+19,sy+15),(sx,sy+31),(sx-20,sy+15)],'#c8a77b',INK,2)
        art.line(d,[(sx-15,sy-17*shield),(sx+13,sy-17*shield)],'#f5dfb7',2)
        q=p(t,.65,1.9)
        if t<1.95:
            x=234-139*q;y=66+56*q
            poly(d,[(x+35,y-20),(x+16,y-1),(x+7,y+10),(x-8,y-9)],CORAL)
            art.ellipse(d,(x-10,y-10,x+10,y+10),'#8e817e',GOLD,1.5)
        elif t<2.65:
            u=p(t,1.95,2.65);x=95+37*u;y=116-37*math.sin(math.pi*u)+13*u
            art.rounded(d,(x-6,y-6,x+6,y+6),2,CREAM,GOLD,1)
        hit=math.exp(-((t-1.95)/.12)**2)
        if hit>.03:
            for n in range(5):
                a=n*math.tau/5;art.line(d,[(92+math.cos(a)*17,117+math.sin(a)*17),(92+math.cos(a)*(17+10*hit),117+math.sin(a)*(17+10*hit))],GOLD,1.5)
        if t>2.7:steam(d,129,107,t)
    elif name=='meme-still-mending':
        q=p(t,.45,1.9);join=15*(1-q)
        mug=cup_layer('#cedacf');left=mug.crop((0,0,54*S,112*S));right=mug.crop((54*S,0,120*S,112*S))
        im.alpha_composite(left,(round((80-join)*S),78*S))
        im.alpha_composite(right,(round((134+join)*S),78*S))
        seam=p(t,1.8,2.6)
        line=[(134,103),(129,118),(139,130),(132,145),(140,158),(135,167)]
        if seam:
            count=max(2,round(2+seam*4));art.line(d,line[:count],GOLD,2.5)
        grow=p(t,2.5,3.45)
        if grow:
            art.line(d,[(132,105),(132-4*grow,105-40*grow)],SAGE,2)
            for side,level in ((-1,18),(1,27)):
                lx=130+side*7*grow;ly=105-level*grow
                art.ellipse(d,(lx-6*grow,ly-3*grow,lx+6*grow,ly+3*grow),SAGE)
            cx=128;cy=105-43*grow
            for n in range(5):
                a=n*math.tau/5;art.ellipse(d,(cx+(math.cos(a)*8-5)*grow,cy+(math.sin(a)*8-5)*grow,cx+(math.cos(a)*8+5)*grow,cy+(math.sin(a)*8+5)*grow),CORAL)
            art.ellipse(d,(cx-4*grow,cy-4*grow,cx+4*grow,cy+4*grow),GOLD)
    elif name=='meme-shrimp-authority':
        nod=math.exp(-((t-2)/.18)**2)
        art.stamp(im,prop('judge-shrimp',107,124),151,114+2*nod,-2*nod)
        art.rounded(d,(48,159,232,185),5,'#966c50',INK,2)
        art.line(d,[(55,165),(225,165)],'#d4ac75',2)
        art.ellipse(d,(70,151,117,164),'#5a4142',GOLD,1)
        # Pivot around the handle: the head contacts the block at t=2.0.
        angle=math.radians(225-65*p(t,1.45,2)+20*p(t,2,2.3))
        hx=130+43*math.cos(angle);hy=120+43*math.sin(angle)
        art.line(d,[(130,120),(hx,hy)],GOLD,5)
        ax,ay=-math.sin(angle),math.cos(angle);bx,by=math.cos(angle),math.sin(angle)
        poly(d,[(hx+u*ax+v*bx,hy+u*ay+v*by) for u,v in ((-18,-7),(18,-7),(18,7),(-18,7))],'#b98965',INK,1)
        if t>2.25:
            q=p(t,2.25,2.65);art.rounded(d,(143,164,218,181),2,CREAM)
            text(d,'APPROVED',180,173,10, '#567a61',71)
            art.star(d,218,143,5*q,GOLD)
    elif name=='meme-capypause':
        art.ellipse(d,(69,159,214,193),'#d0a0a5',INK,2)
        art.ellipse(d,(78,157,207,180),BLUE)
        breath=1+.012*math.sin(t*2.1)
        c.deform_stamp(im,prop('capybara-rest',119,112),141,134,1,breath)
        art.rounded(d,(70,174,213,191),9,'#dfb2ac',INK,1)
        # Mail visibly hits the canopy and changes direction.
        canopy=p(t,.35,.8)
        art.line(d,[(165,84),(165,171)],GOLD,2)
        dome=[(165+72*canopy*math.cos(a),91-40*math.sin(a)) for a in np.linspace(math.pi,0,25)]
        poly(d,dome,SAGE,INK,1)
        for dx in (-36,0,36):art.line(d,[(165,52),(165+dx*canopy,91)],'#85a88d',.7)
        for j,start in enumerate((.9,1.55,2.15)):
            q=p(t,start,start+.85)
            if 0<q<1:
                x=70+54*j+(-50 if j%2==0 else 40)*max(0,(q-.5)*2)
                y=54+40*min(1,q*2)-44*max(0,(q-.5)*2)
                art.rounded(d,(x-13,y-8,x+13,y+8),2,CREAM,CORAL,1)
                art.line(d,[(x-12,y-7),(x,y+1),(x+12,y-7)],CORAL,1)
        if t>2.7:
            art.rounded(d,(177,143,244,165),4,GOLD,INK,1)
            text(d,'OFFLINE',210,154,10,INK,62)
        for x,y in ((83,166),(198,169)):art.ellipse(d,(x-3,y-1,x+3,y+1),CREAM)
    elif name=='meme-hot-take-freezer':
        art.rounded(d,(155,62,231,188),8,BLUE,INK,2)
        art.rounded(d,(164,72,223,153),3,'#244b60',CREAM,1)
        for y in (102,133):art.line(d,[(166,y),(221,y)],'#7cb3b7',2)
        q=p(t,.65,1.65);x=67+123*q;y=111
        if t<1.85:
            bubble(d,x,y,60,'!!!',CORAL)
            for n in range(3):
                xx=x-19+n*17;h=15+4*math.sin(t*11+n)
                poly(d,[(xx-7,96),(xx+1,96-h),(xx+8,96)],GOLD)
        shut=p(t,1.65,2.05)
        if shut:
            art.rounded(d,(155,62,155+76*shut,188),8,BLUE,INK,2)
            if shut>.8:
                art.rounded(d,(165,105,170,125),2,CREAM)
                for n in range(3):
                    a=n*math.pi/3;art.line(d,[(194-8*math.cos(a),87-8*math.sin(a)),(194+8*math.cos(a),87+8*math.sin(a))],CREAM,1.3)
                art.rounded(d,(177,146,209,167),3,'#244b60',CREAM,1)
        art.ellipse(d,(51,173,126,186),'#aab9c6',CREAM,1)
        cube=p(t,2.2,2.85)
        if cube:
            xx=183-95*cube;yy=157+17*cube-19*math.sin(math.pi*cube)
            art.rounded(d,(xx-14,yy-14,xx+14,yy+12),5,'#c0e1df',CREAM,1)
            art.line(d,[(xx-8,yy-6),(xx+5,yy-10)],'#ffffff',2)
        if t>2.8:art.star(d,112,146,4,GOLD)
    elif name=='meme-control-tea':
        q=p(t,1.8,2.7)
        # An entire fixed panel pivots down and shrinks onto its console base.
        panel=Image.new('RGBA',(198*S,120*S));pd=ImageDraw.Draw(panel)
        art.rounded(pd,(1,1,197,118),9,'#637f84',INK,2)
        for row in range(2):
            for col in range(5):
                x=24+col*37;y=29+row*40;a=math.sin(t*(3+col)+row)*2.3*(1-q)
                art.ellipse(pd,(x-10,y-10,x+10,y+10),INK,CREAM,1)
                art.line(pd,[(x,y),(x+math.sin(a)*7,y-math.cos(a)*7)],GOLD,2)
        text(pd,'EVERYTHING',100,103,14,CREAM,170)
        if q<1:c.deform_stamp(im,panel,140,119+63*q,1,1-.96*q)
        if q>0:
            layer=Image.new('RGBA',im.size);art.stamp(layer,cup_layer(),143,143)
            layer.putalpha(layer.getchannel('A').point(lambda a:round(a*q)));im.alpha_composite(layer)
        if q>.9:steam(d,138,110,t)
        art.rounded(d,(47,182,233,192),3,'#8a9b91')
    else:raise ValueError(name)
    if caption:
        detail=CLIPS[name];layer=Image.new('RGBA',im.size);ld=ImageDraw.Draw(layer)
        if t<2.8:
            alpha=1-p(t,2.5,2.75);text(ld,detail['setup'],140,216,14,'#c7c2b9')
        else:
            alpha=p(t,2.8,3.02);art.rounded(ld,(8,200,272,234),8,SAGE)
            text(ld,detail['quote'],140,217,18,INK)
        layer.putalpha(layer.getchannel('A').point(lambda a:round(a*alpha)));im.alpha_composite(layer)
    return im

@lru_cache(None)
def cover(name):return scene(name,4.8).convert('RGB')

def render(name,t):
    card=cover(name)
    if t<.1 or t>=5.8:return card.copy()
    im=scene(name,t).convert('RGB')
    # All moving props are clipped below the stable title strip.
    im.paste(card.crop((0,0,840,44*S)),(0,0))
    if t<.3:return Image.blend(card,im,p(t,.1,.3))
    if t>5.55:return Image.blend(im,card,p(t,5.55,5.8))
    return im
