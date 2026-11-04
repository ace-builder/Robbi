"""Small 16 kHz speech turn detector. Bounded buffers; no inference on the Pi."""
from array import array
from collections import deque
import math


class VoiceGate:
    FRAME_BYTES = 640  # 20 ms, 16 kHz, signed PCM16 mono.

    def __init__(self, silence_ms=700, classifier=None):
        if classifier is None:
            import webrtcvad
            classifier = webrtcvad.Vad(2).is_speech
        self.classifier = classifier
        self.silence_frames = silence_ms // 20
        self.noise = 60.0
        self.reset()

    def reset(self):
        self.recent = deque(maxlen=10)
        self.votes = deque(maxlen=10)
        self.frames = []
        self.silence = 0
        self.voiced = 0

    def feed(self, chunk):
        if len(chunk) != self.FRAME_BYTES:
            return None
        samples = array("h", chunk)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        speech = rms > max(120, self.noise * 2.5) and self.classifier(chunk, 16000)
        if not self.frames:
            self.recent.append(chunk)
            self.votes.append(bool(speech))
            if not speech:
                self.noise = .98 * self.noise + .02 * min(rms, 1000)
            if sum(self.votes) >= 6:
                self.frames = list(self.recent)
                self.voiced = sum(self.votes)
            return None
        self.frames.append(chunk)
        self.voiced += bool(speech)
        self.silence = 0 if speech else self.silence + 1
        if self.silence >= self.silence_frames or len(self.frames) >= 600:
            # Reject short clicks/bursts. Keep a little silence for natural endings.
            pcm = b"".join(self.frames) if self.voiced >= 12 else None
            self.reset()
            return pcm
        return None
