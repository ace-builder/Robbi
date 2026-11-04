"""Narrow hardware port: prepared files, measured motion, cancellable receipts."""
import threading
import time
from concurrent.futures import wait

from ..errors import UnavailableError, ValidationError
from ..execution import BoundedExecutor
from ..orchestration import Intent, intent_scope
from .timed_motion import TIMED_MODES


class Timer:
    def __init__(self, milliseconds, cancelled):
        self.end=time.monotonic()+milliseconds/1000;self.cancelled=cancelled
    def done(self):return self.cancelled.is_set() or time.monotonic()>=self.end
    def result(self):return {'ok':not self.cancelled.is_set(),'reason':'Cancelled'}


def filming_motion_receipt(result):
    """A finished motion failure ends that cue, not the filming timeline.

    The motor worker has already stopped/released ownership before its receipt.
    Keep the original failure visible; ok here means the cue was handled.
    """
    if (isinstance(result,dict) and result.get('ok') is False
            and result.get('reason') == 'turn_accuracy_not_reached'
            and result.get('control') == 'settled_turn'):
        result = {**result, 'ok':True, 'accuracy_reached':False,
                'warning':'Turn finished outside its accuracy tolerance; continuing the take.'}
    elif (isinstance(result,dict) and result.get('ok') is False
            and str(result.get('reason','')).lower() not in {'stopped','cancelled','canceled'}):
        result = {**result, 'ok':True, 'motion_completed':False, 'motion_result':dict(result),
                  'warning':'Movement ended early; continuing the take.'}
    if isinstance(result,dict) and result.get('ok') and result.get('motion_completed') is not False:
        headings = [result.get('heading',{})] + [s.get('heading',{}) for s in result.get('segments',[])]
        if any(h.get('source')=='encoders' and h.get('reason','').startswith('imu_') for h in headings):
            warning = 'IMU feedback unavailable or inconsistent; completed with encoder feedback.'
            result = {**result, 'warning':' '.join(filter(None,(result.get('warning'),warning)))}
    return result


