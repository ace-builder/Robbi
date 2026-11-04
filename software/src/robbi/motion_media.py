"""Bounded media library and portable animation bundles; no archive extraction."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time
import wave
import zipfile

from .errors import ValidationError
from .storage import atomic_write

MAX_FILE = 12*1024*1024
MAX_BUNDLE = 24*1024*1024
MAX_STORAGE = 128*1024*1024
TYPES = {'.png': ('image', 'image/png'), '.jpg': ('image', 'image/jpeg'),
         '.jpeg': ('image', 'image/jpeg'), '.webp': ('image', 'image/webp'),
         '.wav': ('sound', 'audio/wav'), '.mp4': ('video', 'video/mp4')}
MEDIA_ID = re.compile(r'(?:media-[a-f0-9]{64}|builtin-[a-f0-9]{24})\Z')


def media_refs(project):
    return {value for p in project['waypoints'] for value in
            ((p['cue']['media'] if p['cue']['screen'] != 'face' else ''), p['cue']['sound']) if value}


class MotionMedia:
    def __init__(self, root, data_dir):
        self.root, self.directory = Path(root), Path(data_dir)/'motion_studio_media'
        self._lock = threading.RLock()
        self._catalog = {}
        self._refreshed = -100.0

    def catalog(self, *, refresh=False):
        with self._lock:
            if refresh or time.monotonic()-self._refreshed > 30:
                values = {}
                for folder in ('images', 'images/faces', 'sounds', 'videos'):
                    try:
                        with os.scandir(self.root/'assets'/folder) as scan:
                            entries = sorted(scan, key=lambda e: e.name)
                    except FileNotFoundError:
                        continue
                    for entry in entries:
                        path = Path(entry.path)
                        if path.suffix.lower() not in TYPES or not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        if not 0 < stat.st_size <= MAX_FILE:
                            continue
                        relative = path.relative_to(self.root/'assets').as_posix()
                        media_id = 'builtin-'+hashlib.sha256(relative.encode()).hexdigest()[:24]
                        kind, mime = TYPES[path.suffix.lower()]
                        values[media_id] = dict(id=media_id, name=path.name, kind=kind, mime=mime,
                            bytes=stat.st_size, duration=None,
                            source='Robbi assets', _path=path)
                for path in self.directory.glob('media-*.json'):
                    try:
                        if path.stat().st_size > 4096:
                            continue
                        entry = json.loads(path.read_text(encoding='utf-8'))
                        media_id, suffix = entry['id'], entry['suffix']
                        if not MEDIA_ID.fullmatch(media_id) or not media_id.startswith('media-') or suffix not in TYPES:
                            continue
                        source = self.directory/(media_id+suffix)
                        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FILE:
                            continue
                        kind, mime = TYPES[suffix]
                        values[media_id] = dict(id=media_id, name=str(entry['name'])[:100], kind=kind, mime=mime,
                            bytes=source.stat().st_size, duration=entry.get('duration'), source='Uploaded', _path=source)
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                by_path = {entry['_path']: entry['id'] for entry in values.values()}
                for entry in values.values():
                    if entry['kind'] == 'video':
                        entry['soundtrack'] = by_path.get(entry['_path'].with_name(entry['_path'].stem+'_audio.wav'), '')
                self._catalog, self._refreshed = values, time.monotonic()
            return [self.public(entry) for entry in self._catalog.values()]

    @staticmethod
    def public(entry):
        return {key: value for key, value in entry.items() if not key.startswith('_')}

    def resolve(self, media_id, kind=None):
        if not isinstance(media_id, str) or not MEDIA_ID.fullmatch(media_id):
            raise ValidationError('Select media from the library')
        with self._lock:
            if media_id not in self._catalog:
                self.catalog()
            entry = self._catalog.get(media_id)
        if entry is None or not entry['_path'].is_file():
            raise ValidationError('Media is missing on this device. Import the animation bundle or upload its files.')
        if kind and entry['kind'] != kind:
            raise ValidationError(f"Choose a {kind} file for this cue")
        return entry

    def _check(self, name, data):
        suffix = Path(name).suffix.lower()
        if suffix not in TYPES or not data or len(data) > MAX_FILE:
            raise ValidationError('Upload PNG, JPEG, WebP, WAV, or MP4, up to 12 MiB per file')
        duration = None
        if TYPES[suffix][0] == 'image':
            from PIL import Image
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.width*image.height > 8_000_000 or image.format not in {'PNG', 'JPEG', 'WEBP'}:
                        raise ValidationError('Images must be PNG, JPEG, or WebP, up to 8 megapixels')
                    detected = {'PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}[image.format]
                    image.verify()
                suffix = detected
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                raise ValidationError('This image could not be decoded') from error
        elif suffix == '.wav':
            try:
                with wave.open(io.BytesIO(data), 'rb') as sound:
                    duration = sound.getnframes()/sound.getframerate()
                    if sound.getnchannels() not in (1, 2) or sound.getsampwidth() not in (1, 2, 3, 4) or not 0 < duration <= 110:
                        raise ValidationError('Use a PCM WAV sound up to 110 seconds, with one or two channels')
                    if len(sound.readframes(sound.getnframes())) != sound.getnframes()*sound.getnchannels()*sound.getsampwidth():
                        raise ValidationError('The WAV file is incomplete')
            except (EOFError, OSError, wave.Error, ZeroDivisionError) as error:
                raise ValidationError('Upload a valid PCM WAV sound') from error
        elif len(data) < 16 or data[4:8] != b'ftyp':
            raise ValidationError('Upload a valid MP4 video')
        media_id = 'media-'+hashlib.sha256(data).hexdigest()
        kind, mime = TYPES[suffix]
        clean_name = re.sub(r'[\x00-\x1f\\/]', '_', str(name))[:100]
        return dict(id=media_id, name=clean_name, suffix=suffix, kind=kind, mime=mime, bytes=len(data), duration=duration)

    def install(self, name, data):
        entry = self._check(name, data)
        with self._lock:
            target = self.directory/(entry['id']+entry['suffix'])
            if not target.exists():
                used = sum(p.stat().st_size for p in self.directory.glob('*') if p.is_file())
                if used+len(data) > MAX_STORAGE:
                    raise ValidationError('The motion media library is full (128 MiB)')
                atomic_write(target, data)
            atomic_write(self.directory/(entry['id']+'.json'), json.dumps(entry, ensure_ascii=False))
            self._refreshed = -100.0
        return self.public(entry)

    def validate_refs(self, project):
        for point in project['waypoints']:
            cue = point['cue']
            if cue['screen'] != 'face':
                self.resolve(cue['media'], cue['screen'])
            if cue['sound']:
                self.resolve(cue['sound'], 'sound')

    def export_bundle(self, project):
        self.validate_refs(project)
        portable, files, unique, total = copy.deepcopy(project), {}, {}, 0
        for media_id in media_refs(project):
            entry = self.resolve(media_id)
            data = entry['_path'].read_bytes()
            checked = self._check(entry['name'], data)
            if checked['id'] not in unique:
                total += len(data)
            if total > MAX_BUNDLE:
                raise ValidationError('An animation bundle is limited to 24 MiB of media')
            files[media_id] = (checked, data)
            unique[checked['id']] = (checked, data)
        for p in portable['waypoints']:
            for field in ('media', 'sound'):
                if p['cue'][field] in files:
                    p['cue'][field] = files[p['cue'][field]][0]['id']
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('animation.json', json.dumps(portable, ensure_ascii=False))
            archive.writestr('media.json', json.dumps([entry for entry, _ in unique.values()], ensure_ascii=False))
            for entry, data in unique.values():
                archive.writestr('media/'+entry['id']+entry['suffix'], data)
        output.seek(0)
        return output

    def import_bundle(self, data, validate_project):
        if len(data) > MAX_BUNDLE+1024*1024:
            raise ValidationError('Animation bundles are limited to 24 MiB')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 130 or len({e.filename for e in entries}) != len(entries):
                    raise ValidationError('The bundle contains too many or duplicate files')
                if sum(e.file_size for e in entries) > MAX_BUNDLE+1024*1024:
                    raise ValidationError('The uncompressed bundle exceeds 24 MiB')
                for entry in entries:
                    if entry.filename not in ('animation.json', 'media.json') and not re.fullmatch(r'media/media-[a-f0-9]{64}\.(png|jpg|jpeg|webp|wav|mp4)', entry.filename):
                        raise ValidationError('The bundle contains an unsupported file path')
                    limit = 512*1024 if entry.filename.endswith('.json') else MAX_FILE
                    if entry.file_size > limit or entry.flag_bits & 1:
                        raise ValidationError('An oversized or encrypted bundle entry is unsupported')
                project = validate_project(json.loads(archive.read('animation.json')))
                labels = json.loads(archive.read('media.json')) if 'media.json' in archive.namelist() else []
                if not isinstance(labels, list) or len(labels) > 128:
                    raise ValidationError('Invalid media manifest')
                names = {e['id']:e['name'] for e in labels if isinstance(e, dict) and isinstance(e.get('id'), str) and isinstance(e.get('name'), str)}
                pending = []
                for entry in entries:
                    if not entry.filename.startswith('media/'):
                        continue
                    payload = archive.read(entry)
                    media_id = Path(entry.filename).stem
                    name = names.get(media_id, Path(entry.filename).name)
                    checked = self._check(name if Path(name).suffix.lower() in TYPES else Path(entry.filename).name, payload)
                    if checked['id'] != media_id:
                        raise ValidationError('A media file does not match its content identifier')
                    pending.append((name, payload, checked))
                checked_media = {e[2]['id']:e[2] for e in pending}
                if not media_refs(project).issubset(checked_media):
                    raise ValidationError('The bundle is missing media used by the animation')
                for point in project['waypoints']:
                    cue = point['cue']
                    for media_id, kind in ((cue['media'], cue['screen']), (cue['sound'], 'sound')):
                        if media_id and kind != 'face' and checked_media[media_id]['kind'] != kind:
                            raise ValidationError(f'The bundle needs a {kind} file for this cue')
                # Validate every entry before writing any file; never extract paths.
                with self._lock:
                    extra = sum(len(payload) for _, payload, e in pending if not (self.directory/(e['id']+e['suffix'])).exists())
                    if sum(p.stat().st_size for p in self.directory.glob('*') if p.is_file())+extra > MAX_STORAGE:
                        raise ValidationError('The motion media library is full (128 MiB)')
                    for name, payload, entry in pending:
                        self.install(name if Path(name).suffix.lower() in TYPES else entry['id']+entry['suffix'], payload)
                return project
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeError) as error:
            raise ValidationError('This is not a complete Motion Studio animation bundle') from error
