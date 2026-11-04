"""Controller adapter for the same bounded search service used by apps."""
from __future__ import annotations
import time
from robbi.app_services.search import SearchService
from robbi.app_services.http import ProviderError
from robbi.errors import ValidationError
from robbi.configuration.settings import settings

class WebSearchClient:
    def __init__(self, service=None):
        self.service = service or SearchService()

    def available(self):
        return settings.web_search_provider == "tavily" and bool(settings.tavily_api_key)

    def search(self, query, max_results=None, search_depth=None, *, topic="general", time_range=None,
               cancellation=None, deadline=None):
        if not self.available():
            return {"ok": False, "error": "Enable Tavily and save its API key in Settings > Connections.", "results": []}
        started = time.perf_counter()
        try:
            count = settings.web_search_max_results if max_results is None else max_results
            if type(count) is not int or not 1 <= count <= 10:
                raise ValidationError("Search result count must be an integer from 1 to 10")
            result = self.service.search({"api_key": settings.tavily_api_key, "timeout_s": 8}, query,
                count=min(count, 8), search_depth=search_depth or settings.web_search_search_depth,
                topic=topic, time_range=time_range, cancellation=cancellation, deadline=deadline)
            for item in result["results"]:
                item["content"] = item["snippet"]
            result["response_time_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result
        except (ProviderError, ValidationError, TimeoutError) as error:
            return {"ok": False, "provider": "tavily", "error": str(error), "results": []}
