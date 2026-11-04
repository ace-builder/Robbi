"""Render and master the ten meme encores from cached artwork and audio, offline."""
from asset_catalog import catalog_group, catalog_path, read_catalog
from concurrent.futures import ProcessPoolExecutor, as_completed
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import wave
import numpy as np
from PIL import Image, ImageDraw
from scipy import signal
from prepare_emotion_audio import loudness, metrics
from meme_encore_scenes import ROOT, SOURCE, CLIPS, GROUPS, render, cover, art

OUT=ROOT/'test-results/meme-encore-preview'
CATALOG=catalog_group(ROOT, 'meme_encore')
DURATION=6
RATE=44100
STARTS=[0,.1,.3,.55,.8,1.05,1.3,1.55,1.8,2.05,2.3,2.55,2.8,3.05,3.5,4.1,4.7,5.2,5.6,5.85]
TIMING=[round((b-a)*1000) for a,b in zip(STARTS,STARTS[1:]+[DURATION])]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args,**kwargs):return subprocess.run(args,capture_output=True,check=True,**kwargs).stdout


def pcm(x,rate=RATE):
    out=io.BytesIO()
    with wave.open(out,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate)
        w.writeframes(np.rint(np.clip(x,-1,.999969)*32768).astype('<i2').tobytes())
    return out.getvalue()


def decode(path):
    return np.frombuffer(run(['ffmpeg','-v','error','-i',str(path),'-f','f32le','-ac','1','-ar',str(RATE),'pipe:1']),dtype='<f4').astype(float)


