"""Compose short LCD cartoons from fixed artwork; export sheets, GIFs and MP4s.

No generation APIs or robot hardware are used by this repeatable export step.
Imagegen originals, prompts and font licenses live in dynamics_sources/screen-gags-v1.
"""
from __future__ import annotations

from asset_catalog import catalog_group, catalog_path, read_catalog
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from robbi.face_animation import FaceAnimations
from robbi.expression_catalog import SCREEN_GAGS

SOURCE=ROOT/'assets/dynamics_sources/screen-gags-v1'
SHEETS=ROOT/'assets/face_animations'
PREVIEW=ROOT/'test-results/screen-gags-preview'
S=3
BG='#11292c'; MINT='#a9d7c5'; INK='#173337'; CREAM='#fff2cc'; ORANGE='#ffb56e'
TIMING=[100,100]+[150]*16+[100,300]
DETAILS={
 'gag-buffering':('One sec…','For a tiny pause that somehow takes forever.','emo_thoughtful_a'),
 'gag-deal-with-it':('Deal with it','For an absurdly confident tiny victory.','emo_proud_b'),
 'gag-this-is-fine':('This is fine','For harmless everyday chaos.','emo_worried_c'),
 'gag-tiny-violin':('The tiny violin','For a deliberately overdramatic minor inconvenience.','emo_disappointed_b'),
 'gag-no-thoughts':('No thoughts. Just vibes.','For a blissfully unbothered moment.','emo_content_c'),
 'gag-hamster-brain':('One brain cell','For thinking very hard about something very small.','emo_determined_a'),
 'gag-touch-grass':('Touch grass','For the moment a coding marathon needs a breather.','emo_relieved_b'),
 'gag-task-failed':('Task failed successfully','For a wrong turn that somehow works out.','emo_confused_a'),
 'gag-social-battery':('Social battery','For recharging with a little kindness.','emo_sleepy_c'),
 'gag-bonk':('Bonk','For a harmless cartoon oops.','emo_startled_a'),
 'gag-dramatic-zoom':('Wait… what?','For the most unnecessarily dramatic realization.','emo_amazed_b'),
 'gag-achievement':('Did a thing','For celebrating a very modest accomplishment.','emo_happy_a'),
}


@lru_cache(None)
def font(size,bold=True):
    return ImageFont.truetype(str(SOURCE/'fonts'/('Lato-Bold.ttf' if bold else 'Lato-Regular.ttf')),round(size*S))


def ease(t):
    t=max(0,min(1,t));return t*t*(3-2*t)


def xy(values):return tuple(round(v*S) for v in values)


def line(draw,points,color,width=2):
    draw.line([xy(p) for p in points],fill=color,width=round(width*S),joint='curve')


def ellipse(draw,box,fill=None,outline=None,width=2):
    draw.ellipse(xy(box),fill=fill,outline=outline,width=round(width*S))


def rounded(draw,box,radius,fill,outline=None,width=2):
    draw.rounded_rectangle(xy(box),round(radius*S),fill=fill,outline=outline,width=round(width*S))


def text(draw,value,y,size=24,color=CREAM):
    f=font(size)
    bounds=draw.textbbox((0,0),value,font=f)
    if bounds[2]-bounds[0]>256*S:raise ValueError('Caption too wide: '+value)
    draw.text((140*S,round(y*S)),value,font=f,fill=color,anchor='mm',stroke_width=0)


def star(draw,cx,cy,r,color=ORANGE,angle=0):
    pts=[]
    for n in range(10):
        a=angle-math.pi/2+n*math.pi/5;radius=r if n%2==0 else r*.45
        pts.append(xy((cx+math.cos(a)*radius,cy+math.sin(a)*radius)))
    draw.polygon(pts,fill=color)


def heart(draw,cx,cy,r,color='#f28c8b'):
    points=[]
    for n in range(65):
        t=n/64*2*math.pi
        points.append(xy((cx+r*math.sin(t)**3,cy-r*(13*math.cos(t)-5*math.cos(2*t)-2*math.cos(3*t)-math.cos(4*t))/16)))
    draw.polygon(points,fill=color)


PLAYER=FaceAnimations(SHEETS,None)


@lru_cache(None)
def face(name='natural-blink',index=0):
    return PLAYER.frames(name)[index].convert('RGBA')


@lru_cache(None)
def face_badge(width,name,index):
    height=round(width*240/280)
    image=face(name,index).resize((round(width*S),height*S),Image.Resampling.LANCZOS)
    mask=Image.new('L',image.size)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,image.width-1,image.height-1),round(18*S),fill=255)
    image.putalpha(mask)
    return image


