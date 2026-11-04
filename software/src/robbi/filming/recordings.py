"""Bounded durable camera takes, independently removable from shot definitions."""
import shutil
import json
import time
import uuid

from ..errors import UnavailableError, ValidationError


class Recordings:
    def __init__(self, library):
        self.library = library

    def preflight(self):
        with self.library.db() as db:
            count, size = db.execute('SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM videos').fetchone()
        if count >= 300 or size > (2048-480)*1024*1024:
            raise ValidationError('Video library is full (300 takes / 2 GiB). Delete old recordings.')
        if shutil.disk_usage(self.library.directory).free < 512*1024*1024:
            raise UnavailableError('At least 512 MiB free storage is required for a 1080p camera take')

    def begin(self, shot_id, run_id):
        self.preflight()
        identifier = uuid.uuid4().hex
        with self.library.db() as db:
            db.execute('INSERT INTO videos(id,shot_id,run_id,status,created) VALUES (?,?,?,?,?)',
                       (identifier,shot_id,run_id,'recording',time.time()))
        return identifier, self.library.path(identifier,'.mp4.part')

    def finish(self, identifier, *, duration_ms=0, outcome='completed', error='', metadata=None):
        part = self.library.path(identifier,'.mp4.part')
        path = self.library.path(identifier,'.mp4')
        if part.is_file(): part.replace(path)
        size = path.stat().st_size if path.exists() else 0
        if not size and not error: error = 'No camera frames were saved'
        with self.library.db() as db:
            db.execute('UPDATE videos SET status=?,duration_ms=?,bytes=?,error=?,outcome=?,metadata=? WHERE id=?',
                       ('failed' if error else 'ready',duration_ms,size,str(error)[:500],outcome,json.dumps(metadata or {}),identifier))
        return next(v for v in self.listing() if v['id']==identifier)

    def listing(self, shot_id=None):
        with self.library.db() as db:
            rows = db.execute('SELECT * FROM videos '+('WHERE shot_id=? ' if shot_id else '')+'ORDER BY created DESC',
                              (shot_id,) if shot_id else ()).fetchall()
        result=[]
        for row in rows:
            value=dict(row)
            value['metadata']=json.loads(value['metadata'])
            value['video_url']='/api/filming/video/'+row['id'] if row['status']!='recording' and self.library.path(row['id'],'.mp4').is_file() else None
            result.append(value)
        return result

    def delete(self, shot_id, identifier):
        self.library.path(identifier)
        with self.library.db() as db:
            row=db.execute('SELECT status FROM videos WHERE id=? AND shot_id=?',(identifier,shot_id)).fetchone()
            if not row: raise ValidationError('Recording not found')
            if row['status']=='recording': raise ValidationError('Stop recording before deleting a take')
            db.execute('DELETE FROM videos WHERE id=?',(identifier,))
        self.library.path(identifier,'.mp4').unlink(missing_ok=True)
        self.library.path(identifier,'.mp4.part').unlink(missing_ok=True)

    def file(self, identifier):
        path=self.library.path(identifier,'.mp4')
        with self.library.db() as db:
            row=db.execute('SELECT status FROM videos WHERE id=?',(identifier,)).fetchone()
            if not row or row[0]=='recording' or not path.is_file(): raise ValidationError('Video is not available')
            return path.open('rb'),path.stat().st_size
