"""Nine fixed-artwork seasonal object postcards, composed without robot faces."""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageChops
import seasonal_common as c

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/dynamics_sources/seasonal-postcards-v1/holidays'
CLIPS={
 'season-birthday':dict(title='A wish with sprinkles',heading='HAPPY BIRTHDAY!',heading_size=24,quote='MAKE A LITTLE WISH.',cover=['A LITTLE WISH.','A LOT OF JOY.'],when='A birthday wish gets a slightly excessive helping of confetti.',sound='A candle twinkle, a soft puff, and a playful confetti flourish.',cues=[(.5,'candle lights'),(1.45,'wish puff'),(1.65,'confetti surprise'),(2.75,'birthday sparkle')],group='holidays',theme='birthday',occasion='birthday'),
 'season-christmas':dict(title='A sweet Christmas surprise',heading='MERRY CHRISTMAS',heading_size=23,quote='SWEET LITTLE SURPRISE.',cover=['A SWEET LITTLE','CHRISTMAS.'],when='A gingerbread cookie springs out of its gift and offers a tiny heart.',sound='Ribbon rustle, a gift lid pop, and warm gingerbread bells.',cues=[(.5,'gift wiggles'),(1.1,'lid opens'),(1.55,'cookie surprise'),(2.5,'sweet heart')],group='holidays',theme='christmas',occasion='christmas'),
 'season-halloween':dict(title='A very polite boo',heading='HAPPY HALLOWEEN',heading_size=23,quote='BOO. HAVE A TREAT.',cover=['A TINY BOO.','A BIG TREAT.'],when='A tiny ghost borrows the pumpkin hat and leaves a candy thank-you.',sound='A soft ghost whoosh, a hat pop, and a candy chime.',cues=[(.5,'ghost arrives'),(1.3,'hat floats'),(2.2,'candy thank-you'),(2.8,'tiny boo sparkle')],group='holidays',theme='halloween',occasion='halloween'),
 'season-easter':dict(title='An egg-stra small surprise',heading='HAPPY EASTER',heading_size=26,quote='EGG-STRA GOOD THINGS.',cover=['EGG-STRA','GOOD THINGS.'],when='A decorated egg cracks open and grows an unexpectedly proud carrot.',sound='A delicate egg tap, a tiny shell pop, and a cheerful sprout chime.',cues=[(.55,'egg rocks'),(1.2,'shell cracks'),(1.75,'carrot appears'),(2.55,'sprout sparkle')],group='holidays',theme='easter',occasion='easter'),
 'season-summer':dict(title='Shade for a sweet day',heading='HELLO, SUMMER',heading_size=25,quote='STAY COOL, SWEET THING.',cover=['STAY COOL,','SWEET THING.'],when='A melting ice cream receives its own miniature beach umbrella.',sound='A sunny chime, a tiny melting plop, and an umbrella pop.',cues=[(.5,'summer sunshine'),(1.15,'melting drip'),(1.7,'umbrella opens'),(2.65,'cool relief')],group='holidays',theme='summer'),
 'season-autumn':dict(title='The leaf blanket service',heading='HELLO, AUTUMN',heading_size=25,quote='LEAF YOUR WORRIES HERE.',cover=['LEAF YOUR','WORRIES HERE.'],when='A sleepy hedgehog catches a falling leaf and calls it a blanket.',sound='A papery leaf rustle, a tiny landing, and a sleepy warm chime.',cues=[(.5,'leaf drifts'),(1.7,'leaf blanket lands'),(2.45,'cozy heart')],group='holidays',theme='autumn'),
 'season-winter':dict(title='A scarf-sized hug',heading='HELLO, WINTER',heading_size=25,quote='A LITTLE EXTRA COZY.',cover=['A LITTLE','EXTRA COZY.'],when='A snowman pulls its scarf right up to its nose and enjoys the snow.',sound='Soft snowfall sparkle, a woolly rustle, and a cozy little bell.',cues=[(.5,'snow drifts'),(1.3,'scarf lifts'),(2.1,'cozy settle'),(2.7,'warm heart')],group='holidays',theme='winter'),
 'season-spring':dict(title='Just add a little kindness',heading='HELLO, SPRING',heading_size=25,quote='GOOD THINGS GROW HERE.',cover=['GOOD THINGS','ARE GROWING.'],when='A tiny watering can gives bare stems the confidence to bloom.',sound='Gentle water drops and three flowers blooming as a rising chime.',cues=[(.6,'watering can tips'),(1.0,'water drops'),(1.7,'first bloom'),(2.4,'flowers flourish')],group='holidays',theme='spring'),
 'season-new-year':dict(title='A pocket-sized fresh start',heading='HAPPY NEW YEAR!',heading_size=24,quote='HERE\'S TO WHAT\'S NEXT.',cover=['A FRESH LITTLE','START.'],when='A party popper launches a constellation of hopeful little stars.',sound='Three bright countdown ticks, a soft pop, and a starry chime.',cues=[(.5,'three'),(.9,'two'),(1.3,'one'),(1.7,'star pop'),(2.6,'fresh start sparkle')],group='holidays',theme='new-year',occasion='new-year'),
}

