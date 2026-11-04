"""Exercise bundled project loading, providers, SDK and storage without HTTP I/O."""
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.sdk import AppCancelled
from robbi.app_services.http import HttpClient, HttpResponse
from robbi.app_services.images import ImageService
from robbi.app_services.weather import WeatherService


ROOT = Path(__file__).resolve().parents[1]


class Robot:
    def __init__(self, display_available=True):
        self.cancelled = threading.Event()
        self.display_available = display_available
        self.shown = []

    def capture_token(self):
        return 0, self.cancelled

    def show_image(self, path, token, cancelled, *, hold_s=60):
        if cancelled() or token[1].is_set():
            raise AppCancelled("cancelled")
        self.shown.append(path)
        return {"displayed": self.display_available, "display_hold_s": hold_s,
                "reason": "" if self.display_available else "Display is unavailable; the generated preview is still saved"}


class FakeHTTP:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.respond(method, url, **kwargs)


def response(value):
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(value).encode("utf-8"))


def generated_png(size=(1024, 600)):
    stream = io.BytesIO()
    Image.new("RGB", size, "black").save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def make_host(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        pytest.fail("A bundled app tried to make a real external request")

    monkeypatch.setattr(HttpClient, "request", forbid_network)
    monkeypatch.setenv("ROBBI_OFFLINE_IMAGE_KEY", "offline-fake-key")
    created = []

    def make(services, display_available=True):
        host = AppHost(ROOT, tmp_path / str(len(created)), Robot(display_available), services=services, permissions_enabled=False)
        created.append(host)
        assert host.scan()["errors"] == []
        return host

    yield make
    for host in created:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def configure_image(host):
    host.configure("image-studio", {"provider": "openai", "model": "offline-image-model", "api_key_env": "ROBBI_OFFLINE_IMAGE_KEY"})


def test_saved_image_can_be_shown_without_generating_again_or_configuring_provider(make_host):
    host = make_host({})
    artifact = host.artifacts("image-studio").save(generated_png(), "Saved image", lambda: None)
    result = host.call("image-studio", "show", {"artifact_id": artifact["id"]})
    assert result["ok"] is True
    assert result["data"]["displayed"] is True
    assert result["data"]["display_hold_s"] == 10
    assert host.robot.shown == [host.artifacts("image-studio").path(artifact["id"])]


@pytest.mark.parametrize("display_available,show_on_screen", [(True, True), (False, True), (True, False)],
                         ids=["shown", "missing-display", "preview-only"])
def test_bundled_image_provider_normalizes_and_retains_preview(make_host, display_available, show_on_screen):
    pixels = generated_png()
    transport = FakeHTTP(lambda *args, **kwargs: response({"data": [{"b64_json": base64.b64encode(pixels).decode("ascii")}]}))
    host = make_host({"images": ImageService(transport)}, display_available)
    configure_image(host)

    result = host.call("image-studio", "generate", {"prompt": "A simple diagram of a plant cell", "show_on_screen": show_on_screen})

    assert result["ok"] is True
    assert result["data"]["provider"] == "openai"
    assert result["data"]["displayed"] is (display_available and show_on_screen)
    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert (artifact["width"], artifact["height"], artifact["mime"]) == (280, 240, "image/png")
    path = host.artifacts("image-studio").path(artifact["id"])
    with Image.open(path) as saved:
        assert saved.size == (280, 240)
        assert saved.mode == "RGB"
        assert saved.getpixel((140, 0)) == (255, 255, 255)  # Fits the landscape image with white padding.
        assert saved.getpixel((140, 120)) == (0, 0, 0)
    assert host.robot.shown == ([path] if show_on_screen else [])
    assert len(transport.calls) == 1
    method, url, options = transport.calls[0]
    assert method == "POST" and url == "https://api.openai.com/v1/images/generations"
    assert options["json_body"]["prompt"] == "A simple diagram of a plant cell"
    assert options["json_body"]["n"] == 1
    assert options["headers"]["Authorization"] == "Bearer offline-fake-key"
    assert "offline-fake-key" not in json.dumps(result)
    if not display_available:
        assert "Display is unavailable" in result["text"]


def test_bundled_weather_uses_city_local_tomorrow_and_cached_second_request(make_host):
    now = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)
    day = "2026-10-13"  # Berlin is already on October 12 at the supplied UTC time.

    def respond(method, url, **kwargs):
        assert method == "GET"
        if urlsplit(url).hostname == "geocoding-api.open-meteo.com":
            return response({"results": [{"name": "Berlin", "country": "Germany", "country_code": "DE",
                                           "latitude": 52.52, "longitude": 13.405, "timezone": "Europe/Berlin"}]})
        assert urlsplit(url).hostname == "api.open-meteo.com"
        query = parse_qs(urlsplit(url).query)
        assert query["start_date"] == query["end_date"] == [day]
        assert query["timezone"] == ["Europe/Berlin"]
        return response({"daily": {
            "time": [day], "weather_code": [61], "temperature_2m_min": [9.2], "temperature_2m_max": [16.8],
            "precipitation_sum": [2.4], "precipitation_probability_max": [70], "wind_speed_10m_max": [18.5],
        }})

    transport = FakeHTTP(respond)
    host = make_host({"weather": WeatherService(transport, now=lambda: now)})
    # A configured home timezone must not relabel an explicitly requested city.
    host.configure("weather", {"default_location": "New York", "timezone": "America/New_York"})
    arguments = {"location": "Berlin", "day": "tomorrow", "country_code": "DE"}
    first = host.call("weather", "forecast", arguments)
    second = host.call("weather", "forecast", arguments)

    assert first["ok"] and second["ok"]
    assert first["data"]["date"] == day
    assert first["data"]["location"]["timezone"] == "Europe/Berlin"
    assert first["data"]["condition"] == "light rain"
    assert first["data"]["temperature_min"] == 9.2
    assert first["data"]["temperature_max"] == 16.8
    assert first["data"]["units"]["temperature"] == "°C"
    assert "Berlin" in first["text"] and "16.8" in first["text"]
    assert first["data"]["cached"] is False
    assert second["data"]["cached"] is True
    assert first["data"]["stale"] is second["data"]["stale"] is False
    assert any(item["name"] == "Open-Meteo" for item in first["data"]["attribution"])
    assert len(transport.calls) == 2  # Geocoding and forecast; both are cached.


