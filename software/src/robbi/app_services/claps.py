"""Local transient timing for rhythm practice, not speech transcription."""
from array import array
import io
import math
import statistics
import sys
import wave

from robbi.errors import ValidationError


def detect_claps(data):
    if not isinstance(data, bytes) or not 44 <= len(data) <= 1600000:
        raise ValidationError("No bounded microphone recording was received")
    try:
        with wave.open(io.BytesIO(data), "rb") as recording:
            rate, frames = recording.getframerate(), recording.getnframes()
            if (recording.getnchannels() != 1 or recording.getsampwidth() != 2
                    or recording.getcomptype() != "NONE" or not 8000 <= rate <= 48000
                    or not 0 < frames <= rate * 8.1):
                raise ValidationError("Clap capture needs at most eight seconds of mono 16-bit PCM audio")
            samples = array("h", recording.readframes(frames))
            if len(samples) != frames:
                raise ValidationError("Microphone recording is incomplete")
    except (wave.Error, EOFError) as error:
        raise ValidationError("Microphone did not return valid WAV audio") from error
    if sys.byteorder != "little":
        samples.byteswap()
    width = max(1, rate // 100)
    energy = [math.sqrt(sum(value * value for value in samples[start:start + width]) /
                        len(samples[start:start + width])) for start in range(0, len(samples), width)]
    noise = statistics.median(energy)
    threshold = max(650, noise * 4.5)
    times, onset = [], None
    for index, rms in enumerate(energy + [0]):
        if rms >= threshold and onset is None:
            onset = index
        elif rms < threshold and onset is not None:
            at = onset * width / rate
            # Reject sustained sounds; consolidate the sharp transient of a clap.
            if (index - onset) * width / rate <= .18 and (not times or at - times[-1] >= .12):
                times.append(round(at, 3))
            onset = None
    if len(times) > 40:
        return {"ok": False, "clap_times_s": [], "duration_s": frames / rate,
                "reason": "Too many sharp sounds; try a quieter room or the tap input"}
    return {"ok": True, "clap_times_s": times, "duration_s": round(frames / rate, 3),
            "method": "local_transients", "reason": "" if times else "No clear claps detected; try closer to the microphone or use tap input"}
