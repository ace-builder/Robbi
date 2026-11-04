from concurrent.futures import CancelledError
import gzip
import io
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.parse import parse_qs

import pytest

from robbi.app_services.http import HttpClient, HttpResponse, ProviderError, _SafeRedirects, checked_url
from robbi.errors import ValidationError


class Response(io.BytesIO):
    def __init__(self, body=b"{}", headers=None, status=200):
        super().__init__(body)
        self.headers = headers or {}
        self.status = status


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def test_compressed_response_is_bounded_after_inflation():
    opener = Opener(Response(gzip.compress(b"A" * 10000), {"Content-Encoding": "gzip"}))
    with pytest.raises(ProviderError, match="decompressed"):
        HttpClient(opener=opener).request("GET", "https://example.test", max_bytes=1000)
    assert opener.response.closed


def test_gzip_json_and_request_headers():
    opener = Opener(Response(gzip.compress(b'{"answer":42}'), {"Content-Encoding": "gzip"}))
    response = HttpClient(opener=opener).request("POST", "https://example.test", json_body={"prompt": "a cat"})
    assert response.json() == {"answer": 42}
    request, timeout = opener.calls[0]
    assert b'a cat' in request.data
    assert request.get_header("Accept-encoding") == "gzip, deflate"
    assert timeout <= 20


def test_oauth_form_encoding_preserves_reserved_characters():
    opener = Opener()
    fields = {"client_id": "client", "refresh_token": "a+b&c=ä", "grant_type": "refresh_token"}
    HttpClient(opener=opener).request("POST", "https://example.test/token", form_body=fields)
    request, _ = opener.calls[0]
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert parse_qs(request.data.decode()) == {k: [v] for k, v in fields.items()}


@pytest.mark.parametrize("options", [
    {"json_body": {}, "form_body": {}}, {"form_body": {"token": ["invalid"]}},
    {"form_body": {"token": "ä" * 50000}}, {"form_body": {"token": "x" * 300000}},
])
def test_invalid_form_requests_are_rejected_before_network(options):
    opener = Opener()
    with pytest.raises(ValidationError):
        HttpClient(opener=opener).request("POST", "https://example.test/token", **options)
    assert not opener.calls


@pytest.mark.parametrize("headers,body", [({"Content-Length": "101"}, b""), ({}, b"X" * 101)])
def test_content_length_and_actual_stream_bounded(headers, body):
    with pytest.raises(ProviderError, match="byte limit"):
        HttpClient(opener=Opener(Response(body, headers))).request("GET", "https://example.test", max_bytes=100)


def test_cancellation_and_deadline_prevent_any_network_request():
    opener = Opener()
    with pytest.raises(CancelledError):
        HttpClient(opener=opener).request("GET", "https://example.test", cancellation=lambda: True)
    with pytest.raises(TimeoutError):
        HttpClient(opener=opener).request("GET", "https://example.test", deadline=time.monotonic() - 1)
    assert not opener.calls


def test_cancellation_during_response_discards_result():
    cancelled = False
    class Stream(Response):
        def read1(self, size):
            nonlocal cancelled
            cancelled = True
            return super().read1(size)
    with pytest.raises(CancelledError):
        HttpClient(opener=Opener(Stream(b"data"))).request("GET", "https://example.test", cancellation=lambda: cancelled)


def test_post_failure_is_not_retried_and_never_exposes_key():
    opener = Opener(error=HTTPError("https://example.test?key=SECRET", 429, "SECRET", {}, None))
    with pytest.raises(ProviderError) as failure:
        HttpClient(opener=opener).request("POST", "https://example.test", json_body={})
    assert "SECRET" not in str(failure.value)
    assert "rate limit" in str(failure.value)
    assert len(opener.calls) == 1


def test_connection_failure_is_actionable_and_sanitized():
    with pytest.raises(ProviderError, match="Wi-Fi") as failure:
        HttpClient(opener=Opener(error=URLError("SECRET"))).request("GET", "https://example.test")
    assert "SECRET" not in str(failure.value)


