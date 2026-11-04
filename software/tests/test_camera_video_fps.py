from types import SimpleNamespace

import pytest

from robot import camera_video


def test_encoder_waits_until_idle_frames_have_drained(monkeypatch):
    # Real failure pattern: six 333 ms frames before 33 ms frames arrive.
    timestamps=[i*333_333_333 for i in range(7)]
    timestamps += [timestamps[-1]+i*33_333_333 for i in range(1,4)]
    captured=[]
    def capture(**kwargs):
        assert 0 < kwargs['wait'] <= 2
        stamp=timestamps.pop(0);captured.append(stamp)
        return {'SensorTimestamp':stamp}
    def start(*args,**kwargs):
        assert not timestamps, 'Encoder started before three confirmed 30 fps intervals'
    output=SimpleNamespace(first=SimpleNamespace(wait=lambda timeout:True),error='')
    recording=camera_video.VideoRecording.__new__(camera_video.VideoRecording)
    recording.camera=SimpleNamespace(capture_metadata=capture,start_encoder=start)
    recording.fps=30;recording.encoder=object();recording.output=output
    recording.start()
    assert len(captured)==10


def test_low_framerate_fails_with_bounded_wait(monkeypatch):
    clock=[0.]
    monkeypatch.setattr(camera_video,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    def capture(**kwargs):
        clock[0]+=.333
        return {'SensorTimestamp':clock[0]*1e9}
    with pytest.raises(RuntimeError,match='did not settle'):
        camera_video.wait_for_framerate(SimpleNamespace(capture_metadata=capture),30,timeout=1)
    assert clock[0]<1.4


def test_missing_frames_dont_queue_retries():
    calls=[]
    def capture(**kwargs):
        calls.append(kwargs)
        raise TimeoutError('No frame')
    with pytest.raises(TimeoutError):
        camera_video.wait_for_framerate(SimpleNamespace(capture_metadata=capture),30)
    assert len(calls)==1


def test_warmup_failure_cleanup_does_not_stop_an_unstarted_encoder():
    recording=camera_video.VideoRecording.__new__(camera_video.VideoRecording)
    recording.closed=False;recording.encoder_started=False
    recording.camera=SimpleNamespace(stop_encoder=lambda *_:pytest.fail('Encoder was never started'))
    stopped=[]
    recording.output=SimpleNamespace(stop=lambda:stopped.append(True),error='')
    recording.stop()
    assert stopped==[True] and recording.closed


@pytest.mark.parametrize('value',[None,'123',float('nan')])
def test_bad_timestamp_cannot_confirm_rate(value):
    with pytest.raises(RuntimeError,match='timing is unavailable'):
        camera_video.wait_for_framerate(SimpleNamespace(capture_metadata=lambda **kw:{'SensorTimestamp':value}),30)
