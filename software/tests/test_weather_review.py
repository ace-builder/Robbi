"""Regression cases for location isolation, honest daily coverage and cache recovery."""
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
import json
import time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from robbi.app_services.http import HttpResponse, ProviderError
from robbi.app_services.weather import WeatherService
from robbi.errors import ValidationError
from test_app_weather_providers import FakeHttp, NOW, coords, daily, place


def test_remote_city_does_not_inherit_home_country_filter():
    london = {**place("London", 51.5, -0.12, "Europe/London"), "country_code": "GB", "country": "United Kingdom"}
    transport = FakeHttp({"results": [london]}, daily())
    result = WeatherService(transport, now=lambda: NOW).forecast(coords(default_location="Berlin", country_code="DE"), "London")
    assert result["location"]["name"] == "London, United Kingdom"
    assert "countryCode" not in parse_qs(urlsplit(transport.calls[0][1]).query)


def test_default_city_still_uses_home_country_and_explicit_filter_wins():
    for config, location, country in [({"default_location": "Berlin", "country_code": "DE"}, "", None),
                                       ({"country_code": "GB"}, "Berlin", "DE")]:
        transport = FakeHttp({"results": [place(zone="UTC")]}, daily())
        WeatherService(transport, now=lambda: NOW).forecast(config, location, country_code=country)
        assert parse_qs(urlsplit(transport.calls[0][1]).query)["countryCode"] == ["DE"]


def test_explicit_coordinates_are_not_labelled_with_home_city():
    result = WeatherService(FakeHttp(daily()), now=lambda: NOW).forecast(
        coords(default_location="Berlin"), latitude=40.7, longitude=-74, timezone="America/New_York")
    assert result["location"]["name"] == "40.7000, -74.0000"


@pytest.mark.parametrize("arguments", [{"latitude": 40.7, "timezone": "UTC"}, {"longitude": -74, "timezone": "UTC"},
                                        {"latitude": 40.7, "longitude": -74}, {"latitude": True, "longitude": 2, "timezone": "UTC"}])
def test_request_coordinate_pair_and_timezone_cannot_be_completed_from_home(arguments):
    transport = FakeHttp(daily())
    with pytest.raises(ValidationError):
        WeatherService(transport, now=lambda: NOW).forecast(coords(default_location="Berlin"), **arguments)
    assert not transport.calls


def met_point(hour, *, hours=1, amount=1, temperature=10):
    return {"time": f"2026-10-12T{hour:02}:00:00Z", "data": {
        "instant": {"details": {"air_temperature": temperature, "wind_speed": 1}},
        f"next_{hours}_hours": {"details": {"precipitation_amount": amount}, "summary": {"symbol_code": "rain"}},
    }}


def met_forecast(rows):
    return WeatherService(FakeHttp({"properties": {"timeseries": rows}}), now=lambda: NOW).forecast(
        coords("met_no", user_agent="Robbi/2 test@example.org"))


def test_met_discontinuous_samples_are_not_reported_as_full_day():
    result = met_forecast([met_point(0), met_point(23)])
    assert result["partial_day"] and result["precipitation"] == 2


def test_met_overlapping_periods_do_not_double_count_precipitation():
    result = met_forecast([met_point(0, hours=6, amount=6), met_point(3, hours=6, amount=6)])
    assert result["precipitation"] == 6 and result["partial_day"]


def test_met_complete_six_hourly_forecast_is_full_day():
    result = met_forecast([met_point(hour, hours=6, amount=6) for hour in (0, 6, 12, 18)])
    assert result["precipitation"] == 24 and not result["partial_day"]


def test_representative_condition_uses_local_minutes_in_quarter_hour_timezone():
    # Kathmandu is UTC+05:45: 06:05 UTC is 11:50 local, closer to noon
    # than 06:35 UTC (12:20 local). Mixing UTC minutes with local hours
    # incorrectly makes the second sample look closer.
    rows = []
    for minute, condition in ((5, "clear"), (35, "rain")):
        row = met_point(6)
        row["time"] = f"2026-10-12T06:{minute:02}:00Z"
        row["data"]["next_1_hours"]["summary"]["symbol_code"] = condition
        rows.append(row)
    result = WeatherService(FakeHttp({"properties": {"timeseries": rows}}), now=lambda: NOW).forecast(
        coords("met_no", timezone="Asia/Kathmandu", user_agent="Robbi/2 test@example.org"))
    assert result["condition"] == "clear"


def test_openweather_credits_final_midnight_period_to_previous_day():
    rows = [{"dt": datetime(2026, 10, 12, hour, tzinfo=timezone.utc).timestamp(),
             "main": {"temp": 10}, "rain": {"3h": 3}} for hour in (3, 6, 9, 12, 15, 18, 21)]
    rows.append({"dt": datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp(),
                 "main": {"temp": 99}, "rain": {"3h": 3}})
    result = WeatherService(FakeHttp({"list": rows}), now=lambda: NOW).forecast(coords("openweather", api_key="test"))
    assert result["precipitation"] == 24 and not result["partial_day"]
    assert result["temperature_max"] == 10


def test_malformed_refresh_does_not_poison_good_cached_forecast():
    expired = HttpResponse(200, {"expires": formatdate(time.time() - 1, usegmt=True)}, json.dumps(daily()).encode())
    transport = FakeHttp(expired, {"daily": {"time": ["2026-10-12"]}}, ProviderError("offline"))
    service = WeatherService(transport, now=lambda: NOW)
    first = service.forecast(coords())
    with pytest.raises(ProviderError):
        service.forecast(coords())
    fallback = service.forecast(coords())
    assert fallback["stale"] and fallback["temperature_max"] == first["temperature_max"]
    assert fallback["retrieved_at"] == first["retrieved_at"]


