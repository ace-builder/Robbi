"""Durable shot revisions and audio variants. Names never become file paths."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from ..errors import BusyError, UnavailableError, ValidationError
from .recordings import Recordings


def signature(cue, definition):
    return hashlib.sha256(json.dumps([cue['text'], cue.get('voice_id', definition['voice_id'])], ensure_ascii=False).encode()).hexdigest()


class Library:
    def __init__(self, data_dir):
        self.directory = Path(data_dir)/'filming'
        self.lock = threading.RLock()
        self.initialized = False
        self.videos = Recordings(self)

    @contextmanager
    def db(self):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                with closing(sqlite3.connect(self.directory/'shots.sqlite3', timeout=2)) as db:
                    db.row_factory = sqlite3.Row
                    with db:
                        if not self.initialized:
                            db.executescript('''
                                CREATE TABLE IF NOT EXISTS shots(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                                    definition TEXT NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL);
                                CREATE TABLE IF NOT EXISTS audio(id TEXT PRIMARY KEY, shot_id TEXT NOT NULL,
                                    cue_id TEXT NOT NULL, signature TEXT NOT NULL, clip_id TEXT, status TEXT NOT NULL,
                                    duration_ms INTEGER, error TEXT NOT NULL DEFAULT '', selected INTEGER NOT NULL DEFAULT 0,
                                    created REAL NOT NULL);
                                CREATE INDEX IF NOT EXISTS audio_shot ON audio(shot_id,cue_id);
                                CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, shot_id TEXT NOT NULL,
                                    run_id TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                                    duration_ms INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                                    error TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '');
                                CREATE INDEX IF NOT EXISTS videos_shot ON videos(shot_id);
                            ''')
                            if 'metadata' not in {r[1] for r in db.execute('PRAGMA table_info(videos)')}:
                                db.execute("ALTER TABLE videos ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
                            db.execute("UPDATE audio SET status='failed',error='Preparation interrupted by restart. Retry manually.' WHERE status='preparing'")
                            for video in db.execute("SELECT id FROM videos WHERE status='recording'").fetchall():
                                part=self.path(video[0],'.mp4.part');path=self.path(video[0],'.mp4')
                                if part.is_file():part.replace(path)
                                db.execute("UPDATE videos SET status='interrupted',outcome='restart',bytes=?,error='Recording interrupted by restart; saved fragments may be incomplete.' WHERE id=?",
                                           (path.stat().st_size if path.exists() else 0,video[0]))
                            ready={r[0] for r in db.execute("SELECT id FROM audio WHERE status='ready'")}
                            for path in self.directory.iterdir():
                                if re.fullmatch(r'[a-f0-9]{32}\.(mp3\.part|wav\.part|wav)',path.name):
                                    if path.suffix=='.part' or path.stem not in ready:path.unlink(missing_ok=True)
                            self.initialized = True
                        yield db
            except sqlite3.IntegrityError:
                raise ValidationError('A shot with that name already exists') from None
            except sqlite3.Error:
                raise UnavailableError('The filming library could not be read or saved; check storage') from None

    def path(self, identifier, suffix='.wav'):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValidationError('Invalid filming ID')
        return self.directory/(identifier+suffix)

    def get(self, identifier):
        self.path(identifier)
        with self.db() as db:
            row = db.execute('SELECT * FROM shots WHERE id=?', (identifier,)).fetchone()
            if row is None: raise ValidationError('Shot not found')
            audio = [dict(r) for r in db.execute('SELECT * FROM audio WHERE shot_id=? ORDER BY created DESC', (identifier,))]
        result = dict(row); result['definition'] = json.loads(result['definition'])
        cues = {cue['id']: cue for cue in result['definition']['actions']}
        for entry in audio:
            cue = cues.get(entry['cue_id'])
            entry['current'] = bool(cue and cue['action']=='speak' and entry['signature']==signature(cue, result['definition']))
            if entry['status']=='ready' and not self.path(entry['id']).is_file():
                entry.update(status='failed', error='Prepared audio is missing. Regenerate this line.')
            entry['audio_url'] = '/api/filming/audio/'+entry['id'] if entry['status']=='ready' else None
        result['audio'] = audio
        result['videos'] = self.videos.listing(identifier)
        return result

    def listing(self, query=''):
        if not isinstance(query, str) or len(query)>200: raise ValidationError('Search must be at most 200 characters')
        with self.db() as db:
            rows = db.execute('SELECT * FROM shots ORDER BY name COLLATE NOCASE').fetchall()
        values = []
        for row in rows:
            if query.casefold() not in (row['name']+' '+row['definition']).casefold(): continue
            d = json.loads(row['definition'])
            values.append({'id':row['id'], 'name':row['name'], 'revision':row['revision'], 'updated':row['updated'],
                           'description':d['description'], 'actions':len(d['actions']),
                           'speech':sum(c['action']=='speak' for c in d['actions'])})
        return values

    def import_shots(self, definitions, replace=False):
        identifiers = []
        with self.db() as db:
            existing = {r['name']:r['id'] for r in db.execute('SELECT id,name FROM shots')}
            if not replace and any(d['name'] in existing for d in definitions):
                raise ValidationError('Some shot names already exist. Enable Replace matching names or rename them.')
            if len(existing)+sum(d['name'] not in existing for d in definitions)>200:
                raise ValidationError('The filming library is limited to 200 shots')
            for d in definitions:
                identifier = existing.get(d['name'], uuid.uuid4().hex)
                db.execute('''INSERT INTO shots VALUES (?,?,?,1,?) ON CONFLICT(name) DO UPDATE SET
                    definition=excluded.definition, revision=shots.revision+1, updated=excluded.updated''',
                    (identifier,d['name'],json.dumps(d,ensure_ascii=False),time.time()))
                identifiers.append(identifier)
        return identifiers

    def update(self, identifier, definition, revision):
        self.path(identifier)
        with self.db() as db:
            changed = db.execute('UPDATE shots SET name=?,definition=?,revision=revision+1,updated=? WHERE id=? AND revision=?',
                (definition['name'],json.dumps(definition,ensure_ascii=False),time.time(),identifier,revision)).rowcount
            if not changed: raise BusyError('This shot changed in another session. Reload before saving your edits.')
        return self.get(identifier)

    def delete(self, identifier):
        self.path(identifier)
        for video in self.videos.listing(identifier): self.videos.delete(identifier, video['id'])
        with self.db() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM audio WHERE shot_id=?',(identifier,))]
            db.execute('DELETE FROM audio WHERE shot_id=?',(identifier,))
            db.execute('DELETE FROM shots WHERE id=?',(identifier,))
        for variant in ids: self.path(variant).unlink(missing_ok=True)

    def begin_audio(self, identifier, cue, definition):
        with self.db() as db:
            if db.execute('SELECT COUNT(*) FROM audio').fetchone()[0]>=2000:
                raise ValidationError('Filming audio history is full. Delete old versions or shots.')
            count = db.execute('SELECT COUNT(*) FROM audio WHERE shot_id=? AND cue_id=?',(identifier,cue['id'])).fetchone()[0]
            if count>=10: raise ValidationError('This line has ten versions. Delete an old version before regenerating.')
            variant=uuid.uuid4().hex
            db.execute('INSERT INTO audio(id,shot_id,cue_id,signature,status,created) VALUES (?,?,?,?,?,?)',
                       (variant,identifier,cue['id'],signature(cue,definition),'preparing',time.time()))
        return variant

    def finish_audio(self, variant, *, clip_id=None, duration_ms=None, error=''):
        with self.db() as db:
            row=db.execute('SELECT * FROM audio WHERE id=?',(variant,)).fetchone()
            if not row: return
            if not error:
                db.execute('UPDATE audio SET selected=0 WHERE shot_id=? AND cue_id=?',(row['shot_id'],row['cue_id']))
            db.execute('UPDATE audio SET clip_id=?,duration_ms=?,error=?,status=?,selected=? WHERE id=?',
                (clip_id,duration_ms,str(error)[:500],'failed' if error else 'ready',int(not error),variant))

    def select(self, identifier, variant):
        entry = next((v for v in self.get(identifier)['audio'] if v['id']==variant),None)
        if not entry or not entry['current'] or entry['status']!='ready':
            raise ValidationError('Choose a ready version matching the current text and voice')
        with self.db() as db:
            db.execute('UPDATE audio SET selected=0 WHERE shot_id=? AND cue_id=?',(identifier,entry['cue_id']))
            db.execute('UPDATE audio SET selected=1 WHERE id=?',(variant,))
        return self.get(identifier)

    def delete_audio(self, identifier, variant):
        entry=next((v for v in self.get(identifier)['audio'] if v['id']==variant),None)
        if not entry: raise ValidationError('Audio version not found')
        with self.db() as db: db.execute('DELETE FROM audio WHERE id=?',(variant,))
        self.path(variant).unlink(missing_ok=True)
        return self.get(identifier)

    def selected_audio(self, detail):
        return {a['cue_id']:a for a in detail['audio'] if a['selected'] and a['current'] and a['status']=='ready'}

    def audio_file(self, variant):
        path = self.path(variant)
        with self.db() as db:
            row=db.execute("SELECT status FROM audio WHERE id=?",(variant,)).fetchone()
            if not row or row[0]!='ready' or not path.is_file(): raise ValidationError('Prepared audio is not available')
            return path.open('rb'),path.stat().st_size
