"""Seven-second camera clips, using the shared hardware encoder and take store."""
import threading
import time
import uuid

from flask import Blueprint, jsonify

from .errors import BusyError


class VideoCapture:
    DURATION = 7
    COLLECTION = 'dashboard-video-capture'

    def __init__(self, runtime):
        self.runtime = runtime
        self.videos = runtime.filming.library.videos
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.current = {'state':'idle'}

    def status(self):
        with self.lock:
            current = dict(self.current)
        if current.get('started'):
            current['remaining_s'] = round(max(0, self.DURATION-(time.monotonic()-current['started'])),1)
        current.pop('started',None)
        return {'current':current, 'duration_s':self.DURATION,
                'directory':str(self.runtime.filming.library.directory),
                'clips':self.videos.listing(self.COLLECTION)}

    def start(self):
        with self.lock:
            if self.current['state'] in {'starting','recording','saving'}:
                raise BusyError('A video is already recording. Wait for it to finish.')
            controller = self.runtime.controller()
            controller.camera.video_ready()
            self.videos.preflight()
            token = controller._action_token()[1]
            self.cancel = threading.Event()
            self.current = {'state':'starting'}
            try:
                threading.Thread(target=self._record,args=(controller.camera,token,self.cancel),
                                 name='video-capture',daemon=True).start()
            except Exception:
                self.current = {'state':'failed','error':'Could not start recording worker'}
                raise
        return {'ok':True}

    def stop(self):
        with self.lock:
            self.cancel.set()
        return {'ok':True}

    def _record(self,camera,token,cancel):
        video = identifier = began = None
        error = ''
        outcome = 'completed'
        metadata = None
        try:
            identifier,path = self.videos.begin(self.COLLECTION,uuid.uuid4().hex)
            if token.is_set() or cancel.is_set():
                raise RuntimeError('Recording cancelled before the camera started')
            video = camera.start_video(path)
            began = time.monotonic()
            with self.lock:
                self.current = {'state':'recording','id':identifier,'started':began}
            while time.monotonic()-began < self.DURATION:
                if token.is_set() or cancel.is_set():
                    outcome = 'cancelled'
                    break
                video.check()
                cancel.wait(min(.1,max(0,self.DURATION-(time.monotonic()-began))))
        except Exception as exc:
            error = str(exc)[:500]
        finally:
            with self.lock:
                self.current = {'state':'saving','id':identifier}
            try:
                if video:
                    camera.stop_video(video)
                    metadata = video.metadata()
            except Exception as exc:
                error = error or str(exc)[:500]
            try:
                if identifier:
                    result = self.videos.finish(identifier,
                        duration_ms=round((time.monotonic()-began)*1000) if began else 0,
                        outcome=outcome,error=error,metadata=metadata)
                    error = error or result.get('error','')
            except Exception as exc:
                error = error or str(exc)[:500]
            with self.lock:
                self.current = {'state':'failed' if error else 'saved','id':identifier,
                                'error':error,'outcome':outcome}


def blueprint(runtime):
    service = VideoCapture(runtime)
    bp = Blueprint('video_capture',__name__,url_prefix='/api/video-capture')

    @bp.get('')
    def status():return jsonify(service.status())

    @bp.post('')
    def start():return jsonify(service.start()),202

    @bp.post('/stop')
    def stop():return jsonify(service.stop())

    @bp.delete('/<identifier>')
    def delete(identifier):
        service.videos.delete(service.COLLECTION,identifier)
        return jsonify(ok=True)

    return bp
