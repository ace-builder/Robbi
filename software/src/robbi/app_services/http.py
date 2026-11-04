"""Small bounded HTTP transport, shared by optional apps without provider SDKs."""
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
import json
from http.client import HTTPException
import math
import re
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, HTTPRedirectHandler, build_opener
import zlib

from robbi.errors import UnavailableError, ValidationError

Cancellation = Callable[[], bool] | None


class ProviderError(UnavailableError):
    """A safe, actionable provider failure without keys or response bodies."""


def checkpoint(cancellation: Cancellation = None, deadline: float | None = None) -> None:
    if cancellation is not None and cancellation():
        raise CancelledError("App request cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("App request deadline exceeded")


def bounded_number(value, default: float, minimum: float, maximum: float) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(default if value is None else value)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError("Expected a numeric configuration value") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValidationError(f"Configuration value must be between {minimum:g} and {maximum:g}")
    return number


def checked_url(url: str, *, allow_http: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise ValidationError("Provider endpoint is not a valid URL")
    try:
        parsed = urlsplit(url)
        valid = bool(parsed.hostname) and not parsed.username and not parsed.password
        parsed.port  # Reject malformed/out-of-range ports before a socket is opened.
    except ValueError:
        valid = False
        parsed = None
    if not valid or parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValidationError("Use an HTTPS provider endpoint (HTTP requires allow_http)")
    if parsed.fragment:
        raise ValidationError("Provider endpoint must not have a URL fragment")
    return url


def query_url(url: str, parameters: dict) -> str:
    parsed = urlsplit(url)
    values = dict(parse_qsl(parsed.query, keep_blank_values=True))
    values.update({key: str(value) for key, value in parameters.items() if value is not None})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(values), ""))


def same_origin(left: str, right: str) -> bool:
    def origin(url):
        p = urlsplit(url)
        return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)
    return origin(left) == origin(right)


class _SafeRedirects(HTTPRedirectHandler):
    # urllib's redirect handler can forward custom API-key headers. Only same
    # origin redirects may retain headers, and POST is never silently replayed.
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        checked_url(newurl, allow_http=urlsplit(request.full_url).scheme == "http")
        if request.get_method() not in {"GET", "HEAD"}:
            raise ProviderError("Provider redirected a submission; configure its final endpoint")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and not same_origin(request.full_url, newurl):
            redirected.headers = {k: v for k, v in redirected.headers.items()
                                  if k.lower() in {"accept", "accept-encoding", "user-agent"}}
            redirected.unredirected_hdrs = {}
        return redirected


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Non-finite JSON number")
            return number

        try:
            return json.loads(self.body, parse_float=finite_float, parse_constant=finite_float)
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("Provider returned invalid JSON") from None


