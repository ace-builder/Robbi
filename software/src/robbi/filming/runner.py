"""One monotonic timeline, bounded output concurrency, no provider work."""
import threading
import time
import uuid

from ..errors import BusyError, ValidationError
from .schema import MAX_SHOT_MS, number, output_channels, planned_times


class Runner:
    def __init__(self, *, clock=time.monotonic):
        self.clock=clock;self.lock=threading.RLock();self.cancel=threading.Event();self.thread=None
        self.state={'state':'idle','actions':[]}

    def status(self):
        with self.lock:
            result={**self.state,'actions':[dict(a) for a in self.state['actions']]}
            if 'origin' in result:
                origin=result.pop('origin')
                now=result.pop('finished_at',None) or self.clock()
                result['remaining_ms']=max(0,round((origin-now)*1000))
                result['elapsed_ms']=max(0,round((now-origin)*1000))
            return result

    def active(self):
        with self.lock:return self.thread is not None and self.thread.is_alive()

    def start(self, detail, port, delay_ms):
        number(delay_ms,'Start delay',0,60000,True)
        definition=detail['definition'];durations=port.durations
        planned_times(definition,durations)  # Validate timing before starting the thread.
        with self.lock:
            if self.active():raise BusyError('A shot is already running')
            self.cancel=threading.Event();origin=self.clock()+delay_ms/1000
            self.state={'state':'countdown' if delay_ms else 'running','shot_id':detail['id'],
                'shot_name':definition['name'],'run_id':uuid.uuid4().hex,'origin':origin,'error':'',
                'actions':[{'id':c['id'],'action':c['action'],'state':'pending','started_ms':None,'ended_ms':None,'late_ms':None} for c in definition['actions']]}
            self.thread=threading.Thread(target=self._run,args=(definition,port,self.cancel,origin),daemon=True,name='filming-shot')
            self.thread.start()
            return self.status()

    def _run(self, definition, port, cancelled, origin):
        running={};records={a['id']:a for a in self.state['actions']};cues=definition['actions']
        began=False
        try:
            while not cancelled.is_set() and not port.cancelled():
                now=self.clock()
                if now-origin>MAX_SHOT_MS/1000:raise ValidationError('Shot exceeded its 10-minute deadline')
                if now<origin:cancelled.wait(min(.02,origin-now));continue
                if not began:
                    with self.lock:self.state['state']='starting'
                    if hasattr(port,'begin_take'):port.begin_take(self.state['run_id'])
                    # Camera readiness precedes cue zero; no motion/speech can
                    # outrun the first encoded frame. Countdown never records.
                    origin=self.clock();now=origin;began=True
                    with self.lock:self.state['origin']=origin
                    if cancelled.is_set() or port.cancelled():continue
                if hasattr(port,'check_take'):port.check_take()
                with self.lock:self.state['state']='running'
                # Complete before admitting new work, so end-linked cues see
                # actual playback/process cleanup, not an estimated timer.
                for identifier,(handle,channels,deadline) in list(running.items()):
                    if handle.done():
                        receipt=handle.result()
                        if receipt is False or (isinstance(receipt,dict) and receipt.get('ok') is False):
                            raise ValidationError(f'{identifier}: '+str(receipt.get('reason',receipt.get('error','Action failed')) if isinstance(receipt,dict) else 'Action failed'))
                        with self.lock:
                            records[identifier].update(state='done',ended_ms=(self.clock()-origin)*1000)
                            if isinstance(receipt,dict) and receipt.get('warning'):
                                records[identifier].update(warning=receipt['warning'],result=receipt)
                        del running[identifier]
                    elif now>deadline:raise ValidationError(f'{identifier}: action timed out')
                for cue in cues:
                    record=records[cue['id']]
                    if record['state']!='pending':continue
                    timing=cue['start'];due=timing.get('at_ms')
                    if due is None:
                        ref=records[timing['after']];offset=timing.get('offset_ms',0)
                        if timing['event']=='start':anchor=ref['started_ms']
                        elif offset<0:
                            anchor=None if ref['started_ms'] is None else ref['started_ms']+port.durations[timing['after']]
                        else:anchor=ref['ended_ms']
                        if anchor is None:continue
                        due=anchor+offset
                    if (now-origin)*1000+.1<due:continue
                    channels=output_channels(cue)
                    if any(channels & entry[1] for entry in running.values()):
                        # Real output duration can exceed a planned estimate.
                        # Keep this cue pending until cleanup releases the output;
                        # independent channels continue on their own schedule.
                        with self.lock:
                            record['warning']='Waited for the previous output to finish.'
                        continue
                    with self.lock:record.update(state='starting',late_ms=max(0,round((now-origin)*1000-due)))
                    def started(record=record):
                        with self.lock:record.update(state='running',started_ms=(self.clock()-origin)*1000)
                    handle=port.start(cue,started,cancelled)
                    running[cue['id']]=(handle,channels,now+cue['timeout_ms']/1000)
                if all(a['state']=='done' for a in records.values()):
                    with self.lock:self.state['state']='completed'
                    return
                cancelled.wait(.01)
            with self.lock:self.state['state']='cancelled'
        except Exception as error:
            with self.lock:self.state.update(state='failed',error=str(error)[:600])
        finally:
            cancelled.set()
            try:port.stop()
            except Exception as error:
                with self.lock:self.state.update(state='failed',error='Shot cleanup could not be confirmed: '+str(error)[:400])
            outcome=self.state['state']
            finished=self.clock()
            if hasattr(port,'finish_take'):
                with self.lock:self.state['state']='saving'
                try:
                    video=port.finish_take(outcome)
                    with self.lock:self.state.update(state=outcome,video=video)
                except Exception as error:
                    with self.lock:self.state.update(state='failed',error='Video could not be saved: '+str(error)[:400])
            with self.lock:
                for record in records.values():
                    if record['state'] not in {'done'}:record['state']='cancelled'
                self.state['finished_at']=finished

    def stop(self):
        self.cancel.set()
