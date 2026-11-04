"""Opt-in session hosted by V2. Constructing/status polling starts no providers."""
import asyncio
from collections import deque
from dataclasses import asdict
import queue
import threading
import time
import uuid
from types import SimpleNamespace

from robbi.errors import BusyError, ValidationError
from .settings import ContinuousSettings


class ConversationLab:
    def __init__(self, runtime):
        self.runtime=runtime
        self.config=SimpleNamespace(root=runtime.config.data_dir/'conversation-lab')
        self.lock=threading.RLock()
        self.thread=None
        self.cancelled=threading.Event()
        self.inputs=queue.Queue(maxsize=4)
        self.events=deque(maxlen=180)
        self.sequence=0
        self.session_id=uuid.uuid4().hex[:12]
        self.state='idle'
        self.microphone=False
        self.hardware=self.executor=self.conversation=None
        self.usage={}
        from robbi.services.companion import DialogueHistory
        count=getattr(runtime.config,'values',{}).get('memory',{}).get('history_messages',100)
        self.history=DialogueHistory(self.config.root/'dialogue.json',limit=max(20,min(500,count)))
        for row in list(self.history.rows):
            self.sequence+=1
            self.events.append({'id':self.sequence,'kind':'you' if row['role']=='user' else 'robbi','text':row['content'],'time':row['time']})

    def settings(self):return asdict(ContinuousSettings.load(self.config.root))

    def configure(self, values):
        with self.lock:
            if self.thread and self.thread.is_alive():raise BusyError('Stop this conversation session before changing settings')
            try:options=ContinuousSettings.parse(values)
            except (ValueError,TypeError) as exc:raise ValidationError(str(exc)) from None
            self.config.root.mkdir(parents=True,exist_ok=True)
            options.save(self.config.root)
        return self.settings()

    def event(self,kind,text):
        with self.lock:
            self.sequence+=1
            self.events.append({'id':self.sequence,'kind':kind,'text':str(text)[:3000],'time':time.time()})
        if kind=='robbi':self.history.append('assistant',str(text))

    def mood(self,reset=False):
        try:
            state=self.runtime.controller().behavior.social_state
            return state.reset() if reset else state.snapshot()
        except (AttributeError,RuntimeError):return {'mood':'unavailable'}

    def connected(self):
        if not self.cancelled.is_set():self.state='live'

    def start(self, *, microphone=True):
        if type(microphone) is not bool:raise ValidationError('microphone must be boolean')
        with self.lock:
            if self.thread and self.thread.is_alive():raise BusyError('Conversation session already active')
            robot=self.runtime.controller()
            if getattr(robot,'_filming_mode',False):raise BusyError('Finish filming mode before starting this conversation')
            if getattr(robot.microphone,'live_audio_tap',None) is not None:
                raise BusyError('Stop live awareness before using the microphone in Conversation Lab')
            if getattr(robot,'_voice_processing',False) or robot.media.is_audio_playing():
                raise BusyError('Wait for the current voice reply to finish')
            if microphone:
                from robbi.configuration.settings import settings
                if not settings.voice_enabled or settings.mic_sample_rate!=16000:
                    raise ValidationError('Enable the V2 microphone at 16 kHz in Settings, or start with microphone off')
                if not robot.microphone.health().get('available'):
                    raise ValidationError('Microphone unavailable; start with microphone off for typed testing')
            # Dependencies are optional and checked only on explicit Start.
            try:
                import httpx
                if microphone:import webrtcvad
            except ImportError:
                raise ValidationError('Install the conversation extra in the existing V2 environment') from None
            ContinuousSettings.load(self.config.root)
            self.cancelled=threading.Event();self.inputs=queue.Queue(maxsize=4)
            self.hardware=self.executor=self.conversation=None
            self.session_id=uuid.uuid4().hex[:12]
            self.state='starting';self.microphone=microphone;self.usage={}
            robot._conversation_lab_active=True
            self.thread=threading.Thread(target=self._run,args=(robot,),daemon=True,name='conversation-lab')
            try:
                self.thread.start()
            except Exception:
                robot._conversation_lab_active=False
                self.state='error'
                raise
        return self.status()

    def _run(self,robot):
        old_wake=None
        attached=False
        try:
            from .native import Executor,SharedHardware
            from .provider import Continuous
            self.hardware=SharedHardware(robot,self.cancelled,self.event)
            self.executor=Executor(self.runtime,self.cancelled,self.event)
            # Scoped handoff, not persisted configuration. Existing capture keeps running.
            if callable(getattr(robot.microphone,'wake_enabled',None)):
                old_wake=robot.microphone.wake_enabled()
                robot.microphone.cancel_pending_input()
                robot.microphone.set_wake_enabled(False)
            if self.microphone:
                if robot.microphone.live_audio_tap is not None:raise BusyError('Microphone tap became occupied')
                robot.microphone.live_audio_tap=self.hardware.audio
                attached=True;self.hardware.audio.mic_available=True
            active=getattr(robot,'_autonomy_intent',None)
            if active:active.cancel('Owner started Conversation Lab')
            self.hardware.audio.worker.start()
            async def session():
                task=asyncio.create_task(Continuous(self).run())
                try:
                    while not task.done() and not self.cancelled.is_set():
                        if getattr(robot,'_filming_mode',False):self.cancelled.set()
                        await asyncio.sleep(.05)
                    if task.done():task.result()
                finally:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
            asyncio.run(session())
        except Exception as exc:
            if not self.cancelled.is_set():
                self.state='error'
                self.event('error',f'Conversation experiment failed ({type(exc).__name__}). Check PC speech endpoint, model name and OpenRouter key in Settings → Connections.')
        finally:
            self.cancelled.set()
            if attached and robot.microphone.live_audio_tap is self.hardware.audio:
                robot.microphone.live_audio_tap=None
            cleanup=[]
            if self.executor:cleanup.append(self.executor.cancel)
            if self.hardware:cleanup.append(self.hardware.audio.close)
            if old_wake is not None:
                cleanup.extend([robot.microphone.cancel_pending_input,
                    lambda:robot.microphone.set_wake_enabled(old_wake)])
            for operation in cleanup:
                try:operation()
                except Exception as exc:self.event('error',f'Conversation cleanup issue ({type(exc).__name__})')
            robot._conversation_lab_active=False
            if self.state!='error':self.state='idle'
            self.event('status','Experiment stopped; normal V2 wake conversation restored')

    def stop(self):
        self.cancelled.set()
        if self.thread and self.thread.is_alive():self.state='stopping'
        for component,method in ((self.executor,'cancel'),(self.hardware.audio if self.hardware else None,'interrupt')):
            if component:
                try:getattr(component,method)()
                except Exception as exc:self.event('error',f'Conversation stop issue ({type(exc).__name__})')
        return self.status()

    def shutdown(self):
        self.stop()
        if self.thread and self.thread.ident is not None:
            self.thread.join(timeout=3)

    def arm(self, enabled):
        if type(enabled) is not bool:raise ValidationError('enabled must be boolean')
        if self.state!='live' or not self.executor:raise BusyError('Start the experiment first')
        self.executor.arm(enabled)
        self.event('status','Motor control enabled' if enabled else 'Motors disarmed')
        return self.status()

    def text(self,text):
        if not isinstance(text,str) or not text.strip() or len(text)>1000:raise ValidationError('Enter 1–1000 characters')
        if self.state!='live':raise BusyError('Start the experiment first')
        try:self.inputs.put_nowait(text.strip())
        except queue.Full:raise BusyError('Conversation input queue is full') from None
        if self.hardware:self.hardware.audio.interrupt()
        self.event('you',text.strip())
        return {'accepted':True}

    def status(self, *, since=0, jobs_revision=None):
        with self.lock:
            reset=since>self.sequence
            if reset:since=0
            revision=self.session_id+':'+(self.conversation.revision() if self.conversation else '')
            result={'state':self.state,'provider':'continuous','microphone':self.microphone,
                'armed':bool(self.executor and self.executor.armed),
                'listening':bool(self.state=='live' and self.microphone and self.hardware and not self.hardware.speaking()),
                'events':[event for event in self.events if event['id']>since],
                'events_reset':reset,'event_cursor':self.sequence,'usage':dict(self.usage),
                'jobs_revision':revision,'mood':self.mood(),
                'history':{'retained':len(self.history.rows),'limit':self.history.limit,'error':self.history.error}}
            if jobs_revision!=revision:
                result['conversation_jobs']=self.conversation.snapshot() if self.conversation else []
            return result
