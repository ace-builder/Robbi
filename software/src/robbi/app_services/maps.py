"""Key-free Maps links and optional, explicitly requested Google Maps API reads."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from urllib.parse import urlencode

from robbi.errors import ValidationError
from .http import HttpClient, ProviderError, bounded_number, checkpoint, query_url
from .images import api_key

_MODES = {"driving": "DRIVE", "walking": "WALK", "bicycling": "BICYCLE", "transit": "TRANSIT"}
_ATTRIBUTION = {"name": "Google Maps", "url": "https://maps.google.com/"}


def _text(value, label, *, optional=False, maximum=300):
    if not isinstance(value, str) or len(value) > maximum or (not value.strip() and not optional):
        raise ValidationError(f"{label} must contain {'0' if optional else '1'}–{maximum} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationError(f"{label} must not contain control characters")
    return value.strip()


def _coordinates(value):
    pieces = value.split(",")
    if len(pieces) != 2:
        return None
    try:
        latitude, longitude = (float(piece.strip()) for piece in pieces)
    except ValueError:
        return None  # An ordinary address, e.g. Berlin, Germany.
    if not math.isfinite(latitude) or not -90 <= latitude <= 90 or not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValidationError("Coordinates require latitude -90 to 90 and longitude -180 to 180")
    return {"latitude": latitude, "longitude": longitude}


def _location(value, label, *, optional=False):
    value = _text(value, label, optional=optional)
    _coordinates(value)
    return value


def _mode(value):
    if not isinstance(value, str) or value not in _MODES:
        raise ValidationError("Travel mode must be driving, walking, bicycling or transit")
    return value


def _url(path, parameters):
    url = "https://www.google.com/maps/" + path + "/?" + urlencode({"api": 1, **parameters})
    if len(url) > 2048:
        raise ValidationError("Google Maps link exceeds 2,048 characters; shorten the place names")
    return url


def search_url(query: str, place_id: str = "") -> str:
    parameters = {"query": _location(query, "Place")}
    place_id = _text(place_id, "Place ID", optional=True, maximum=250)
    if place_id:
        parameters["query_place_id"] = place_id
    return _url("search", parameters)


def directions_url(destination: str, origin: str = "", mode: str = "driving") -> str:
    parameters = {"destination": _location(destination, "Destination"), "travelmode": _mode(mode)}
    origin = _location(origin, "Origin", optional=True)
    if origin:
        parameters["origin"] = origin
    return _url("dir", parameters)


def _waypoint(value):
    coordinates = _coordinates(value)
    return {"location": {"latLng": coordinates}} if coordinates is not None else {"address": value}


def _valid_number(value, maximum):
    return type(value) in {int, float} and 0 <= value <= maximum and math.isfinite(value)


class MapsService:
    def __init__(self, http=None):
        self.http = http or HttpClient()

    @staticmethod
    def _options(config, cancellation, deadline):
        return {"timeout_s": bounded_number(config.get("timeout_s"), 20, 1, 30),
                "max_bytes": 256 * 1024, "cancellation": cancellation, "deadline": deadline}

    def geocode(self, config: dict, address: str, *, cancellation=None, deadline=None) -> dict:
        checkpoint(cancellation, deadline)
        address = _text(address, "Address")
        if _coordinates(address) is not None:
            raise ValidationError("Geocoding requires a street address; use search for coordinates")
        key = api_key(config, "GOOGLE_MAPS_API_KEY")
        response = self.http.request(
            "GET", query_url("https://maps.googleapis.com/maps/api/geocode/json", {"address": address, "key": key}),
            **self._options(config, cancellation, deadline),
        )
        checkpoint(cancellation, deadline)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderError("Google Maps returned an invalid geocoding response")
        status = payload.get("status")
        if not isinstance(status, str):
            raise ProviderError("Google Maps returned an invalid geocoding status")
        if status not in {"OK", "ZERO_RESULTS"}:
            hint = {"OVER_QUERY_LIMIT": "Quota reached; check Google Maps usage limits",
                    "OVER_DAILY_LIMIT": "Check Google Maps billing, quota and API key",
                    "REQUEST_DENIED": "Check that Geocoding API is enabled and the API key is allowed",
                    "INVALID_REQUEST": "Google Maps could not accept this address"}.get(status, "Google Maps geocoding failed; try again later")
            raise ProviderError(hint)
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise ProviderError("Google Maps returned an invalid location list")
        places = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            geometry = row.get("geometry")
            point = geometry.get("location") if isinstance(geometry, dict) else None
            if not isinstance(point, dict):
                continue
            lat, lon = point.get("lat"), point.get("lng")
            if type(lat) not in {int, float} or type(lon) not in {int, float}:
                continue
            if not -90 <= lat <= 90 or not -180 <= lon <= 180 or not math.isfinite(lat) or not math.isfinite(lon):
                continue
            label = row.get("formatted_address")
            if not isinstance(label, str) or not label.strip():
                continue
            label = " ".join(label.split())[:300]
            place_id = row.get("place_id", "")
            place_id = place_id if isinstance(place_id, str) and len(place_id) <= 250 and not any(ord(c) < 32 or ord(c) == 127 for c in place_id) else ""
            places.append({"address": label, "latitude": lat, "longitude": lon, "place_id": place_id,
                           "partial_match": row.get("partial_match") is True,
                           "url": search_url(f"{lat},{lon}", place_id)})
        if status == "OK" and not places:
            raise ProviderError("Google Maps returned no usable locations")
        if status == "ZERO_RESULTS":
            places = []
        needs_choice = len(places) > 1 or any(place["partial_match"] for place in places)
        summary = f"Google Maps found {len(places)} address matches."
        if needs_choice:
            summary += " Confirm the intended match before using its coordinates."
        if not places:
            summary = "Google Maps found no address matches. Try a complete street address."
        return {"ok": bool(places), "provider": "google_maps", "query": address, "results": places,
                "needs_choice": needs_choice, "summary": summary, "attribution": [dict(_ATTRIBUTION)]}

    def route(self, config: dict, origin: str, destination: str, *, mode="driving", cancellation=None, deadline=None) -> dict:
        checkpoint(cancellation, deadline)
        origin, destination = _location(origin, "Origin"), _location(destination, "Destination")
        mode = _mode(mode)
        link = directions_url(destination, origin, mode)
        key = api_key(config, "GOOGLE_MAPS_API_KEY")
        body = {"origin": _waypoint(origin), "destination": _waypoint(destination), "travelMode": _MODES[mode],
                "computeAlternativeRoutes": False, "units": "METRIC"}
        if mode == "driving":
            body["routingPreference"] = "TRAFFIC_UNAWARE"
        response = self.http.request(
            "POST", "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.warnings"},
            json_body=body, **self._options(config, cancellation, deadline),
        )
        checkpoint(cancellation, deadline)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("routes", []), list):
            raise ProviderError("Google Maps returned an invalid route response")
        base = {"provider": "google_maps", "origin": origin, "destination": destination, "mode": mode,
                "url": link, "link_only": False, "traffic_aware": False,
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "attribution": [dict(_ATTRIBUTION)]}
        if not payload.get("routes"):
            return {**base, "ok": False, "summary": "Google Maps found no route for these locations and travel mode."}
        route = payload["routes"][0]
        if not isinstance(route, dict):
            raise ProviderError("Google Maps returned an invalid route")
        # Protobuf JSON omits default-valued scalar fields, even in a field
        # mask: an absent distanceMeters represents a valid zero distance.
        distance, duration = route.get("distanceMeters", 0), route.get("duration")
        if type(distance) is not int or not _valid_number(distance, 1_000_000_000) or not isinstance(duration, str) or not re.fullmatch(r"\d{1,10}(?:\.\d{1,9})?s", duration):
            raise ProviderError("Google Maps route is missing valid distance or duration")
        seconds = float(duration[:-1])
        if not _valid_number(seconds, 100_000_000):
            raise ProviderError("Google Maps returned an invalid route duration")
        warnings = route.get("warnings", [])
        if not isinstance(warnings, list) or len(warnings) > 5 or any(not isinstance(warning, str) or len(warning) > 400 for warning in warnings):
            raise ProviderError("Google Maps returned invalid route warnings")
        warnings = [" ".join(warning.split()) for warning in warnings]
        if mode in {"walking", "bicycling"}:
            warnings.insert(0, "Walking and cycling routes are in beta and may omit sidewalks or pedestrian and bicycle paths.")
        summary = f"Google Maps estimates {distance / 1000:g} km and {seconds / 60:.1f} minutes ({mode})."
        if mode == "driving":
            summary += " This estimate does not include live traffic."
        if warnings:
            summary += " " + " ".join(warnings)
        return {**base, "ok": True, "distance_meters": distance, "duration_seconds": seconds,
                "warnings": warnings, "summary": summary}
