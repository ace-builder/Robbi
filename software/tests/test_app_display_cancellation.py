import threading
from types import SimpleNamespace

from PIL import Image

from robot.media import Display


def test_stop_while_waiting_for_display_lock_prevents_stale_spi_write(tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (280, 240), "white").save(image)
    writes = []
    display = Display()
    display._disp = SimpleNamespace(display=lambda frame: writes.append(frame))
    cancelled = threading.Event()
    entered = threading.Event()
    result = []
    display._lock.acquire()
    def work():
        entered.set()
        result.append(display.show_image(image, cancel_event=cancelled))
    worker = threading.Thread(target=work)
    try:
        worker.start()
        assert entered.wait(1)
        cancelled.set()
    finally:
        display._lock.release()
        worker.join(1)
    assert not worker.is_alive()
    assert result == [False]
    assert not writes
    assert not display._faces


def test_stop_during_decode_prevents_spi_write(tmp_path, monkeypatch):
    image = tmp_path / "image.png"
    Image.new("RGB", (280, 240), "white").save(image)
    writes = []
    display = Display()
    display._disp = SimpleNamespace(display=lambda frame: writes.append(frame))
    cancelled = threading.Event()
    from robot import media
    pad = media.ImageOps.pad
    def cancel_on_decode(*args, **kwargs):
        result = pad(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(media.ImageOps, "pad", cancel_on_decode)
    assert not display.show_image(image, cancel_event=cancelled)
    assert not writes


def test_existing_display_callers_need_no_cancellation_argument(tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (20, 20), "white").save(image)
    writes = []
    display = Display()
    display._disp = SimpleNamespace(display=lambda frame: writes.append(frame))
    assert display.show_image(image)
    assert writes[0].size == (280, 240)
