"""At most two media workers per clip, sharing its cancellable output owner."""
import threading
import time

from .errors import BusyError, UnavailableError
from .orchestration import intent_scope


class CueSignal:
    def __init__(self, parent, local, duration=None):
        self.parent, self.local = parent, local
        self.deadline = time.monotonic()+duration if duration else float('inf')
    def is_set(self):
        return self.parent.is_set() or self.local.is_set() or time.monotonic() >= self.deadline
    def wait(self, seconds):
        end = time.monotonic()+max(0, seconds)
        while not self.is_set() and time.monotonic() < end:
            self.local.wait(min(.02, max(0, end-time.monotonic())))
        return self.is_set()


class StudioCuePlayer:
    def __init__(self, controller, store, root, intent, lease):
        self.controller, self.store, self.root = controller, store, root
        self.intent, self.lease = intent, lease
        self.threads, self.errors = [], []
        self.cancel = threading.Event()

    def alive(self):
        return any(t.is_alive() for t in self.threads)

    def check(self):
        if self.errors:
            raise UnavailableError(self.errors[0])

    def _worker(self, callback, signal):
        try:
            with intent_scope(self.intent):
                if not signal.is_set():
                    callback(signal)
        except Exception as error:
            if not signal.is_set():
                self.errors.append(str(error)[:300])

    def _start_worker(self, callback, signal, name):
        thread = threading.Thread(target=self._worker, args=(callback, signal), name=name, daemon=True)
        self.threads.append(thread)
        thread.start()

    def _wait_audio(self, handle, signal):
        if handle is None:
            raise UnavailableError('Audio output did not start')
        while not signal.is_set():
            result = handle.wait(timeout=.05)
            if result is not None:
                if result != 0:
                    raise UnavailableError('Audio playback failed')
                return

    def start(self, point):
        self.check()
        if self.stop():
            raise BusyError('The previous media cue is still stopping. Use Wait for media for speech cues.')
        self.cancel = threading.Event()
        self.threads, self.errors = [], []
        cue = point['cue']
        if self.lease.is_set():
            return
        if cue['screen'] == 'video':
            entry = self.store.resolve(cue['media'], 'video')
            signal = CueSignal(self.lease, self.cancel, cue['duration'] or 110)
            def video(signal):
                result = self.controller.media.play_video(entry['_path'], cancel_event=signal)
                if not signal.is_set() and not result.get('ok'):
                    raise UnavailableError(result.get('reason', 'Video playback failed'))
            self._start_worker(video, signal, 'studio-video')
        else:
            path = (self.store.resolve(cue['media'], 'image')['_path'] if cue['screen'] == 'image'
                    else self.root/'assets/images/faces'/(point['face']+'.png'))
            if not path.is_file():
                raise UnavailableError('The selected screen image is unavailable')
            if self.controller.media.show_face(path, cancel_event=self.lease) is False:
                raise UnavailableError('The screen could not show this cue')
        if cue['sound'] or cue['speech']:
            signal = CueSignal(self.lease, self.cancel)
            def audio(signal):
                if cue['sound']:
                    entry = self.store.resolve(cue['sound'], 'sound')
                    self._wait_audio(self.controller.media.play_sound(entry['_path'], wait=False, cancel_event=signal), signal)
                if cue['speech'] and not signal.is_set():
                    wav = self.controller.llm.synthesize(cue['speech'])
                    if signal.is_set():
                        return
                    if not wav:
                        raise UnavailableError('Speech synthesis returned no audio; check Robbi’s voice provider')
                    self._wait_audio(self.controller.media.speak_wav_bytes(wav, wait=False, cancel_event=signal), signal)
            self._start_worker(audio, signal, 'studio-audio')

    def finish(self):
        while self.alive() and not self.lease.is_set():
            self.check()
            self.lease.wait(.02)
        self.check()

    def stop(self):
        self.cancel.set()
        if self.threads:
            stop_audio = getattr(self.controller.media, 'stop_audio', None)
            if stop_audio:
                stop_audio(owner=self.intent)
        for thread in self.threads:
            thread.join(timeout=.25)
        return self.alive()