REVEALS={
 'season-birthday':(2.75,'one wish. extra sprinkles.'),
 'season-christmas':(2.85,'the gift has a sweet tooth.'),
 'season-halloween':(3.15,'a hat loan. a candy thank-you.'),
 'season-easter':(2.85,'the egg had other plans.'),
 'season-summer':(2.85,'even ice cream needs a break.'),
 'season-autumn':(2.75,'blanket delivery. no rush.'),
 'season-winter':(2.85,'scarf mode: maximum.'),
 'season-spring':(3.0,'just add a little kindness.'),
 'season-new-year':(2.8,'small pop. big possibilities.'),
}
CUES={
 'season-birthday':[(.5,'candle lights'),(1.42,'wish puff'),(1.7,'confetti surprise'),(2.75,'birthday sparkle')],
 'season-christmas':[(.5,'gift wiggles'),(1.08,'lid pops'),(1.55,'cookie springs up'),(2.15,'lid lands'),(2.7,'heart offered')],
 'season-halloween':[(.5,'ghost approaches'),(1.15,'hat lifts'),(1.75,'hat fits'),(2.3,'candy drops'),(2.95,'candy lands'),(3.2,'thank-you sparkle')],
 'season-easter':[(.55,'egg rocks'),(1.12,'shell cracks'),(1.65,'carrot sprouts'),(2.45,'cap lands'),(2.8,'egg-stra sparkle')],
 'season-summer':[(.5,'hot sunshine'),(1.05,'drip falls'),(1.55,'drip splashes'),(1.85,'umbrella opens'),(2.5,'cool relief')],
 'season-autumn':[(.5,'leaf drifts'),(1.85,'blanket lands'),(2.2,'leaf settles'),(2.75,'sleepy heart')],
 'season-winter':[(.5,'snow drifts'),(1.15,'scarf pulls'),(2.05,'scarf settles'),(2.4,'hand lets go'),(2.85,'cozy heart')],
 'season-spring':[(.65,'can tips'),(1.,'water flows'),(1.65,'coral bloom'),(1.95,'lilac bloom'),(2.25,'cream bloom'),(3.,'garden flourish')],
 'season-new-year':[(.5,'three'),(.9,'two'),(1.3,'one'),(1.7,'pop and recoil'),(2.3,'stars settle'),(2.8,'fresh start sparkle')],
}
for _name,_detail in CLIPS.items():
    _detail.update(cues=CUES[_name],sound_revision=2)

def p(name,w,h):return c.prop(SOURCE/(name+'.png'),w,h)

def polygon(d,points,fill,outline=None,width=1.4):
    pts=[c.art.xy(v) for v in points];d.polygon(pts,fill=fill)
    if outline:c.art.line(d,points+[points[0]],outline,width)

def sparkle(d,x,y,r,t,color=c.GOLD):
    c.art.star(d,x,y,r*(.88+.12*math.sin(t*4+x)),color)


def composite(im,layer,opacity=1):
    if opacity<=0:return
    if opacity<1:layer.putalpha(layer.getchannel('A').point(lambda a:round(a*opacity)))
    im.alpha_composite(layer)


def anchored(im,sprite,x,base,sx=1,sy=1,angle=0):
    c.deform_stamp(im,sprite,x,base-sprite.height/c.S*sy/2,sx,sy,angle)


def pulse(t,start,duration=.45):
    u=(t-start)/duration
    return math.sin(math.pi*u)**2 if 0<u<1 else 0


def path_point(points,u):
    a,b,cc,d=points;v=1-u
    return tuple(v**3*a[k]+3*v*v*u*b[k]+3*v*u*u*cc[k]+u**3*d[k] for k in (0,1))


def leaf(d,x,y,size,angle=0,color='#db9463',sx=1,sy=1):
    def point(px,py):
        px*=sx;py*=sy
        return (x+size*(px*math.cos(angle)-py*math.sin(angle)),y+size*(px*math.sin(angle)+py*math.cos(angle)))
    shape=[(0,-1),(.5,-.55),(.4,-.2),(1,-.15),(.6,.3),(.6,.7),(.12,.6),(0,1),(-.3,.55),(-.75,.6),(-.55,.2),(-1,0),(-.4,-.2),(-.5,-.6)]
    polygon(d,[point(*v) for v in shape],color,c.INK,1)
    c.art.line(d,[point(-.1,-.7),point(.1,.85)],'#9e654e',1)
    for side in (-1,1):c.art.line(d,[point(0,.2),point(side*.48,-.13)],'#9e654e',.7)