class HttpClient:
    """No retries or background threads; callers own admission and cancellation.

    Cancellation is checked before connecting and between bounded reads. It
    cannot interrupt OS DNS resolution or an already-blocked socket operation;
    the socket timeout still bounds that operation.
    """
    def __init__(self, *, opener=None):
        self._opener = opener or build_opener(_SafeRedirects())

    def request(self, method: str, url: str, *, headers=None, json_body=None, form_body=None,
                timeout_s=20, max_bytes=1024 * 1024, cancellation=None, deadline=None,
                allow_http=False) -> HttpResponse:
        checkpoint(cancellation, deadline)
        checked_url(url, allow_http=allow_http)
        timeout = bounded_number(timeout_s, 20, 0.05, 180)
        if deadline is not None:
            timeout = min(timeout, max(0.01, deadline - time.monotonic()))
        limit = int(bounded_number(max_bytes, 1024 * 1024, 1, 20 * 1024 * 1024))
        request_headers = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate",
                           "User-Agent": "Robbi/2"}
        request_headers.update(headers or {})
        if any(not isinstance(k, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k) for k in request_headers):
            raise ValidationError("Provider header names are invalid")
        if any("\r" in str(v) or "\n" in str(v) for v in request_headers.values()):
            raise ValidationError("Provider headers must not contain line breaks")
        if json_body is not None and form_body is not None:
            raise ValidationError("Choose JSON or form data for a provider request, not both")
        data = None
        if form_body is not None:
            if (not isinstance(form_body, dict) or len(form_body) > 40
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in form_body.items())):
                raise ValidationError("Provider form data must map field names to text")
            if sum(len(k) + len(v) for k, v in form_body.items()) > 256 * 1024:
                raise ValidationError("Provider request exceeds 256 KiB")
            data = urlencode(form_body).encode("utf-8")
            if len(data) > 256 * 1024:
                raise ValidationError("Provider request exceeds 256 KiB")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body is not None:
            try:
                data = json.dumps(json_body, allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (ValueError, TypeError, RecursionError):
                raise ValidationError("Provider request must be valid JSON") from None
            if len(data) > 256 * 1024:
                raise ValidationError("Provider request exceeds 256 KiB")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=request_headers, method=method.upper())
        try:
            try:
                response = self._opener.open(request, timeout=timeout)
            except HTTPError as error:
                if error.code == 304:
                    with error:
                        checkpoint(cancellation, deadline)
                        return HttpResponse(304, {k.lower(): v for k, v in error.headers.items()}, b"")
                error.close()
                hints = {401: "check the API key", 403: "check access and provider configuration",
                         402: "check the provider account credits", 429: "rate limit reached; try later"}
                raise ProviderError(f"Provider HTTP {error.code}: {hints.get(error.code, 'request failed')}") from None
            with response:
                checkpoint(cancellation, deadline)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                length = response_headers.get("content-length", "").strip()
                expected_length = None
                if length:
                    if not re.fullmatch(r"[0-9]+", length):
                        raise ProviderError("Provider returned an invalid content length")
                    if len(length.lstrip("0")) > 20:
                        raise ProviderError("Provider response exceeds the byte limit")
                    expected_length = int(length.lstrip("0") or "0")
                if request.get_method() == "HEAD" or response.status in {204, 304}:
                    return HttpResponse(response.status, response_headers, b"")
                if expected_length is not None and expected_length > limit:
                    raise ProviderError("Provider response exceeds the byte limit")
                encoding = response_headers.get("content-encoding", "identity").lower()
                if encoding not in {"identity", "", "gzip", "deflate"}:
                    raise ProviderError("Provider returned an unsupported content encoding")
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
                body = bytearray()
                wire_size = 0
                reader = getattr(response, "read1", response.read)
                while True:
                    checkpoint(cancellation, deadline)
                    chunk = reader(min(65536, limit + 1 - wire_size))
                    if not chunk:
                        break
                    wire_size += len(chunk)
                    if wire_size > limit:
                        raise ProviderError("Provider response exceeds the byte limit")
                    if decoder is None:
                        body.extend(chunk)
                    else:
                        while chunk:
                            checkpoint(cancellation, deadline)
                            if decoder.eof:
                                if encoding != "gzip":
                                    raise ProviderError("Provider returned trailing compressed data")
                                # A gzip stream may contain multiple members;
                                # they share the same decoded byte budget.
                                decoder = zlib.decompressobj(31)
                            body.extend(decoder.decompress(chunk, limit + 1 - len(body)))
                            if len(body) > limit or decoder.unconsumed_tail:
                                raise ProviderError("Provider response exceeds the decompressed byte limit")
                            chunk = decoder.unused_data
                if expected_length is not None and wire_size != expected_length:
                    raise ProviderError("Provider returned truncated or mismatched response data")
                if decoder and not decoder.eof:
                    raise ProviderError("Provider returned truncated compressed data")
                checkpoint(cancellation, deadline)
                return HttpResponse(response.status, response_headers, bytes(body))
        except (TimeoutError, socket.timeout):
            raise TimeoutError("Provider request timed out") from None
        except (URLError, OSError, HTTPException, zlib.error):
            raise ProviderError("Provider connection failed; check Wi-Fi and the endpoint") from None
