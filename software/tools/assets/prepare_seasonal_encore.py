"""Render twelve new holiday and seasonal stories without replacing earlier packs."""
from asset_catalog import catalog_group, catalog_path, read_catalog
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
import argparse
import importlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import seasonal_common as c
from prepare_pocket_postcards import STARTS, TIMING, encode, sha
from prepare_seasonal_postcards import export_gif

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'test-results/seasonal-encore-preview'
CATALOG = catalog_group(ROOT, 'seasonal_encore')
MODULE_NAMES = ('seasonal_encore_festive', 'seasonal_encore_nature', 'seasonal_encore_holidays')
GROUPS = {'celebrations':'Little celebrations', 'seasons':'Outside, together', 'holidays':'Holiday surprises'}


@lru_cache(None)
def definitions():
    clips, owners = {}, {}
    for module_name in MODULE_NAMES:
        module = importlib.import_module(module_name)
        for name, detail in module.CLIPS.items():
            assert name not in clips and detail['group'] in GROUPS, name
            clips[name], owners[name] = detail, module
    assert len(clips) == 12
    return clips, owners


@lru_cache(maxsize=12)
def cover(name):
    return c.cover(definitions()[0][name])


def render(name, t):
    card = cover(name)
    if t < .1 or t >= 4.35:
        return card.copy()
    frame = definitions()[1][name].scene(name, t).convert('RGB')
    assert frame.size == (840,720)
    if t < .35:
        return Image.blend(card, frame, c.phase(t,.1,.35))
    if t > 4:
        return Image.blend(frame, card, c.phase(t,4,4.35))
    return frame


def soundtrack(name):
    return c.synthesize(name, definitions()[1][name].audio_events(name))


def export_one(name):
    detail = definitions()[0][name]
    audio, audio_metrics = soundtrack(name)
    wav = ROOT/'assets/sounds'/(name+'.wav')
    sidecar = ROOT/'assets/videos'/(name+'_audio.wav')
    wav.write_bytes(audio)
    sidecar.write_bytes(audio)
    masters = [render(name,n/c.FPS) for n in range(90)]
    frames = [f.resize((280,240),Image.Resampling.LANCZOS) for f in masters]
    assert frames[0].tobytes() == frames[-1].tobytes()
    video = ROOT/'assets/videos'/(name+'.mp4')
    share = OUT/'share'/(name+'.mp4')
    encode(frames,(280,240),wav,video)
    encode(masters,(840,720),wav,share)
    atlas = Image.new('RGB',(1120,1200))
    for n,t in enumerate(STARTS):
        atlas.paste(render(name,t).resize((280,240),Image.Resampling.LANCZOS),((n%4)*280,(n//4)*240))
    sheet = ROOT/'assets/face_animations'/(name+'.png')
    atlas.save(sheet)
    assert sheet.stat().st_size <= 4*1024*1024
    manifest = sheet.with_suffix('.json')
    manifest.write_text(json.dumps(dict(sheet=sheet.name,columns=4,rows=5,order=list(range(20)),
                                        durations_ms=TIMING,blend_ms=0))+'\n')
    gif = OUT/'gifs'/(name+'.gif')
    palette = export_gif(name,frames,gif)
    poster = OUT/(name+'-poster.png')
    frames[71].save(poster)
    record = {**{k:v for k,v in detail.items() if k!='cues'},'id':name,'revision':1,
        'duration_s':4.5,'fps':20,'frames':90,'native_size':[280,240],'share_size':[840,720],
        'sprite_frames':20,'gif_palette':palette,
        'cues':[dict(time_s=t,label=label,video_frame=round(t*20)) for t,label in detail['cues']],
        'video':video.relative_to(ROOT).as_posix(),'audio':wav.relative_to(ROOT).as_posix(),
        'sidecar':sidecar.relative_to(ROOT).as_posix(),'sheet':sheet.relative_to(ROOT).as_posix(),
        'manifest':manifest.relative_to(ROOT).as_posix(),'gif':gif.relative_to(OUT).as_posix(),
        'share':share.relative_to(OUT).as_posix(),'poster':poster.name,
        'contains_robbi_face':False,'bookend':'typographic-postcard','sound_default':True,
        'video_sha256':sha(video),'audio_sha256':sha(wav),'sheet_sha256':sha(sheet),
        'audio_metrics':audio_metrics}
    (OUT/'records'/(name+'.json')).write_text(json.dumps(record,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return name


def assemble():
    clips = [json.loads((OUT/'records'/(name+'.json')).read_text(encoding='utf-8')) for name in definitions()[0]]
    payload = dict(version=1,title='A few more reasons to smile',groups=GROUPS,duration_s=4.5,clips=clips,
                   sound_provenance='Original deterministic nonverbal synthesis; no voices, recordings or sampled songs.')
    CATALOG.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (OUT/'audio-checks.json').write_text(json.dumps([dict(id=c['id'],**c['audio_metrics']) for c in clips],indent=2)+'\n')
    for group in [*GROUPS,'all']:
        subset = [clip for clip in clips if group=='all' or clip['group']==group]
        overview = Image.new('RGB',(900,285*((len(subset)+2)//3)),c.BG)
        draw = ImageDraw.Draw(overview)
        for n,clip in enumerate(subset):
            x,y = (n%3)*300+10,(n//3)*285+5
            with Image.open(OUT/clip['poster']) as poster:
                overview.paste(poster,(x,y))
            draw.text((x,y+247),clip['title'],font=ImageFont.truetype(str(c.art.SOURCE/'fonts/Lato-Bold.ttf'),14),fill=c.CREAM)
        overview.save(OUT/(group+'-overview.png'))
    print('Assembled all twelve new stories.',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',nargs='+',help='Render selected IDs; keep completed records for the others')
    parser.add_argument('--workers',type=int,default=3,choices=range(1,5))
    parser.add_argument('--assemble-only',action='store_true')
    args = parser.parse_args()
    for folder in (OUT,OUT/'gifs',OUT/'share',OUT/'records',CATALOG.parent):
        folder.mkdir(parents=True,exist_ok=True)
    names = args.only or list(definitions()[0])
    assert all(name in definitions()[0] for name in names)
    if not args.assemble_only:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for future in as_completed([pool.submit(export_one,name) for name in names]):
                print('Exported '+future.result(),flush=True)
    if all((OUT/'records'/(name+'.json')).is_file() for name in definitions()[0]):
        assemble()


if __name__=='__main__':
    main()
