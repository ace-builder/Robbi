from pathlib import Path
from types import SimpleNamespace
import threading
import pytest
from robbi.audio.wake_training import dataset_stamp, prepare, WakeTraining
from robbi.errors import ValidationError, BusyError
from robot.config import settings


@pytest.mark.parametrize("rate,ends", [(16000,[6400,12800,32000]),(8000,[3200,6400,16000]),(16000,[6501,12803,32005])])
def test_cached_frames_match_live_window_preemphasis_and_padding(rate,ends):
    import numpy as np
    from robot.local_wake import LocalWakeDetector
    from robbi.audio.wake_math import normalize_rows
    from robbi.audio.wake_training import feature_windows
    detector = LocalWakeDetector.__new__(LocalWakeDetector)
    detector._sample_rate = rate;detector._buffer_size_samples = round(rate * 1.8)
    audio = np.random.default_rng(47).normal(0,.08,rate*3).astype(np.float32)
    padded = np.concatenate((np.zeros(detector._buffer_size_samples,dtype=np.float32),audio))
    cached = list(feature_windows(detector,audio,ends))
    for end,actual in zip(ends,cached):
        expected=normalize_rows(detector._extract_mfcc_features(padded[end:end+detector._buffer_size_samples]))
        np.testing.assert_allclose(actual,expected,atol=1e-6)


def dataset(tmp_path):
    dirs = {c: tmp_path / c for c in ("positive", "negative", "noise")}
    for directory in dirs.values():
        directory.mkdir()
    return SimpleNamespace(CATEGORY_DIRS=dirs)


def test_fingerprint_binds_recordings_and_runtime_cadence(tmp_path, monkeypatch):
    data = dataset(tmp_path)
    path = data.CATEGORY_DIRS["positive"] / "one.wav"
    path.write_bytes(b"a" * 64)
    before = dataset_stamp(data, settings)[0]
    path.write_bytes(b"b" * 64)
    assert dataset_stamp(data, settings)[0] != before
    before = dataset_stamp(data, settings)[0]
    monkeypatch.setattr(settings, "voice_chunk_ms", 80)
    assert dataset_stamp(data, settings)[0] != before


def test_oversize_and_missing_training_examples_fail_before_compute(tmp_path):
    data = dataset(tmp_path)
    with pytest.raises(ValidationError, match="at least"):
        prepare(data, settings, threading.Event())
    (data.CATEGORY_DIRS["noise"] / "large.wav").write_bytes(b"x" * (512 * 1024 + 1))
    with pytest.raises(ValidationError, match="512 KiB"):
        dataset_stamp(data, settings)


def test_only_one_preparation_can_run_and_cancellation_discards_result(monkeypatch):
    import robbi.audio.wake_training as module
    started, release = threading.Event(), threading.Event()
    def work(*args):
        started.set();release.wait(2);return {"prepare_seconds": .1}
    monkeypatch.setattr(module, "prepare", work)
    subject = WakeTraining()
    job = subject.start(None, None)
    assert started.wait(1)
    with pytest.raises(BusyError):
        subject.start(None, None)
    subject.cancelled.set();release.set()
    import time
    until = time.monotonic() + 2
    while subject.get(job["id"])["state"] == "running" and time.monotonic() < until:
        time.sleep(.01)
    assert subject.result is None and subject.get(job["id"])["state"] == "cancelled"


def test_apply_rejects_changed_dataset_before_touching_voice_settings(tmp_path,monkeypatch):
    from flask import Flask
    from robbi.audio import wake_training_web as module
    from robbi.web import install_error_handlers
    from unittest.mock import Mock
    from robot import local_wake
    training = SimpleNamespace(lock=threading.RLock(), get=lambda *a,**k:{"fingerprint":"old", "references":[]})
    monkeypatch.setattr(module,'WakeTraining',lambda:training)
    monkeypatch.setattr(module,'dataset_stamp',lambda *a:('new',[]))
    controller=Mock()
    config=SimpleNamespace(root=tmp_path,data_dir=tmp_path/'data',values={'server':{'auth_enabled':False}})
    runtime=SimpleNamespace(config=config,controller=lambda:controller)
    app=Flask(__name__);install_error_handlers(app)
    app.config.update(ROBBI_CONFIG_LOCK=threading.RLock(),ROBBI_RUNTIME_CONFIG=config)
    app.register_blueprint(module.blueprint(runtime))
    response=app.test_client().post('/api/wake-training/test/apply',json={'threshold':.2,'fingerprint':'old'})
    assert response.status_code==400 and 'changed' in response.json['error']
    controller.apply_settings.assert_not_called()
