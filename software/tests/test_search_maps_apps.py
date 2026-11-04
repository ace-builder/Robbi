"""Search/Maps service contracts and bundled AppHost flows, with no real requests."""
from concurrent.futures import CancelledError
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_services.http import HttpClient, HttpResponse, ProviderError
from robbi.app_services.maps import MapsService, directions_url, search_url
from robbi.app_services.search import SearchService
from robbi.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result()
        return result if isinstance(result, HttpResponse) else HttpResponse(200, {}, json.dumps(result).encode())


class Robot:
    def __init__(self):
        self.cancelled = threading.Event()

    def capture_token(self):
        return 0, self.cancelled


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **kw: pytest.fail("Unexpected external HTTP request"))
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-secret")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-maps-secret")


@pytest.fixture
def host_factory(tmp_path):
    hosts = []

    def make(services=None, *, permissions=False):
        host = AppHost(ROOT, tmp_path / str(len(hosts)), Robot(), services=services, permissions_enabled=permissions)
        hosts.append(host)
        assert host.scan()["errors"] == []
        return host

    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def test_web_search_app_returns_sources_and_uses_one_credit_basic_request(host_factory):
    transport = FakeHTTP({"results": [
        {"title": "Example", "url": "https://example.com/source", "content": "Useful current information"},
        {"title": "Ignored duplicate", "url": "https://example.com/source", "content": "Duplicate"},
        {"title": "Bad link", "url": "javascript:alert(1)", "content": "bad"},
        {"title": "Other", "url": "https://example.org/", "content": "Another source"},
    ]})
    host = host_factory({"search": SearchService(transport)})
    result = host.call("web-search", "search", {"query": "  robot projects  ", "count": 2})
    assert result["ok"] and result["data"]["count"] == 2
    assert result["data"]["untrusted_content"] is True
    assert result["data"]["results"][0]["snippet"] == "Useful current information"
    assert "https://example.com/source" in result["text"]
    method, url, options = transport.calls[0]
    assert method == "POST" and url == "https://api.tavily.com/search"
    assert options["headers"]["Authorization"] == "Bearer test-tavily-secret"
    assert options["json_body"]["query"] == "robot projects"
    assert options["json_body"]["search_depth"] == "basic"
    assert options["json_body"]["auto_parameters"] is False
    assert options["json_body"]["include_raw_content"] is False
    assert options["json_body"]["include_answer"] is False
    assert options["deadline"] > time.monotonic() and callable(options["cancellation"])
    assert "test-tavily-secret" not in json.dumps(result)


def test_empty_search_is_successful_but_does_not_invent_answer(host_factory):
    host = host_factory({"search": SearchService(FakeHTTP({"results": []}))})
    result = host.call("web-search", "search", {"query": "unusual search"})
    assert result["ok"] and result["data"]["empty"]
    assert result["data"]["results"] == [] and "No web search results" in result["text"]


@pytest.mark.parametrize("payload", [[], {}, {"results": {}}, {"results": [None]},
                                      {"results": [{"url": "https://user:password@example.com/"}]},
                                      {"results": [{"url": "https://example.com/\ud800"}]},
                                      {"results": [], "error": "private provider detail"}])
def test_search_rejects_malformed_response(payload):
    with pytest.raises(ProviderError):
        SearchService(FakeHTTP(payload)).search({}, "query")


@pytest.mark.parametrize("query,count", [("", 1), (" ", 1), ("x" * 501, 1), ("x", 0), ("x", 9), ("x", True), ("x", 1.5)])
def test_search_rejects_invalid_inputs_without_request(query, count):
    transport = FakeHTTP()
    with pytest.raises(ValidationError):
        SearchService(transport).search({}, query, count=count)
    assert transport.calls == []


def test_search_large_unicode_results_fit_host_limit(host_factory):
    rows = [{"title": "😀" * 500, "content": "😀" * 5000, "url": "https://example.org/" + str(i) + "a" * 1500} for i in range(8)]
    host = host_factory({"search": SearchService(FakeHTTP({"results": rows}))})
    result = host.call("web-search", "search", {"query": "large results", "count": 8})
    assert result["ok"] and len(json.dumps(result, ensure_ascii=False).encode()) < 65536
    assert len(result["data"]["results"][0]["snippet"]) == 600


