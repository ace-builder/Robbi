"""Bounded Tavily basic search, with source links and no automatic paid upgrades."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from robbi.errors import ValidationError
from .http import HttpClient, ProviderError, bounded_number, checkpoint
from .images import api_key


def _source_url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        if len(value.encode("utf-8")) > 1600:
            return None
        parsed = urlsplit(value)
        parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except (ValueError, UnicodeError):
        return None
    return value


def _snippet(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


class SearchService:
    def __init__(self, http=None):
        self.http = http or HttpClient()

    def search(self, config: dict, query: str, *, count=5, topic="general", time_range=None,
               search_depth="basic", cancellation=None, deadline=None) -> dict:
        checkpoint(cancellation, deadline)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValidationError("Search query must contain 1–500 characters")
        if type(count) is not int or not 1 <= count <= 8:
            raise ValidationError("Search result count must be an integer from 1 to 8")
        key = api_key(config, "TAVILY_API_KEY")
        if topic not in ("general", "news", "finance"):
            raise ValidationError("Search topic must be general, news or finance")
        if time_range not in (None, "", "day", "week", "month", "year"):
            raise ValidationError("Search time range must be day, week, month or year")
        if search_depth not in ("basic", "fast", "ultra-fast", "advanced"):
            raise ValidationError("Unsupported search depth")
        timeout = bounded_number(config.get("timeout_s"), 20, 1, 30)
        body = {"query": query.strip(), "max_results": count, "search_depth": search_depth,
                "topic": topic, "auto_parameters": False, "include_answer": False,
                "include_raw_content": False, "include_images": False, "include_published_date": True}
        if time_range:
            body["time_range"] = time_range
        response = self.http.request(
            "POST", "https://api.tavily.com/search",
            headers={"Authorization": "Bearer " + key},
            json_body=body,
            timeout_s=timeout, max_bytes=512 * 1024, cancellation=cancellation, deadline=deadline,
        )
        checkpoint(cancellation, deadline)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("results"), list):
            raise ProviderError("Search provider returned an invalid result list")
        results, seen = [], set()
        for item in payload["results"][:40]:
            if not isinstance(item, dict):
                continue
            url = _source_url(item.get("url"))
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({"title": _snippet(item.get("title"), 180) or urlsplit(url).hostname[:180],
                            "url": url, "snippet": _snippet(item.get("content"), 600),
                            "published_date": _snippet(item.get("published_date"), 80)})
            if len(results) >= count:
                break
        if payload["results"] and not results:
            raise ProviderError("Search provider returned no usable source links")
        return {"ok": True, "provider": "tavily", "query": query.strip(), "results": results,
                "topic": topic, "time_range": time_range or None, "search_depth": search_depth,
                "count": len(results), "empty": not results, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "untrusted_content": True,
                "summary": f"Found {len(results)} web search results." if results else "No web search results found. Try a more specific query.",
                "attribution": [{"name": "Tavily", "url": "https://www.tavily.com/"}]}
