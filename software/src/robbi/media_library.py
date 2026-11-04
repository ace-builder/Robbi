"""Read-only, paged media browsing and bounded previews for the dashboard."""
from collections import OrderedDict
import io
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlencode

from flask import Blueprint, abort, jsonify, request, send_file
from .app_runtime.owner import require_owner
from .errors import BusyError, ValidationError, UnavailableError

FOLDERS = {'faces': 'assets/images/faces', 'sounds': 'assets/sounds',
           'videos': 'assets/videos', 'timeline': 'animations', 'sprite': 'assets/face_animations'}
SUFFIXES = {'faces': {'.png', '.jpg', '.jpeg', '.webp'},
            'sounds': {'.wav', '.mp3', '.ogg', '.flac'}, 'videos': {'.mp4', '.webm'},
            'timeline': {'.yaml'}, 'sprite': {'.json'}}


class MediaLibrary:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.decode = threading.BoundedSemaphore(1)
        self.cache = OrderedDict()
        self.index, self.scanned = {}, -100.0

    def path(self, kind, name, *, sheet=False):
        if kind not in FOLDERS or not isinstance(name, str) or not name or len(name) > 240 or any(c in name for c in '/\\\x00') or name.startswith('.'):
            raise ValidationError('Choose an item from the media library')
        folder = (self.root / FOLDERS[kind]).resolve()
        path = folder / name
        if not folder.is_relative_to(self.root) or path.is_symlink() or path.resolve().parent != folder:
            raise ValidationError('Media must stay inside its library folder')
        suffixes = {'.png', '.jpg', '.webp'} if sheet else SUFFIXES[kind]
        if path.suffix.lower() not in suffixes or not path.is_file():
            raise ValidationError('Media is missing or unsupported')
        return path

    def names(self, kind):
        with self.lock:
            if time.monotonic() - self.scanned > 30:
                index = {}
                for group, directory in FOLDERS.items():
                    folder = self.root / directory
                    values = []
                    if folder.is_dir() and folder.resolve().is_relative_to(self.root):
                        for i, path in enumerate(folder.iterdir()):
                            if i >= 10000:
                                break
                            if not path.name.startswith('.') and path.suffix.lower() in SUFFIXES[group] and path.is_file() and not path.is_symlink():
                                values.append(path.name)
                    index[group] = sorted(values, key=str.casefold)
                self.index, self.scanned = index, time.monotonic()
            return list(self.index[kind])

    def sprite(self, name):
        manifest = self.path('sprite', name)
        if manifest.stat().st_size > 8192:
            raise ValidationError('Animation manifest is too large')
        spec = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(spec, dict):
            raise ValidationError('Invalid animation manifest')
        sheet = self.path('sprite', spec.get('sheet'), sheet=True)
        if sheet.stat().st_size > 4 * 1024 * 1024:
            raise ValidationError('Sprite sheet exceeds 4 MiB')
        columns, rows = spec.get('columns', 1), spec.get('rows', 10)
        if type(columns) is not int or type(rows) is not int or min(columns, rows) < 1 or columns * rows > 20:
            raise ValidationError('Invalid sprite dimensions')
        order = spec.get('order', list(range(columns * rows)))
        durations = spec.get('durations_ms', [100] * len(order)) if isinstance(order, list) else None
        if not isinstance(order, list) or not 1 <= len(order) <= 60 or any(type(i) is not int or not 0 <= i < columns * rows for i in order):
            raise ValidationError('Invalid sprite frame order')
        if not isinstance(durations, list) or len(durations) != len(order) or any(type(v) not in (float, int) or not math.isfinite(v) or not 20 <= v <= 5000 for v in durations) or sum(durations) > 30000:
            raise ValidationError('Invalid animation timing')
        return sheet, {'columns': columns, 'rows': rows, 'order': order, 'durations_ms': durations}

    def timeline(self, name):
        from robbi.engine.animations import AnimationPlayer
        import yaml
        path = self.path('timeline', name)
        if path.stat().st_size > 64 * 1024:
            raise ValidationError('Timeline exceeds preview limit')
        try:
            events = AnimationPlayer(None, None)._load_events(path)
        except yaml.YAMLError as error:
            raise ValidationError('Invalid animation timeline') from error
        return events

    @staticmethod
    def url(operation, kind, name):
        return '/api/media-library/' + operation + '?' + urlencode({'kind': kind, 'name': name})

    def entry(self, kind, name):
        item = {'kind': kind, 'name': name, 'label': Path(name).stem.replace('_', ' ').replace('-', ' '),
                'thumbnail': None, 'requirements': ['audio'] if kind == 'sounds' else ['display'], 'motion': False}
        if kind in ('faces', 'sprite'):
            item['thumbnail'] = self.url('thumbnail', kind, name)
        if kind == 'videos' and Path(name).stem + '.json' in self.names('sprite'):
            item['thumbnail'] = self.url('thumbnail', 'sprite', Path(name).stem + '.json')
        if kind == 'timeline':
            try:
                events = self.timeline(name)
                item['motion'] = any('drive' in e.payload for e in events)
                item['requirements'] = sorted({'display' if key in ('face', 'video') else 'motion' if key == 'drive' else 'audio'
                                               for e in events for key in e.payload})
                face = next((e.payload['face'] for e in events if isinstance(e.payload.get('face'), str)), None)
                if face:
                    item['thumbnail'] = self.url('thumbnail', 'faces', face)
            except (ValueError, OSError, TypeError):
                item['error'] = 'This timeline could not be read'
        return item

    def page(self, category, query='', offset=0, limit=18):
        if category not in ('faces', 'sounds', 'videos', 'animations'):
            raise ValidationError('Choose faces, sounds, videos or animations')
        if not 0 <= offset <= 20000 or not 1 <= limit <= 24 or len(query) > 120:
            raise ValidationError('Invalid media page')
        groups = ('sprite', 'timeline') if category == 'animations' else (category,)
        query = query.casefold().replace('_', ' ').replace('-', ' ')
        names = [(kind, name) for kind in groups for name in self.names(kind)
                 if query in name.casefold().replace('_', ' ').replace('-', ' ')]
        return {'items': [self.entry(*pair) for pair in names[offset:offset+limit]], 'total': len(names),
                'next': offset + limit if offset + limit < len(names) else None}

    def thumbnail(self, kind, name):
        from PIL import Image
        path, spec = (self.sprite(name) if kind == 'sprite' else (self.path(kind, name), None))
        if kind not in ('faces', 'sprite') or path.stat().st_size > 8 * 1024 * 1024:
            raise ValidationError('No image preview available')
        key = (str(path), path.stat().st_mtime_ns, json.dumps(spec, sort_keys=True))
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        if not self.decode.acquire(timeout=.3):
            raise BusyError('Preview busy; try this preview again')
        try:
            with Image.open(path) as image:
                if image.width * image.height > 8_000_000:
                    raise ValidationError('Image exceeds preview resolution limit')
                if spec:
                    if image.size != (280 * spec['columns'], 240 * spec['rows']):
                        raise ValidationError('Invalid sprite image size')
                    cell = spec['order'][0]
                    x, y = cell % spec['columns'] * 280, cell // spec['columns'] * 240
                    image = image.crop((x, y, x + 280, y + 240))
                else:
                    image = image.copy()
                image.thumbnail((168, 144))
                output = io.BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=78)
                blob = output.getvalue()
            with self.lock:
                self.cache[key] = blob
                while len(self.cache) > 48:
                    self.cache.popitem(last=False)
            return blob
        finally:
            self.decode.release()