def fade(x,attack=.012,release=.06):
    x=x.copy();a=min(len(x)//2,round(attack*RATE));b=min(len(x)//2,round(release*RATE))
    x[:a]*=np.linspace(0,1,a);x[-b:]*=np.linspace(1,0,b)
    return x


def normalize(x,target,peak,rate):
    # Loudness normalization with true-peak limiting keeps sparse effect sequences
    # and spoken punchlines equally audible across the collection.
    normalized=np.frombuffer(run(['ffmpeg','-v','error','-i','pipe:0','-af',
        f'loudnorm=I={target}:TP={peak}:LRA=7','-f','f32le','-ac','1','-ar',str(rate),'pipe:1'],
        input=pcm(x,rate)),dtype='<f4').astype(float)
    measured_level,measured_peak=loudness(pcm(normalized,rate))
    normalized*=10**(min(0,target-measured_level,peak-measured_peak-.1)/20)
    return normalized


def soundtrack(name):
    voice=decode(SOURCE/'audio'/f'{name}-voice.mp3')
    live=np.flatnonzero(abs(voice)>.003)
    assert len(live),f'Empty voice: {name}'
    voice=voice[max(0,live[0]-round(.035*RATE)):min(len(voice),live[-1]+round(.08*RATE))]
    original_duration=len(voice)/RATE
    speed=max(1,original_duration/2.5)
    assert speed<1.35,(name,'Voice too long; regenerate shorter delivery',speed)
    if speed>1:
        voice=np.frombuffer(run(['ffmpeg','-v','error','-i','pipe:0','-af',f'atempo={speed}',
            '-f','f32le','-ac','1','-ar',str(RATE),'pipe:1'],input=pcm(voice)),dtype='<f4').astype(float)
    voice=fade(voice)
    voice=signal.sosfilt(signal.butter(2,95,btype='highpass',fs=RATE,output='sos'),voice)
    voice*=min(.17/max(np.sqrt(np.mean(voice**2)),1e-8),.68/max(abs(voice)))
    sfx=decode(SOURCE/'audio'/f'{name}-sfx.mp3')[:round(2.65*RATE)]
    sfx=fade(sfx,.02,.12)
    sfx*=min(.095/max(np.sqrt(np.mean(sfx**2)),1e-8),.5/max(abs(sfx)))
    mix=np.zeros(DURATION*RATE)
    start=round(.35*RATE);mix[start:start+len(sfx)]+=sfx
    start=round(3.02*RATE);mix[start:start+len(voice)]+=voice
    # Frame-accurate tactile punctuation beneath the payoff, distinct from the generated bed.
    tt=np.arange(round(.18*RATE))/RATE
    accent=.055*np.sin(2*np.pi*(620*tt-720*tt**2))*np.exp(-tt*32)
    accent=fade(accent,.004,.025)
    at=round(2.8*RATE);mix[at:at+len(accent)]+=accent
    mix=signal.sosfilt(signal.butter(2,[100,10500],btype='bandpass',fs=RATE,output='sos'),mix)
    # Gentle compression preserves transient character while keeping spoken punchlines clear.
    mix=np.frombuffer(run(['ffmpeg','-v','error','-i','pipe:0','-af',
        'acompressor=threshold=0.18:ratio=2.4:attack=8:release=100:makeup=1',
        '-f','f32le','-ac','1','-ar',str(RATE),'pipe:1'],input=pcm(mix)),dtype='<f4').astype(float)
    mix=normalize(mix,-18,-2.2,RATE)
    mix[:round(.1*RATE)]=0;mix[-round(.2*RATE):]=0
    master=pcm(mix)
    native=signal.resample_poly(mix,1,2)
    native=signal.sosfilt(signal.butter(3,[150,7500],btype='bandpass',fs=22050,output='sos'),native)
    native=normalize(native,-21,-5.2,22050)
    native[:2205]=0;native[-4410:]=0
    data=pcm(native,22050);report=metrics(data)
    level,tp=loudness(master)
    report.update(master_lufs=level,master_true_peak_dbfs=tp,voice_start_s=3.02,
        voice_end_s=round(3.02+len(voice)/RATE,4),original_voice_duration_s=round(original_duration,4),
        voice_tempo_factor=round(speed,5),master_rate=RATE)
    assert report['clipped_samples']==0 and report['true_peak_dbfs']<=-5,(name,report)
    assert -29<report['lufs']<=-20.7 and tp<=-2,(name,report['lufs'],tp)
    assert report['voice_end_s']<5.7
    return data,master,report


def encode(frames,size,fps,audio,output):
    cmd=['ffmpeg','-v','error','-y','-f','rawvideo','-pixel_format','rgb24',
        '-video_size',f'{size[0]}x{size[1]}','-framerate',str(fps),'-i','pipe:0','-i',str(audio),
        '-map','0:v:0','-map','1:a:0','-c:v','libx264','-preset','medium','-crf','16',
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','160k' if fps==30 else '96k',
        '-movflags','+faststart','-t',str(DURATION),str(output)]
    process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for frame in frames:process.stdin.write(frame.tobytes())
        process.stdin.close()
        error=process.stderr.read();code=process.wait()
        if code:raise RuntimeError(error.decode(errors='replace'))
    except BaseException:
        process.kill();process.wait();raise


def export_gif(frames,path):
    swatches=Image.new('RGB',(2800,240*math.ceil(len(frames)/10)))
    for n,f in enumerate(frames):swatches.paste(f,((n%10)*280,(n//10)*240))
    candidates=[]
    for method in (Image.Quantize.MEDIANCUT,Image.Quantize.FASTOCTREE):
        palette=swatches.quantize(colors=256,method=method)
        quantized=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]
        errors=[float(np.abs(np.asarray(a).astype(float)-np.asarray(b.convert('RGB')).astype(float)).mean()) for a,b in zip(frames,quantized)]
        candidates.append((max(errors),method.name,quantized,errors))
    _,method,quantized,errors=min(candidates,key=lambda item:item[0])
    assert max(errors)<3.5,max(errors)
    quantized[0].save(path,save_all=True,append_images=quantized[1:],duration=50,loop=0,optimize=False,disposal=2)
    return dict(method=method,worst_mean_error=round(max(errors),5))


def export_one(name):
    audio,master,report=soundtrack(name)
    wav=ROOT/'assets/sounds'/(name+'.wav');wav.write_bytes(audio)
    sidecar=ROOT/'assets/videos'/(name+'_audio.wav');sidecar.write_bytes(audio)
    master_path=OUT/'masters'/(name+'.wav');master_path.write_bytes(master)
    frames=[render(name,n/20).resize((280,240),Image.Resampling.LANCZOS) for n in range(120)]
    video=ROOT/'assets/videos'/(name+'.mp4');encode(frames,(280,240),20,wav,video)
    share=OUT/'share'/(name+'.mp4')
    encode((render(name,n/30) for n in range(180)),(840,720),30,master_path,share)
    atlas=Image.new('RGB',(1120,1200))
    for n,t in enumerate(STARTS):atlas.paste(render(name,t).resize((280,240),Image.Resampling.LANCZOS),((n%4)*280,(n//4)*240))
    sheet=ROOT/'assets/face_animations'/(name+'.png');atlas.save(sheet)
    manifest=sheet.with_suffix('.json')
    manifest.write_text(json.dumps(dict(sheet=sheet.name,columns=4,rows=5,order=list(range(20)),durations_ms=TIMING,blend_ms=0))+'\n')
    gif=OUT/'gifs'/(name+'.gif');palette=export_gif(frames,gif)
    poster=OUT/(name+'-poster.png');frames[80].save(poster)
    record={**CLIPS[name],'id':name,'revision':1,'duration_s':6,'fps':20,'frames':120,
        'share_fps':30,'share_frames':180,'native_size':[280,240],'share_size':[840,720],
        'sprite_frames':20,'contains_robbi_face':False,'bookend':'illustrated-punchline','sound_default':True,
        'video':video.relative_to(ROOT).as_posix(),'audio':wav.relative_to(ROOT).as_posix(),
        'sidecar':sidecar.relative_to(ROOT).as_posix(),'sheet':sheet.relative_to(ROOT).as_posix(),
        'manifest':manifest.relative_to(ROOT).as_posix(),'master_audio':master_path.relative_to(OUT).as_posix(),
        'share':share.relative_to(OUT).as_posix(),'gif':gif.relative_to(OUT).as_posix(),'poster':poster.name,
        'video_sha256':sha(video),'audio_sha256':sha(wav),'sheet_sha256':sha(sheet),
        'audio_metrics':report,'gif_palette':palette,'voice_id':'',
        'cues':[dict(time_s=.35,label='generated foley begins'),dict(time_s=2.8,label='payoff accent'),dict(time_s=3.02,label='spoken punchline')]}
    (OUT/'records'/(name+'.json')).write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return name


def assemble():
    clips=[json.loads((OUT/'records'/(name+'.json')).read_text()) for name in CLIPS]
    CATALOG.write_text(json.dumps(dict(version=1,title='Professionally unbothered',groups=GROUPS,clips=clips,
        sound_provenance='ElevenLabs generated voice and effects, with original timed accents. Offline cached playback.'),indent=2)+'\n',encoding='utf-8')
    (OUT/'audio-checks.json').write_text(json.dumps([dict(id=c['id'],**c['audio_metrics']) for c in clips],indent=2)+'\n')
    overview=Image.new('RGB',(1400,540),'#1c2228');d=ImageDraw.Draw(overview)
    for n,clip in enumerate(clips):
        x,y=(n%5)*280,(n//5)*270
        with Image.open(OUT/clip['poster']) as im:overview.paste(im,(x,y))
        d.text((x+10,y+245),clip['title'],font=art.font(5),fill='#fff1d6')
    overview.save(OUT/'all-overview.png')
    print('Assembled ten animations.',flush=True)


def refresh_audio(name):
    """Remix cached inputs and replace AAC without another lossy video encode."""
    record_path=OUT/'records'/(name+'.json')
    record=json.loads(record_path.read_text())
    native,master,report=soundtrack(name)
    (ROOT/record['audio']).write_bytes(native)
    (ROOT/record['sidecar']).write_bytes(native)
    (OUT/record['master_audio']).write_bytes(master)
    for target,wav,bitrate in [(ROOT/record['video'],ROOT/record['audio'],'96k'),
                                (OUT/record['share'],OUT/record['master_audio'],'160k')]:
        temporary=target.with_name(target.stem+'-remux.mp4')
        run(['ffmpeg','-v','error','-y','-i',str(target),'-i',str(wav),'-map','0:v:0','-map','1:a:0',
             '-c:v','copy','-c:a','aac','-b:a',bitrate,'-movflags','+faststart','-t','6',str(temporary)])
        temporary.replace(target)
    record.update(audio_metrics=report,audio_sha256=sha(ROOT/record['audio']),video_sha256=sha(ROOT/record['video']))
    record_path.write_text(json.dumps(record,indent=2)+'\n')
    return name


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',nargs='+',choices=list(CLIPS))
    parser.add_argument('--workers',type=int,default=2,choices=(1,2,3))
    parser.add_argument('--assemble-only',action='store_true')
    parser.add_argument('--audio-only',action='store_true',help='Remix audio and remux existing video frames')
    args=parser.parse_args()
    for folder in (OUT,OUT/'masters',OUT/'share',OUT/'gifs',OUT/'records',CATALOG.parent):folder.mkdir(parents=True,exist_ok=True)
    if not args.assemble_only:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for job in as_completed([pool.submit(refresh_audio if args.audio_only else export_one,n) for n in (args.only or CLIPS)]):print('Exported '+job.result(),flush=True)
    if all((OUT/'records'/(n+'.json')).exists() for n in CLIPS):assemble()


if __name__=='__main__':main()