class HardwarePort:
    def __init__(self, controller, library, detail):
        self.controller,self.library=controller,library
        self.durations={};self.paths={};self.face_patterns={};self.selected=library.selected_audio(detail)
        self.intent=Intent('filming-shot',90,cancelled=controller._action_token()[1],deadline=time.monotonic()+670)
        self.local=threading.Event();self.pool=BoundedExecutor(3,0,'filming-cue')
        self.record_video=detail['definition'].get('record_video',False)
        self.shot_id=detail['id'];self.video=None;self.video_id=None;self.video_started=None
        self.futures=[]
        try:
            if self.record_video:
                library.videos.preflight()
                try: controller.camera.video_ready()
                except Exception as error: raise UnavailableError(str(error)) from None
            for cue in detail['definition']['actions']:
                kind=cue['action'];identifier=cue['id']
                if kind=='speak':
                    entry=self.selected.get(identifier)
                    if not entry:raise ValidationError(f'{identifier}: generate speech for the current text/voice before playing')
                    self.paths[identifier]=library.path(entry['id']);self.durations[identifier]=entry['duration_ms']
                    if cue.get('face_pattern'):
                        paths=[]
                        for name in cue['face_pattern']:
                            path=controller.assets.get('faces',name,extensions=('.png',))
                            if not path or not path.is_file():raise ValidationError(f'{identifier}: registered face {name} is missing')
                            paths.append(path)
                        self.face_patterns[identifier]=paths
                elif kind=='pause':self.durations[identifier]=cue['duration_ms']
                elif kind=='wait_for_wake':controller.microphone.check_wake_wait_ready()
                elif kind in {'face','sound'}:
                    category='faces' if kind=='face' else 'sounds'
                    path=controller.assets.get(category,cue['name'],extensions=('.png',) if kind=='face' else ('.wav',))
                    if not path or not path.is_file():raise ValidationError(f'{identifier}: registered {kind} {cue["name"]} is missing')
                    self.paths[identifier]=path
                    self.durations[identifier]=0 if kind=='face' else round((controller.media.duration_s(path) or 0)*1000)
                    if kind=='sound' and not 0<self.durations[identifier]<=110000:raise ValidationError('Use a WAV sound up to 110 seconds')
                elif kind=='face_animation':
                    self.durations[identifier]=round(controller.dynamics.faces.clip(cue['name']).duration(10)*1000)
                elif kind=='gesture':
                    from ..dynamics import GESTURES
                    if cue['name'] not in GESTURES:raise ValidationError('Unknown registered gesture')
                    controller.dynamics.faces.clip(GESTURES[cue['name']]['face'])
                elif kind=='move' and cue['move']['mode']!='stop':
                    timed=cue['move']['mode'] in TIMED_MODES
                    if timed:
                        self.durations[identifier]=cue['move']['duration_ms']
                    else:
                        # Reject impossible curve geometry before the countdown.
                        controller.motion._curve_for_spec(cue['move'])
                if kind in {'speak','sound'}:
                    if controller.behavior.snapshot().muted:raise UnavailableError('Robbi is muted. Unmute it before playing a spoken shot.')
                if identifier in self.durations and self.durations[identifier]+1000>cue['timeout_ms'] and kind!='face':
                    raise ValidationError(f'{identifier}: timeout_ms must allow the prepared duration plus 1000 ms')
        except Exception:
            self.pool.shutdown(wait=False,cancel_futures=True)
            raise

    def cancelled(self):return self.intent.is_set() or self.local.is_set()

    def _finish_motion(self):
        """On dispatch/driver exceptions, confirm output is stopped before advancing."""
        motion=self.controller.motion
        motion.stop(owner=self.intent)
        running=getattr(motion,'is_running',lambda:False)
        deadline=time.monotonic()+3
        while running():
            if time.monotonic()>=deadline:
                raise UnavailableError('Movement cleanup could not be confirmed')
            time.sleep(.01)

    def _movement(self,cue,started,cancel):
        announced=False
        def announce():
            nonlocal announced
            if not announced:
                announced=True;started()
        try:
            controller=self.controller
            if cue['action']=='gesture':
                announce()
                result=controller.dynamics.play(cue['name'],cancel_event=self.intent)
            else:
                timed=cue['move']['mode'] in TIMED_MODES
                if not timed and cue['move']['mode']!='stop' and not controller.motion.status().get('encoder_enabled'):
                    raise UnavailableError('Encoders unavailable for measured movement')
                if timed:
                    future=controller.motion.filming_timed_drive(cue['move'],cancel_event=self.intent,started=announce)
                else:
                    announce()
                    future=controller.motion.drive({**cue['move'],'require_encoders':True},
                        cancel_event=self.intent,allow_encoder_fallback=True)
                if future is None:raise UnavailableError('Motion could not start')
                while not future.done():
                    if self.cancelled() or cancel.wait(.01):return {'ok':False,'reason':'Cancelled'}
                result=future.result() or controller.motion.status().get('last_result',{'ok':True})
            # A timed cue may be rejected before its first PWM write. Give it
            # an actual start/end so start-linked speech is not left pending.
            announce()
            if self.cancelled() or cancel.is_set():return {'ok':False,'reason':'Cancelled'}
            return filming_motion_receipt(result)
        except Exception as error:
            self._finish_motion()
            announce()
            if self.cancelled() or cancel.is_set():return {'ok':False,'reason':'Cancelled'}
            return filming_motion_receipt({'ok':False,'reason':'motion_unavailable','error':str(error)[:300]})

    def begin_take(self, run_id):
        if not self.record_video:return
        self.video_id,path=self.library.videos.begin(self.shot_id,run_id)
        self.video_started=time.monotonic()
        self.video=self.controller.camera.start_video(path)
        self.video_started=time.monotonic()  # First-frame readiness, excluding encoder setup.

    def check_take(self):
        if self.video:self.video.check()

    def finish_take(self, outcome):
        if not self.video_id:return None
        error=''
        try:
            if self.video:self.controller.camera.stop_video(self.video)
            else:error='Camera recording could not start'
        except Exception as exc:error=str(exc)
        result=self.library.videos.finish(self.video_id,
            duration_ms=round((time.monotonic()-self.video_started)*1000),outcome=outcome,error=error,
            metadata=self.video.metadata() if self.video else None)
        if error:raise UnavailableError(error)
        return result

    def start(self, cue, started, cancel):
        if cue['action']=='pause':
            started();return Timer(cue['duration_ms'],cancel)
        def run():
            with intent_scope(self.intent):
                if self.cancelled() or cancel.is_set():return {'ok':False,'reason':'Cancelled'}
                kind=cue['action'];controller=self.controller
                if kind=='wait_for_wake':
                    with controller.microphone.wake_only(self.intent) as detected:
                        started()
                        while not self.cancelled() and not cancel.is_set():
                            if detected.wait(.02):
                                return {'ok':not self.cancelled() and not cancel.is_set(), 'reason':'wake_detected'}
                        return {'ok':False,'reason':'Cancelled'}
                if kind in {'speak','sound'}:
                    handle=controller.media.play_sound(self.paths[cue['id']],wait=False,cancel_event=self.intent)
                    if handle is None:raise UnavailableError('Audio playback could not start')
                    announced=False;next_face=0;face_index=0
                    pattern=self.face_patterns.get(cue['id'])
                    while not self.cancelled() and not cancel.is_set():
                        if not announced and getattr(handle,'started_at',None) is not None:
                            started();announced=True
                        result=handle.wait(timeout=.01)
                        if result is not None:
                            if not announced:started()
                            if pattern and result==0 and not self.cancelled() and not cancel.is_set():
                                if not controller.media.show_face(pattern[0],cancel_event=self.intent):
                                    raise UnavailableError('Speaking face could not be restored')
                            return {'ok':result==0,'reason':'Audio playback failed or was stopped' if result else 'completed'}
                        if pattern and announced and time.monotonic()>=next_face:
                            if not controller.media.show_face(pattern[face_index%len(pattern)],cancel_event=self.intent):
                                raise UnavailableError('Speaking face animation could not be displayed')
                            face_index+=1;next_face=time.monotonic()+.32
                    return {'ok':False,'reason':'Cancelled'}
                if kind in {'move','gesture'}:return self._movement(cue,started,cancel)
                started()
                if kind=='face':return {'ok':controller.media.show_face(self.paths[cue['id']],cancel_event=self.intent)}
                if kind=='face_animation':return controller.dynamics.faces.play(cue['name'],cancel_event=self.intent,hold_s=0)
        future=self.pool.submit(run);self.futures.append(future);return future

    def stop(self):
        self.local.set();self.intent.cancel('Filming take ended')
        try:self.controller.media.stop_audio(owner=self.intent)
        finally:
            try:
                claim=self.controller.orchestrator.snapshot()['resources'].get('motion',{})
                if claim.get('intent_id')==self.intent.id:self.controller.motion.stop()
            finally:
                self.pool.shutdown(wait=False,cancel_futures=True)
                if self.futures and wait(self.futures,timeout=3).not_done:
                    raise UnavailableError('A shot output is still stopping; use Stop robot before trying again')
