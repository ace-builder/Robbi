"""Small ElevenLabs adapter. Stream MP3 to disk, never buffer an entire clip."""
from __future__ import annotations

import json
import os
import time

from ..errors import UnavailableError

MODEL = "eleven_v4"
DEFAULT_VOICE = ""
MAX_TEXT = 10_000
BASE_URL = "https://api.elevenlabs.io"


class ProviderError(UnavailableError):
    """Safe, actionable messages only: provider bodies can contain secrets."""


class ElevenLabs:
    def __init__(self, client_factory=None):
        self.client_factory = client_factory

    @staticmethod
    def _httpx():
        try:
            import httpx
        except ImportError:
            raise ProviderError("Install Robbi's providers extra to enable ElevenLabs.") from None
        return httpx

    def _client(self, key, read_timeout):
        httpx = self._httpx()
        factory = self.client_factory or httpx.Client
        return factory(headers={"xi-api-key": key}, follow_redirects=False,
                       timeout=httpx.Timeout(read_timeout, connect=8, write=10, pool=2))

    @staticmethod
    def _check(response):
        if response.status_code == 200:
            return
        messages = {
            400: "ElevenLabs rejected this text, voice or model. Check the voice ID and Eleven v4 access.",
            401: "ElevenLabs rejected the API key. Update it in Connection settings.",
            402: "ElevenLabs needs available credits or a subscription for this request.",
            403: "This API key cannot access the voice or model. Check its permissions and your plan.",
            404: "ElevenLabs could not find this voice or model. Check the voice ID and Eleven v4 access.",
            422: "ElevenLabs rejected the input. Check the text length and voice ID.",
            429: "ElevenLabs is rate-limited or out of credits. Check your account before retrying.",
        }
        raise ProviderError(messages.get(response.status_code,
                            "ElevenLabs is unavailable. Try again later; this request will not be retried automatically."))

    def generate(self, *, key, text, voice_id, destination, max_bytes, cancelled):
        httpx = self._httpx()
        deadline = time.monotonic() + 180
        try:
            with self._client(key, 30) as client:
                with client.stream("POST", f"{BASE_URL}/v1/text-to-speech/{voice_id}",
                                   params={"output_format": "mp3_44100_128"},
                                   json={"text": text, "model_id": MODEL}) as response:
                    self._check(response)
                    content_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                        raise ProviderError("ElevenLabs returned an unexpected audio format. No clip was saved.")
                    size, prefix = 0, b""
                    with destination.open("xb") as output:
                        for chunk in response.iter_bytes(chunk_size=16_384):
                            if cancelled.is_set():
                                raise ProviderError("Generation cancelled locally.")
                            if time.monotonic() > deadline:
                                raise ProviderError("Generation exceeded three minutes. Check ElevenLabs before retrying.")
                            size += len(chunk)
                            if size > max_bytes:
                                raise ProviderError("The audio exceeded the clip/storage limit. Shorten the text or delete old clips.")
                            prefix = (prefix + chunk)[:3]
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if not size or not (prefix.startswith(b"ID3") or
                                       (len(prefix) >= 2 and prefix[0] == 255 and prefix[1] & 224 == 224)):
                        raise ProviderError("ElevenLabs returned empty or invalid MP3 audio. No clip was saved.")
        except httpx.HTTPError:
            raise ProviderError("Connection to ElevenLabs failed or timed out. A request may have used credits; retry manually if needed.") from None

    def voices(self, key, page_token=""):
        httpx = self._httpx()
        params = {"page_size": 100}
        if page_token:
            params["next_page_token"] = page_token
        try:
            with self._client(key, 10) as client:
                with client.stream("GET", f"{BASE_URL}/v2/voices", params=params) as response:
                    self._check(response)
                    raw = bytearray()
                    deadline = time.monotonic() + 15
                    for chunk in response.iter_bytes(chunk_size=16_384):
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024 or time.monotonic() > deadline:
                            raise ProviderError("The voice list was too large or slow. You can still enter a voice ID manually.")
                    data = json.loads(raw)
            return {"voices": [{"voice_id": str(v["voice_id"])[:128], "name": str(v.get("name", "Voice"))[:120]}
                               for v in data["voices"][:100]],
                    "next_page_token": str(data.get("next_page_token") or "")[:2048] if data.get("has_more") else ""}
        except httpx.HTTPError:
            raise ProviderError("Could not load your voices. Check the connection or enter a voice ID manually.") from None
        except (ValueError, KeyError, TypeError):
            raise ProviderError("ElevenLabs returned an unreadable voice list. Enter a voice ID manually.") from None