def test_search_and_maps_missing_keys_explain_setup_without_request(host_factory, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY")
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY")
    transport = FakeHTTP()
    host = host_factory({"search": SearchService(transport), "maps": MapsService(transport)})
    search = host.call("web-search", "search", {"query": "query"})
    maps = host.call("google-maps", "route", {"origin": "Berlin", "destination": "Potsdam"})
    assert not search["ok"] and "TAVILY_API_KEY" in search["text"]
    assert not maps["ok"] and "GOOGLE_MAPS_API_KEY" in maps["text"]
    assert transport.calls == []


def test_maps_link_commands_need_no_permission_key_or_network(host_factory, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY")
    host = host_factory(permissions=True)
    host.set_permissions("google-maps", {"network": "denied"})
    result = host.call("google-maps", "search", {"query": "Café & Kuchen, München", "place_id": "abc/def"})
    assert result["ok"] and result["data"]["link_only"]
    query = parse_qs(urlsplit(result["data"]["url"]).query)
    assert query == {"api": ["1"], "query": ["Café & Kuchen, München"], "query_place_id": ["abc/def"]}
    result = host.call("google-maps", "directions", {"destination": "Berlin Hauptbahnhof", "mode": "walking"})
    assert result["ok"] and result["data"]["link_only"]
    query = parse_qs(urlsplit(result["data"]["url"]).query)
    assert query["travelmode"] == ["walking"] and "origin" not in query
    assert "distance_meters" not in result["data"] and "duration_seconds" not in result["data"]
    assert "no distance or travel time" in result["text"]
    assert host._services == {}


@pytest.mark.parametrize("app,tool,arguments", [
    ("web-search", "search", {"query": "query"}),
    ("google-maps", "geocode", {"address": "Berlin"}),
    ("google-maps", "route", {"origin": "Berlin", "destination": "Potsdam"}),
])
def test_actual_api_commands_request_network_permission(host_factory, app, tool, arguments):
    transport = FakeHTTP()
    host = host_factory({"search": SearchService(transport), "maps": MapsService(transport)}, permissions=True)
    result = host.call(app, tool, arguments)
    assert not result["ok"] and result["permission_required"]
    assert result["permissions"] == ["network"]
    assert transport.calls == []


@pytest.mark.parametrize("value", ["91,0", "0,181", "nan,0", "0,inf", "-90.1,0", "x\nquery", "😀" * 300])
def test_maps_rejects_bad_coordinates_controls_and_oversized_encoded_links(value):
    with pytest.raises(ValidationError):
        search_url(value)
    with pytest.raises(ValidationError):
        directions_url(value, "Berlin")


def test_directions_link_encodes_origin_and_valid_boundary_coordinates():
    query = parse_qs(urlsplit(directions_url("90,180", "-90,-180", "bicycling")).query)
    assert query == {"api": ["1"], "origin": ["-90,-180"], "destination": ["90,180"], "travelmode": ["bicycling"]}
    with pytest.raises(ValidationError):
        directions_url("Berlin", mode="flying")


def test_geocode_returns_matches_and_partial_ambiguity_without_choosing(host_factory):
    transport = FakeHTTP({"status": "OK", "results": [
        {"formatted_address": "Berlin, Germany", "geometry": {"location": {"lat": 52.52, "lng": 13.405}}, "place_id": "berlin"},
        {"formatted_address": "Berlin, Ohio", "geometry": {"location": {"lat": 40.56, "lng": -81.79}}, "partial_match": True},
    ]})
    host = host_factory({"maps": MapsService(transport)})
    result = host.call("google-maps", "geocode", {"address": "Berlin"})
    assert result["ok"] and result["data"]["needs_choice"]
    assert len(result["data"]["results"]) == 2
    assert result["data"]["results"][0]["latitude"] == 52.52
    assert result["data"]["results"][1]["partial_match"]
    assert "test-maps-secret" not in json.dumps(result)
    assert transport.calls[0][0] == "GET"
    query = parse_qs(urlsplit(transport.calls[0][1]).query)
    assert query["key"] == ["test-maps-secret"] and query["address"] == ["Berlin"]


def test_geocode_no_results_and_provider_error_are_distinct():
    result = MapsService(FakeHTTP({"status": "ZERO_RESULTS", "results": []})).geocode({}, "Nowhere")
    assert not result["ok"] and result["results"] == []
    with pytest.raises(ProviderError, match="Geocoding API is enabled") as error:
        MapsService(FakeHTTP({"status": "REQUEST_DENIED", "error_message": "private detail"})).geocode({}, "Berlin")
    assert "private detail" not in str(error.value)


@pytest.mark.parametrize("location", [{"lat": 91, "lng": 0}, {"lat": True, "lng": 1}, {"lat": 0, "lng": "east"},
                                      {"lat": 10 ** 500, "lng": 1}])
def test_geocode_rejects_invalid_provider_coordinates(location):
    with pytest.raises(ProviderError):
        MapsService(FakeHTTP({"status": "OK", "results": [{"formatted_address": "Place", "geometry": {"location": location}}]})).geocode({}, "Place")


def test_geocode_rejects_malformed_provider_status():
    with pytest.raises(ProviderError, match="status"):
        MapsService(FakeHTTP({"status": []})).geocode({}, "Place")


def test_route_returns_real_estimate_and_uses_minimal_field_mask(host_factory):
    transport = FakeHTTP({"routes": [{"distanceMeters": 12500, "duration": "751.5s", "warnings": ["Toll road"]}]})
    host = host_factory({"maps": MapsService(transport)})
    result = host.call("google-maps", "route", {"origin": "52.52,13.405", "destination": "Potsdam"})
    assert result["ok"] and result["data"]["link_only"] is False
    assert result["data"]["distance_meters"] == 12500 and result["data"]["duration_seconds"] == 751.5
    assert result["data"]["traffic_aware"] is False
    assert "live traffic" in result["text"] and "Toll road" in result["text"]
    method, url, options = transport.calls[0]
    assert method == "POST" and url == "https://routes.googleapis.com/directions/v2:computeRoutes"
    assert options["headers"]["X-Goog-Api-Key"] == "test-maps-secret"
    assert options["headers"]["X-Goog-FieldMask"] == "routes.distanceMeters,routes.duration,routes.warnings"
    assert options["json_body"]["origin"] == {"location": {"latLng": {"latitude": 52.52, "longitude": 13.405}}}
    assert options["json_body"]["destination"] == {"address": "Potsdam"}
    assert options["json_body"]["routingPreference"] == "TRAFFIC_UNAWARE"
    assert "test-maps-secret" not in json.dumps(result)


@pytest.mark.parametrize("mode,api_mode", [("walking", "WALK"), ("bicycling", "BICYCLE"), ("transit", "TRANSIT")])
def test_nondriving_routes_use_matching_mode_and_walk_cycle_warning(mode, api_mode):
    transport = FakeHTTP({"routes": [{"distanceMeters": 500, "duration": "600s"}]})
    result = MapsService(transport).route({}, "Origin", "Destination", mode=mode)
    body = transport.calls[0][2]["json_body"]
    assert body["travelMode"] == api_mode and "routingPreference" not in body
    assert ("beta" in result["summary"]) is (mode != "transit")


@pytest.mark.parametrize("payload", [{"routes": [None]}, {"routes": [{"distanceMeters": -1, "duration": "1s"}]},
                                     {"routes": [{"distanceMeters": True, "duration": "1s"}]},
                                     {"routes": [{"distanceMeters": 10 ** 500, "duration": "1s"}]},
                                     {"routes": [{"distanceMeters": 1, "duration": "1s", "warnings": ["a" * 401]}]},
                                     {"routes": [{"distanceMeters": 1, "duration": "soon"}]}, {"error": {"message": "private detail"}}, []])
def test_routes_reject_malformed_provider_data(payload):
    with pytest.raises(ProviderError):
        MapsService(FakeHTTP(payload)).route({}, "Origin", "Destination")


def test_no_route_returns_explicit_failure_without_facts():
    result = MapsService(FakeHTTP({"routes": []})).route({}, "Origin", "Destination")
    assert not result["ok"] and "distance_meters" not in result


def test_route_accepts_zero_distance_omitted_by_google_field_mask():
    result = MapsService(FakeHTTP({"routes": [{"duration": "0s"}]})).route({}, "Origin", "Origin")
    assert result["ok"] and result["distance_meters"] == 0 and result["duration_seconds"] == 0


@pytest.mark.parametrize("route", [{}, {"duration": "1s", "distanceMeters": 0.5},
                                  {"duration": "1s", "distanceMeters": None}])
def test_routes_require_duration_and_integer_distance_when_present(route):
    with pytest.raises(ProviderError):
        MapsService(FakeHTTP({"routes": [route]})).route({}, "Origin", "Destination")


@pytest.mark.parametrize("service,method,args", [(SearchService, "search", ("query",)),
                                                  (MapsService, "route", ("Origin", "Destination")),
                                                  (MapsService, "geocode", ("Address",))])
def test_cancellation_and_expired_deadlines_do_not_request(service, method, args):
    transport = FakeHTTP()
    operation = getattr(service(transport), method)
    with pytest.raises(CancelledError):
        operation({}, *args, cancellation=lambda: True)
    with pytest.raises(TimeoutError):
        operation({}, *args, deadline=time.monotonic() - 1)
    assert transport.calls == []


@pytest.mark.parametrize("service,method,args,payload", [
    (SearchService, "search", ("query",), {"results": []}),
    (MapsService, "route", ("Origin", "Destination"), {"routes": []}),
    (MapsService, "geocode", ("Address",), {"status": "ZERO_RESULTS", "results": []}),
])
def test_late_provider_result_is_discarded_after_cancellation(service, method, args, payload):
    cancelled = threading.Event()

    def reply():
        cancelled.set()
        return payload

    transport = FakeHTTP(reply)
    with pytest.raises(CancelledError):
        getattr(service(transport), method)({}, *args, cancellation=cancelled.is_set)
    assert len(transport.calls) == 1


def test_provider_failures_are_not_retried_or_fabricated(host_factory):
    transport = FakeHTTP(ProviderError("Provider HTTP 429: rate limit reached; try later"))
    result = host_factory({"search": SearchService(transport)}).call("web-search", "search", {"query": "query"})
    assert not result["ok"] and "429" in result["text"]
    assert "data" not in result and len(transport.calls) == 1
