"""Pinned, signed app-store transport. No remote code is imported here."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPConnection
import ipaddress
import json
from pathlib import Path
import re
import socket
import threading
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, ProxyHandler, build_opener

from robbi.app_services.http import HttpClient, checkpoint
from robbi.errors import ValidationError, UnavailableError
from robbi.storage import write_json
from .schema import identifier


class StoreNotConfigured(UnavailableError):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A configured origin is a trust boundary, even for public downloads.
        return None


class _LocalHTTPHandler(HTTPHandler):
    def http_open(self, request):
        # Validate once, then connect to that numeric address while urllib keeps
        # the original Host header. A second DNS answer cannot redirect HTTP.
        address = _local_http(request.full_url)

        def connection(host, **kwargs):
            result = HTTPConnection(host, **kwargs)
            connect = result._create_connection
            result._create_connection = lambda target, *args, **kw: connect((address, target[1]), *args, **kw)
            return result

        return self.do_open(connection, request)


def _public_key(value):
    try:
        if not isinstance(value, str) or len(value) != 44:
            raise ValueError
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
            raise ValueError
        return hashlib.sha256(raw).hexdigest()
    except (ValueError, TypeError):
        raise ValidationError("Store signing key must be a base64 Ed25519 public key") from None


def store_url(value, allow_http=False):
    if (not isinstance(value, str) or len(value) > 1000 or not value
            or any(ord(c) < 33 for c in value) or "\\" in value or type(allow_http) is not bool):
        raise ValidationError("Enter the Robbi App Hub's HTTPS address or an explicitly enabled local HTTP address")
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed/out-of-range ports before any request.
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "%" in parsed.netloc
                or not re.fullmatch(r"(?:/[a-zA-Z0-9_-]+)*/?", parsed.path)):
            raise ValueError
    except ValueError:
        raise ValidationError("Use a store URL without credentials, query, fragment or dot segments") from None
    if parsed.scheme == "http" and not allow_http:
        raise ValidationError("Enable local HTTP for a development store; use HTTPS for a public server")
    return value.rstrip("/")


def _local_http(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        return
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)}
    except (OSError, ValueError):
        raise UnavailableError("Cannot resolve the local Robbi App Hub") from None
    if not addresses or any(not (ip.is_private or ip.is_loopback) or ip.is_link_local
                            or ip.is_multicast or ip.is_unspecified or ip.is_reserved for ip in addresses):
        raise ValidationError("HTTP stores must resolve to local/private addresses; public stores require HTTPS")
    return str(sorted(addresses, key=lambda ip: (ip.version, int(ip)))[0])


def _timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValidationError("Store supplied an invalid signed timestamp") from None


def _record(value):
    if not isinstance(value, dict):
        raise ValidationError("Invalid store app record")
    identifier(value.get("id"))
    for field, limit in (("name", 120), ("version", 50), ("description", 2000)):
        if not isinstance(value.get(field), str) or len(value[field]) > limit:
            raise ValidationError("Invalid store app metadata")
    digest, size = value.get("sha256"), value.get("size")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or not 1 <= size <= 16 * 1024 * 1024
            or value.get("download_path") != f"/api/v1/packages/{digest}.zip"):
        raise ValidationError("Store package hash, length or download path is invalid")
    paths, hashes = value.get("image_paths", {}), value.get("image_hashes", {})
    if not isinstance(paths, dict) or not isinstance(hashes, dict) or set(paths) - {"icon", "screen"} or set(paths) != set(hashes):
        raise ValidationError("Invalid store image metadata")
    for kind, path in paths.items():
        if (path != f"/api/v1/images/{digest}/{kind}" or not isinstance(hashes[kind], str)
                or not re.fullmatch(r"[0-9a-f]{64}", hashes[kind])):
            raise ValidationError("Invalid signed image path or hash")
    if not isinstance(value.get("tools"), list) or not 1 <= len(value["tools"]) <= 12:
        raise ValidationError("Store app needs a bounded tool list")
    return copy.deepcopy(value)


class StoreClient:
    MAX_CATALOG_BYTES = 2 * 1024 * 1024

    def __init__(self, path, *, http=None, clock=None):
        self.path = Path(path)
        self._pinned_http = http is None
        self.http = http or HttpClient(opener=build_opener(ProxyHandler({}), _NoRedirects(), _LocalHTTPHandler()))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._config, self._error, self._cached = None, "", None
        self._generation = 0
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                raise ValidationError("Store settings exceed their limit")
            self._config = self._validate_config(json.loads(raw))
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError, RecursionError):
            self._error = "Store settings could not be read. Reconnect the store in App library."

    @staticmethod
    def _validate_config(value):
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValidationError("Invalid store settings")
        clean = {"version": 1, "url": store_url(value.get("url"), value.get("allow_http", False)),
                 "allow_http": value.get("allow_http", False), "public_key": value.get("public_key")}
        clean["fingerprint"] = _public_key(clean["public_key"])
        for key in ("store_id", "name"):
            if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 120:
                raise ValidationError("Invalid store identity")
            clean[key] = value[key]
        revision = value.get("revision", 0)
        if type(revision) is not int or not 0 <= revision < 2**63:
            raise ValidationError("Invalid store revision")
        clean["revision"] = revision
        return clean

    def status(self):
        with self._lock:
            return {"configured": self._config is not None, "store": copy.deepcopy(self._config), "error": self._error}

    def _get(self, config, path, *, limit, cancelled=None, deadline=None):
        checkpoint(cancelled, deadline)
        if not self._pinned_http:
            _local_http(config["url"])
        checkpoint(cancelled, deadline)
        if "_generation" in config:
            self._assert_current(config)
        return self.http.request("GET", config["url"] + path, max_bytes=limit,
                                 timeout_s=12, allow_http=config["allow_http"],
                                 cancellation=cancelled, deadline=deadline)

    def probe(self, url, *, allow_http=False, cancelled=None, deadline=None):
        config = {"url": store_url(url, allow_http), "allow_http": allow_http}
        info = self._get(config, "/api/v1/info", limit=8192, cancelled=cancelled, deadline=deadline).json()
        if not isinstance(info, dict) or info.get("schema_version") != 1:
            raise ValidationError("This server does not support Robbi store protocol 1")
        fingerprint = _public_key(info.get("public_key"))
        if info.get("fingerprint") != fingerprint:
            raise ValidationError("Store fingerprint does not match its signing key")
        clean = self._validate_config({**config, **{key: info.get(key) for key in ("name", "store_id", "public_key")},
                                       "version": 1, "revision": 0})
        return {**clean, "trusted": False}

    def connect(self, url, public_key, *, allow_http=False, cancelled=None, deadline=None):
        # Only the owner dashboard calls this, after showing the proposed key.
        expected = _public_key(public_key)
        candidate = self.probe(url, allow_http=allow_http, cancelled=cancelled, deadline=deadline)
        if candidate["fingerprint"] != expected:
            raise ValidationError("Store key changed during connection; review the new fingerprint")
        candidate.pop("trusted", None)
        with self._lock:
            checkpoint(cancelled, deadline)
            old = self._config
            if old and (old["store_id"], old["public_key"]) == (candidate["store_id"], candidate["public_key"]):
                candidate["revision"] = old["revision"]
            write_json(self.path, candidate)
            self._config, self._error, self._cached = candidate, "", None
            self._generation += 1
        return self.status()

    def disconnect(self):
        with self._lock:
            # An empty config is a valid disconnected state, not an old trust key.
            self.path.unlink(missing_ok=True)
            self._config, self._error, self._cached = None, "", None
            self._generation += 1
        return self.status()

    def _trusted(self):
        with self._lock:
            if self._config is None:
                raise StoreNotConfigured(self._error or "Connect a trusted Robbi App Hub in Robbi App Hub first")
            return {**copy.deepcopy(self._config), "_generation": self._generation}

    def _assert_current(self, config):
        with self._lock:
            if (self._config is None or config.get("_generation") != self._generation
                    or any(self._config.get(key) != config[key] for key in ("url", "store_id", "public_key", "allow_http"))):
                raise ValidationError("Store connection changed during this request; run it again")

    def _verified(self, config, envelope):
        from .store_protocol import verify_envelope
        payload = verify_envelope(envelope, config["public_key"])
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or payload.get("store_id") != config["store_id"]):
            raise ValidationError("Signed catalog belongs to another store or protocol")
        revision = payload.get("revision")
        if type(revision) is not int or not 0 <= revision < 2**63 or revision < config["revision"]:
            raise ValidationError("Store catalog is older than the last trusted revision")
        generated, expires = _timestamp(payload.get("generated_at")), _timestamp(payload.get("expires_at"))
        now = self.clock()
        if generated > now + timedelta(minutes=5) or expires <= now or not timedelta(0) < expires - generated <= timedelta(hours=25):
            raise ValidationError("Store catalog expired or has invalid dates; check the server and robot clocks")
        return payload

    def _accept(self, config, payload, *, cache=False, cancelled=None, deadline=None):
        with self._lock:
            checkpoint(cancelled, deadline)
            self._assert_current(config)
            if payload["revision"] < self._config["revision"]:
                raise ValidationError("Store catalog revision was superseded")
            if payload["revision"] > self._config["revision"]:
                updated = {**self._config, "revision": payload["revision"]}
                write_json(self.path, updated)
                self._config = updated
            if cache:
                self._cached = copy.deepcopy(payload)

    def catalog(self, *, refresh=False, cancelled=None, deadline=None):
        config = self._trusted()
        with self._lock:
            cached = copy.deepcopy(self._cached)
        if (not refresh and cached and timedelta(0) <= self.clock() - _timestamp(cached["generated_at"]) < timedelta(seconds=60)
                and _timestamp(cached["expires_at"]) > self.clock() and cached["revision"] >= config["revision"]):
            self._accept(config, cached, cancelled=cancelled, deadline=deadline)
            return cached
        envelope = self._get(config, "/api/v1/catalog", limit=self.MAX_CATALOG_BYTES, cancelled=cancelled, deadline=deadline).json()
        payload = self._verified(config, envelope)
        apps = payload.get("apps")
        if not isinstance(apps, list) or len(apps) > 256:
            raise ValidationError("Store catalog exceeds 256 apps")
        clean = [_record(item) for item in apps]
        if len({item["id"] for item in clean}) != len(clean):
            raise ValidationError("Store catalog repeats an app ID")
        payload["apps"] = clean
        self._accept(config, payload, cache=True, cancelled=cancelled, deadline=deadline)
        return payload

    def app(self, app_id, *, version=None, cancelled=None, deadline=None):
        identifier(app_id)
        config = self._trusted()
        path = "/api/v1/apps/" + app_id
        if version is not None:
            if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z.+-]{1,50}", version):
                raise ValidationError("Invalid app version")
            path += "?" + urlencode({"version": version})
        payload = self._verified(config, self._get(config, path, limit=256*1024, cancelled=cancelled, deadline=deadline).json())
        item = _record(payload.get("app"))
        if item["id"] != app_id or version is not None and item["version"] != version:
            raise ValidationError("Store returned a different app or version")
        self._accept(config, payload, cancelled=cancelled, deadline=deadline)
        return item

    def download(self, item, *, cancelled=None, deadline=None):
        item = _record(item)
        config = self._trusted()
        # Fetch a fresh signed exact-version record before every install. A UI
        # cannot turn a previously displayed app into a different unsigned ZIP.
        current = self.app(item["id"], version=item["version"], cancelled=cancelled, deadline=deadline)
        self._assert_current(config)
        if (current["sha256"], current["size"]) != (item["sha256"], item["size"]):
            raise ValidationError("This app version changed; refresh the store before installing")
        data = self._get(config, item["download_path"], limit=16*1024*1024, cancelled=cancelled, deadline=deadline).body
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValidationError("Downloaded app does not match its signed hash and size")
        checkpoint(cancelled, deadline)
        self._assert_current(config)
        return data

    def image(self, app_id, kind, *, cancelled=None, deadline=None):
        if kind not in {"icon", "screen"}:
            raise ValidationError("Unknown app image kind")
        config = self._trusted()
        item = next((app for app in self.catalog(cancelled=cancelled, deadline=deadline)["apps"] if app["id"] == app_id), None)
        if item is None or kind not in item.get("image_paths", {}):
            raise ValidationError("This app has no selected store image")
        self._assert_current(config)
        data = self._get(config, item["image_paths"][kind], limit=8*1024*1024, cancelled=cancelled, deadline=deadline).body
        if hashlib.sha256(data).hexdigest() != item["image_hashes"][kind]:
            raise ValidationError("Store image does not match the signed image hash")
        # Decode/re-encode before exposing remotely supplied image bytes to the
        # owner's dashboard. This also strips metadata and trailing payloads.
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as image:
            if image.size != ({"icon": (1024, 1024), "screen": (280, 240)}[kind]) or getattr(image, "n_frames", 1) != 1:
                raise ValidationError("Store image dimensions are invalid")
            image.load()
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
        checkpoint(cancelled, deadline)
        self._assert_current(config)
        return output.getvalue()