def test_bundled_weather_ambiguity_is_returned_without_inventing_forecast(make_host):
    places = [{"name": "Springfield", "country": "United States", "admin1": state, "country_code": "US",
               "latitude": latitude, "longitude": -89, "timezone": "America/Chicago"}
              for state, latitude in (("Illinois", 39.78), ("Missouri", 37.21))]
    transport = FakeHTTP(lambda *args, **kwargs: response({"results": places}))
    host = make_host({"weather": WeatherService(transport)})
    result = host.call("weather", "forecast", {"location": "Springfield", "day": "tomorrow"})
    assert result["ok"] is False
    assert result["data"]["needs_location"] is True
    assert len(result["data"]["choices"]) == 2
    assert "temperature_max" not in result["data"]
    assert len(transport.calls) == 1


def test_cancelled_bundled_generation_discards_late_provider_image_before_storage(make_host):
    entered, release = threading.Event(), threading.Event()
    pixels = generated_png()

    def respond(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        # Simulate an HTTP read that cannot be interrupted until the server replies.
        return response({"data": [{"b64_json": base64.b64encode(pixels).decode("ascii")} ]})

    transport = FakeHTTP(respond)
    host = make_host({"images": ImageService(transport)})
    configure_image(host)
    results = []
    waiter = threading.Thread(target=lambda: results.append(host.call("image-studio", "generate", {"prompt": "A water cycle diagram"})))
    try:
        waiter.start()
        assert entered.wait(1)
        host.cancel_all()
        waiter.join(.5)
        assert not waiter.is_alive()
        assert results[0]["cancelled"]
        assert host.artifacts("image-studio").list() == []
        release.set()
        deadline = time.monotonic() + 2
        while host._active and time.monotonic() < deadline:
            threading.Event().wait(.005)
        assert not host._active
        assert host.artifacts("image-studio").list() == []
        assert host.robot.shown == []
        assert len(transport.calls) == 1  # No automatic replay after cancellation.
        assert next(iter(host._jobs.values()))["result"] is None
    finally:
        release.set()
        waiter.join(3)
