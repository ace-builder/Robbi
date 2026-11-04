"""Single batch worker; reuse Audio Studio and decode speech before a take."""
import os
import shutil
import subprocess
import threading
import time
import wave

from ..errors import BusyError, UnavailableError, ValidationError
from ..execution import BoundedExecutor


def decode(source, destination):
    try:
        result = subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),
            '-t','111','-ac','1','-ar','22050','-acodec','pcm_s16le','-f','wav',str(destination)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        if result.returncode: raise UnavailableError('Generated audio could not be decoded. Regenerate this line.')
    except FileNotFoundError:
        raise UnavailableError('Install ffmpeg on Robbi to prepare filming speech.') from None
    except subprocess.TimeoutExpired:
        raise UnavailableError('Audio preparation timed out; retry this line manually.') from None
    with wave.open(str(destination),'rb') as audio:
        duration=audio.getnframes()/audio.getframerate()
        if not 0 < duration <= 110: raise ValidationError('Each spoken line must be at most 110 seconds; split it into smaller actions.')
        return round(duration*1000)


class Preparation:
    def __init__(self, library, audio_studio, *, decoder=decode):
        self.library,self.audio,self.decoder=library,audio_studio,decoder
        self.lock=threading.RLock();self.cancel=threading.Event()
        self.executor=BoundedExecutor(1,0,'filming-prepare')
        self.state={'active':False,'completed':0,'total':0,'errors':[],'current':''}
        self.active_clip=None

    def status(self):
        with self.lock: return {**self.state,'errors':list(self.state['errors'])}

    def start(self, identifiers, cue_id=None, force=False):
        with self.lock:
            if self.state['active']: raise BusyError('Audio preparation is already running')
            items=[]
            for identifier in identifiers:
                detail=self.library.get(identifier);selected=self.library.selected_audio(detail)
                for cue in detail['definition']['actions']:
                    if cue['action']=='speak' and (cue_id is None or cue['id']==cue_id) and (force or cue['id'] not in selected):
                        items.append((identifier,detail['definition'],cue))
            if cue_id is not None and not items: raise ValidationError('Speech action not found')
            if not items: return self.status()
            if not self.audio.settings()['api_key_configured']:
                raise ValidationError('Configure ElevenLabs in Audio studio before generating speech.')
            self.cancel=threading.Event()
            self.state={'active':True,'completed':0,'total':len(items),'errors':[],'current':''}
            try:self.executor.submit(self._run,items,self.cancel)
            except Exception:self.state['active']=False;raise
            return self.status()

    def _run(self, items, cancelled):
        try:
            for identifier,definition,cue in items:
                if cancelled.is_set(): break
                variant=None;clip=None
                with self.lock:self.state['current']=definition['name']+' · '+cue['id']
                try:
                    used=sum(p.stat().st_size for p in self.library.directory.glob('*.wav'))
                    if used+5*1024*1024>256*1024*1024 or shutil.disk_usage(self.library.directory).free<24*1024*1024:
                        raise ValidationError('Prepared audio storage is full. Delete old versions or shots.')
                    variant=self.library.begin_audio(identifier,cue,definition)
                    # Wait for the shared studio without submitting a second paid call.
                    deadline=time.monotonic()+240
                    while not cancelled.is_set():
                        try:
                            clip=self.audio.generate(cue['text'],cue.get('voice_id',definition['voice_id']))
                            break
                        except BusyError:
                            if time.monotonic()>deadline:raise BusyError('Audio studio stayed busy; retry preparation later')
                            cancelled.wait(.25)
                    if cancelled.is_set():raise UnavailableError('Preparation cancelled')
                    with self.lock:self.active_clip=clip['id']
                    while clip['status'] in {'queued','generating'} and not cancelled.wait(.15):
                        clip=self.audio.get(clip['id'])
                        if not clip:raise UnavailableError('Audio generation was deleted')
                        if time.monotonic()>deadline:raise UnavailableError('Audio generation timed out; check the provider before retrying')
                    if cancelled.is_set():raise UnavailableError('Preparation cancelled')
                    if clip['status'] not in {'draft','kept'}:raise UnavailableError(clip['error'] or 'Audio generation did not finish')
                    opened=self.audio.audio(clip['id'])
                    if not opened:raise UnavailableError('Generated audio is missing')
                    source=self.library.path(variant,'.mp3.part');target=self.library.path(variant,'.wav.part')
                    with opened[0] as incoming,source.open('wb') as output:shutil.copyfileobj(incoming,output,64*1024)
                    duration=self.decoder(source,target)
                    if cancelled.is_set():raise UnavailableError('Preparation cancelled')
                    os.replace(target,self.library.path(variant))
                    self.library.finish_audio(variant,clip_id=clip['id'],duration_ms=duration)
                except Exception as error:
                    if variant:self.library.finish_audio(variant,clip_id=clip['id'] if clip else None,error=str(error))
                    with self.lock:self.state['errors'].append({'shot':definition['name'],'cue':cue['id'],'error':str(error)[:500]})
                finally:
                    if variant:
                        for suffix in ('.mp3.part','.wav.part'):self.library.path(variant,suffix).unlink(missing_ok=True)
                    with self.lock:self.active_clip=None;self.state['completed']+=1
        finally:
            with self.lock:self.state['active']=False;self.state['current']=''

    def stop_batch(self):
        with self.lock:
            self.cancel.set()
            if self.active_clip:self.audio.discard(self.active_clip)

    def shutdown(self):
        self.stop_batch();self.executor.shutdown(wait=False,cancel_futures=True)