def test_cross_origin_redirect_strips_all_secret_headers():
    source = Request("https://api.example.test/a", headers={"Authorization": "SECRET", "X-key": "SECRET2",
                                                             "Custom-Secret": "SECRET3", "Accept": "image/*"})
    redirected = _SafeRedirects().redirect_request(source, None, 302, "", {}, "https://cdn.example.test/b")
    assert redirected.headers == {"Accept": "image/*"}
    assert not redirected.unredirected_hdrs
    with pytest.raises(ProviderError, match="submission"):
        _SafeRedirects().redirect_request(Request("https://example.test", data=b"{}"), None, 302, "", {}, "https://other.test")
    with pytest.raises(ValidationError):
        _SafeRedirects().redirect_request(source, None, 302, "", {}, "http://other.test")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@api.example.test", "http://localhost/x", "https://example.test/#fragment", "https://example.test:999999/"])
def test_bad_urls_rejected(url):
    with pytest.raises(ValidationError):
        checked_url(url)


def test_explicit_local_http_and_invalid_json():
    assert checked_url("http://localhost:9090/image", allow_http=True)
    with pytest.raises(ProviderError, match="invalid JSON"):
        HttpResponse(200, {}, b"oops").json()


@pytest.mark.parametrize("body", [b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}', b'{"value":1e400}'])
def test_nonfinite_provider_json_is_rejected(body):
    with pytest.raises(ProviderError, match="invalid JSON"):
        HttpResponse(200, {}, body).json()


@pytest.mark.parametrize("read_size", [1, 19, 65536])
def test_concatenated_gzip_members_are_read_completely(read_size):
    class ChunkedResponse(Response):
        def read1(self, size):
            return super().read1(min(size, read_size))

    body = gzip.compress(b'{"answer":') + gzip.compress(b'42}')
    response = HttpClient(opener=Opener(ChunkedResponse(body, {"Content-Encoding": "gzip"}))).request("GET", "https://example.test")
    assert response.json() == {"answer": 42}


def test_concatenated_gzip_members_share_the_decompression_limit():
    body = gzip.compress(b"a" * 600) + gzip.compress(b"b" * 600)
    with pytest.raises(ProviderError, match="decompressed"):
        HttpClient(opener=Opener(Response(body, {"Content-Encoding": "gzip"}))).request("GET", "https://example.test", max_bytes=1000)


def test_truncated_identity_body_is_not_returned_as_success():
    with pytest.raises(ProviderError, match="truncated"):
        HttpClient(opener=Opener(Response(b"{}", {"Content-Length": "20"}))).request("GET", "https://example.test")


def test_truncated_second_gzip_member_is_rejected():
    body = gzip.compress(b"first") + gzip.compress(b"second")[:-4]
    with pytest.raises(ProviderError, match="truncated"):
        HttpClient(opener=Opener(Response(body, {"Content-Encoding": "gzip"}))).request("GET", "https://example.test")


def test_deflate_response_and_unexpected_trailing_bytes():
    compressed = zlib.compress(b'{"answer":42}')
    response = HttpClient(opener=Opener(Response(compressed, {"Content-Encoding": "deflate"}))).request("GET", "https://example.test")
    assert response.json() == {"answer": 42}
    with pytest.raises(ProviderError, match="trailing"):
        HttpClient(opener=Opener(Response(compressed + b"unexpected", {"Content-Encoding": "deflate"}))).request("GET", "https://example.test")


def test_head_metadata_does_not_require_or_decode_a_response_body():
    response = HttpClient(opener=Opener(Response(b"", {"Content-Length": "10000", "Content-Encoding": "gzip"}))).request(
        "HEAD", "https://example.test", max_bytes=100)
    assert response.body == b"" and response.headers["content-length"] == "10000"


@pytest.mark.parametrize("timeout", [True, 10 ** 500])
def test_invalid_numeric_options_are_rejected_without_request(timeout):
    opener = Opener()
    with pytest.raises(ValidationError):
        HttpClient(opener=opener).request("GET", "https://example.test", timeout_s=timeout)
    assert not opener.calls

