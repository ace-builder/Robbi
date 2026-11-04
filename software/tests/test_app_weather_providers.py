from concurrent.futures import CancelledError
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from robbi.app_services.http import HttpResponse, ProviderError
from robbi.app_services.weather import WeatherService
from robbi.errors import ValidationError


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, HttpResponse) else HttpResponse(200, {}, json.dumps(result).encode())


def daily(day="2026-10-12"):
    return {"daily": {"time": [day], "weather_code": [61], "temperature_2m_min": [9.1],
                      "temperature_2m_max": [15.8], "precipitation_sum": [2],
                      "precipitation_probability_max": [60], "wind_speed_10m_max": [14]}}


def place(name="Berlin", lat=52.52, lon=13.405, zone="Europe/Berlin"):
    return {"name": name, "latitude": lat, "longitude": lon, "timezone": zone, "country_code": "DE", "country": "Germany"}


def coords(provider="open_meteo", **kwargs):
    return {"provider": provider, "latitude": 52.52, "longitude": 13.405, "timezone": "UTC", **kwargs}


def test_keyless_open_meteo_normalizes_units_and_credits():
    http = FakeHttp({"results": [place(zone="UTC")]}, daily())
    result = WeatherService(http, now=lambda: NOW).forecast({}, "Berlin", "tomorrow")
    assert result["temperature_max"] == 15.8
    assert result["date"] == "2026-10-12"
    assert result["location"]["name"] == "Berlin, Germany"
    assert result["units"]["temperature"] == "°C"
    assert result["attribution"][0]["name"] == "Open-Meteo"
    query = parse_qs(urlsplit(http.calls[1][1]).query)
    assert query["timezone"] == ["UTC"] and query["start_date"] == ["2026-10-12"]
    assert "apikey" not in query


def test_ambiguous_place_returns_choices_without_fetching_wrong_weather():
    http = FakeHttp({"results": [place("Berlin", 52, 13), place("Berlin", 40, -70, "America/New_York")]})
    result = WeatherService(http, now=lambda: NOW).forecast({}, "Berlin")
    assert not result["ok"] and result["needs_location"]
    assert len(result["choices"]) == 2 and len(http.calls) == 1


def test_explicit_city_overrides_configured_home_coordinates_and_timezone():
    http = FakeHttp({"results": [place("Other", 1, 2, "UTC")]}, daily())
    result = WeatherService(http, now=lambda: NOW).forecast(coords(timezone="Europe/Berlin"), "Other")
    assert result["location"]["latitude"] == 1
    assert result["location"]["timezone"] == "UTC"


def test_tomorrow_is_resolved_in_destination_timezone_near_midnight():
    pytest.importorskip("tzdata")
    now = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)
    http = FakeHttp({"results": [place()]}, daily("2026-10-13"))
    result = WeatherService(http, now=lambda: now).forecast({}, "Berlin", "tomorrow")
    assert result["date"] == "2026-10-13"
    assert result["location"]["timezone"] == "Europe/Berlin"


def test_dst_uses_named_timezone_instead_of_fixed_offset():
    pytest.importorskip("tzdata")
    now = datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc)
    http = FakeHttp(daily("2026-10-26"))
    result = WeatherService(http, now=lambda: now).forecast(coords(timezone="Europe/Berlin"), day="tomorrow")
    assert result["date"] == "2026-10-26"


@pytest.mark.parametrize("config,day", [(coords(timezone="not/a-zone"), "tomorrow"),
                                       (coords(latitude=91), "tomorrow"),
                                       (coords(), "2026-11-30"), (coords(), "2026-10-10"),
                                       ({"latitude": 1, "longitude": 2}, "tomorrow")])
def test_invalid_location_and_date_fail_before_network(config, day):
    http = FakeHttp()
    with pytest.raises(ValidationError):
        WeatherService(http, now=lambda: NOW).forecast(config, day=day)
    assert not http.calls


def test_missing_forecast_date_does_not_substitute_a_different_day():
    with pytest.raises(ProviderError, match="requested date"):
        WeatherService(FakeHttp(daily("2026-10-14")), now=lambda: NOW).forecast(coords())


def test_cached_requests_do_not_repeat_network_io():
    http = FakeHttp(daily())
    service = WeatherService(http, now=lambda: NOW)
    one = service.forecast(coords())
    two = service.forecast(coords())
    assert not one["cached"] and two["cached"] and not two["stale"]
    assert len(http.calls) == 1


