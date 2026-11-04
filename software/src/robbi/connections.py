"""Owner-managed provider connections. Secrets are write-only and stay off YAML."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import time
from urllib.parse import quote, urlsplit

from flask import Blueprint, current_app, jsonify, request
from .app_runtime.owner import require_owner
from .app_services.http import HttpClient, ProviderError, checked_url
from .config import merge, validate
from .errors import BusyError, ValidationError
from .setup.configuration import Configuration

PROVIDERS = {
    "openai": ("OpenAI", "OPENAI_API_KEY", "openai_api_key", "https://api.openai.com/v1"),
    "openrouter": ("OpenRouter", "OPENROUTER_API_KEY", "openrouter_api_key", "https://openrouter.ai/api/v1"),
    "alibaba": ("Alibaba", "ALIBABA_API_KEY", "alibaba_api_key", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "gemini": ("Google Gemini", "GEMINI_API_KEY", "gemini_api_key", "https://generativelanguage.googleapis.com/v1beta"),
    "local": ("Custom / local (OpenAI-compatible)", "LOCAL_API_KEY", "local_api_key", ""),
    "tavily": ("Tavily web search", "TAVILY_API_KEY", "tavily_api_key", "https://api.tavily.com"),
}


@contextmanager
def remote_slot():
    slots = current_app.config["ROBBI_REMOTE_SLOTS"]
    if not slots.acquire(blocking=False):
        raise BusyError("Robbi is busy with other requests; try again shortly")
    try:
        yield
    finally:
        slots.release()


def validated(payload, settings):
    provider = payload.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValidationError("Choose a supported provider or Custom / local")
    if set(payload) - {"provider", "api_key", "base_url", "model", "activate"}:
        raise ValidationError("Unknown connection field")
    key = payload.get("api_key", "")
    if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() or c in "\x00\"'" for c in key):
        raise ValidationError("Paste a single API key without spaces or quotes")
    model = payload.get("model", "")
    if not isinstance(model, str) or len(model) > 200 or any(ord(c) < 33 for c in model):
        raise ValidationError("Model ID must be text up to 200 characters, without spaces")
    activate = payload.get("activate", False)
    if type(activate) is not bool:
        raise ValidationError("activate must be true or false")
    if provider == "local":
        base = payload.get("base_url", settings.local_llm_base_url)
        if not isinstance(base, str):
            raise ValidationError("Endpoint must be a URL")
        base = base.strip().rstrip("/")
        checked_url(base, allow_http=True)
        if urlsplit(base).query:
            raise ValidationError("Endpoint must not include query parameters")
    else:
        base = getattr(settings, provider + "_base_url", "") or PROVIDERS[provider][3]
        if payload.get("base_url") not in (None, "", base):
            raise ValidationError("Use Custom / local for a different endpoint")
        checked_url(base)
    return provider, key, model, base, activate


def connection_request(provider, key, model, base, *, discover=False, http=None):
    """Probe one saved or draft connection; never execute returned robot actions."""
    http = http or HttpClient()
    if not key and provider != "local":
        raise ValidationError("Save or enter an API key first")
    if provider == "tavily":
        from .app_services.search import SearchService
        result = SearchService(http).search({"api_key": key, "timeout_s": 12}, "latest science news",
                                           count=2, topic="news", time_range="week")
        return {"ok": True, "message": "Tavily connected", "results": result["results"]}
    if not discover and not model:
        raise ValidationError("Enter a model ID")
    headers = {"x-goog-api-key": key} if provider == "gemini" else {"Authorization": "Bearer " + (key or "local")}
    options = dict(headers=headers, timeout_s=18, max_bytes=512 * 1024, allow_http=provider == "local")
    if discover:
        result = http.request("GET", base + "/models", **options).json()
        if not isinstance(result, dict):
            raise ProviderError("Provider returned an invalid model list; enter the model ID manually")
        rows = result.get("models" if provider == "gemini" else "data", [])
        if not isinstance(rows, list):
            raise ProviderError("Provider returned an invalid model list")
        names = {str(row.get("name" if provider == "gemini" else "id", "")).removeprefix("models/")
                 for row in rows[:1000] if isinstance(row, dict)}
        return {"ok": True, "models": sorted(name for name in names if re.fullmatch(r"[^\s\x00-\x1f]{1,200}", name))[:300]}
    prompt = 'Connection test only. Return exactly this JSON object: {"speak":"Connected."}. Do not call tools.'
    if provider == "gemini":
        result = http.request("POST", base + "/models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent",
            json_body={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 512}}, **options).json()
        try:
            raw = "".join(p.get("text", "") for p in result["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Model returned no text") from None
    else:
        result = http.request("POST", base + "/chat/completions",
            json_body={"model": model, "messages": [{"role": "user", "content": prompt}]}, **options).json()
        try:
            raw = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Model returned no text") from None
    try:
        action = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        compatible = isinstance(action, dict) and set(action) == {"speak"} and isinstance(action["speak"], str)
    except (ValueError, AttributeError, TypeError):
        compatible = False
    return {"ok": True, "json_compatible": compatible,
            "message": "Connected; robot JSON response verified." if compatible else "Connected, but this model did not follow Robbi's JSON contract. Try a more capable instruction model."}


def blueprint(runtime):
    bp = Blueprint("connections", __name__)

    def snapshot():
        from robbi.configuration.settings import settings
        return {"providers": [{"id": name, "label": spec[0],
                 "key_configured": bool(os.getenv(spec[1]) or (getattr(settings, spec[2], "") if name != "local" else "")),
                 "base_url": settings.local_llm_base_url if name == "local" else getattr(settings, name + "_base_url", "") or spec[3]}
                for name, spec in PROVIDERS.items()],
                "active": {"provider": settings.ui_text_provider, "model": settings.ui_text_model},
                "search_enabled": settings.web_search_provider == "tavily"}

    @bp.get("/api/connections")
    def get_connections():
        require_owner()
        return jsonify(snapshot())

    @bp.post("/api/connections/<operation>")
    def update_connection(operation):
        require_owner()
        from robbi.configuration.settings import settings
        if operation not in {"save", "test", "models"}:
            raise ValidationError("Unknown connection operation")
        payload = request.get_json()
        if not isinstance(payload, dict) or request.content_length and request.content_length > 8192:
            raise ValidationError("Use a connection object up to 8 KiB")
        provider, key, model, base, activate = validated(payload, settings)
        spec = PROVIDERS[provider]
        if operation != "save":
            with remote_slot():
                started = time.perf_counter()
                result = connection_request(provider, key or getattr(settings, spec[2], ""), model, base,
                                            discover=operation == "models")
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
                return jsonify(result)
        if activate and provider != "tavily" and not model:
            raise ValidationError("Enter a model ID before using this connection for conversations")
        patch = {}
        if provider == "local":
            patch["providers"] = {"local": {"llm_base_url": base}}
        if provider == "tavily" and "activate" in payload:
            patch["web_search"] = {"provider": "tavily" if activate else "disabled"}
        elif activate:
            patch["llm"] = {key: value for prefix in ("chat", "ui_text", "speech_input")
                            for key, value in ((prefix + "_provider", provider), (prefix + "_model", model))}
        with current_app.config["ROBBI_CONFIG_LOCK"]:
            # Validate all public settings before touching the secret file.
            validate(merge(runtime.config.values, patch))
            if key:
                Configuration(runtime.config.root).save_key(spec[1], key)
                os.environ[spec[1]] = key
                setattr(settings, spec[2], key)
            controller = runtime.controller()
            if patch:
                controller.apply_settings(patch)
                runtime.config.values = merge(runtime.config.values, patch)
            controller.llm.reload_runtime_settings()
            controller._diagnostics.invalidate()
        return jsonify(ok=True, **snapshot())

    return bp