def ghost(d,x,y,t):
    flutter=.8*math.sin(t*5)
    pts=[(x-18,y+17),(x-18,y-6)]
    pts += [(x+18*math.cos(math.pi-i*math.pi/16),y-6-20*math.sin(math.pi-i*math.pi/16)) for i in range(17)]
    pts += [(x+18,y+17),(x+10,y+13+flutter),(x+3,y+19),(x-5,y+14-flutter),(x-12,y+19)]
    polygon(d,pts,c.CREAM,c.INK,1.5)
    for dx in [-6,6]:c.art.ellipse(d,(x+dx-1.5,y-7,x+dx+1.5,y-2),c.INK)
    c.art.ellipse(d,(x-2,y+2,x+3,y+7),c.INK)


@lru_cache(None)
def witch_hat():
    im=Image.new('RGBA',(58*c.S,53*c.S));d=ImageDraw.Draw(im)
    polygon(d,[(12,39),(28,5),(40,39)],'#8b6b9a',c.INK,1.5)
    c.art.ellipse(d,(3,34,55,46),'#8b6b9a',c.INK,1.5)
    polygon(d,[(17,29),(37,29),(40,35),(14,35)],c.CORAL)
    c.art.rounded(d,(25,28,32,35),1,c.GOLD)
    return im


@lru_cache(None)
def candy():
    im=Image.new('RGBA',(38*c.S,23*c.S));d=ImageDraw.Draw(im)
    polygon(d,[(11,11),(2,4),(2,19)],c.LILAC,c.INK,1)
    polygon(d,[(26,11),(36,4),(36,19)],c.LILAC,c.INK,1)
    c.art.rounded(d,(9,3,28,20),7,c.CORAL,c.INK,1)
    c.art.line(d,[(17,4),(21,19)],c.CREAM,2)
    return im


@lru_cache(None)
def gift_lid():
    im=Image.new('RGBA',(94*c.S,26*c.S));d=ImageDraw.Draw(im)
    c.art.rounded(d,(1,8,93,25),3,c.CORAL,c.INK,1.5)
    c.art.line(d,[(46,19),(33,2),(46,9),(60,2),(47,19)],c.GOLD,3)
    c.art.line(d,[(47,10),(47,24)],c.GOLD,7)
    return im


@lru_cache(None)
def egg_parts():
    # Both halves are masks of ONE original contour, with one shared seam.
    full=Image.new('RGBA',(88*c.S,100*c.S));d=ImageDraw.Draw(full)
    c.art.ellipse(d,(5,5,83,96),c.CREAM,c.INK,1.5)
    c.art.line(d,[(15,31),(73,31)],c.LILAC,3)
    c.art.line(d,[(10,73),(22,79),(34,73),(46,79),(58,73),(76,79)],c.CORAL,3)
    for x in (25,44,62):c.art.ellipse(d,(x-2,87,x+2,91),c.SAGE)
    seam=[(5,54),(18,48),(30,56),(43,48),(55,56),(69,48),(83,54)]
    topmask=Image.new('L',full.size);md=ImageDraw.Draw(topmask)
    md.polygon([c.art.xy(v) for v in [(0,0),(88,0),(88,54)]+list(reversed(seam))+[(0,54)]],fill=255)
    cap=full.copy();cap.putalpha(ImageChops.multiply(full.getchannel('A'),topmask))
    bottom=full.copy();bottom.putalpha(ImageChops.multiply(full.getchannel('A'),ImageChops.invert(topmask)))
    capbox=cap.getchannel('A').getbbox();basebox=bottom.getchannel('A').getbbox()
    def offset(box):return ((box[0]+box[2])/(2*c.S)-44,(box[1]+box[3])/(2*c.S)-50)
    return cap.crop(capbox),bottom.crop(basebox),offset(capbox),offset(basebox),seam,full


@lru_cache(None)
def carrot():
    im=Image.new('RGBA',(44*c.S,100*c.S));d=ImageDraw.Draw(im)
    for pts in [[(22,31),(6,11),(13,27)],[(22,31),(20,1),(25,4)],[(22,31),(39,11),(32,27)]]:
        polygon(d,pts,c.SAGE,c.INK,.8)
    polygon(d,[(9,30),(34,30),(22,94)],'#f4a15f',c.INK,1.3)
    for yy in (43,56,69):c.art.line(d,[(13+(yy-43)*.1,yy),(23,yy-2)],'#c87b50',1)
    return im


def snowflake(d,x,y,r,color=c.CREAM):
    for n in range(3):
        a=n*math.pi/3;c.art.line(d,[(x-r*math.cos(a),y-r*math.sin(a)),(x+r*math.cos(a),y+r*math.sin(a))],color,1)


