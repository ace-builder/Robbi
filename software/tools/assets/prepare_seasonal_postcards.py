"""Build 33 distinct seasonal and Advent cartoons with stable artwork and sound."""
from asset_catalog import catalog_group, catalog_path, read_catalog
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
import argparse
import importlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import seasonal_common as c
from prepare_pocket_postcards import STARTS, TIMING, encode, sha

ROOT = c.ROOT
OUT = ROOT / 'test-results/seasonal-postcards-preview'
CATALOG = catalog_group(ROOT, 'seasonal_postcards')
MODULE_NAMES = ('seasonal_holidays', 'seasonal_advent_early', 'seasonal_advent_late')


@lru_cache(None)
def definitions():
    clips, owners = {}, {}
    for module_name in MODULE_NAMES:
        module = importlib.import_module(module_name)
        for name, detail in module.CLIPS.items():
            assert name not in clips, name
            clips[name] = detail
            owners[name] = module
    assert len(clips) == 33
    assert {v['day'] for v in clips.values() if v['group'] == 'advent'} == set(range(1, 25))
    return clips, owners


@lru_cache(maxsize=33)
def cover(name):
    return c.cover(definitions()[0][name])


def render(name, t):
    card = cover(name)
    if t < .1 or t >= 4.35:
        return card.copy()
    frame = definitions()[1][name].scene(name, t).convert('RGB')
    assert frame.size == (840, 720)
    if t < .35:
        return Image.blend(card, frame, c.phase(t, .1, .35))
    if t > 4.0:
        return Image.blend(frame, card, c.phase(t, 4.0, 4.35))
    return frame


def soundtrack(name):
    return c.synthesize(name, definitions()[1][name].audio_events(name))


