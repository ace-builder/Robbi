import threading
import time
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError
from robbi.filming.library import Library
from robbi.video_capture import VideoCapture


class Camera:
    stopped=False
    broken=False
    def video_ready(self):pass
    def start_video(self,path):
        path.write_bytes(b'fake-video')
        return self
    def check(self):
        if self.broken:raise RuntimeError('Camera disconnected')
    def stop_video(self,video):self.stopped=True
    def metadata(self):return {'width':1920,'height':1080,'audio':False}


def setup(tmp_path):
    camera=Camera();token=threading.Event()
    library=Library(tmp_path)
    # Initialize durable schema before its disk preflight.
    library.videos.listing()
    runtime=SimpleNamespace(filming=SimpleNamespace(library=library),
        controller=lambda:SimpleNamespace(camera=camera,_action_token=lambda:(0,token)))
    service=VideoCapture(runtime)
    return service,camera,token


def finished(service):
    deadline=time.monotonic()+2
    while service.status()['current']['state'] in {'starting','recording','saving'}:
        assert time.monotonic()<deadline
        time.sleep(.01)
    return service.status()


def test_completed_persistent_hd_clip_and_single_worker(tmp_path):
    service,camera,token=setup(tmp_path)
    assert service.DURATION==7
    service.DURATION=.1
    service.start()
    with pytest.raises(BusyError):service.start()
    result=finished(service)
    assert camera.stopped and result['current']['state']=='saved'
    assert result['clips'][0]['duration_ms']>=100
    assert result['clips'][0]['metadata']['width']==1920
    assert VideoCapture(service.runtime).status()['clips'][0]['video_url']


def test_camera_failure_closes_encoder_and_reports_failure(tmp_path):
    service,camera,token=setup(tmp_path);camera.broken=True
    service.start();result=finished(service)
    assert camera.stopped
    assert result['current']['state']=='failed'
    assert result['clips'][0]['error']=='Camera disconnected'


def test_stop_saves_short_clip_without_waiting_seven_seconds(tmp_path):
    service,camera,token=setup(tmp_path)
    service.start()
    deadline=time.monotonic()+1
    while service.status()['current']['state']=='starting':
        assert time.monotonic()<deadline
        time.sleep(.005)
    token.set()
    result=finished(service)
    assert camera.stopped and result['current']['outcome']=='cancelled'
    assert result['clips'][0]['duration_ms']<1000
