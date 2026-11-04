"""Explicit owner power controls; never exposed as an LLM or app tool."""
import subprocess
import sys
import threading
import time

from robbi.errors import BusyError, UnavailableError


class HostManagement:
    def __init__(self, runtime, *, run=subprocess.run, platform=lambda: sys.platform):
        self.runtime, self.run, self.platform = runtime, run, platform
        self._lock = threading.Lock()
        self._pending_until = 0

    def status(self):
        return {'enabled': bool(self.runtime.config.values.get('runtime', {}).get('allow_host_management')),
                'supported': self.platform() == 'linux'}

    def request(self, operation):
        if operation not in {'shutdown', 'restart'}:
            raise ValueError('Unknown host operation')
        if not self.status()['enabled']:
            raise UnavailableError('Enable power controls in Settings → Service & power first.')
        if not self.status()['supported']:
            raise UnavailableError('Power controls require the Raspberry Pi Linux installation.')
        if not self._lock.acquire(blocking=False):
            raise BusyError('A power request is already being submitted.')
        try:
            if time.monotonic() < self._pending_until:
                raise BusyError('A power request was already accepted. Wait for the Pi to shut down or reconnect.')
            self.runtime.emergency_stop(operator_stop=False)
            # Await systemd's acceptance, not the shutdown/restart itself. A
            # daemon thread used to hide permission failures behind HTTP 202.
            command = ['/usr/bin/systemctl', '--no-ask-password', '--no-block', '--no-wall']
            command += ['poweroff'] if operation == 'shutdown' else ['restart', 'robbi-v2.service']
            try:
                self.run(command, check=True, timeout=4, capture_output=True, text=True)
            except (OSError, subprocess.SubprocessError) as error:
                message = ('Linux did not confirm the power request. Check the Pi connection before retrying.'
                           if isinstance(error, subprocess.TimeoutExpired) else
                           'Linux rejected the power request. Check the Robbi power-control permissions and shutdown inhibitors in Diagnostics.')
                self.runtime.health.set('host-management', 'failed', message,
                                        'Install the Robbi power-control policy using the setup helper; see docs/INSTALL.md.')
                raise UnavailableError(message) from error
            self._pending_until = time.monotonic() + 30
            self.runtime.health.set('host-management', 'ready', 'Linux accepted the ' + operation + ' request.')
            return {'ok': True, 'requested': True, 'operation': operation,
                    'message': ('Linux accepted shutdown. Wait for the Pi to halt before disconnecting power.'
                                if operation == 'shutdown' else 'Linux accepted the service restart. The dashboard will reconnect.')}
        finally:
            self._lock.release()