def birthday_confetti(im,t):
    if t<1.7:return
    layer=Image.new('RGBA',im.size);d=ImageDraw.Draw(layer);age=t-1.7
    for n in range(25):
        vx=(-1 if n%2 else 1)*(19+(n*13)%63);vy=-15-(n*11)%23
        land=(-vy+math.sqrt(vy*vy+4*50*(185-78)))/100
        a=min(age,land)
        x=140+vx*(1-math.exp(-1.25*a))/1.25
        y=78+vy*a+50*a*a
        if age>land:y=185-3.5*abs(math.sin((age-land)*17))*math.exp(-(age-land)*10)
        if not (49<y<190 and 17<x<263):continue
        color=[c.CORAL,c.GOLD,c.SAGE,c.LILAC][n%4]
        if age<land:
            prev=max(0,a-.045);xx=140+vx*(1-math.exp(-1.25*prev))/1.25;yy=78+vy*prev+50*prev*prev
            c.art.line(d,[(xx,yy),(x,y)],'#7c6276',1)
        if n%4==0:c.art.star(d,x,y,2.8,color,angle=min(age,land)*3+n)
        else:
            a0=n+min(age,land)*5
            c.art.line(d,[(x-2*math.cos(a0),y-2*math.sin(a0)),(x+2*math.cos(a0),y+2*math.sin(a0))],color,2)
    composite(im,layer)


def birthday_scene(im,d,t):
    squeeze=.055*pulse(t,1.19,.34);bounce=.08*pulse(t,1.7,.5)
    anchored(im,p('birthday-cake',108,111),140,189-4*pulse(t,1.7,.55),1+squeeze-bounce*.35,1-squeeze+bounce*.35)
    wick=78+4*squeeze
    lit=c.phase(t,.5,.7)*(1-c.phase(t,1.42,1.59))
    if lit>0:
        flame=Image.new('RGBA',im.size);fd=ImageDraw.Draw(flame)
        lean=1.8*math.sin(t*11)-10*c.phase(t,1.42,1.59)
        polygon(fd,[(137,wick),(135,wick-5),(140+lean,wick-17*lit),(145,wick-5),(143,wick)],c.GOLD)
        c.art.ellipse(fd,(139,wick-8*lit,142,wick),c.CREAM)
        composite(im,flame,lit)
    if 1.42<t<2.3:
        age=t-1.42;smoke=Image.new('RGBA',im.size);sd=ImageDraw.Draw(smoke)
        for n in range(3):
            a=max(0,age-n*.1);x=140+15*a+3*math.sin(a*7+n);y=77-20*a-n*2;r=1.6+a*2
            c.art.ellipse(sd,(x-r,y-r,x+r,y+r),None,c.MUTED,.8)
        composite(im,smoke,1-c.phase(t,1.8,2.3))
    if 1.42<t<1.74:
        breeze=Image.new('RGBA',im.size);bd=ImageDraw.Draw(breeze);u=c.phase(t,1.42,1.7)
        for n in range(2):c.art.line(bd,[(86+20*u,80+n*7),(111+14*u,77+n*7),(130+6*u,79+n*5)],c.CREAM,1)
        composite(im,breeze,math.sin(u*math.pi))
    birthday_confetti(im,t)
    if t>=2.75:
        for x,y in [(77,113),(206,93),(221,155)]:sparkle(d,x,y,4*c.phase(t,2.75,3.05),t)


def christmas_scene(im,d,t):
    wiggle=2.3*math.sin((t-.5)*19)*c.phase(t,.5,.7)*(1-c.phase(t,.9,1.1))
    lid_u=c.phase(t,1.08,2.15)
    lx=140+79*lid_u;ly=137+32*lid_u-65*math.sin(math.pi*lid_u)
    if t>2.15:ly-=3*pulse(t,2.15,.27)
    c.deform_stamp(im,gift_lid(),lx+wiggle,ly,angle=-14*lid_u)
    rise=c.spring(t,1.36,2.1,1.1)
    if rise>0:
        cookie=Image.new('RGBA',im.size)
        c.deform_stamp(cookie,p('gingerbread',79,100),137,196-80*rise,1-.035*pulse(t,1.4,.65),1+.05*pulse(t,1.4,.65),-3*math.sin(math.pi*min(1,rise)))
        cookie.paste((0,0,0,0),c.art.xy((0,184,280,240)))
        composite(im,cookie)
    # Foreground gift opening hides the cookie's legs throughout emergence.
    c.art.rounded(d,(94+wiggle,142,186+wiggle,184),4,c.CORAL,c.INK,1.5)
    c.art.rounded(d,(133+wiggle,142,147+wiggle,184),0,c.GOLD)
    c.art.line(d,[(98+wiggle,144),(182+wiggle,144)],'#ffd3af',1.5)
    if t>=2.3:
        u=c.phase(t,2.3,2.8);size=9*c.spring(t,2.3,2.85,1.1)
        hx=169+25*u;hy=116-22*u
        c.art.heart(d,hx,hy,size,c.CORAL)
        c.art.heart(d,hx-2,hy-2,size*.25,c.CREAM)
    if t>=2.7:
        for x,y in [(78,96),(86,125),(209,123)]:sparkle(d,x,y,3*c.phase(t,2.7,3.0),t)


def ghost_position(t):
    approach=c.phase(t,.5,1.15);depart=c.phase(t,1.75,2.35)
    return 56+56*approach+90*depart,121+7*approach-2*depart+math.sin(t*4)*.6