def test_expired_cache_revalidates_and_uses_304():
    response = HttpResponse(200, {"expires": formatdate(time.time() - 1, usegmt=True),
                                  "last-modified": "Sun, 11 Oct 2026 10:00:00 GMT"}, json.dumps(daily()).encode())
    http = FakeHttp(response, HttpResponse(304, {}, b""))
    service = WeatherService(http, now=lambda: NOW)
    service.forecast(coords())
    result = service.forecast(coords())
    assert result["cached"] and not result["stale"]
    assert http.calls[1][2]["headers"]["If-Modified-Since"] == response.headers["last-modified"]


def test_wifi_failure_serves_explicitly_stale_recent_cache():
    response = HttpResponse(200, {"expires": formatdate(time.time() - 1, usegmt=True)}, json.dumps(daily()).encode())
    http = FakeHttp(response, ProviderError("Wi-Fi interrupted"))
    service = WeatherService(http, now=lambda: NOW)
    service.forecast(coords())
    result = service.forecast(coords())
    assert result["cached"] and result["stale"]
    assert "using a cached forecast" in result["summary"]


def test_cancelled_request_never_returns_cached_data():
    http = FakeHttp(daily())
    service = WeatherService(http, now=lambda: NOW)
    service.forecast(coords())
    with pytest.raises(CancelledError):
        service.forecast(coords(), cancellation=lambda: True)
    assert len(http.calls) == 1


def test_met_requires_identifiable_user_agent():
    http = FakeHttp()
    with pytest.raises(ValidationError, match="real contact"):
        WeatherService(http, now=lambda: NOW).forecast(coords("met_no"))
    assert not http.calls


def test_met_aggregates_only_nonoverlapping_hours_and_converts_wind():
    rows = []
    for hour in range(24):
        rows.append({"time": f"2026-10-12T{hour:02}:00:00Z", "data": {
            "instant": {"details": {"air_temperature": 10 + hour / 10, "wind_speed": 2}},
            "next_1_hours": {"details": {"precipitation_amount": 1}, "summary": {"symbol_code": "rain"}},
            "next_6_hours": {"details": {"precipitation_amount": 6}},
            "next_12_hours": {"details": {"precipitation_amount": 12}},
        }})
    http = FakeHttp({"properties": {"timeseries": rows}})
    result = WeatherService(http, now=lambda: NOW).forecast(coords("met_no", user_agent="Robbi/2 person@example.com"))
    assert result["precipitation"] == 24
    assert result["wind_speed_max"] == 7.2
    assert not result["partial_day"]
    assert "sample range" in result["note"]
    assert http.calls[0][2]["headers"]["User-Agent"] == "Robbi/2 person@example.com"
    query = parse_qs(urlsplit(http.calls[0][1]).query)
    assert query["lat"] == ["52.5200"]


def test_openweather_uses_free_forecast_endpoint_and_reports_partial_sample_range():
    rows = [{"dt": datetime(2026, 10, 12, hour, tzinfo=timezone.utc).timestamp(), "main": {"temp": 10 + hour},
             "wind": {"speed": 2}, "pop": 0.5, "rain": {"3h": 3}, "weather": [{"description": "rain"}]} for hour in (9, 12, 15)]
    http = FakeHttp({"list": rows})
    result = WeatherService(http, now=lambda: NOW).forecast(coords("openweather", api_key="secret"))
    assert result["precipitation"] == 9 and result["precipitation_probability"] == 50
    assert result["partial_day"]
    assert urlsplit(http.calls[0][1]).path == "/data/2.5/forecast"
    assert result["attribution"][0]["name"] == "OpenWeather"
    assert "secret" not in json.dumps(result)


def test_generic_contract_requires_correct_date_and_units():
    valid = {"date": "2026-10-12", "units": {"temperature": "C", "wind_speed": "km/h", "precipitation": "mm"},
             "temperature_min": 10, "temperature_max": 20, "condition": "sunny"}
    config = coords("generic", endpoint="https://weather.example.test/forecast")
    result = WeatherService(FakeHttp(valid), now=lambda: NOW).forecast(config)
    assert result["condition"] == "sunny"
    for invalid in ({**valid, "date": "2026-10-13"}, {**valid, "units": {"temperature": "F"}}):
        with pytest.raises(ProviderError):
            WeatherService(FakeHttp(invalid), now=lambda: NOW).forecast(config)


def test_cache_entry_count_remains_bounded():
    http = FakeHttp(*(daily() for _ in range(20)))
    service = WeatherService(http, now=lambda: NOW)
    for latitude in range(20):
        service.forecast(coords(latitude=latitude))
    assert len(service._cache) == 16
    assert service._cache_bytes <= 2 * 1024 * 1024
