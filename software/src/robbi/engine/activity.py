"""Attach output ownership to controller work and its asynchronous children."""
from functools import wraps
import threading

from robbi.actions import validate_action
from robbi.errors import BusyError, UnavailableError
from robbi.orchestration import APP, Intent, current_intent, intent_scope


def output_scene(callback):
    """Admit a complete native response before dispatch, including queued work."""
    @wraps(callback)
    def invoke(self,step,*args,**kwargs):
        validate_action(step)
        coordinator=getattr(self,'orchestrator',None)
        if coordinator is None or step.get('sequence'):
            return callback(self,step,*args,**kwargs)
        request_id=kwargs.get('request_id',args[0] if args else None)
        cancelled=self._action_token()[1]
        owner=current_intent()
        if cancelled.is_set() or (owner is not None and owner.is_set()) or not self._request_current(request_id):return
        preserves=getattr(self,'_app_preserves_display',lambda request_id=None:False)
        preserve=preserves(request_id) or preserves()
        resources=set()
        if not preserve and any(step.get(k) for k in ('face','speak','gesture','face_animation','animation','filming_sequence','video')):
            resources.add('display')
        if any(step.get(k) for k in ('speak','sound','gesture','animation','filming_sequence','video')):
            resources.add('audio')
        if step.get('drive') or step.get('slam') or (not preserve and any(step.get(k) for k in ('gesture','animation','filming_sequence'))):
            resources.add('motion')
        if not resources:return callback(self,step,*args,**kwargs)
        lease=coordinator.acquire(resources,cancel_event=cancelled)
        handed_off=False
        try:
            with intent_scope(lease.intent):pending=callback(self,step,*args,**kwargs)
            pending=[p for p in (pending or []) if callable(getattr(p,'add_done_callback',None))]
            if pending:
                # A queued TTS/effect worker has not necessarily acquired its
                # own output claims yet. Keep the scene until all dispatching
                # workers/handles finish; native children retain their claims.
                lock=threading.Lock();remaining=len(pending)
                def done(_):
                    nonlocal remaining
                    with lock:
                        remaining-=1;last=remaining==0
                    if last:lease.release()
                for future in pending:future.add_done_callback(done)
                handed_off=True
        finally:
            if not handed_off:lease.release()
    return invoke


def activity(owner, priority, *, replace=False):
    def decorate(callback):
        @wraps(callback)
        def invoke(self, *args, **kwargs):
            inherited = current_intent()
            token = self._action_token() if hasattr(self, "_action_token") else None
            intent = inherited if inherited is not None and not replace else Intent(
                owner, priority, cancelled=token[1] if token else None)
            try:
                with intent_scope(intent):
                    return callback(self, *args, **kwargs)
            except (BusyError, UnavailableError):
                if priority >= APP:
                    raise
                # Optional presentation is dropped, never replayed after an app.
                return None
        return invoke
    return decorate