def halloween_scene(im,d,t):
    gx,gy=ghost_position(t)
    anchored(im,p('pumpkin',105,87),128,188,1+.018*pulse(t,1.15,.5),1-.018*pulse(t,1.15,.5))
    ghost(d,gx,gy,t)
    transfer=c.phase(t,1.15,1.75)
    hx=128+(gx-128)*transfer;hy=99+(gy-36-99)*transfer-10*math.sin(math.pi*transfer)
    hat_bounce=-2*pulse(t,1.75,.35)
    c.art.stamp(im,witch_hat(),hx,hy+hat_bounce,-6*math.sin(math.pi*transfer))
    if t>=2.3:
        u=c.phase(t,2.3,2.95)
        start=ghost_position(2.3);x=start[0]-10+(91-(start[0]-10))*u;y=start[1]+12+(179-start[1]-12)*u-23*math.sin(math.pi*u)
        squash=.20*pulse(t,2.95,.3);rebound=3*pulse(t,3.08,.27)
        c.deform_stamp(im,candy(),x,y-rebound,1+squash,1-squash,(-95*math.sin(math.pi*u))*(1-u))
        if 2.95<t<3.18:
            u2=c.phase(t,2.95,3.18)
            for dx in (-1,1):c.art.line(d,[(91+dx*(21+9*u2),182-3*u2),(91+dx*(24+9*u2),179-5*u2)],c.GOLD,1)
    if t>=3.2:sparkle(d,232,125,4*c.phase(t,3.2,3.4),t)


def easter_scene(im,d,t):
    cap,base,capoff,baseoff,seam,whole=egg_parts()
    rock=(3.3*math.sin((t-.55)*16)*c.phase(t,.55,.75)*(1-c.phase(t,.95,1.12)))
    crack=c.phase(t,1.12,1.38);lift=c.phase(t,1.12,2.45)
    grow=c.spring(t,1.65,2.65,1.0)
    if lift==0:
        # One rigid transform keeps the shared seam invisible while rocking.
        c.art.stamp(im,whole,140+rock,134,-rock)
        return
    # Sprout and shell are separate permanent layers; no background erasure.
    if grow>0:
        sprout=Image.new('RGBA',im.size)
        c.deform_stamp(sprout,carrot(),140,190-59*grow,.88+.12*min(1,grow),.8+.2*min(1,grow),2*math.sin(math.pi*min(1,grow)))
        sprout.paste((0,0,0,0),c.art.xy((0,178,280,240)))
        composite(im,sprout)
    center_y=134
    a=math.radians(-rock)
    bx=140+rock+baseoff[0]*math.cos(a)+baseoff[1]*math.sin(a)
    by=center_y-baseoff[0]*math.sin(a)+baseoff[1]*math.cos(a)
    c.art.stamp(im,base,bx,by,-rock)
    # The shell cap follows one continuous lift / fall arc, then settles beside it.
    cx=140+rock*(1-lift)+capoff[0]*math.cos(a)+capoff[1]*math.sin(a)-65*lift
    cy=center_y-capoff[0]*math.sin(a)+capoff[1]*math.cos(a)+39*lift-45*math.sin(math.pi*lift)
    if t>2.45:cy-=2.5*pulse(t,2.45,.3)
    c.art.stamp(im,cap,cx,cy,-18*lift-rock*(1-lift))
    if 0<crack and lift<.22:
        line_layer=Image.new('RGBA',im.size);ld=ImageDraw.Draw(line_layer)
        c.art.line(ld,[(140+x-44,134+y-50) for x,y in seam],c.INK,1)
        composite(im,line_layer,crack*(1-c.phase(lift,.08,.22)))
    if t>=2.8:
        for x,y in [(74,113),(202,98),(213,153)]:sparkle(d,x,y,3*c.phase(t,2.8,3.08),t)


def summer_scene(im,d,t):
    sun_heat=1+.05*math.sin(t*5)*(1-c.phase(t,2.0,2.5))
    for n in range(8):
        a=n*math.tau/8;c.art.line(d,[(216+18*math.cos(a),76+18*math.sin(a)),(216+24*sun_heat*math.cos(a),76+24*sun_heat*math.sin(a))],c.GOLD,2)
    c.art.ellipse(d,(203,63,229,89),c.GOLD)
    pole=c.phase(t,1.6,1.94);canopy=c.spring(t,1.85,2.45,1.25)
    topx=175-15*pole;topy=182-89*pole
    if pole>0:c.art.line(d,[(175,182),(topx,topy)],c.CREAM,2)
    if canopy>0:
        w=52*canopy;h=27*canopy
        polygon(d,[(160-w,95),(160-28*canopy,95-h*.7),(160,95-h),(160+28*canopy,95-h*.7),(160+w,95)],c.SAGE,c.INK,1.4)
        polygon(d,[(160-w,95),(160,95-h),(160-16*canopy,95)],c.CORAL)
        polygon(d,[(160+16*canopy,95),(160,95-h),(160+w,95)],c.CREAM)
        c.art.line(d,[(160-w,95),(160+w,95)],c.INK,1.4)
    relief=pulse(t,2.5,.5)
    anchored(im,p('summer-icecream',78,111),128,189-5*relief,1-.025*relief,1+.025*relief)
    if 1.05<=t<1.55:
        u=(t-1.05)/.5;y=132+47*u*u
        c.art.ellipse(d,(99, y-4,105,y+5),c.CORAL)
    if t>=1.55:
        spread=c.spring(t,1.55,1.82,1.0)
        c.art.ellipse(d,(102-10*spread,183-1.5*spread,102+10*spread,185+1.5*spread),c.CORAL)
        if t<1.95:
            a=t-1.55
            for vx in (-23,20):
                x=102+vx*a;y=183-45*a+115*a*a
                c.art.ellipse(d,(x-1.7,y-1.7,x+1.7,y+1.7),c.CORAL)
    if t>=2.65:c.art.heart(d,82,105,8*c.spring(t,2.65,3.05),c.CORAL)