def export_gif(name, frames, path):
    # Evaluate both fixed palettes across the complete movie, including every
    # fade. Select the one with the smallest worst-frame color error.
    swatches = Image.new('RGB', (2800, 2160))
    for n, frame in enumerate(frames):
        swatches.paste(frame, ((n%10)*280, (n//10)*240))
    reference = [np.asarray(f).astype(np.int16) for f in frames]
    candidates = []
    for method in (Image.Quantize.MEDIANCUT, Image.Quantize.FASTOCTREE):
        palette = swatches.quantize(colors=256, method=method)
        quantized = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
        errors = [float(np.abs(a-np.asarray(b.convert('RGB')).astype(np.int16)).mean())
                  for a, b in zip(reference, quantized)]
        candidates.append((max(errors), method.name, quantized, float(np.mean(errors))))
    worst, method, quantized, average = min(candidates, key=lambda candidate:candidate[0])
    assert worst < 3, (name, method, worst)
    quantized[0].save(path, save_all=True, append_images=quantized[1:], duration=50,
                      loop=0, optimize=False, disposal=2)
    return dict(method=method, worst_mean_error=round(worst,5), average_mean_error=round(average,5))


def export_one(name):
    detail = definitions()[0][name]
    audio, report = soundtrack(name)
    wav = ROOT/'assets/sounds'/(name+'.wav')
    wav.write_bytes(audio)
    sidecar = ROOT/'assets/videos'/(name+'_audio.wav')
    sidecar.write_bytes(audio)
    masters = [render(name, n/c.FPS) for n in range(90)]
    frames = [f.resize((280, 240), Image.Resampling.LANCZOS) for f in masters]
    assert frames[0].tobytes() == frames[-1].tobytes()
    video = ROOT/'assets/videos'/(name+'.mp4')
    encode(frames, (280, 240), wav, video)
    share = OUT/'share'/(name+'.mp4')
    encode(masters, (840, 720), wav, share)
    keys = [render(name, t).resize((280, 240), Image.Resampling.LANCZOS) for t in STARTS]
    atlas = Image.new('RGB', (1120, 1200))
    for n, frame in enumerate(keys):
        atlas.paste(frame, ((n%4)*280, (n//4)*240))
    sheet = ROOT/'assets/face_animations'/(name+'.png')
    atlas.save(sheet)
    assert sheet.stat().st_size <= 4*1024*1024
    manifest = sheet.with_suffix('.json')
    manifest.write_text(json.dumps(dict(sheet=sheet.name, columns=4, rows=5,
        order=list(range(20)), durations_ms=TIMING, blend_ms=0))+'\n')
    gif = OUT/'gifs'/(name+'.gif')
    palette_report = export_gif(name, frames, gif)
    frames[71].save(OUT/(name+'-poster.png'))
    record = {**{k:v for k,v in detail.items() if k != 'cues'}, 'id':name,
        'revision':2, 'gif_palette':palette_report,
        'duration_s':4.5, 'fps':20, 'frames':90, 'native_size':[280,240],
        'share_size':[840,720], 'sprite_frames':20,
        'cues':[dict(time_s=t, label=label, video_frame=round(t*20)) for t,label in detail['cues']],
        'video':video.relative_to(ROOT).as_posix(), 'audio':wav.relative_to(ROOT).as_posix(),
        'sidecar':sidecar.relative_to(ROOT).as_posix(), 'sheet':sheet.relative_to(ROOT).as_posix(),
        'manifest':manifest.relative_to(ROOT).as_posix(), 'gif':gif.relative_to(OUT).as_posix(),
        'share':share.relative_to(OUT).as_posix(), 'poster':name+'-poster.png',
        'contains_robbi_face':False, 'bookend':'typographic-postcard', 'sound_default':True,
        'audio_sha256':sha(wav), 'video_sha256':sha(video), 'sheet_sha256':sha(sheet), 'audio_metrics':report}
    # Independent records allow interrupted runs to resume without losing completed clips.
    (OUT/'records'/(name+'.json')).write_text(json.dumps(record, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    return name


def assemble():
    records = [json.loads((OUT/'records'/(name+'.json')).read_text(encoding='utf-8')) for name in definitions()[0]]
    CATALOG.write_text(json.dumps(dict(version=2, title='A year of little moments', duration_s=4.5,
        advent_days='December 1–24; every day can be previewed at any time.', clips=records,
        sound_provenance='Original deterministic synthesis; no voices, recordings or third-party samples.'),
        indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    (OUT/'audio-checks.json').write_text(json.dumps([{'id':r['id'], **r['audio_metrics']} for r in records], indent=2)+'\n')
    for group, subset in [('holidays', records[:9]), ('advent-01-12', records[9:21]), ('advent-13-24', records[21:])]:
        overview = Image.new('RGB', (900, 285*((len(subset)+2)//3)), c.BG)
        draw = ImageDraw.Draw(overview)
        for i, record in enumerate(subset):
            x,y = (i%3)*300+10, (i//3)*285+5
            with Image.open(OUT/record['poster']) as poster:
                overview.paste(poster, (x,y))
            title = (f"{record['day']:02d} · " if 'day' in record else '')+record['title']
            draw.text((x, y+247), title, font=ImageFont.truetype(str(c.art.SOURCE/'fonts/Lato-Bold.ttf'),14), fill=c.CREAM)
        overview.save(OUT/(group+'-overview.png'))
    print('Assembled all 33 postcards.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', help='Rebuild selected IDs, preserving other completed records')
    parser.add_argument('--workers', type=int, default=3, choices=range(1,5))
    parser.add_argument('--assemble-only', action='store_true')
    args = parser.parse_args()
    for folder in (OUT, OUT/'gifs', OUT/'share', OUT/'records', CATALOG.parent):
        folder.mkdir(parents=True, exist_ok=True)
    names = args.only or list(definitions()[0])
    assert all(n in definitions()[0] for n in names)
    if not args.assemble_only:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for future in as_completed([pool.submit(export_one, name) for name in names]):
                print('Exported '+future.result(), flush=True)
    if all((OUT/'records'/(name+'.json')).is_file() for name in definitions()[0]):
        assemble()


if __name__ == '__main__':
    main()