def head(image,x,y,width=130,name='natural-blink',index=0):
    badge=face_badge(width,name,index)
    image.alpha_composite(badge,(round(x*S-badge.width/2),round(y*S-badge.height/2)))


@lru_cache(None)
def prop(name,width,height):
    with Image.open(SOURCE/(name+'.png')) as original:
        original=original.convert('RGBA')
        bbox=original.getchannel('A').point(lambda a:255 if a>80 else 0).getbbox()
        if bbox is None:raise ValueError('Empty sprite '+name)
        pad=8
        bbox=(max(0,bbox[0]-pad),max(0,bbox[1]-pad),min(original.width,bbox[2]+pad),min(original.height,bbox[3]+pad))
        sprite=original.crop(bbox)
        sprite.thumbnail((round(width*S),round(height*S)),Image.Resampling.LANCZOS)
        return sprite


def stamp(image,sprite,x,y,angle=0):
    if angle:sprite=sprite.rotate(angle,resample=Image.Resampling.BICUBIC,expand=True)
    image.alpha_composite(sprite,(round(x*S-sprite.width/2),round(y*S-sprite.height/2)))


def note(draw,x,y):
    ellipse(draw,(x-4,y-3,x+4,y+3),MINT)
    line(draw,[(x+3,y),(x+3,y-14),(x+10,y-11)],MINT,2)