def autumn_scene(im,d,t):
    u=c.phase(t,.5,1.85);contact=pulse(t,1.85,.38)
    breathing=.008*math.sin((t-2.5)*3)*c.phase(t,2.5,3.0)
    anchored(im,p('autumn-hedgehog',151,87),146,189,1+.025*contact-breathing,1-.025*contact+breathing)
    x=105+31*u+10*math.sin(2*math.pi*u)*(1-u)
    y=76+50*u
    land_age=max(0,t-1.85);settle=math.sin(land_age*17)*math.exp(-land_age*7) if t>1.85 else 0
    y-=2.5*settle
    angle=.55*math.sin(u*math.pi*2)*(1-u)-.08*u+.13*settle
    drape=c.phase(t,1.72,2.25)
    leaf(d,x,y,23,angle,'#d58e60',1+.14*drape,1-.3*drape)
    for n,(xx,yy) in enumerate([(65,174),(225,169),(82,157)]):leaf(d,xx,yy,8,.3+n,c.GOLD if n%2 else c.CORAL)
    if t>=2.45:
        z=Image.new('RGBA',im.size);zd=ImageDraw.Draw(z);r=c.phase(t,2.45,2.85)
        c.label(zd,'z',180,96-5*r,13,c.LILAC);c.label(zd,'z',191,85-5*r,10,c.LILAC)
        composite(im,z,r)
    if t>=2.75:c.art.heart(d,210,98,9*c.spring(t,2.75,3.12),c.CORAL)


def winter_scene(im,d,t):
    for n in range(13):
        x=38+(n*47)%207+2*math.sin(t+n);y=52+((n*29+max(0,t-.5)*14)%123)
        snowflake(d,x,y,2.5 if n%3 else 3.8,c.MUTED)
    cozy=c.phase(t,2.15,2.65)
    c.art.ellipse(d,(98,113,182,187),c.CREAM,c.INK,1.5)
    c.art.ellipse(d,(111,68,169,127),c.CREAM,c.INK,1.5)
    for x in [128,151]:
        if cozy<.98:c.art.ellipse(d,(x-2,88+cozy*1.8,x+2,92-cozy*1.8),c.INK)
        else:c.art.line(d,[(x-3,90),(x,91),(x+3,90)],c.INK,1.1)
    polygon(d,[(139,97),(159,102),(140,105)],'#f0a063',c.INK,1)
    for y in [144,163]:c.art.ellipse(d,(138,y-2,142,y+2),c.INK)
    c.art.line(d,[(106,141),(78,117),(70,117)],'#aa7c62',3)
    pull=c.spring(t,1.15,2.05,.65);tailpull=c.spring(t,1.3,2.22,.75)
    c.art.rounded(d,(111,115-10*pull,170,130-10*pull),6,c.CORAL,c.INK,1)
    tailtop=125-10*pull;tailbottom=168-25*tailpull;sway=2*math.sin((t-1.3)*10)*pulse(t,1.3,.9)
    polygon(d,[(154,tailtop),(168,tailtop),(168+sway,tailbottom),(154+sway,tailbottom)],c.CORAL,c.INK,1)
    for a in (.55,.72,.89):
        yy=tailtop+(tailbottom-tailtop)*a;c.art.line(d,[(155+sway*a,yy),(167+sway*a,yy)],c.GOLD,1.5)
    reach=c.phase(t,.55,1.1);release=c.phase(t,2.25,2.55)
    handx=190-22*reach+22*release;handy=130-10*pull-3*release
    c.art.line(d,[(175,145),(194,139-14*pull),(handx,handy)],'#aa7c62',3)
    c.art.line(d,[(handx,handy),(handx-4,handy-3)],'#aa7c62',1.7)
    if t>=2.85:c.art.heart(d,211,96,10*c.spring(t,2.85,3.2),c.CORAL)


def watering_pose(t):
    tip=c.phase(t,.65,1.0);returning=c.phase(t,2.3,2.85)
    angle=35*tip*(1-returning)
    return 199+5*returning,109-3*math.sin(tip*math.pi),angle


