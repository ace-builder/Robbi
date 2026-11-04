"""Optional event extension hooks; no persistence, capture or worker by default."""
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import logging
import re
import uuid
from importlib.metadata import entry_points

_TURN = ContextVar('robbi_activity_turn', default=None)


class OptionalSink:
    """Isolate extension failures from conversation and hardware control.

    Implementations must enqueue bounded work and return immediately. The core
    does not create capture queues, copy media or start an extension by default.
    """
    METHODS = {'emit', 'model_request', 'sample_imu', 'stop', 'status',
               'bind_request', 'turn_for_request'}

    def __init__(self, sink):
        self._sink = sink
        self._failed = False

    def __getattr__(self, name):
        value = getattr(self._sink, name)
        if name not in self.METHODS:
            return value
        def call(*args, **kwargs):
            fallback = {'available': False} if name == 'status' else None
            if self._failed and name != 'stop':
                return fallback
            try:
                return value(*args, **kwargs)
            except Exception as error:
                if not self._failed:
                    logging.getLogger(__name__).warning(
                        'Optional event extension failed (%s); events disabled',
                        type(error).__name__)
                self._failed = True
                return fallback
        return call

def create_sink(config):
    name = config.values.get('runtime', {}).get('event_sink', '')
    if not name:
        return None
    try:
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', name):
            raise ValueError('Invalid extension name')
        matches = list(entry_points(group='robbi.event_sinks', name=name))
        if len(matches) != 1:
            raise ValueError('Install exactly one matching extension package')
        sink = matches[0].load()(config)
        if not all(callable(getattr(sink, key, None)) for key in OptionalSink.METHODS):
            if callable(getattr(sink, 'stop', None)): sink.stop()
            raise TypeError('Event extension does not implement the contract')
        return OptionalSink(sink)
    except Exception as error:
        logging.getLogger(__name__).warning('Optional event extension unavailable (%s); continuing without it', type(error).__name__)
        return None

def current_turn_id():
    return _TURN.get()


@contextmanager
def turn_scope(turn_id=None):
    token = _TURN.set(turn_id)
    try:
        yield turn_id
    finally:
        _TURN.reset(token)


def bind_turn(callback):
    """Propagate only event context across the existing bounded workers."""
    turn_id = current_turn_id()
    @wraps(callback)
    def invoke(*args, **kwargs):
        with turn_scope(turn_id):
            return callback(*args, **kwargs)
    return invoke


def record_turn(callback):
    @wraps(callback)
    def invoke(self, text, *args, **kwargs):
        recorder = getattr(self, "recording", None)
        if recorder is None:
            return callback(self, text, *args, **kwargs)
        with turn_scope(current_turn_id() or uuid.uuid4().hex):
            recorder.emit("conversation.input", {"text": text, "source": kwargs.get("source", "ui_text")})
            try:
                result = callback(self, text, *args, **kwargs)
                recorder.emit("conversation.finished", {"action": result})
                return result
            except Exception as error:
                recorder.emit("conversation.failed", {"error": str(error)})
                raise
    return invoke


def record_speech_input(callback):
    @wraps(callback)
    def invoke(self, transcript, wav_bytes, *args, **kwargs):
        recorder = getattr(self, "recording", None)
        if recorder is None:
            return callback(self, transcript, wav_bytes, *args, **kwargs)
        with turn_scope(current_turn_id() or uuid.uuid4().hex):
            recorder.emit("speech.received", {"text": transcript or "", "source": "microphone"},
                          media=[(wav_bytes, "audio/wav")] if wav_bytes else [])
            return callback(self, transcript, wav_bytes, *args, **kwargs)
    return invoke


def record_context(callback):
    """Start a turn before work such as remote speech transcription."""
    @wraps(callback)
    def invoke(*args, **kwargs):
        with turn_scope(current_turn_id() or uuid.uuid4().hex):
            return callback(*args, **kwargs)
    return invoke


def record_tool(name):
    """Record the actual operation, including exceptions, without changing it."""
    def decorate(callback):
        @wraps(callback)
        def invoke(self, *args, **kwargs):
            recorder = getattr(self, "recording", None)
            if recorder is None:
                return callback(self, *args, **kwargs)
            turn = recorder.turn_for_request(kwargs.get("request_id")) or current_turn_id()
            call_id = uuid.uuid4().hex
            with turn_scope(turn):
                recorder.emit("tool.call", {"tool": name, "call_id": call_id,
                                             "args": args, "kwargs": kwargs})
                try:
                    result = callback(self, *args, **kwargs)
                    if isinstance(result, Future):
                        recorder.emit("tool.result", {"tool": name, "call_id": call_id, "status": "dispatched"})
                        def finished(future):
                            with turn_scope(turn):
                                try:
                                    value = future.result()
                                    recorder.emit("tool.result", {"tool": name, "call_id": call_id, "status": "completed", "result": value})
                                except Exception as error:
                                    recorder.emit("tool.result", {"tool": name, "call_id": call_id, "status": "failed", "ok": False, "error": str(error)})
                        result.add_done_callback(finished)
                    else:
                        recorder.emit("tool.result", {"tool": name, "call_id": call_id, "result": result})
                    return result
                except Exception as error:
                    recorder.emit("tool.result", {"tool": name, "call_id": call_id,
                                                  "ok": False, "error": str(error)})
                    raise
        return invoke
    return decorate


