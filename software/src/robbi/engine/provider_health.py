"""Per-provider/task circuit state. Recovery permits a new request, never replay."""
from __future__ import annotations
import functools
import time


def guarded_provider(task):
    def decorate(function):
        @functools.wraps(function)
        def call(self, *args, **kwargs):
            from robbi.configuration.settings import settings
            provider = kwargs.get('provider') or getattr(settings, {'chat':'chat_provider', 'stt':'stt_provider', 'tts':'tts_provider'}[task])
            key = f'{provider}:{task}'
            now = time.monotonic()
            with self._provider_health_lock:
                state = self._provider_states.setdefault(key, {'provider': provider, 'task': task, 'failures': 0, 'last_error': '', 'last_success': None, 'retry_at': 0.0})
                if now < state['retry_at']:
                    return {'speak': 'The language provider is temporarily unavailable. Please try again shortly.', 'error': 'provider_unavailable'} if task == 'chat' else (b'' if task == 'tts' else '')
            self._provider_context.error = ''
            try:
                result = function(self, *args, **kwargs)
                error = self._provider_context.error
                if task == 'tts' and not result and not error:
                    error = 'Speech provider returned no audio or is not configured'
            except Exception as exc:
                error = str(exc)
                result = {'speak': 'The language provider could not be reached.', 'error': 'provider_error'} if task == 'chat' else (b'' if task == 'tts' else '')
            if error and task == 'chat' and isinstance(result, dict):
                result = dict(result, error='provider_error')
            with self._provider_health_lock:
                if error:
                    state['failures'] += 1
                    delay = min(settings.network_max_cooldown_s, settings.network_cooldown_s * 2 ** min(6, state['failures'] - 1))
                    state.update(last_error=error[:500], retry_at=time.monotonic() + delay)
                else:
                    state.update(failures=0, last_error='', last_success=time.time(), retry_at=0.0)
            return result
        return call
    return decorate