def spout_position(t):
    x,y,a=watering_pose(t);a=math.radians(a)
    # Measured outlet in the cropped fixed source: -43,-23 native pixels.
    dx,dy=-43,-23
    return x+dx*math.cos(a)+dy*math.sin(a),y-dx*math.sin(a)+dy*math.cos(a)


def spring_scene(im,d,t):
    c.art.rounded(d,(52,174,181,184),5,'#7b5b59',c.INK,1)
    c.art.line(d,[(59,176),(175,176)],'#a68170',1)
    x,y,angle=watering_pose(t)
    c.art.stamp(im,p('spring-wateringcan',102,79),x,y,angle)
    # One matched ballistic stream: every drop leaves the actual transformed rose.
    for n in range(16):
        emitted=1.+n*.075;age=t-emitted
        if age<0:continue
        xx,yy=spout_position(emitted);flight=(-50+math.sqrt(2500+480*(176-yy)))/240
        if age<flight:
            dx=xx-65*age;dy=yy+50*age+120*age*age
            c.art.ellipse(d,(dx-1.15,dy-2.3,dx+1.15,dy+2.3),c.LILAC)
        elif age<flight+.12:
            splash=(age-flight)/.12;lx=xx-65*flight
            c.art.line(d,[(lx-3-3*splash,174-3*splash),(lx-1,175)],c.LILAC,.8)
            c.art.line(d,[(lx+1,175),(lx+3+3*splash,174-3*splash)],c.LILAC,.8)
    for n,(xx,target) in enumerate([(77,133),(116,121),(155,136)]):
        start=1.42+n*.22;grow=c.phase(t,start,start+.62)
        yy=176-(176-target)*grow
        sway=2*math.sin((t-start)*7+n)*pulse(t,start,.95)
        headx=xx+sway*grow
        c.art.line(d,[(xx,177),(xx+sway*.4,176-(176-yy)*.45),(headx,yy)],c.SAGE,3)
        if grow>0:
            for side,offset in [(-1,18),(1,9)]:
                leaf_y=yy+offset*grow;lw=12*grow;lh=4*grow
                c.art.ellipse(d,(headx+(side-1)*lw/2,leaf_y-lh,headx+(side+1)*lw/2,leaf_y+lh),c.SAGE)
        opening=c.spring(t,1.65+n*.3,2.2+n*.3,.9)
        if opening>0:
            color=[c.CORAL,c.LILAC,c.CREAM][n];twist=(1-min(1,opening))*.6
            for a in range(5):
                aa=a*math.tau/5+twist;px=headx+math.cos(aa)*8*opening;py=yy+math.sin(aa)*8*opening
                c.art.ellipse(d,(px-6*opening,py-6*opening,px+6*opening,py+6*opening),color)
            c.art.ellipse(d,(headx-5*opening,yy-5*opening,headx+5*opening,yy+5*opening),c.GOLD)
    if t>=3.0:
        for xx,yy in [(56,113),(137,93),(173,155)]:sparkle(d,xx,yy,2.7*c.phase(t,3.,3.25),t,c.SAGE)


@lru_cache(None)
def popper():
    im=Image.new('RGBA',(70*c.S,68*c.S));d=ImageDraw.Draw(im)
    polygon(d,[(7,63),(24,12),(64,44)],c.CORAL,c.INK,1.5)
    for points in [[(13,49),(27,56)],[(18,36),(39,47)],[(23,22),(51,37)]]:c.art.line(d,points,c.GOLD,4)
    c.art.line(d,[(25,13),(63,44)],c.CREAM,5)
    c.art.line(d,[(27,14),(62,42)],c.GOLD,1.4)
    c.art.line(d,[(9,59),(3,64),(12,67)],c.CREAM,1)
    return im


def new_year_scene(im,d,t):
    preload=.06*pulse(t,1.41,.29);recoil=pulse(t,1.7,.42)
    c.deform_stamp(im,popper(),120-7*recoil,156+3*recoil,1+preload-.045*recoil,1-preload+.045*recoil,-6*recoil)
    for value,start,end in [('3',.5,.9),('2',.9,1.3),('1',1.3,1.7)]:
        if start<=t<end:
            layer=Image.new('RGBA',im.size);ld=ImageDraw.Draw(layer)
            c.label(ld,value,177,109+3*(1-c.spring(t,start,start+.25)),round(44+4*c.spring(t,start,start+.28)),c.GOLD)
            composite(im,layer,1-c.phase(t,end-.09,end))
    if t>=1.7:
        u=c.spring(t,1.7,2.55,1.0)
        targets=[(85,79,10),(133,66,12),(184,79,13),(225,111,9),(199,156,8),(151,121,7)]
        for n,(tx,ty,r) in enumerate(targets):
            x=130+(tx-130)*u;y=149+(ty-149)*u-8*math.sin(math.pi*min(1,u))
            if t<2.3:
                old=c.spring(max(1.7,t-.055),1.7,2.55,1.0)
                ox=130+(tx-130)*old;oy=149+(ty-149)*old-8*math.sin(math.pi*min(1,old))
                c.art.line(d,[(ox,oy),(x,y)],'#8f7485',1.4)
            c.art.star(d,x,y,r*min(1,u),[c.GOLD,c.CORAL,c.LILAC][n%3],angle=.1*n+(1-min(1,u))*.6)
        for n in range(12):
            target=(55+(n*31)%176,87+(n*19)%87)
            q=c.phase(t,1.76,2.7);xx=130+(target[0]-130)*q;yy=149+(target[1]-149)*q
            aa=n+3*q
            c.art.line(d,[(xx-2*math.cos(aa),yy-2*math.sin(aa)),(xx+2*math.cos(aa),yy+2*math.sin(aa))],c.SAGE if n%2 else c.CREAM,1.5)
        if t<2.02:
            burst=Image.new('RGBA',im.size);bd=ImageDraw.Draw(burst);a=t-1.7
            c.art.ellipse(bd,(130-12*a,149-12*a,130+12*a,149+12*a),None,c.CREAM,1.3)
            composite(im,burst,1-c.phase(t,1.72,2.02))
    if t>=2.8:c.art.heart(d,234,165,6*c.spring(t,2.8,3.15),c.CORAL)


