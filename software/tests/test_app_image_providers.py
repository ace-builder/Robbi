import base64
from concurrent.futures import CancelledError
import io
import json
import time

from PIL import Image
import pytest

from robbi.app_services.http import HttpResponse, ProviderError
from robbi.app_services.images import ImageService
from robbi.errors import ValidationError


def image_data(size=(20, 20)):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format="PNG")
    return output.getvalue()


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return HttpResponse(200, {}, result if isinstance(result, bytes) else json.dumps(result).encode())


def test_openai_generates_exactly_one_small_image_without_legacy_parameter():
    data = image_data()
    http = FakeHttp({"data": [{"b64_json": base64.b64encode(data).decode()}]})
    result = ImageService(http).generate({"provider": "openai", "model": "gpt-image-1-mini", "api_key": "secret"}, "diagram")
    assert result.data == data and result.media_type == "image/png"
    assert result.width == result.height == 20
    body = http.calls[0][2]["json_body"]
    assert body["n"] == 1 and body["quality"] == "low"
    assert "response_format" not in body
    assert http.calls[0][2]["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize("provider,response,path", [
    ("google", {"steps": [{"type": "model_output", "content": [{"type": "image", "data": "IMAGE"}]}]}, "/interactions"),
    ("google_generate_content", {"candidates": [{"content": {"parts": [{"inlineData": {"data": "IMAGE"}}]}}]}, ":generateContent"),
])
def test_google_current_and_legacy_contracts(provider, response, path):
    encoded = base64.b64encode(image_data()).decode()
    response = json.loads(json.dumps(response).replace("IMAGE", encoded))
    http = FakeHttp(response)
    result = ImageService(http).generate({"provider": provider, "model": "configured-image-model", "api_key": "secret"}, "diagram")
    assert result.width == 20
    assert http.calls[0][1].endswith(path)
    assert http.calls[0][2]["headers"] == {"x-goog-api-key": "secret"}


def test_bfl_uses_returned_regional_poll_url_and_drops_key_for_delivery():
    http = FakeHttp({"id": "1", "polling_url": "https://api.eu.bfl.ai/v1/get_result?id=1"},
                    {"status": "Ready", "result": {"sample": "https://delivery.eu.bfl.ai/image?signature=secret"}}, image_data())
    result = ImageService(http).generate({"provider": "bfl", "model": "flux-2-pro", "api_key": "secret", "poll_interval_s": 0.2}, "image")
    assert result.width == 20
    assert http.calls[1][1].startswith("https://api.eu.bfl.ai/")
    assert http.calls[1][2]["headers"]["x-key"] == "secret"
    assert http.calls[2][2]["headers"] == {"Accept": "image/*"}


def test_replicate_poll_and_single_url_output():
    http = FakeHttp({"status": "starting", "urls": {"get": "https://api.replicate.com/v1/predictions/abc"}},
                    {"status": "succeeded", "output": ["https://replicate.delivery/example.png"]}, image_data())
    result = ImageService(http).generate({"provider": "replicate", "model": "owner/model", "api_key": "secret", "poll_interval_s": 0.2}, "image")
    assert result.provider == "replicate"
    assert "Cancel-After" in http.calls[0][2]["headers"]


def test_poll_url_cannot_steal_credentials():
    http = FakeHttp({"status": "starting", "urls": {"get": "https://attacker.test/steal"}})
    with pytest.raises(ProviderError, match="outside"):
        ImageService(http).generate({"provider": "replicate", "model": "owner/model", "api_key": "secret"}, "image")
    assert len(http.calls) == 1


def test_cancellation_stops_polling_without_repeated_paid_submission():
    http = FakeHttp({"status": "starting", "urls": {"get": "https://api.replicate.com/v1/predictions/abc"}})
    with pytest.raises(CancelledError):
        ImageService(http).generate({"provider": "replicate", "model": "owner/model", "api_key": "secret"}, "image", cancellation=lambda: len(http.calls) > 0)
    assert len(http.calls) == 1


def test_deadline_releases_poll_waiter():
    http = FakeHttp({"status": "starting", "urls": {"get": "https://api.replicate.com/v1/predictions/abc"}})
    with pytest.raises(TimeoutError):
        ImageService(http).generate({"provider": "replicate", "model": "owner/model", "api_key": "secret"}, "image", deadline=time.monotonic() + 0.02)
    assert len(http.calls) == 1


def test_generic_binary_and_json_template():
    http = FakeHttp(image_data())
    result = ImageService(http).generate({"provider": "generic", "endpoint": "http://localhost:8888/image", "allow_http": True,
                                         "response_encoding": "binary", "request_template": {"input": {"text": "Draw {prompt}"}}}, "a dog")
    assert result.width == 20
    assert http.calls[0][2]["json_body"] == {"input": {"text": "Draw a dog"}}


def test_generic_url_mapping():
    http = FakeHttp({"output": {"image": "https://cdn.test/image"}}, image_data())
    result = ImageService(http).generate({"provider": "generic", "endpoint": "https://custom.test/image", "response_encoding": "url", "response_path": "output.image"}, "a dog")
    assert result.width == 20


@pytest.mark.parametrize("body", ["not-base64", base64.b64encode(b"not an image").decode()])
def test_malformed_provider_images_fail_without_display(body):
    with pytest.raises(ProviderError):
        ImageService(FakeHttp({"data": [{"b64_json": body}]})).generate({"model": "test", "api_key": "secret"}, "image")


def test_pixel_budget_checked_before_full_decode():
    http = FakeHttp({"data": [{"b64_json": base64.b64encode(image_data((100, 100))).decode()}]})
    with pytest.raises(ProviderError, match="pixel limit"):
        ImageService(http).generate({"model": "test", "api_key": "secret", "max_pixels": 1024}, "image")


def test_missing_key_model_and_unknown_provider_fail_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    http = FakeHttp()
    for config in ({"model": "test"}, {"api_key": "secret"}, {"provider": "unknown"}):
        with pytest.raises(ValidationError):
            ImageService(http).generate(config, "image")
    assert http.calls == []