def blueprint(runtime):
    library = MediaLibrary(runtime.config.root)
    bp = Blueprint('media_library', __name__, url_prefix='/api/media-library')

    def selected():
        return request.args.get('kind', ''), request.args.get('name', '')

    @bp.get('')
    def listing():
        return jsonify(library.page(request.args.get('kind', 'faces'), request.args.get('q', ''),
                                    int(request.args.get('offset', 0)), int(request.args.get('limit', 18))))

    @bp.post('/refresh')
    def refresh():
        require_owner()
        with library.lock:
            library.scanned = -100.0
            library.cache.clear()
        runtime.refresh_assets()
        return jsonify(ok=True)

    @bp.get('/thumbnail')
    def thumbnail():
        return send_file(io.BytesIO(library.thumbnail(*selected())), mimetype='image/jpeg', max_age=60)

    @bp.get('/file')
    def file():
        kind, name = selected()
        if kind == 'sprite':
            path, _ = library.sprite(name)
        elif kind in ('faces', 'sounds', 'videos'):
            path = library.path(kind, name)
        else:
            abort(404)
        return send_file(path, conditional=True, max_age=60)

    @bp.get('/preview')
    def preview():
        kind, name = selected()
        if kind == 'sprite':
            _, spec = library.sprite(name)
            return jsonify(type='sprite', url=library.url('file', kind, name), **spec)
        if kind == 'timeline':
            events = library.timeline(name)
            frames = [{'at_ms': event.at_ms, 'url': library.url('file', 'faces', event.payload['face'])}
                      for event in events if isinstance(event.payload.get('face'), str)]
            return jsonify(type='timeline', frames=frames, duration_ms=min(30000, max([f['at_ms'] for f in frames] or [0])+1500))
        library.path(kind, name)
        return jsonify(type=kind, url=library.url('file', kind, name))

    @bp.post('/play')
    def play():
        require_owner()
        payload = request.get_json() or {}
        if set(payload) != {'kind', 'name'}:
            raise ValidationError('Select a media item')
        kind, name = payload['kind'], payload['name']
        library.path(kind, name)
        if kind == 'sprite':
            library.sprite(name)
        if kind == 'timeline':
            library.timeline(name)
        key = {'faces': 'face', 'sounds': 'sound', 'videos': 'video', 'sprite': 'face_animation', 'timeline': 'animation'}[kind]
        action = {key: Path(name).stem if kind == 'sprite' else name}
        controller = runtime.controller()
        generation, token = runtime._generation, controller._action_token()
        def execute():
            if runtime._generation != generation:
                raise UnavailableError('Playback cancelled by Stop')
            local = controller._action_local
            previous = getattr(local, 'token', None)
            local.token = token
            try:
                return controller.execute_action(action)
            finally:
                local.token = previous
        return jsonify(runtime.jobs.submit('media', execute)), 202

    return bp