SCENES={
 'season-birthday':birthday_scene,'season-christmas':christmas_scene,
 'season-halloween':halloween_scene,'season-easter':easter_scene,
 'season-summer':summer_scene,'season-autumn':autumn_scene,
 'season-winter':winter_scene,'season-spring':spring_scene,
 'season-new-year':new_year_scene,
}


def scene(name,t):
    im=c.stage(CLIPS[name]);d=ImageDraw.Draw(im)
    c.art.ellipse(d,(54,182,226,191),'#392c3c')
    SCENES[name](im,d,t)
    ready,before=REVEALS[name]
    c.reveal_caption(im,CLIPS[name],t,ready,before,size=18)
    return im


def audio_events(name):
    def e(at,kind,hz,duration=.2,gain=.13):return dict(at=at,kind=kind,hz=hz,duration=duration,gain=gain)
    return {
      'season-birthday':[e(.5,'sparkle',1047,.3,.1),e(1.42,'whoosh',500,.26,.09),e(1.7,'pop',470,.12,.14),e(1.8,'pluck',659,.18,.1),e(1.96,'pluck',784,.18,.1),e(2.12,'pluck',1047,.2,.1),e(2.75,'chord',[659,784,1047],.55,.14)],
      'season-christmas':[e(.5,'rustle',450,.36,.1),e(1.08,'pop',330,.14,.13),e(1.55,'pluck',659,.25,.13),e(1.78,'pluck',784,.25,.11),e(2.15,'thud',250,.17,.09),e(2.7,'chord',[523,659,784],.65,.14)],
      'season-halloween':[e(.5,'whoosh',420,.45,.08),e(1.15,'whoosh',510,.35,.08),e(1.75,'pop',540,.13,.11),e(2.3,'rustle',650,.3,.08),e(2.95,'pop',620,.14,.12),e(3.2,'chord',[659,880],.42,.12)],
      'season-easter':[e(.55,'tick',720,.12,.1),e(1.12,'pop',520,.12,.12),e(1.65,'pluck',659,.3,.12),e(1.95,'pluck',784,.3,.11),e(2.45,'thud',270,.15,.08),e(2.8,'chord',[659,784,1047],.55,.13)],
      'season-summer':[e(.5,'chime',784,.35,.11),e(1.05,'pluck',660,.15,.08),e(1.55,'pop',410,.13,.11),e(1.85,'whoosh',600,.3,.08),e(2.02,'pop',720,.12,.11),e(2.5,'chord',[587,740,880],.6,.14)],
      'season-autumn':[e(.5,'rustle',400,.7,.09),e(1.85,'thud',220,.16,.08),e(2.2,'rustle',350,.3,.07),e(2.75,'chord',[392,494,587],.7,.13)],
      'season-winter':[e(.5,'sparkle',1175,.45,.075),e(1.15,'rustle',310,.6,.085),e(2.05,'pluck',523,.3,.11),e(2.4,'rustle',350,.18,.06),e(2.85,'chord',[523,659,784],.6,.13)],
      'season-spring':[e(.65,'rustle',540,.25,.07),e(1.,'pluck',740,.2,.08),e(1.18,'pop',880,.12,.07),e(1.37,'pop',988,.12,.07),e(1.65,'pluck',523,.28,.12),e(1.95,'pluck',659,.28,.12),e(2.25,'pluck',784,.28,.12),e(3.,'chord',[523,659,784],.55,.14)],
      'season-new-year':[e(.5,'tick',523,.12,.1),e(.9,'tick',659,.12,.11),e(1.3,'tick',784,.12,.12),e(1.7,'pop',440,.15,.15),e(1.8,'whoosh',750,.25,.07),e(2.3,'chord',[523,659,784,1047],.55,.14),e(2.8,'sparkle',1319,.55,.1)],
    }[name]
