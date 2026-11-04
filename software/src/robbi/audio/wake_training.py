"""Bounded preparation for browser-side personal wake calibration, not neural training.

Only MFCC extraction runs on the Pi. The browser fits a threshold using the same
cosine-DTW recurrence as live detection, excluding each positive's own template.
No audio or feature data leaves the robot/browser connection.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid
import wave

from robbi.errors import BusyError, ValidationError

LIMITS = {"positive": 32, "negative": 24, "noise": 16}


def feature_windows(detector, audio, ends):
    """Reuse aligned interior MFCC frames; recompute both window boundaries.

    MFCCs are frame-local, but pre-emphasis at the start and zero padding at
    the end differ from a whole recording. Never reuse those boundary frames.
    Irregular capture settings use the exact original extraction path.
    """
    import numpy as np
    from .wake_math import normalize_rows
    size = detector._buffer_size_samples
    step = max(1, round(detector._sample_rate * .01))
    frame = round(detector._sample_rate * .025)
    count = 1 + max(0, math.ceil((size - frame) / step))
    aligned = size % step == 0 and count >= 2 and all(end % step == 0 for end in ends)
    padded = np.concatenate((np.zeros(size, dtype=np.float32), audio))
    cache = normalize_rows(detector._extract_mfcc_features(padded)) if aligned and ends else None
    for end in ends:
        window = padded[end:end + size]
        if cache is None:
            yield normalize_rows(detector._extract_mfcc_features(window))
            continue
        features = cache[end // step:end // step + count].copy()
        features[0] = normalize_rows(detector._extract_mfcc_features(window[:frame]))[0]
        tail_start = (count - 2) * step
        features[-1] = normalize_rows(detector._extract_mfcc_features(window[tail_start:]))[1]
        yield features


def dataset_stamp(dataset, settings):
    rows = []
    for category, directory in dataset.CATEGORY_DIRS.items():
        directory = Path(directory).resolve()
        # Bound traversal as well as processing. Files outside these folders are rejected.
        paths = []
        if directory.exists():
            for index, path in enumerate(directory.iterdir()):
                if index >= 256:
                    raise ValidationError("Wake directory is too large; export and remove unused examples")
                if path.suffix.lower() != ".wav":
                    continue
                if path.is_symlink() or path.resolve().parent != directory or not path.is_file():
                    raise ValidationError("Wake samples must be regular files in the dataset directory")
                paths.append(path)
        if len(paths) > LIMITS[category]:
            raise ValidationError(f"Use at most {LIMITS[category]} {category} examples for browser training")
        for path in sorted(paths):
            if not 44 <= path.stat().st_size <= 512 * 1024:
                raise ValidationError("Wake examples must be WAV files below 512 KiB")
            rows.append((category, path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
    parameters = [settings.mic_sample_rate, settings.voice_chunk_ms,
                  settings.voice_local_wake_buffer_s, settings.voice_local_wake_slide_s,
                  settings.voice_local_wake_min_active_rms, settings.voice_local_wake_max_references,
                  settings.voice_local_wake_max_total_feature_frames]
    return hashlib.sha256(json.dumps([rows, parameters]).encode()).hexdigest(), rows


def prepare(dataset, settings, cancelled, progress=lambda *_: None):
    import numpy as np
    from robbi.engine.local_wake import LocalWakeDetector

    started = time.monotonic()
    stamp, rows = dataset_stamp(dataset, settings)
    counts = {category: sum(row[0] == category for row in rows) for category in LIMITS}
    if counts["positive"] < 4 or counts["negative"] < 3 or counts["noise"] < 2:
        raise ValidationError("Record at least 4 positive, 3 negative and 2 background-noise examples first")
    detector = LocalWakeDetector()
    if len(detector._references) > 8:
        raise ValidationError("Browser calibration supports at most eight active templates; lower voice.local_wake_max_references first")
    if len(detector._references) < 3:
        raise ValidationError("At least three valid positive templates must fit the wake detector's feature budget")
    total_bytes = 0

    def check():
        if cancelled.is_set():
            raise ValidationError("Training preparation cancelled")
        if time.monotonic() - started > 180:
            raise ValidationError("Preparation exceeded three minutes; use fewer or shorter samples")

    def encode(matrix):
        nonlocal total_bytes
        raw = np.asarray(matrix, dtype="<f4").tobytes()
        total_bytes += len(raw)
        if total_bytes > 8 * 1024 * 1024:
            raise ValidationError("Training feature budget reached; use fewer or shorter examples")
        return {"rows": len(matrix), "data": base64.b64encode(raw).decode("ascii")}

    references = [{"name": ref.name, "features": encode(ref.features)} for ref in detector._references]
    samples = []
    rate = detector._sample_rate
    # Reproduce actual chunk/slide cadence and RMS gate, then evenly sample at
    # most 12 candidate windows per recording to bound transfer and browser CPU.
    chunk = max(1, round(rate * max(20, int(settings.voice_chunk_ms)) / 1000))
    stride = max(chunk, math.ceil(detector._slide_size_samples / chunk) * chunk)
    for index, (category, name, _) in enumerate(rows):
        check()
        path = dataset.sample_file(category, name)
        with wave.open(str(path), "rb") as stream:
            if stream.getsampwidth() != 2 or stream.getnchannels() != 1 or not 8000 <= stream.getframerate() <= 48000:
                raise ValidationError("Training needs mono 16-bit PCM WAV recordings (8–48 kHz)")
            if not .2 <= stream.getnframes() / stream.getframerate() <= 16:
                raise ValidationError("Training examples must last 0.2–16 seconds")
            original_rate = stream.getframerate()
            audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768
        audio = detector._resample_if_needed(audio, original_rate)
        ends = []
        for end in range(stride, len(audio) + 1, stride):
            level = float(np.sqrt(np.mean(audio[max(0, end - chunk):end] ** 2))) * 32768
            if level >= settings.voice_local_wake_min_active_rms:
                ends.append(end)
        if len(ends) > 12:
            ends = [ends[i] for i in np.linspace(0, len(ends) - 1, 12, dtype=int)]
        windows = []
        for matrix in feature_windows(detector, audio, ends):
            check()
            windows.append(encode(matrix))
            time.sleep(.001)  # Yield between windows to foreground robot work.
        if category == "positive" and not windows:
            raise ValidationError("A positive example is below the wake RMS gate; re-record it closer to Robbi")
        samples.append({"category": category, "name": name, "windows": windows})
        progress(index + 1, len(rows))
    if dataset_stamp(dataset, settings)[0] != stamp:
        raise ValidationError("Dataset or wake settings changed during preparation; prepare again")
    return {"version": 1, "fingerprint": stamp, "references": references, "samples": samples,
            "counts": counts, "feature_bytes": total_bytes, "prepare_seconds": round(time.monotonic() - started, 3),
            "note": "Personal MFCC/DTW calibration. Leave-one-out estimates on recorded clips, using up to 12 sampled live windows per clip; not a general accuracy guarantee."}


class WakeTraining:
    """One ephemeral preparation job, one result, no persistent worker at idle."""
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.record = None
        self.result = None

    def start(self, dataset, settings):
        with self.lock:
            if self.record and self.record["state"] == "running":
                raise BusyError("Wake preparation is already running")
            self.cancelled = threading.Event()
            self.result = None
            self.record = {"id": uuid.uuid4().hex, "state": "running", "done": 0, "total": 0, "error": "", "created": time.monotonic()}
            identifier = self.record["id"]
        def progress(done, total):
            with self.lock:
                self.record.update(done=done, total=total)
        def run():
            try:
                result = prepare(dataset, settings, self.cancelled, progress)
                with self.lock:
                    if self.cancelled.is_set():
                        self.record.update(state="cancelled")
                    else:
                        self.result = result
                        self.record.update(state="done", prepare_seconds=result["prepare_seconds"])
            except Exception as error:
                with self.lock:
                    self.record.update(state="cancelled" if self.cancelled.is_set() else "failed", error=str(error)[:300])
        threading.Thread(target=run, name="wake-feature-preparation", daemon=True).start()
        return {"id": identifier, "state": "running"}

    def get(self, identifier, features=False):
        with self.lock:
            if not self.record or self.record["id"] != identifier:
                raise ValidationError("Unknown wake preparation")
            if time.monotonic() - self.record["created"] > 600:
                self.result = None
                raise ValidationError("Wake preparation expired; prepare again")
            if features:
                if self.result is None or self.record["state"] != "done":
                    raise ValidationError("Wake features are not ready")
                return self.result
            return {k: v for k, v in self.record.items() if k != "created"}
