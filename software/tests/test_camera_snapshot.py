"""App camera reads reuse captured data with the correct sample timestamp."""
from types import SimpleNamespace

import pytest

from robot import camera


class Frame:
    ndim = 3

    def __init__(self, width=1280, height=720, tag="first"):
        self.shape = (height, width, 3)
        self.tag = tag

    def copy(self):
        return Frame(self.shape[1], self.shape[0], self.tag)


class Encoder:
    IMWRITE_JPEG_QUALITY = 1
    INTER_AREA = 2

    def __init__(self):
        self.calls = []
        self.before_encode = lambda: None

    def resize(self, frame, size, interpolation=None):
        return Frame(size[0], size[1], frame.tag)

    def imencode(self, extension, frame, options):
        self.calls.append((extension, frame.shape, options))
        self.before_encode()
        data = frame.tag.encode()
        return True, SimpleNamespace(nbytes=len(data), tobytes=lambda: data)


@pytest.fixture
def subject(monkeypatch):
    clock = [100.]
    encoder = Encoder()
    monkeypatch.setattr(camera, "cv2", encoder)
    monkeypatch.setattr(camera, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(camera.settings, "camera_enabled", True)
    service = camera.CameraService()
    service._running = True
    service._capture_array = lambda: pytest.fail("App snapshot must never read hardware")
    service._store_frame(Frame())
    return service, encoder, clock


def test_snapshot_is_bounded_cached_and_returns_independent_metadata(subject):
    service, encoder, clock = subject
    first = service.get_image_snapshot()
    assert first == {"ok": True, "data": b"first", "media_type": "image/jpeg", "width": 640,
                     "height": 360, "age_s": 0., "captured_monotonic": 100.}
    first["width"] = 9999
    first["data"] = b"caller mutation"
    clock[0] += .1
    second = service.get_image_snapshot()
    assert second["width"] == 640 and second["data"] == b"first"
    assert second["age_s"] == pytest.approx(.1)
    assert len(encoder.calls) == 1
    for quality in range(40, 55):
        assert service.get_image_snapshot(quality=quality)["ok"]
    assert len(service._jpeg_cache) <= 4


def test_new_capture_during_encoding_keeps_original_copied_frame_timestamp(subject):
    service, encoder, clock = subject

    def publish_new():
        clock[0] += .1
        service._store_frame(Frame(tag="second"))

    encoder.before_encode = publish_new
    result = service.get_image_snapshot()
    assert result["data"] == b"first"
    assert result["captured_monotonic"] == 100.
    assert result["age_s"] == pytest.approx(.1)
    assert not service._jpeg_cache
    encoder.before_encode = lambda: None
    newest = service.get_image_snapshot()
    assert newest["data"] == b"second"
    assert newest["captured_monotonic"] == pytest.approx(100.1)


def test_fresh_new_frame_cannot_hide_old_encoded_copy_expiring(subject):
    service, encoder, clock = subject

    def slow_encode():
        clock[0] += 3
        service._store_frame(Frame(tag="newer"))

    encoder.before_encode = slow_encode
    result = service.get_image_snapshot()
    assert result["ok"] is False and result["state"] == "stale"
    assert "data" not in result
    assert not service._jpeg_cache


def test_stale_cache_is_rejected_without_reencode_or_hardware_read(subject):
    service, encoder, clock = subject
    assert service.get_image_snapshot()["ok"]
    clock[0] += 2.1
    result = service.get_image_snapshot()
    assert result["ok"] is False and result["state"] == "stale"
    assert len(encoder.calls) == 1


def test_stopped_camera_cannot_return_frame_encoded_during_shutdown(subject):
    service, encoder, _ = subject
    encoder.before_encode = service._clear_frame
    result = service.get_image_snapshot()
    assert result["ok"] is False and result["state"] == "unavailable"


def test_portrait_snapshot_also_caps_height(subject):
    service, encoder, _ = subject
    service._store_frame(Frame(width=200, height=2000))
    result = service.get_image_snapshot()
    assert (result["width"], result["height"]) == (64, 640)
    assert encoder.calls[-1][1] == (640, 64, 3)


def test_oversized_encoded_buffer_is_rejected_before_another_byte_copy(subject):
    service, encoder, _ = subject
    encoder.imencode = lambda *args: (True, SimpleNamespace(
        nbytes=2 * 1024 * 1024 + 1, tobytes=lambda: pytest.fail("Do not copy oversized output")))
    result = service.get_image_snapshot()
    assert result["ok"] is False and result["state"] == "invalid"
    assert not service._jpeg_cache


def test_oversized_source_is_rejected_before_copy_or_encoding(subject):
    service, encoder, _ = subject
    frame = Frame(width=5000, height=5000)
    frame.copy = lambda: pytest.fail("Do not copy oversized source")
    service._latest_frame = frame
    assert service.get_image_snapshot()["state"] == "invalid"
    assert not encoder.calls


@pytest.mark.parametrize("options", [{"max_width": 641}, {"max_width": True}, {"quality": 95},
                                    {"max_age_s": float("nan")}, {"max_age_s": 0}])
def test_invalid_snapshot_options_are_rejected(subject, options):
    service, encoder, _ = subject
    with pytest.raises(ValueError):
        service.get_image_snapshot(**options)
    assert not encoder.calls


def test_missing_dependencies_and_disabled_camera_are_explicit(subject, monkeypatch):
    service, _, _ = subject
    monkeypatch.setattr(camera, "cv2", None)
    assert service.get_image_snapshot()["state"] == "unavailable"
    monkeypatch.setattr(camera.settings, "camera_enabled", False)
    assert service.get_image_snapshot()["state"] == "disabled"
