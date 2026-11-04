"""Provider-neutral image generation. All inference happens off the Pi."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import io
import os
import re
import time
from urllib.parse import quote, urlsplit

from robbi.errors import ValidationError
from .http import HttpClient, ProviderError, bounded_number, checkpoint, checked_url, same_origin

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
_KEY_ENV = {"openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
            "google_generate_content": "GEMINI_API_KEY", "bfl": "BFL_API_KEY",
            "replicate": "REPLICATE_API_TOKEN"}


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    media_type: str
    provider: str
    model: str
    width: int
    height: int


def api_key(config: dict, default_env: str = "", *, required=True) -> str:
    name = str(config.get("api_key_env") or default_env)
    key = str(config.get("api_key") or os.environ.get(name, "")).strip()
    if required and not key:
        raise ValidationError(f"Configure an API key or set {name or 'api_key_env'}")
    return key


def _at_path(value, path: str):
    try:
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderError("Image response does not match the configured response_path") from None


def _template(value, prompt, model, depth=0):
    if depth > 8:
        raise ValidationError("Image request template is nested too deeply")
    if isinstance(value, str):
        return value.replace("{prompt}", prompt).replace("{model}", model)
    if isinstance(value, dict):
        return {k: _template(v, prompt, model, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_template(v, prompt, model, depth + 1) for v in value]
    return value


def _objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class ImageService:
    def __init__(self, http=None):
        self.http = http or HttpClient()

    def generate(self, config: dict, prompt: str, *, cancellation=None, deadline=None) -> GeneratedImage:
        checkpoint(cancellation, deadline)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
            raise ValidationError("Image prompt must contain 1–8000 characters")
        provider = str(config.get("provider", "openai")).lower()
        provider = {"flux": "bfl", "gemini": "google"}.get(provider, provider)
        if provider not in {*_KEY_ENV, "generic"}:
            raise ValidationError("Unknown image provider")
        model = str(config.get("model", "")).strip()
        if provider != "generic" and (not model or len(model) > 200):
            raise ValidationError("Configure the image provider model")
        timeout = bounded_number(config.get("timeout_s"), 90, 1, 180)
        deadline = min(deadline or float("inf"), time.monotonic() + timeout)
        max_bytes = int(bounded_number(config.get("max_bytes"), MAX_IMAGE_BYTES, 1024, MAX_IMAGE_BYTES))
        max_pixels = int(bounded_number(config.get("max_pixels"), MAX_IMAGE_PIXELS, 1024, MAX_IMAGE_PIXELS))
        allow_http = config.get("allow_http", False) is True
        options = config.get("request_options", {})
        if not isinstance(options, dict):
            raise ValidationError("request_options must be a JSON object")
        headers = {}
        if provider in _KEY_ENV:
            key = api_key(config, _KEY_ENV[provider])
            headers = ({"x-goog-api-key": key} if provider.startswith("google") else
                       {"x-key": key} if provider == "bfl" else {"Authorization": f"Bearer {key}"})
        else:
            key = api_key(config, required=False)
            if key:
                headers[str(config.get("auth_header") or "Authorization")] = str(config.get("auth_prefix", "Bearer ")) + key
        request_kwargs = dict(timeout_s=timeout, max_bytes=min(20 * 1024 * 1024, max_bytes * 4 // 3 + 1024 * 1024),
                              cancellation=cancellation, deadline=deadline, allow_http=allow_http)

        if provider == "openai":
            endpoint = config.get("endpoint") or "https://api.openai.com/v1/images/generations"
            payload = {"size": str(config.get("size", "1024x1024")), "quality": str(config.get("quality", "low")),
                       "output_format": "jpeg", **options, "model": model, "prompt": prompt, "n": 1}
            result = self.http.request("POST", endpoint, headers=headers, json_body=payload, **request_kwargs).json()
            encoded = _at_path(result, "data.0")
            if not isinstance(encoded, dict):
                raise ProviderError("Image provider returned no image")
            image_value, encoding = (encoded.get("b64_json"), "base64") if encoded.get("b64_json") else (encoded.get("url"), "url")
        elif provider == "google":
            endpoint = config.get("endpoint") or "https://generativelanguage.googleapis.com/v1beta/interactions"
            payload = {"response_format": {"type": "image", "mime_type": "image/jpeg",
                                          "aspect_ratio": str(config.get("aspect_ratio", "1:1")), "image_size": "1K"},
                       **options, "model": model, "input": prompt}
            result = self.http.request("POST", endpoint, headers=headers, json_body=payload, **request_kwargs).json()
            parts = [part for step in _objects(result.get("steps")) if step.get("type") == "model_output"
                     for part in _objects(step.get("content")) if part.get("type") == "image"] if isinstance(result, dict) else []
            if not parts:
                raise ProviderError("Google returned no generated image; check model support or prompt restrictions")
            image_value, encoding = parts[0].get("data"), "base64"
        elif provider == "google_generate_content":
            endpoint = config.get("endpoint") or f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='-._')}:generateContent"
            payload = {"generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}, **options,
                       "contents": [{"parts": [{"text": prompt}]}]}
            result = self.http.request("POST", endpoint, headers=headers, json_body=payload, **request_kwargs).json()
            parts = [part.get("inlineData", part.get("inline_data"))
                     for candidate in _objects(result.get("candidates")) if isinstance(candidate.get("content"), dict)
                     for part in _objects(candidate["content"].get("parts"))
                     if not part.get("thought") and ("inlineData" in part or "inline_data" in part)] if isinstance(result, dict) else []
            if not parts or not isinstance(parts[0], dict):
                raise ProviderError("Google returned no generated image; check model support or prompt restrictions")
            image_value, encoding = parts[0].get("data"), "base64"
        elif provider in {"bfl", "replicate"}:
            if provider == "bfl":
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
                    raise ValidationError("BFL model must be an endpoint name")
                endpoint = config.get("endpoint") or "https://api.bfl.ai/v1/" + model
                payload = {**options, "width": int(bounded_number(options.get("width", config.get("width")), 1024, 256, 2048)),
                           "height": int(bounded_number(options.get("height", config.get("height")), 1024, 256, 2048)), "prompt": prompt}
            else:
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model):
                    raise ValidationError("Replicate model must be owner/model")
                endpoint = config.get("endpoint") or f"https://api.replicate.com/v1/models/{model}/predictions"
                payload = {"input": {**options, "prompt": prompt}}
                # Server-side deadline limits abandoned remote work as well.
                headers["Cancel-After"] = f"{max(1, int(deadline - time.monotonic()))}s"
            result = self.http.request("POST", endpoint, headers=headers, json_body=payload, **request_kwargs).json()
            image_value = self._poll(provider, endpoint, result, headers, request_kwargs, config)
            encoding = "url"
        else:
            endpoint = config.get("endpoint")
            if not endpoint:
                raise ValidationError("Configure the generic image endpoint")
            payload = _template(config.get("request_template", {"prompt": "{prompt}", "model": "{model}"}), prompt, model)
            response = self.http.request("POST", endpoint, headers=headers, json_body=payload, **request_kwargs)
            encoding = str(config.get("response_encoding", "base64"))
            image_value = response.body if encoding == "binary" else _at_path(response.json(), str(config.get("response_path", "data.0.b64_json")))

        checkpoint(cancellation, deadline)
        if encoding == "url":
            checked_url(image_value, allow_http=allow_http)
            # Never send provider API credentials to a signed image delivery URL.
            data = self.http.request("GET", image_value, headers={"Accept": "image/*"},
                                     **{**request_kwargs, "max_bytes": max_bytes}).body
        elif encoding == "base64":
            if not isinstance(image_value, str) or len(image_value) > (max_bytes + 2) // 3 * 4:
                raise ProviderError("Provider returned missing or oversized base64 image data")
            try:
                data = base64.b64decode(image_value, validate=True)
            except (ValueError, binascii.Error):
                raise ProviderError("Provider returned invalid base64 image data") from None
        elif encoding == "binary" and isinstance(image_value, bytes):
            data = image_value
        else:
            raise ValidationError("response_encoding must be base64, url or binary")
        if not data or len(data) > max_bytes:
            raise ProviderError("Generated image exceeds the configured byte limit or is empty")
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ProviderError("Generated image must be PNG, JPEG or WebP")
                width, height = picture.size
                if width * height > max_pixels:
                    raise ProviderError("Generated image exceeds the configured pixel limit")
                media_type = Image.MIME[picture.format]
                picture.verify()  # Header/container validation; resize/decode belongs to the host.
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            raise ProviderError("Provider returned an invalid image") from None
        checkpoint(cancellation, deadline)
        return GeneratedImage(data, media_type, provider, model, width, height)

    def _poll(self, provider, endpoint, result, headers, request_kwargs, config):
        interval = bounded_number(config.get("poll_interval_s"), 1, 0.2, 10)
        poll_url = None
        while True:
            checkpoint(request_kwargs["cancellation"], request_kwargs["deadline"])
            if not isinstance(result, dict):
                raise ProviderError("Image provider returned an invalid job response")
            status = str(result.get("status", "")).lower()
            if provider == "bfl":
                if status == "ready":
                    return _at_path(result, "result.sample")
                if status in {"error", "failed", "request moderated", "content moderated", "task not found"}:
                    raise ProviderError(f"Image generation did not complete ({status})")
                poll_url = result.get("polling_url") or poll_url
            else:
                if status == "succeeded":
                    output = result.get("output")
                    return output[0] if isinstance(output, list) and output else output
                if status in {"failed", "canceled", "cancelled"}:
                    raise ProviderError(f"Image generation did not complete ({status})")
                urls = result.get("urls")
                poll_url = (urls.get("get") if isinstance(urls, dict) else None) or poll_url
            checked_url(poll_url, allow_http=request_kwargs["allow_http"])
            trusted_bfl = provider == "bfl" and urlsplit(endpoint).hostname in {"api.bfl.ai", "api.eu.bfl.ai", "api.us.bfl.ai"} and urlsplit(poll_url).scheme == "https" and str(urlsplit(poll_url).hostname).endswith(".bfl.ai")
            if not same_origin(endpoint, poll_url) and not trusted_bfl:
                raise ProviderError("Provider returned a polling URL outside its API origin")
            until = time.monotonic() + interval
            while time.monotonic() < until:
                checkpoint(request_kwargs["cancellation"], request_kwargs["deadline"])
                time.sleep(min(0.05, max(0, until - time.monotonic())))
            result = self.http.request("GET", poll_url, headers=headers, **request_kwargs).json()