def scene(name,t):
    image=Image.new('RGBA',(280*S,240*S),BG);draw=ImageDraw.Draw(image)
    if name=='gag-buffering':
        text(draw,'ONE SEC...',25,26)
        head(image,140,97,100)
        rounded(draw,(38,158,242,180),10,None,MINT,2)
        progress=.18+.81*ease(t/.35)
        rounded(draw,(42,162,42+196*progress,176),6,MINT)
        text(draw,str(min(99,round(progress*100)))+'%',207,25,ORANGE)
        angle=t*360*1.5
        draw.arc(xy((214,197,232,215)),angle,angle+250,fill=ORANGE,width=3*S)
    elif name=='gag-deal-with-it':
        image=face().resize((280*S,240*S),Image.Resampling.LANCZOS);draw=ImageDraw.Draw(image)
        y=-84+140*ease(t/.5)
        for x in (28,174):
            rounded(draw,(x,y,x+84,y+82),9,INK)
            line(draw,[(x+16,y+17),(x+31,y+10)],'#71918e',4)
        line(draw,[(112,y+18),(174,y+18)],INK,10)
        if t>=.4:text(draw,'DEAL WITH IT.',211,24,INK)
    elif name=='gag-this-is-fine':
        text(draw,'THIS IS FINE.',25,24)
        for n,x in enumerate((32,56,224,248)):
            h=42+9*math.sin(t*math.pi*3+n)
            points=[(x-14,190),(x-11,165),(x-4,170),(x+3,190-h),(x+8,166),(x+15,190)]
            draw.polygon([xy(p) for p in points],fill='#ec845a')
            draw.polygon([xy(p) for p in [(x-7,190),(x+1,170),(x+8,190)]],fill='#ffd17d')
        head(image,140,115,130,'proud-smile',4)
        lift=12*math.sin(math.pi*ease(t))
        ellipse(draw,(176,153-lift,195,172-lift),None,CREAM,3)
        rounded(draw,(151,146-lift,181,175-lift),6,CREAM)
        ellipse(draw,(153,145-lift,180,152-lift),'#7b5745')
        line(draw,[(159,136-lift),(163,131-lift),(159,126-lift)],MINT,2)
        text(draw,'tea helps.',216,18,MINT)
    elif name=='gag-tiny-violin':
        text(draw,'TRAGIC.',25,28)
        stamp(image,prop('violin',66,143),140,124)
        shift=17*math.sin(t*math.pi*4)
        line(draw,[(85+shift,115),(185+shift,148)],'#b67e4b',4)
        line(draw,[(85+shift,111),(185+shift,144)],CREAM,1)
        note(draw,78,97-5*math.sin(t*math.pi*2));note(draw,203,137-7*math.sin(t*math.pi*2+.7))
        if t>.48:text(draw,'anyway...',214,20,MINT)
    elif name=='gag-no-thoughts':
        text(draw,'NO THOUGHTS',25,23)
        for n in range(2):
            stretch=6*math.sin(t*math.pi*2+n)
            ellipse(draw,(72-stretch,166+n*9,208+stretch,182+n*9),None,'#548d90',2)
        stamp(image,prop('duck',122,113),140,125+4*math.sin(t*math.pi*2),-3*math.sin(t*math.pi*2))
        text(draw,'just vibes.',216,22,MINT)
    elif name=='gag-hamster-brain':
        text(draw,'PROCESSING...',25,23)
        rounded(draw,(105,191,175,202),4,MINT)
        ellipse(draw,(76,64,204,192),None,MINT,5)
        for n in range(5):
            a=n*2*math.pi/5+t*math.pi*2.5
            line(draw,[(140,128),(140+math.cos(a)*60,128+math.sin(a)*60)],'#426769',2)
        stamp(image,prop('hamster',84,76),143,151-4*abs(math.sin(t*math.pi*2)),2*math.sin(t*math.pi*4))
        text(draw,'one brain cell',220,18,ORANGE)
    elif name=='gag-touch-grass':
        text(draw,'TOUCH GRASS',25,23)
        ellipse(draw,(66,176,214,197),'#274a42')
        grow=.25+.75*ease(t/.6)
        for n in range(13):
            x=78+n*10;h=(28+(n*7)%21)*grow;lean=3*math.sin(t*math.pi*2+n)
            line(draw,[(x,188),(x+lean-2,188-h*.55),(x+lean,188-h)],'#9fd49a',3)
            line(draw,[(x+lean-1,188-h*.5),(x-7+lean,188-h*.72)],'#75b998',2)
        pat=math.sin(math.pi*ease((t-.3)/.7))
        hx=212-60*pat;hy=97+47*pat
        rounded(draw,(hx+5,hy-23,hx+23,hy),6,'#6ea99b',INK,2)
        rounded(draw,(hx-13,hy-7,hx+26,hy+18),11,MINT,INK,2)
        for j in range(3):line(draw,[(hx-5+j*7,hy+3),(hx-5+j*7,hy+10)],'#70aa9e',1)
        text(draw,'achievement unlocked',218,16,ORANGE)
    elif name=='gag-task-failed':
        text(draw,'TASK FAILED',26,26,ORANGE)
        ellipse(draw,(91,70,189,168),None,'#335955',3)
        if t<.35:
            line(draw,[(118,94),(162,144)],'#e78875',8);line(draw,[(162,94),(118,144)],'#e78875',8)
        else:
            q=ease((t-.35)/.2)
            line(draw,[(103,123),(129,147),(179,89)],MINT,9)
            star(draw,212,89,8*q,ORANGE);star(draw,70,145,6*q,ORANGE)
            text(draw,'SUCCESSFULLY',195,24,MINT)
            text(draw,'somehow.',221,16,CREAM)
    elif name=='gag-social-battery':
        text(draw,'SOCIAL BATTERY',25,22)
        rounded(draw,(61,82,215,137),10,None,MINT,3)
        rounded(draw,(216,98,227,121),3,MINT)
        charge=.08+.8*ease((t-.35)/.6)
        rounded(draw,(68,89,68+140*charge,130),5,'#f18e79' if t<.4 else MINT)
        heart(draw,140,169-62*ease(t/.65),15*(1-.2*ease(t/.65)))
        if t>.3:text(draw,'RECHARGING...',179,21,ORANGE)
        text(draw,'tiny kindness detected',216,16,CREAM)
    elif name=='gag-bonk':
        text(draw,'BONK.',25,28,ORANGE)
        impact=math.sin(math.pi*ease((t-.25)/.35))
        head(image,140,144+5*impact,152,'natural-blink',4 if .35<t<.7 else 0)
        hammer=Image.new('RGBA',(100*S,100*S))
        hd=ImageDraw.Draw(hammer)
        rounded(hd,(46,32,56,90),4,'#b38159',INK,2)
        rounded(hd,(18,12,84,43),10,ORANGE,INK,3)
        stamp(image,hammer,186,90+14*impact,-35+55*impact)
        if t>.45:
            a=(t-.45)/.55
            star(draw,80-12*a,81-10*math.sin(a*math.pi),7,MINT,a*2)
            star(draw,219+8*a,105-12*math.sin(a*math.pi),6,ORANGE,a*2)
        text(draw,'recalculating...',220,17,MINT)
    elif name=='gag-dramatic-zoom':
        base=face('amazed-gasp',4)
        scale=1+.52*ease(t/.65)
        wide=base.resize((round(280*S*scale),round(240*S*scale)),Image.Resampling.LANCZOS)
        x=(wide.width-280*S)//2;y=round((wide.height-240*S)*.4)
        image=wide.crop((x,y,x+280*S,y+240*S));draw=ImageDraw.Draw(image)
        for n in range(8):
            a=n*math.pi/4
            line(draw,[(140+math.cos(a)*165,120+math.sin(a)*140),(140+math.cos(a)*126,120+math.sin(a)*100)],INK,2)
        rounded(draw,(78,8,202,40),8,CREAM)
        text(draw,'WAIT.',24,25,INK)
        if t>.4:
            rounded(draw,(61,197,219,231),8,CREAM)
            text(draw,'...WHAT?',214,26,INK)
    elif name=='gag-achievement':
        text(draw,'ACHIEVEMENT',24,22);text(draw,'UNLOCKED',49,18,MINT)
        q=ease(t/.65)
        for n in range(14):
            a=n*2*math.pi/14
            x=140+math.cos(a)*104*q;y=119+math.sin(a)*57*q+25*q*q
            rounded(draw,(x-2,y-3,x+2,y+3),1,(ORANGE,MINT,'#e88b87')[n%3])
        star(draw,140,128,43*(1+.06*math.sin(t*math.pi*2)),ORANGE,.06*math.sin(t*math.pi*2))
        ellipse(draw,(128,119,132,125),INK);ellipse(draw,(148,119,152,125),INK)
        draw.arc(xy((132,126,149,139)),0,180,fill=INK,width=2*S)
        text(draw,'DID A THING.',213,25,MINT)
    else:raise ValueError(name)
    return image.convert('RGB').resize((280,240),Image.Resampling.LANCZOS)