def test_changed_response_without_validator_does_not_reuse_old_last_modified():
    expired = {"expires": formatdate(time.time() - 1, usegmt=True)}
    transport = FakeHttp(HttpResponse(200, {**expired, "last-modified": "Sun, 11 Oct 2026 10:00:00 GMT"}, json.dumps(daily()).encode()),
                         HttpResponse(200, expired, json.dumps(daily()).encode()), daily())
    service = WeatherService(transport, now=lambda: NOW)
    for _ in range(3):
        service.forecast(coords())
    assert "If-Modified-Since" in transport.calls[1][2]["headers"]
    assert "If-Modified-Since" not in transport.calls[2][2]["headers"]


@pytest.mark.parametrize("field,value", [("precipitation_sum", -3), ("precipitation_probability_max", 150),
                                        ("wind_speed_10m_max", -2)])
def test_invalid_measurements_are_unavailable_instead_of_physical_nonsense(field, value):
    payload = daily()
    payload["daily"][field] = [value]
    result = WeatherService(FakeHttp(payload), now=lambda: NOW).forecast(coords())
    output = {"precipitation_sum": "precipitation", "precipitation_probability_max": "precipitation_probability", "wind_speed_10m_max": "wind_speed_max"}[field]
    assert result[output] is None


def test_geocoding_returned_choices_are_bounded_and_have_valid_coordinates():
    rows = [place("bad", 500, 0), place("x" * 40000, 1, 2, "UTC"), place("Other", 3, 4, "UTC")]
    result = WeatherService(FakeHttp({"results": rows}), now=lambda: NOW).forecast({}, "Example")
    assert not result["ok"] and len(result["choices"]) == 2
    assert len(json.dumps(result).encode()) < 65536
    assert all(-90 <= item["latitude"] <= 90 and len(item["name"]) <= 300 for item in result["choices"])


@pytest.mark.parametrize("month,day,hours", [(3, 29, 23), (10, 25, 25)])
def test_met_coverage_uses_real_dst_day_duration(month, day, hours):
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, month, day, tzinfo=zone).astimezone(timezone.utc)
    rows = [{**met_point(0), "time": (start + timedelta(hours=offset)).isoformat()} for offset in range(hours)]
    result = WeatherService(FakeHttp({"properties": {"timeseries": rows}}), now=lambda: start).forecast(
        coords("met_no", timezone="Europe/Berlin", user_agent="Robbi/2 test@example.org"), day="today")
    assert result["precipitation"] == hours and not result["partial_day"]


def test_units_are_converted_before_rounding_and_precipitation_before_summing():
    result = met_forecast([met_point(hour, amount=.004) for hour in range(24)])
    assert result["precipitation"] == .1
    row = {"dt": datetime(2026, 10, 12, 12, tzinfo=timezone.utc).timestamp(), "main": {"temp": 10},
           "wind": {"speed": 1.234}, "pop": .254}
    result = WeatherService(FakeHttp({"list": [row]}), now=lambda: NOW).forecast(coords("openweather", api_key="test"))
    assert result["wind_speed_max"] == 4.44 and result["precipitation_probability"] == 25.4


def test_unknown_rain_amount_remains_unavailable_but_absent_phenomenon_means_zero():
    base = {"dt": datetime(2026, 10, 12, 12, tzinfo=timezone.utc).timestamp(), "main": {"temp": 10}}
    for row, expected in [(base, 0), ({**base, "rain": {"3h": None}}, None), ({**base, "rain": {"3h": -1}}, None)]:
        result = WeatherService(FakeHttp({"list": [row]}), now=lambda: NOW).forecast(coords("openweather", api_key="test"))
        assert result["precipitation"] == expected


@pytest.mark.parametrize("payload", [[], {"error": True, "reason": "private provider details"}, {"results": {}}])
def test_malformed_geocoder_is_reported_as_provider_failure(payload):
    with pytest.raises(ProviderError):
        WeatherService(FakeHttp(payload), now=lambda: NOW).forecast({}, "Example")


def test_non_list_daily_dates_and_inverted_temperatures_are_rejected():
    payload = daily()
    payload["daily"]["time"] = "2026-10-12"
    with pytest.raises(ProviderError):
        WeatherService(FakeHttp(payload), now=lambda: NOW).forecast(coords())
    payload = daily()
    payload["daily"]["temperature_2m_min"] = [20]
    with pytest.raises(ProviderError):
        WeatherService(FakeHttp(payload), now=lambda: NOW).forecast(coords())


def test_huge_numeric_values_and_unknown_condition_code_do_not_crash_or_invent_weather():
    payload = daily()
    payload["daily"]["temperature_2m_min"] = [10 ** 500]
    payload["daily"]["weather_code"] = [61.001]
    result = WeatherService(FakeHttp(payload), now=lambda: NOW).forecast(coords())
    assert result["temperature_min"] is None and result["condition"] == "conditions unavailable"
    assert "Maximum 15.8" in result["summary"]


def test_cached_http_endpoint_still_requires_explicit_http_configuration():
    service = WeatherService(FakeHttp(daily()), now=lambda: NOW)
    config = coords(endpoint="http://localhost/forecast", allow_http=True)
    service.forecast(config)
    with pytest.raises(ValidationError, match="HTTPS"):
        service.forecast({**config, "allow_http": False})
