"""Composition root for the library, preparation, exclusive mode and runner."""
import json
from pathlib import Path
import threading
from ..errors import BusyError, UnavailableError, ValidationError
from ..storage import write_json
from . import schema
from .hardware import HardwarePort
from .library import Library
from .prepare import Preparation
from .runner import Runner


class FilmingStudio:
    def __init__(self, runtime, *, port_factory=HardwarePort, decoder=None):
        self.runtime=runtime;self.library=Library(runtime.config.data_dir)
        self.preparation=Preparation(self.library,runtime.audio_studio,**({'decoder':decoder} if decoder else {}))
        self.runner=Runner();self.port_factory=port_factory;self.lock=threading.RLock()
        self.controller=None;self.enabled=False;self.mode_error=''
        self.mode_path=Path(runtime.config.data_dir)/'filming-mode.json'
        try:
            if self.mode_path.exists():
                data=json.loads(self.mode_path.read_text())
                if type(data.get('enabled')) is not bool:raise ValueError()
                self.enabled=data['enabled']
        except (OSError,ValueError,TypeError):
            self.enabled=True;self.mode_error='Filming mode file is unreadable. Outputs remain reserved; set the mode again to repair it.'

    def bind(self, controller):
        self.controller=controller;controller._filming_service=self
        controller._filming_mode=self.enabled
        if self.enabled:controller.orchestrator.set_exclusive_owner('filming-shot')

    def status(self):
        return {'enabled':self.enabled,'mode_error':self.mode_error,'generation':self.preparation.status(),'run':self.runner.status()}

    def _editable(self):
        if self.runner.active():raise BusyError('Stop the shot before editing its library or audio')
        if self.preparation.status()['active']:raise BusyError('Finish or cancel audio preparation before editing')

    def set_mode(self, enabled):
        if type(enabled) is not bool:raise ValidationError('enabled must be true or false')
        with self.lock:
            controller=self.runtime.controller();self.runner.stop()
            if self.runner.thread:self.runner.thread.join(timeout=1)
            if self.runner.active():raise BusyError('Shot cleanup is still running. Try switching mode again shortly.')
            if enabled:controller.orchestrator.set_exclusive_owner('filming-shot')
            controller._filming_mode=True
            failures=[]
            for name,callback in [('actions',lambda:controller.cancel_actions(pause_autonomy=False)),
                                  ('motion',controller.motion.stop),('mapping',controller.slam.stop),
                                  ('audio',controller.media.stop_audio),('apps',self.runtime.app_host.cancel_all)]:
                try:callback()
                except Exception as error:failures.append(name+': '+str(error))
            if failures:
                self.enabled=True;controller.orchestrator.set_exclusive_owner('filming-shot')
                write_json(self.mode_path,{'enabled':True})
                raise UnavailableError('Filming outputs remain reserved; some activity could not be stopped: '+'; '.join(failures))
            try:write_json(self.mode_path,{'enabled':enabled})
            except Exception:
                self.enabled=True;controller.orchestrator.set_exclusive_owner('filming-shot');raise
            self.enabled=enabled;controller._filming_mode=enabled;self.mode_error=''
            if not enabled:controller.orchestrator.set_exclusive_owner(None)
            return self.status()

    def import_jsonl(self, source, replace=False, generate=True):
        if type(replace) is not bool or type(generate) is not bool:raise ValidationError('replace and generate must be booleans')
        with self.lock:
            self._editable();definitions=schema.jsonl(source,self.runtime.audio_studio.settings()['voice_id'])
            ids=self.library.import_shots(definitions,replace);result={'ids':ids,'imported':len(ids)}
            if generate:
                try:result['generation']=self.preparation.start(ids)
                except (ValidationError,UnavailableError,BusyError) as error:result['generation_error']=str(error)
            return result

    def update(self, identifier, data):
        with self.lock:
            self._editable()
            if not isinstance(data,dict) or type(data.get('revision')) is not int:raise ValidationError('Supply definition and revision')
            definition=schema.shot(data.get('definition'),self.runtime.audio_studio.settings()['voice_id'])
            detail=self.library.update(identifier,definition,data['revision'])
            try:self.preparation.start([identifier])
            except (ValidationError,UnavailableError,BusyError) as error:detail['generation_error']=str(error)
            return detail

    def delete(self, identifier):
        with self.lock:self._editable();self.library.delete(identifier)

    def audio_choice(self, identifier, variant, delete=False):
        with self.lock:
            self._editable()
            return self.library.delete_audio(identifier,variant) if delete else self.library.select(identifier,variant)

    def delete_video(self, identifier, video):
        with self.lock:
            self._editable();self.library.videos.delete(identifier,video)
            return self.library.get(identifier)

    def prepare(self, identifiers, cue_id=None, force=False):
        if type(force) is not bool:raise ValidationError('regenerate must be true or false')
        with self.lock:self._editable();return self.preparation.start(identifiers,cue_id,force)

    def play(self, identifier, delay_ms=5000):
        with self.lock:
            if not self.enabled:raise ValidationError('Enable filming mode before playing a shot')
            self._editable();detail=self.library.get(identifier)
            port=self.port_factory(self.runtime.controller(),self.library,detail)
            try:return self.runner.start(detail,port,delay_ms)
            except Exception:port.stop();raise

    def stop(self):self.runner.stop()
    def shutdown(self):
        self.stop();self.preparation.shutdown()
        if self.runner.thread:self.runner.thread.join(timeout=10)