def frames_for(name):
    neutral=face().convert('RGB')
    return [neutral,Image.blend(neutral,scene(name,0),.5)]+[scene(name,n/15) for n in range(16)]+[Image.blend(scene(name,1),neutral,.5),neutral]


def main():
    PREVIEW.mkdir(parents=True,exist_ok=True)
    (PREVIEW/'gifs').mkdir(exist_ok=True)
    (ROOT/'assets/videos').mkdir(parents=True,exist_ok=True)
    records=[]
    overview=Image.new('RGB',(4*300,3*290),'#11292c');od=ImageDraw.Draw(overview)
    for index,name in enumerate(SCREEN_GAGS):
        frames=frames_for(name)
        atlas=Image.new('RGB',(4*280,5*240))
        for i,frame in enumerate(frames):atlas.paste(frame,((i%4)*280,(i//4)*240))
        png=SHEETS/(name+'.png');atlas.save(png)
        if png.stat().st_size>4*1024*1024:raise ValueError('Sprite sheet exceeds runtime limit')
        spec={'sheet':png.name,'columns':4,'rows':5,'order':list(range(20)),'durations_ms':TIMING,'blend_ms':0}
        (SHEETS/(name+'.json')).write_text(json.dumps(spec)+'\n')
        palette=atlas.quantize(colors=256,method=Image.Quantize.MEDIANCUT)
        gif_frames=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]
        gif=PREVIEW/'gifs'/(name+'.gif')
        gif_frames[0].save(gif,save_all=True,append_images=gif_frames[1:],duration=TIMING,loop=0,optimize=False,disposal=2)
        clip=FaceAnimations(SHEETS,None).clip(name)
        video=ROOT/'assets/videos'/(name+'.mp4')
        raw=b''.join((clip.frames[-1] if i==29 else clip.frame_at(i/10)).tobytes() for i in range(30))
        subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pixel_format','rgb24','-video_size','280x240','-framerate','10','-i','pipe:0',
                        '-an','-c:v','libx264','-preset','medium','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(video)],input=raw,check=True)
        title,when,sound=DETAILS[name]
        peak=frames[12]
        peak.save(PREVIEW/(name+'-poster.png'))
        col,row=index%4,index//4;overview.paste(peak,(col*300+10,row*290+8))
        od.text((col*300+12,row*290+254),title,font=ImageFont.truetype(str(SOURCE/'fonts/Lato-Bold.ttf'),16),fill=CREAM)
        records.append({'id':name,'title':title,'description':SCREEN_GAGS[name],'when':when,'duration_s':3.0,'native_size':[280,240],
                        'frames':20,'sheet':'assets/face_animations/'+png.name,'manifest':'assets/face_animations/'+name+'.json',
                        'video':'assets/videos/'+video.name,'gif':'gifs/'+gif.name,'suggested_sound':sound,'sound_default':False,
                        'sheet_sha256':hashlib.sha256(png.read_bytes()).hexdigest(),'video_sha256':hashlib.sha256(video.read_bytes()).hexdigest()})
        print(name,'20 frames, 3 s',png.stat().st_size,'bytes',flush=True)
    overview.save(PREVIEW/'overview.png')
    (ROOT/'assets').mkdir(exist_ok=True)
    (catalog_group(ROOT, 'screen_gags')).write_text(json.dumps({'version':1,'clips':records},indent=2)+'\n',encoding='utf-8')

    # Restore authored audio after rebuilding the original silent video masters.
    if (catalog_group(ROOT, 'animation_sound')).exists():
        subprocess.run([sys.executable, str(ROOT/'tools/complete_animation_sound.py'),
                        '--render', '--only', *SCREEN_GAGS], check=True)


if __name__=='__main__':main()
