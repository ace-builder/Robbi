"""Independent feature construction, health and explicit safe recovery."""
from __future__ import annotations

import importlib
import logging
import threading
import time
from contextvars import copy_context
from robbi.orchestration import current_intent


class ComponentUnavailable(RuntimeError):
    pass


class UnavailableComponent:
    def __init__(self, name, error):
        self.name, self.error = name, str(error)

    def status(self, *args, **kwargs):
        return {'available': False, 'state': 'failed', 'error': self.error}

    health = status

    def __getattr__(self, method):
        if method in {'start', 'stop', 'shutdown', 'configure', 'reload_runtime_settings'}:
            return lambda *args, **kwargs: None
        def unavailable(*args, **kwargs):
            raise ComponentUnavailable(f'{self.name} unavailable: {self.error}')
        return unavailable


class LifecycleMixin:
    def _init_lifecycle(self):
        self._component_health = {}
        self._component_factories = {}
        self._lifecycle_lock = threading.RLock()
        self._retrying = set()
        self._lifecycle_stop = threading.Event()
        self._component_workers = {}
        self._component_start_times = {}
        self._next_recovery = {}
        self._action_lock = threading.RLock()
        self._action_generation = 0
        self._action_cancel = threading.Event()
        self._action_local = threading.local()
        self._request_generations = {}
        self._conversation_requests = {}
        self._conversation_heads = {}

    @staticmethod
    def _load(module, name):
        return getattr(importlib.import_module(f'robbi.engine.{module}'), name)

    def _component(self, name, factory):
        self._component_factories[name] = factory
        try:
            component = factory()
            self._record_health(name, 'ready', '')
            return component
        except Exception as exc:
            self._record_health(name, 'failed', str(exc))
            logging.getLogger(__name__).exception('Component %s could not initialize', name)
            return UnavailableComponent(name, exc)

    def _record_health(self, name, state, message, hint=''):
        with self._lifecycle_lock:
            old = self._component_health.get(name, {})
            self._component_health[name] = {
                'name': name, 'state': state, 'message': message,
                'hint': hint or ('Check configuration, connection and optional dependencies; retry this component.' if state == 'failed' else ''),
                'retries': old.get('retries', 0), 'updated_at': time.time(),
            }

    def _call_optional(self, name, method, *args, **kwargs):
        component = getattr(self, name, None)
        if isinstance(component, UnavailableComponent) or component is None:
            return None
        try:
            return getattr(component, method)(*args, **kwargs)
        except Exception as exc:
            self._record_health(name, 'failed', str(exc))
            logging.getLogger(__name__).exception('%s.%s failed', name, method)
            return None

    def health_snapshot(self):
        with self._lifecycle_lock:
            snapshots = [dict(item) for item in self._component_health.values()]
        tof_item, tof_status = None, None
        # Device methods only return cached health; never open a device here.
        for item in snapshots:
            name = item['name']
            if name == 'tof':
                tof_item = item
            component = getattr(self, name, None)
            worker = self._component_workers.get(name)
            if worker is not None and worker.is_alive():
                elapsed = time.monotonic() - self._component_start_times.get(name, time.monotonic())
                item.update(state='starting' if elapsed < 15 else 'failed', message='Initializing' if elapsed < 15 else 'Initialization exceeded 15 seconds',
                            hint='' if elapsed < 15 else 'A device driver is still blocked. Other features remain available. Restart the service if it does not return.')
                continue
            if isinstance(component, UnavailableComponent):
                continue
            method = 'health' if name in {'camera', 'microphone', 'media'} else 'status'
            if name not in {'camera', 'microphone', 'media', 'motion', 'tof', 'imu', 'telegram', 'llm', 'apps'}:
                continue
            if item['state'] == 'disabled':
                continue
            try:
                status = getattr(component, method)()
                if isinstance(status, dict):
                    if name == 'tof':
                        tof_status = status
                    if status.get('enabled') is False:
                        item.update(state='disabled', message='Disabled by configuration', hint='')
                    elif status.get('error') or status.get('last_error'):
                        item.update(state='degraded', message=str(status.get('error') or status.get('last_error')))
                    elif status.get('available') is False or status.get('ok') is False:
                        item.update(state='degraded', message='Component is unavailable')
                    elif name in {'tof', 'imu'} and status.get('fresh') is False:
                        item.update(state='degraded', message='Sensor readings are stale or not yet available')
            except Exception as exc:
                item.update(state='degraded', message=str(exc))
        if tof_item is not None:
            # Keep aggregate `tof` tied to H1 for existing motion gates. Each
            # channel also contributes independently to overall robot health.
            sensors = (tof_status or {}).get('sensors', {})
            for channel in ('h1', 'h2'):
                item = {**tof_item, 'name': f'tof.{channel}'}
                if tof_status is not None and tof_item['state'] != 'disabled':
                    data = sensors.get(channel, {})
                    if channel == 'h1' and not data:
                        data = tof_status
                    error = data.get('error') or data.get('last_error')
                    if data.get('enabled') is False:
                        item.update(state='disabled', message='Disabled by configuration', hint='')
                    elif error or not data.get('available') or not data.get('fresh'):
                        message = str(error or ('Sensor is unavailable' if not data.get('available')
                                                else 'Sensor readings are stale or not yet available'))
                        item.update(state='degraded', message=f'Distance sensor {channel.upper()}: {message}',
                                    hint=data.get('hint') or 'Check the sensor cable and XSHUT connection; each channel retries independently.')
                    else:
                        item.update(state='ready', message='', hint='')
                snapshots.append(item)
        media = getattr(self, 'media', None)
        if media is not None and not isinstance(media, UnavailableComponent):
            try:
                details = media.health()
                for name in ('display', 'audio'):
                    data = details.get(name, {})
                    disabled = data.get('enabled') is False
                    state = 'disabled' if disabled else ('ready' if data.get('available') and not data.get('last_error') else 'degraded')
                    snapshots.append({'name': name, 'state': state, 'message': '' if disabled else str(data.get('last_error') or ('' if data.get('available') else 'Device is unavailable')),
                                      'hint': data.get('hint', ''), 'retries': self._component_health.get('media', {}).get('retries', 0)})
            except Exception:
                pass
        return snapshots

    def _start_component(self, name):
        from robbi.configuration.settings import settings
        enabled = {'camera': getattr(settings, 'camera_enabled', True), 'microphone': settings.voice_enabled,
                   'motion': settings.enable_motors, 'tof': settings.tof_enabled, 'imu': settings.imu_enabled,
                   'telegram': settings.telegram_enabled, 'events': settings.events_enabled}.get(name, True)
        if not enabled:
            self._record_health(name, 'disabled', 'Disabled by configuration')
            return
        if isinstance(getattr(self, name, None), UnavailableComponent):
            return
        with self._lifecycle_lock:
            worker = self._component_workers.get(name)
            if worker is not None and worker.is_alive():
                return
            self._record_health(name, 'starting', 'Initializing')
            self._component_start_times[name] = time.monotonic()
            def initialize():
                try:
                    getattr(self, name).start()
                    self._record_health(name, 'ready', '')
                except Exception as exc:
                    self._record_health(name, 'failed', str(exc))
                    logging.getLogger(__name__).exception('Starting %s failed', name)
                finally:
                    if not self._running:
                        self._call_optional(name, 'shutdown' if name in {'motion', 'media', 'tof', 'imu', 'reminders'} else 'stop')
            thread = threading.Thread(target=initialize, daemon=True, name=f'initialize-{name}')
            self._component_workers[name] = thread
            thread.start()

    def _recovery_loop(self):
        # Reconnection is safe for these leaf devices; motion/ToF/SLAM require
        # a service restart so dependent callbacks cannot retain a stale owner.
        recoverable = {'camera', 'microphone', 'media'}
        while not self._lifecycle_stop.wait(5):
            for item in self.health_snapshot():
                name = item['name']
                if name not in recoverable or item['state'] not in {'failed', 'degraded'}:
                    continue
                now = time.monotonic()
                if now < self._next_recovery.get(name, 0):
                    continue
                attempts = min(6, item.get('retries', 0))
                self._next_recovery[name] = now + min(300, 10 * (2 ** attempts))
                self.retry_component(name)

    def retry_component(self, name):
        # Explicit retries initialize resources only. They never repeat actions.
        with self._lifecycle_lock:
            if name not in {'camera', 'microphone', 'media', 'telegram'} or name not in self._component_factories or name in self._retrying:
                return False
            worker = self._component_workers.get(name)
            if worker is not None and worker.is_alive():
                return False
            self._retrying.add(name)
            self._component_health[name]['retries'] += 1
        def retry():
            try:
                self._record_health(name, 'starting', 'Retrying initialization')
                old = getattr(self, name)
                method = 'shutdown' if name == 'media' else 'stop'
                try:
                    getattr(old, method)()
                except Exception as exc:
                    self._record_health(name, 'failed', f'Cannot safely stop old owner: {exc}')
                    return
                for attr in ('_thread', '_capture_thread', '_voice_thread', '_processing_thread'):
                    thread = getattr(old, attr, None)
                    if isinstance(thread, threading.Thread) and thread.is_alive():
                        self._record_health(name, 'failed', 'Previous device worker did not stop', 'Restart service to recover the blocked native driver safely.')
                        return
                if not self._running:
                    return
                new = self._component(name, self._component_factories[name])
                setattr(self, name, new)
                if name == 'microphone':
                    self._configure_microphone()
                if self._running:
                    self._start_component(name)
            finally:
                with self._lifecycle_lock:
                    self._retrying.discard(name)
        try:
            self._maintenance_executor.submit(retry)
            return True
        except Exception:
            with self._lifecycle_lock:
                self._retrying.discard(name)
            return False

    def cancel_actions_for_shutdown(self):
        return self.cancel_actions(pause_autonomy=False)

    def cancel_actions(self, *, pause_autonomy=True):
        """Cancel queued/late presentation without waiting for provider calls."""
        # Stop releases app-screen protection too, so the stopped/idle face can
        # replace an old board (and its listening badge) immediately.
        self._app_display_until = 0.
        live = getattr(self, 'live_awareness', None)
        if live is not None and not isinstance(live, UnavailableComponent):
            try:
                live.stop("Stopped with robot actions")
            except Exception:
                logging.getLogger(__name__).exception('Could not stop live awareness')
        orchestrator = getattr(self, 'orchestrator', None)
        if orchestrator is not None:
            orchestrator.cancel_all()
        with self._action_lock:
            self._action_cancel.set()
            self._action_generation += 1
            self._action_cancel = threading.Event()
        background = getattr(self, 'background_work', None)
        if background is not None and not isinstance(background, UnavailableComponent):
            try:
                background.cancel_all()
            except Exception:
                logging.getLogger(__name__).exception('Could not cancel background work')
        # Operator Stop persists a pause so the next heartbeat cannot drive
        # again. Normal shutdown clears _running before cancelling actions.
        if pause_autonomy and getattr(self, '_running', False):
            pause = getattr(self, 'autonomy_pause', None)
            if pause is not None:
                try:
                    pause('Paused by Stop; resume autonomy explicitly', persist_async=True)
                except Exception:
                    logging.getLogger(__name__).exception('Could not persist autonomy pause')
        # Invalidates pending STT without joining a microphone/native worker.
        # Missing/failed microphone must not prevent the other Stop actions.
        microphone = getattr(self, 'microphone', None)
        cancel_input = getattr(microphone, 'cancel_pending_input', None)
        if cancel_input is not None and not isinstance(microphone, UnavailableComponent):
            try:
                cancel_input()
            except Exception:
                logging.getLogger(__name__).exception('Could not cancel pending microphone input')
        return self._action_generation

    def _action_token(self):
        inherited = getattr(self._action_local, 'token', None)
        if inherited is not None:
            return inherited
        with self._action_lock:
            return self._action_generation, self._action_cancel

    def _submit_guarded(self, executor, callback, *args, **kwargs):
        generation, cancelled = self._action_token()
        context = copy_context()
        def invoke():
            owner = current_intent()
            if cancelled.is_set() or not self._action_current(generation) or (owner is not None and owner.is_set()):
                return None
            previous = getattr(self._action_local, 'token', None)
            self._action_local.token = (generation, cancelled)
            try:
                return callback(*args, **kwargs)
            finally:
                self._action_local.token = previous
        return executor.submit(context.run, invoke)

    def _action_current(self, generation):
        with self._action_lock:
            return generation == self._action_generation

    def _register_request(self, request_id, generation):
        recorder = getattr(self, "recording", None)
        if recorder is not None:
            recorder.bind_request(request_id)
        with self._action_lock:
            self._request_generations[request_id] = generation
            while len(self._request_generations) > 256:
                old = next(iter(self._request_generations))
                self._request_generations.pop(old)
                scope = self._conversation_requests.pop(old, None)
                if scope is not None and self._conversation_heads.get(scope) == old:
                    self._conversation_heads.pop(scope, None)

    def _register_conversation_request(self, request_id, source, conversation_id=''):
        with self._action_lock:
            scope = (source, conversation_id)
            self._conversation_requests[request_id] = scope
            self._conversation_heads[scope] = request_id

    def _request_current(self, request_id):
        with self._action_lock:
            if request_id is None:
                return True
            scope = self._conversation_requests.get(request_id)
            return (self._request_generations.get(request_id, -1) == self._action_generation
                    and (scope is None or self._conversation_heads.get(scope) == request_id))

    def _configure_microphone(self):
        def guarded(callback):
            def invoke(*args, context=None):
                if context is None:
                    return
                generation, cancelled = context
                if cancelled.is_set() or not self._action_current(generation):
                    return
                previous = getattr(self._action_local, 'token', None)
                self._action_local.token = context
                try:
                    return callback(*args)
                finally:
                    self._action_local.token = previous
            return invoke
        self._call_optional('microphone', 'configure',
            transcribe_callback=self._transcribe_microphone_audio,
            wake_callback=guarded(self._handle_wake_phrase_detected),
            command_callback=guarded(self._handle_voice_command_transcript),
            followup_check_callback=guarded(getattr(self, "_accept_spoken_followup", lambda *_: False)),
            listening_finished_callback=guarded(self._handle_voice_listening_finished),
            followup_state_callback=guarded(getattr(self, "_handle_followup_listening_state", lambda *_: None)),
            can_listen_callback=self._can_listen_for_voice,
            context_callback=self._action_token)
