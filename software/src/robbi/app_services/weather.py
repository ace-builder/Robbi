"""Small weather adapters with explicit place, date, units and attribution."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone as utc_timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robbi.errors import ValidationError
from .http import HttpClient, ProviderError, bounded_number, checkpoint, checked_url, query_url
from .images import api_key

_GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
_ENDPOINTS = {"open_meteo": "https://api.open-meteo.com/v1/forecast",
              "met_no": "https://api.met.no/weatherapi/locationforecast/2.0/compact",
              "openweather": "https://api.openweathermap.org/data/2.5/forecast"}
_ATTRIBUTION = {
    "open_meteo": [{"name": "Open-Meteo", "url": "https://open-meteo.com/", "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/"}],
    "met_no": [{"name": "MET Norway", "url": "https://www.met.no/", "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/", "changes": "Aggregated into a daily summary"}],
    "openweather": [{"name": "OpenWeather", "url": "https://openweathermap.org/", "changes": "Aggregated from 3-hour forecast samples"}],
}
_WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
        51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light rain showers",
        81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
        95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}


@dataclass
class _Cached:
    body: bytes
    fetched: float
    expires: float
    modified: str


def _number(value, minimum=-1_000_000, maximum=1_000_000, *, precision=2):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if math.isfinite(number) and minimum <= number <= maximum:
            return number if precision is None else round(number, precision)
        return None
    except (TypeError, ValueError, OverflowError):
        return None


def _coordinate(value, minimum, maximum):
    if isinstance(value, bool):
        raise ValidationError("Coordinates must be finite numbers")
    try:
        return bounded_number(value, 0, minimum, maximum)
    except OverflowError:
        raise ValidationError("Coordinates are outside their allowed range") from None


def _text(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _day_bounds(selected_day, zone):
    # Convert each local midnight independently: DST days can span 23 or 25 hours.
    return tuple(datetime.combine(day, clock_time.min, zone).astimezone(utc_timezone.utc)
                 for day in (selected_day, selected_day + timedelta(days=1)))


def _zone(name: str):
    if not isinstance(name, str) or len(name) > 80:
        raise ValidationError("Timezone must be an IANA name such as Europe/Berlin")
    if name in {"UTC", "Etc/UTC", "GMT"}:
        return utc_timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError("Unknown timezone or missing timezone data; use an IANA zone such as Europe/Berlin (install tzdata if needed)") from None


def _day(value, zone, now):
    today = now.astimezone(zone).date()
    relative = {"today": 0, "heute": 0, "tomorrow": 1, "morgen": 1,
                "day after tomorrow": 2, "übermorgen": 2}
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in relative:
            return today + timedelta(days=relative[value.lower()])
    try:
        selected = date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError("Weather date must be today, tomorrow or YYYY-MM-DD") from None
    if not today <= selected <= today + timedelta(days=15):
        raise ValidationError("Choose a forecast date from today through the next 15 days")
    return selected


class WeatherService:
    def __init__(self, http=None, *, now=None):
        self.http = http or HttpClient()
        self.now = now or (lambda: datetime.now(utc_timezone.utc))
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._cache_bytes = 0

    def _get(self, url, *, headers=None, ttl=600, cancellation=None, deadline=None, allow_http=False, validate=None):
        checked_url(url, allow_http=allow_http)
        headers = dict(headers or {})
        cache_key = (url, hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).digest())
        current_time = time.time()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached:
                self._cache.move_to_end(cache_key)
        checkpoint(cancellation, deadline)
        if cached and cached.expires > current_time:
            return json.loads(cached.body), cached.fetched, True, False
        if cached and cached.modified:
            headers["If-Modified-Since"] = cached.modified
        try:
            response = self.http.request("GET", url, headers=headers, timeout_s=15, max_bytes=1024 * 1024,
                                         cancellation=cancellation, deadline=deadline, allow_http=allow_http)
        except (ProviderError, TimeoutError):
            checkpoint(cancellation, deadline)
            if cached and current_time - cached.fetched <= 6 * 3600:
                return json.loads(cached.body), cached.fetched, True, True
            raise
        checkpoint(cancellation, deadline)
        body = response.body
        if response.status == 304:
            if not cached:
                raise ProviderError("Weather provider returned 304 without a cached forecast")
            body = cached.body
        else:
            response.json()  # Validate before admitting bytes to the cache.
        payload = json.loads(body)
        if validate is not None:
            validate(payload)  # A JSON-valid error must not replace the last usable forecast.
        expires = current_time + ttl
        if response.headers.get("expires"):
            try:
                expires = max(current_time, parsedate_to_datetime(response.headers["expires"]).timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
        previous_modified = cached.modified if cached and response.status == 304 else ""
        entry = _Cached(body, current_time, expires, response.headers.get("last-modified", previous_modified))
        with self._cache_lock:
            previous = self._cache.pop(cache_key, None)
            if previous:
                self._cache_bytes -= len(previous.body)
            self._cache[cache_key] = entry
            self._cache_bytes += len(body)
            while len(self._cache) > 16 or self._cache_bytes > 2 * 1024 * 1024:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= len(old.body)
        return payload, current_time, response.status == 304, False

    def forecast(self, config: dict, location: str = "", day: str = "tomorrow", *, latitude=None,
                 longitude=None, timezone=None, country_code=None, cancellation=None, deadline=None) -> dict:
        checkpoint(cancellation, deadline)
        provider = str(config.get("provider", "open_meteo")).lower().replace("-", "_")
        provider = {"openmeteo": "open_meteo", "met": "met_no", "openweathermap": "openweather"}.get(provider, provider)
        if provider not in {*_ENDPOINTS, "generic"}:
            raise ValidationError("Unknown weather provider")
        deadline = min(deadline or float("inf"), time.monotonic() + bounded_number(config.get("timeout_s"), 25, 1, 90))
        common = dict(cancellation=cancellation, deadline=deadline, allow_http=config.get("allow_http", False) is True)
        if not isinstance(location, str):
            raise ValidationError("Location must be a city name")
        location = location.strip()
        explicit_coordinates = latitude is not None or longitude is not None
        use_home = not location and not explicit_coordinates
        # A location is one coherent set of coordinates, name and timezone.
        # Never complete a remote location with unrelated home settings.
        if use_home:
            latitude, longitude = config.get("latitude"), config.get("longitude")
        place_name = str(config.get("default_location", "") if use_home else location).strip()
        if len(place_name) > 160:
            raise ValidationError("Location must contain at most 160 characters")
        place_zone = timezone or (config.get("timezone") if use_home else None)
        geocoded = False
        if latitude is None and longitude is None:
            if not place_name:
                raise ValidationError("Name a city or configure default_location")
            country = str(country_code or (config.get("country_code", "") if use_home else "")).upper()
            if country and (len(country) != 2 or not country.isascii() or not country.isalpha()):
                raise ValidationError("country_code must be a two-letter country code")
            query = query_url(config.get("geocoding_endpoint") or _GEOCODING,
                              {"name": place_name, "count": 5, "language": str(config.get("language", "en")),
                               "format": "json", "countryCode": country or None})
            def validate_geocode(payload):
                if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("results", []), list):
                    raise ProviderError("Geocoding provider returned an invalid location list")
            geocode, _, _, _ = self._get(query, ttl=86400, validate=validate_geocode, **common)
            validate_geocode(geocode)
            places = geocode.get("results", [])
            choices = []
            seen = set()
            for place in places[:5]:
                if not isinstance(place, dict) or _number(place.get("latitude"), -90, 90) is None or _number(place.get("longitude"), -180, 180) is None:
                    continue
                place_country = _text(place.get("country_code"), 2).upper()
                if country and place_country != country:
                    continue
                key = (round(float(place["latitude"]), 3), round(float(place["longitude"]), 3))
                if key in seen:
                    continue
                seen.add(key)
                feature = str(place.get('feature_code', ''))
                choices.append({"name": ", ".join(_text(place.get(k), 100) for k in ("name", "admin1", "country") if _text(place.get(k), 100))[:300],
                                "latitude": float(place["latitude"]), "longitude": float(place["longitude"]),
                                "timezone": _text(place.get("timezone"), 80) or None, "country_code": place_country or None,
                                'locality': _text(place.get('name'), 100),
                                'admin1': _text(place.get('admin1'), 100),
                                'kind': 'city' if feature.startswith('PPL') else 'region' if feature.startswith('ADM') else 'unknown',
                                'population': _number(place.get('population'), 0, 1000000000) or 0})
            if not choices:
                raise ValidationError("Location was not found; add a country code or provide coordinates and timezone")
            from robbi.services.location_context import dominant_city
            preferred = dominant_city(choices, place_name)
            if len(choices) > 1 and preferred is None:
                return {"ok": False, "needs_location": True, "choices": choices,
                        "summary": "Several places match. Choose a location using its coordinates and timezone."}
            place = preferred or choices[0]
            latitude, longitude = place["latitude"], place["longitude"]
            place_name = place["name"]
            # A per-request explicit timezone may override geocoding; a home
            # timezone must not silently replace the timezone of a remote city.
            place_zone = timezone or place.get("timezone")
            geocoded = True
        if latitude is None or longitude is None:
            raise ValidationError("Provide both latitude and longitude")
        latitude = _coordinate(latitude, -90, 90)
        longitude = _coordinate(longitude, -180, 180)
        if not place_zone:
            raise ValidationError("Coordinates require a timezone (for example Europe/Berlin)")
        zone = _zone(place_zone)
        selected_day = _day(day, zone, self.now())
        place_name = place_name or f"{latitude:.4f}, {longitude:.4f}"
        endpoint = config.get("endpoint") or _ENDPOINTS.get(provider)
        if not endpoint:
            raise ValidationError("Configure the generic weather endpoint")
        headers = {}
        if provider == "open_meteo":
            parameters = {"latitude": f"{latitude:.4f}", "longitude": f"{longitude:.4f}", "timezone": place_zone,
                          "start_date": selected_day.isoformat(), "end_date": selected_day.isoformat(),
                          "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
                          "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"}
            if config.get("api_key") or config.get("api_key_env"):
                parameters["apikey"] = api_key(config, required=True)
        elif provider == "met_no":
            contact = str(config.get("user_agent", "")).strip()
            if not contact or not any(token in contact for token in ("@", "https://", "http://")):
                raise ValidationError("MET Norway requires user_agent with your real contact email or website")
            headers["User-Agent"] = contact
            parameters = {"lat": f"{latitude:.4f}", "lon": f"{longitude:.4f}"}
        elif provider == "openweather":
            parameters = {"lat": f"{latitude:.4f}", "lon": f"{longitude:.4f}", "units": "metric",
                          "appid": api_key(config, "OPENWEATHER_API_KEY"), "lang": str(config.get("language", "en"))}
        else:
            parameters = {"latitude": f"{latitude:.4f}", "longitude": f"{longitude:.4f}", "location": place_name,
                          "date": selected_day.isoformat(), "timezone": place_zone}
            key = api_key(config, required=False)
            if key:
                headers["Authorization"] = "Bearer " + key
        def parse_forecast(payload):
            if not isinstance(payload, dict):
                raise ProviderError("Weather provider returned an invalid forecast")
            try:
                if provider == "open_meteo":
                    return self._open_meteo(payload, selected_day)
                if provider == "met_no":
                    return self._met(payload, selected_day, zone)
                if provider == "openweather":
                    return self._openweather(payload, selected_day, zone)
                return self._generic(payload, selected_day)
            except (AttributeError, TypeError, KeyError, IndexError, ValueError, OverflowError):
                raise ProviderError("Weather provider returned malformed forecast data") from None
        payload, fetched, cached, stale = self._get(query_url(endpoint, parameters), headers=headers,
                                                  ttl=bounded_number(config.get("cache_ttl_s"), 600, 60, 3600),
                                                  validate=parse_forecast, **common)
        forecast = parse_forecast(payload)
        attribution = list(_ATTRIBUTION.get(provider, [{"name": str(config.get("attribution_name", "Configured weather provider")),
                                                       "url": str(config.get("attribution_url", endpoint.split("?")[0]))}]))
        if geocoded:
            attribution.append({"name": "GeoNames via Open-Meteo geocoding", "url": "https://open-meteo.com/en/docs/geocoding-api"})
        result = {"ok": True, "provider": provider,
                  "location": {"name": place_name, "latitude": latitude, "longitude": longitude, "timezone": place_zone},
                  "date": selected_day.isoformat(), **forecast,
                  "units": {"temperature": "°C", "precipitation": "mm", "wind_speed": "km/h", "probability": "%"},
                  "attribution": attribution, "cached": cached, "stale": stale,
                  "retrieved_at": datetime.fromtimestamp(fetched, utc_timezone.utc).isoformat()}
        result["summary"] = self._summary(result)
        checkpoint(cancellation, deadline)
        return result

    @staticmethod
    def _open_meteo(payload, selected_day):
        daily = payload.get("daily", {})
        times = daily.get("time", []) if isinstance(daily, dict) else []
        if not isinstance(times, list):
            raise ProviderError("Weather provider returned an invalid daily date list")
        try:
            position = times.index(selected_day.isoformat())
        except (ValueError, AttributeError):
            raise ProviderError("No forecast is available for the requested date") from None
        def field(name, minimum=-1_000_000, maximum=1_000_000, *, precision=2):
            values = daily.get(name, [])
            return _number(values[position], minimum, maximum, precision=precision) if isinstance(values, list) and len(values) > position else None
        code = field("weather_code", precision=None)
        result = {"condition": _WMO.get(code, "conditions unavailable"),
                  "temperature_min": field("temperature_2m_min", -273.15), "temperature_max": field("temperature_2m_max", -273.15),
                  "precipitation": field("precipitation_sum", 0), "precipitation_probability": field("precipitation_probability_max", 0, 100),
                  "wind_speed_max": field("wind_speed_10m_max", 0), "partial_day": False}
        if result["temperature_min"] is None and result["temperature_max"] is None:
            raise ProviderError("Weather forecast contains no temperature data")
        if result["temperature_min"] is not None and result["temperature_max"] is not None and result["temperature_min"] > result["temperature_max"]:
            raise ProviderError("Weather forecast has an inverted temperature range")
        return result

    @staticmethod
    def _met(payload, selected_day, zone):
        series = payload.get("properties", {}).get("timeseries", [])
        points = []
        for row in series[:400] if isinstance(series, list) else []:
            try:
                moment = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                if moment.tzinfo is None or moment.astimezone(zone).date() != selected_day:
                    continue
                moment = moment.astimezone(utc_timezone.utc)
                data = row["data"]
                instant = data.get("instant", {}).get("details", {})
                period = data.get("next_1_hours") or data.get("next_6_hours") or {}
                hours = 1 if data.get("next_1_hours") else 6
                details = period.get("details", {})
                wind = _number(instant.get("wind_speed"), 0, precision=None)
                points.append({"temperature": _number(instant.get("air_temperature"), -273.15),
                               "wind": None if wind is None else wind * 3.6,
                               "rain": _number(details.get("precipitation_amount"), 0, precision=None),
                               "probability": _number(details.get("probability_of_precipitation"), 0, 100),
                               "condition": _text(period.get("summary", {}).get("symbol_code"), 160).replace("_", " "),
                               "time": moment, "start": moment, "end": moment + timedelta(hours=hours)})
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                continue
        return WeatherService._aggregate(points, selected_day, zone, "Forecast sample range; not guaranteed daily extrema")

    @staticmethod
    def _openweather(payload, selected_day, zone):
        points = []
        start, end = _day_bounds(selected_day, zone)
        for row in payload.get("list", [])[:80]:
            try:
                moment = datetime.fromtimestamp(float(row["dt"]), utc_timezone.utc)
                in_day = start <= moment < end
                period_start = moment - timedelta(hours=3)
                if not in_day and not (start <= period_start and moment <= end):
                    continue
                conditions = row.get("weather", [])
                wind = _number(row.get("wind", {}).get("speed"), 0, precision=None)
                probability = _number(row.get("pop"), 0, 1, precision=None)
                # OWM rain/snow 3h describe the preceding three hours.
                # An absent phenomenon means zero; an invalid supplied amount
                # is unknown. Include the interval ending at the next midnight.
                amounts = [0 if kind not in row else _number(row[kind].get("3h"), 0, precision=None) if isinstance(row[kind], dict) else None
                           for kind in ("rain", "snow")]
                rain = sum(amounts) if all(amount is not None for amount in amounts) else None
                points.append({"temperature": _number(row.get("main", {}).get("temp"), -273.15) if in_day else None,
                               "wind": None if wind is None or not in_day else wind * 3.6,
                               "rain": rain,
                               "probability": None if probability is None else probability * 100,
                               "condition": _text(conditions[0].get("description"), 160) if conditions else "",
                               "time": moment, "start": period_start, "end": moment})
            except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
                continue
        return WeatherService._aggregate(points, selected_day, zone, "3-hour forecast sample range; not guaranteed daily extrema")

    @staticmethod
    def _aggregate(points, selected_day, zone, note):
        temperatures = [p["temperature"] for p in points if p["temperature"] is not None]
        if not temperatures:
            raise ProviderError("No forecast is available for the requested date")
        winds = [p["wind"] for p in points if p["wind"] is not None]
        start, end = _day_bounds(selected_day, zone)
        periods = [p for p in points if start <= p["start"] and p["end"] <= end]
        probabilities = [p["probability"] for p in periods if p["probability"] is not None]
        rain, covered, previous_end = [], 0, start
        for point in sorted(periods, key=lambda p: (p["start"], p["end"])):
            if point["rain"] is None or point["start"] < previous_end:
                continue
            rain.append(point["rain"])
            covered += (point["end"] - point["start"]).total_seconds()
            previous_end = point["end"]
        # Condition nearest local noon is labelled as representative, not worst.
        samples = [p for p in points if start <= p["time"] < end]
        local_noon = datetime.combine(selected_day, clock_time(12), zone).astimezone(utc_timezone.utc)
        representative = min(samples, key=lambda p: abs((p["time"] - local_noon).total_seconds()))
        return {"condition": representative["condition"] or "conditions unavailable",
                "condition_basis": "forecast near local noon", "temperature_min": round(min(temperatures), 2),
                "temperature_max": round(max(temperatures), 2), "wind_speed_max": round(max(winds), 2) if winds else None,
                "precipitation": round(sum(rain), 2) if rain else None,
                "precipitation_probability": round(max(probabilities), 2) if probabilities else None,
                "partial_day": covered < (end - start).total_seconds(),
                "sample_count": len(samples), "note": note}

    @staticmethod
    def _generic(payload, selected_day):
        if payload.get("date") != selected_day.isoformat():
            raise ProviderError("Generic weather endpoint returned a different or missing date")
        units = payload.get("units", {})
        if units.get("temperature") not in {"C", "°C", "celsius"} or units.get("wind_speed") not in {"km/h", "kmh"} or units.get("precipitation") != "mm":
            raise ProviderError("Generic weather endpoint must return Celsius, km/h and mm units")
        result = {}
        for name in ("precipitation", "wind_speed_max"):
            result[name] = _number(payload.get(name), 0)
        result["precipitation_probability"] = _number(payload.get("precipitation_probability"), 0, 100)
        for name in ("temperature_min", "temperature_max"):
            result[name] = _number(payload.get(name), -273.15)
        if result["temperature_min"] is None and result["temperature_max"] is None:
            raise ProviderError("Generic weather response contains no valid temperature data")
        if result["temperature_min"] is not None and result["temperature_max"] is not None and result["temperature_min"] > result["temperature_max"]:
            raise ProviderError("Generic weather response has an inverted temperature range")
        if not isinstance(payload.get("partial_day", False), bool):
            raise ProviderError("Generic weather partial_day must be true or false")
        result.update(condition=_text(payload.get("condition"), 160) or "conditions unavailable",
                      partial_day=payload.get("partial_day", False))
        return result

    @staticmethod
    def _summary(result):
        pieces = [f"{result['location']['name']}, {result['date']} ({result['location']['timezone']}): {result['condition']}."]
        low, high = result["temperature_min"], result["temperature_max"]
        if low is not None and high is not None:
            pieces.append(f"{low:g}–{high:g} °C.")
        elif low is not None:
            pieces.append(f"Minimum {low:g} °C; maximum unavailable.")
        elif high is not None:
            pieces.append(f"Maximum {high:g} °C; minimum unavailable.")
        if result.get("precipitation_probability") is not None:
            pieces.append(f"Precipitation probability up to {result['precipitation_probability']:g}%.")
        if result.get("precipitation") is not None:
            qualifier = "Available forecast periods" if result.get("partial_day") else "Precipitation"
            pieces.append(f"{qualifier}: {result['precipitation']:g} mm.")
        if result.get("stale"):
            pieces.append("Refresh unavailable; using a cached forecast. Check the retrieval time.")
        if result.get("partial_day"):
            pieces.append("Only part of the day is covered.")
        if result.get("note"):
            pieces.append(result["note"] + ".")
        return " ".join(pieces)
