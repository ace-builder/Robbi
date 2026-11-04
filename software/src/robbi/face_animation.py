"""Cached LCD expressions with authored timing independent of display frame rate."""
from bisect import bisect_right
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
import json
import math
from pathlib import Path
import threading
import time

from PIL import Image
from .errors import ValidationError
from .orchestration import intent_scope


def _number(value, minimum, maximum):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and minimum <= value <= maximum)


@dataclass(frozen=True)
class FaceClip:
    frames: tuple
    ends: tuple = ()
    blends: tuple = ()

    def duration(self, fps=10):
        return self.ends[-1] if self.ends else len(self.frames) / fps

    def frame_at(self, seconds, fps=10):
        """Hold poses; ease between them without decoding or growing a frame cache."""
        position = max(0, seconds) % self.duration(fps)
        if not self.ends:
            return self.frames[min(len(self.frames)-1, int(position * fps))]
        index = min(len(self.frames)-1, bisect_right(self.ends, position))
        local = position - (self.ends[index-1] if index else 0)
        blend = self.blends[index]
        if blend and local < blend:
            phase = local / blend
            eased = phase * phase * (3 - 2 * phase)
            return Image.blend(self.frames[index-1], self.frames[index], eased)
        return self.frames[index]


class FaceAnimations:
    def __init__(self, directory, display):
        self.directory, self.display = Path(directory), display
        self._cache = OrderedDict()
        self._sheets = OrderedDict()
        self._lock = threading.Lock()
        self.last_result = {}

    def catalog(self):
        # The combined cartoon collections now exceed 128 entries.
        # Keep discovery bounded without hiding earlier entries alphabetically.
        return sorted(p.stem for p in self.directory.glob('*.json'))[:256]

    def frames(self, name):
        return self.clip(name).frames

    def clip(self, name):
        if not isinstance(name, str) or not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in name):
            raise ValidationError('Select a registered face animation')
        manifest = self.directory / (name + '.json')
        if not manifest.is_file() or manifest.stat().st_size > 8192:
            raise ValidationError('Unknown or oversized face animation')
        try:
            spec = json.loads(manifest.read_text(encoding='utf-8'))
        except (ValueError, OSError) as error:
            raise ValidationError('Unreadable face animation manifest') from error
        if not isinstance(spec, dict) or not isinstance(spec.get('sheet'), str):
            raise ValidationError('A face animation must name a local sprite sheet')
        sheet = (self.directory / spec['sheet']).resolve()
        if sheet.parent != self.directory.resolve() or not sheet.is_file() or sheet.stat().st_size > 4 * 1024 * 1024:
            raise ValidationError('Sprite sheet must be a local file up to 4 MiB')
        columns, rows = spec.get('columns', 1), spec.get('rows', 10)
        if type(columns) is not int or type(rows) is not int or not 1 <= columns * rows <= 20 or min(columns, rows) < 1:
            raise ValidationError('Sprite sheets must have 1 to 20 cells')
        order = spec.get('order', list(range(columns * rows)))
        if not isinstance(order, list) or not 1 <= len(order) <= 60 or any(type(i) is not int or not 0 <= i < columns * rows for i in order):
            raise ValidationError('Invalid sprite frame order')
        durations = spec.get('durations_ms')
        ends, blends = (), ()
        if durations is not None:
            if (not isinstance(durations, list) or len(durations) != len(order)
                    or any(not _number(value, 20, 5000) for value in durations) or sum(durations) > 30000):
                raise ValidationError('Supply one 20–5000 ms duration per pose, up to 30 seconds total')
            transitions = spec.get('blend_ms', 0)
            if not isinstance(transitions, list):
                transitions = [transitions] * len(order)
            if (len(transitions) != len(order)
                    or any(not _number(value, 0, min(250, duration)) for value, duration in zip(transitions, durations, strict=True))):
                raise ValidationError('Pose transitions must fit their duration and be at most 250 ms')
            elapsed, timestamps = 0, []
            for duration in durations:
                elapsed += duration / 1000
                timestamps.append(elapsed)
            ends, blends = tuple(timestamps), tuple(value / 1000 for value in transitions)
        elif 'blend_ms' in spec:
            raise ValidationError('Pose transitions require explicit durations')
        sheet_key = (str(sheet), sheet.stat().st_mtime_ns, columns, rows)
        key = (name, manifest.stat().st_mtime_ns, sheet_key)
        with self._lock:
            if key not in self._cache:
                if sheet_key not in self._sheets:
                    with Image.open(sheet) as source:
                        if source.size != (280 * columns, 240 * rows):
                            raise ValidationError('Every sprite cell must be exactly 280 by 240 pixels')
                        source = source.convert('RGB')
                        self._sheets[sheet_key] = tuple(source.crop((i % columns * 280, i // columns * 240,
                            i % columns * 280 + 280, i // columns * 240 + 240)) for i in range(columns * rows))
                self._sheets.move_to_end(sheet_key)
                cells = self._sheets[sheet_key]
                self._cache[key] = FaceClip(tuple(cells[i] for i in order), ends, blends)
                # Clips sharing an atlas also share its decoded images. Blended
                # frames are transient; 30 fps does not allocate a huge movie.
                while len(self._sheets) > 2:
                    self._sheets.popitem(last=False)
                while len(self._cache) > 3:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(key)
            return self._cache[key]

    def play(self, name, *, fps=10, loops=1, cancel_event=None, hold_s=2):
        if not _number(fps, 1, 30):
            raise ValidationError('Animation frame rate must be from 1 to 30 fps')
        if type(loops) is not int or not 1 <= loops <= 30:
            raise ValidationError('Animation loops must be from 1 to 30')
        if not _number(hold_s, 0, 120):
            raise ValidationError('Face hold must be from 0 to 120 seconds')
        cancelled = cancel_event or threading.Event()
        clip = self.clip(name)
        duration = clip.duration(fps) * loops
        if duration > 30:
            raise ValidationError('One face animation is limited to 30 seconds')
        coordinator = self.display.orchestrator
        lease = coordinator.acquire(('display',), cancel_event=cancelled, ttl_s=duration + 3) if coordinator else None
        signal = lease or cancelled
        shown, skipped, writes, costs = 0, 0, [], []
        start = time.monotonic()
        index, total = 0, math.ceil(duration * fps - 1e-9)
        reason = 'completed'
        try:
            while index < total:
                if signal.is_set():
                    reason = 'cancelled'
                    break
                # Advance the timeline instead of queueing stale frames. Always
                # settle on the final authored pose after successful playback.
                due = min(total - 1, int((time.monotonic() - start) * fps))
                if due > index:
                    skipped += due - index
                    index = due
                frame_start = time.monotonic()
                frame = clip.frames[-1] if index == total-1 else clip.frame_at(index / fps, fps)
                write_start = time.monotonic()
                with intent_scope(lease.intent) if lease else nullcontext():
                    written = self.display.show_pil_frame(frame, cancel_event=signal)
                if not written:
                    reason = 'cancelled' if signal.is_set() else 'display_unavailable'
                    break
                writes.append(time.monotonic() - write_start)
                costs.append(time.monotonic() - frame_start)
                shown += 1
                index += 1
                if signal.wait(max(0, min(start + duration, start + index / fps) - time.monotonic())):
                    reason = 'cancelled'
                    break
        except Exception:
            reason = 'failed'
            raise
        finally:
            if lease is not None:
                lease.release(hold_s=hold_s if reason == 'completed' and shown else 0)
        elapsed = time.monotonic() - start
        def p95(samples):
            ordered = sorted(samples)
            return round(ordered[min(len(ordered)-1, int(len(ordered)*.95))] * 1000, 2) if ordered else None
        self.last_result = {'ok': reason == 'completed', 'state': reason, 'name': name, 'frames': shown,
            'displayed': shown > 0,
            'skipped': skipped, 'requested_fps': fps, 'actual_fps': round(shown / max(.001, elapsed), 2),
            'elapsed_s': round(elapsed, 3), 'duration_s': round(duration, 3), 'timed_poses': bool(clip.ends),
            'write_p95_ms': p95(writes), 'frame_p95_ms': p95(costs)}
        self.last_result['recommended_fps'] = 30 if (fps == 30 and costs and p95(costs) <= 30
            and self.last_result['actual_fps'] >= 27 and skipped <= total * .1) else 10
        return dict(self.last_result)
