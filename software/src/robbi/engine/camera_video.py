"""Optional hardware H.264 recording; reuses the live Picamera2 stream."""
from pathlib import Path
import math
import shutil
import threading
import time


MAX_BYTES = 480 * 1024 * 1024
MIN_FREE = 32 * 1024 * 1024


def wait_for_framerate(camera, fps, timeout=8):
    """Drain idle-rate frames before encoding; control submission is asynchronous."""
    deadline = time.monotonic() + timeout
    previous = None
    consecutive = 0
    target_ns = 1_000_000_000 / fps
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f'Camera did not settle at {fps:g} fps; recording was not started')
        # Numeric wait is Picamera2's bounded request timeout. Do not queue
        # another request if it fails or times out.
        sample = camera.capture_metadata(wait=min(2., remaining))
        stamp = sample.get('SensorTimestamp')
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise RuntimeError('Camera frame timing is unavailable; recording was not started')
        if previous is not None and abs((stamp-previous)/target_ns-1) <= .05:
            consecutive += 1
        else:
            consecutive = 0
        previous = stamp
        if consecutive >= 3:
            return


def output_file(path):
    # Lazy imports keep the normal robot usable without recording dependencies.
    import av
    from picamera2.outputs import PyavOutput
    from picamera2.outputs.output import Output

    class BoundedOutput(PyavOutput):
        def __init__(self):
            super().__init__(str(path), format='mp4')
            self.first = threading.Event()
            self.error = ''
            self.bytes = 0
            self.began = time.monotonic()
            self.last = 0.
            self.checked = 0.
            self.frames = 0
            self.first_timestamp = self.last_timestamp = None
            self.error_callback = self.failed

        def failed(self, error):
            self.error = 'Camera video could not be written: ' + str(error)[:250]

        def start(self):
            # Fragments retain already-written frames if power is lost. No
            # separate ffmpeg process, copying frames or software video encode.
            self._container = av.open(str(path), 'w', format='mp4', options={
                'movflags': 'frag_keyframe+empty_moov+default_base_moof'})
            Output.start(self)

        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            if self.error:
                return
            now = time.monotonic()
            try:
                if self.bytes + len(frame) > MAX_BYTES or now - self.began > 610:
                    raise RuntimeError('Recording reached its size or time limit')
                if now - self.checked > 1:
                    self.checked = now
                    if shutil.disk_usage(Path(path).parent).free < MIN_FREE:
                        raise RuntimeError('Camera recording stopped: storage is nearly full')
                super().outputframe(frame, keyframe, timestamp, packet, audio)
                if not self.error:
                    self.bytes += len(frame)
                    self.last = now
                    self.frames += 1
                    if self.first_timestamp is None:self.first_timestamp=timestamp
                    self.last_timestamp=timestamp
                    self.first.set()
            except Exception as error:
                self.failed(error)

        def stop(self):
            # Upstream PyavOutput suppresses close failures; retain an explicit
            # failure so a broken take is never advertised as a successful file.
            Output.stop(self)
            try:
                if self._container:
                    self._container.close()
            except Exception as error:
                self.failed(error)
            finally:
                self._container = None
                self._streams = {}

    return BoundedOutput()


class VideoRecording:
    def __init__(self, camera, path, fps):
        from picamera2.encoders import H264Encoder
        self.camera = camera
        self.output = output_file(path)
        self.encoder = H264Encoder(bitrate=6_000_000, repeat=True, iperiod=max(1, round(fps)), framerate=fps)
        self.fps = fps
        self.closed = False
        self.encoder_started = False

    def start(self):
        wait_for_framerate(self.camera, self.fps)
        self.encoder_started = True
        self.camera.start_encoder(self.encoder, self.output, name='main')
        end = time.monotonic() + 8
        while not self.output.first.wait(.05):
            if self.output.error or time.monotonic() >= end:
                raise RuntimeError(self.output.error or 'Camera encoder did not produce a frame')

    def check(self):
        if self.closed:
            raise RuntimeError('Camera recording was interrupted')
        if self.output.error:
            raise RuntimeError(self.output.error)
        if time.monotonic() - self.output.last > 3:
            raise RuntimeError('Camera stopped delivering video frames')

    def metadata(self):
        output=self.output
        span=(output.last_timestamp or 0)-(output.first_timestamp or 0)
        return {'width':self.encoder.width,'height':self.encoder.height,
                'target_fps':self.fps,'measured_fps':round((output.frames-1)*1_000_000/span,2) if span>0 else None,
                'frames':output.frames,'codec':'h264','bitrate':6_000_000,'audio':False}

    def stop(self):
        if self.closed:
            return
        try:
            if self.encoder_started:
                self.camera.stop_encoder(self.encoder)
        finally:
            self.closed = True
            self.output.stop()
        if self.output.error:
            raise RuntimeError(self.output.error)
