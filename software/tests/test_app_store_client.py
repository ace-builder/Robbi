"""Signed-store trust, integrity and replay protection without external I/O."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import yaml

from robbi.app_runtime.packages import inspect_archive
from robbi.app_runtime.store_client import StoreClient, StoreNotConfigured, store_url
from robbi.app_runtime.store_protocol import encode_public_key, fingerprint, sign_payload
from robbi.errors import ValidationError


NOW = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)


def archive():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("app.yaml", yaml.safe_dump({"api_version": 2, "id": "store-example", "name": "Store example",
            "version": "1.0.0", "description": "A test app", "tools": [{"name": "hello", "description": "Say hello"}]}))
        bundle.writestr("main.py", "def run(c,t,a): return {'ok': True, 'text': 'Hello'}\n")
    return output.getvalue()


class StoreTransport:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.public_key = encode_public_key(self.key.public_key())
        self.calls, self.transform = [], lambda envelope: envelope
        self.blob = archive()
        self.record = {**inspect_archive(self.blob)["manifest"], "sha256": hashlib.sha256(self.blob).hexdigest(), "size": len(self.blob),
                       "image_paths": {}, "image_hashes": {}}
        self.record["download_path"] = "/api/v1/packages/" + self.record["sha256"] + ".zip"
        self.header = {"schema_version": 1, "store_id": "test-store", "name": "Test store", "revision": 1,
                       "generated_at": NOW.isoformat(), "expires_at": (NOW+timedelta(hours=24)).isoformat()}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/info"):
            value = {"schema_version": 1, "store_id": "test-store", "name": "Test store",
                     "public_key": self.public_key, "fingerprint": fingerprint(self.public_key)}
        elif "/packages/" in url:
            return SimpleNamespace(body=self.blob)
        elif "/images/" in url:
            return SimpleNamespace(body=b"not-an-image")
        else:
            payload = {**self.header, **({"apps": [self.record]} if "/catalog" in url else {"app": self.record})}
            value = self.transform(sign_payload(payload, self.key))
        return SimpleNamespace(json=lambda: copy.deepcopy(value))


@pytest.fixture
def connected(tmp_path):
    transport = StoreTransport()
    client = StoreClient(tmp_path / "store.json", http=transport, clock=lambda: NOW)
    candidate = client.probe("http://127.0.0.1:8094", allow_http=True)
    assert not candidate["trusted"] and not client.status()["configured"]
    client.connect(candidate["url"], candidate["public_key"], allow_http=True)
    return client, transport


def test_store_pairing_persists_key_and_verifies_exact_package(connected):
    client, server = connected
    apps = client.catalog()["apps"]
    assert client.download(apps[0]) == server.blob
    loaded = StoreClient(client.path, http=server, clock=lambda: NOW)
    assert loaded.status()["store"]["fingerprint"] == fingerprint(server.public_key)
    assert loaded.status()["store"]["revision"] == 1
    assert loaded.catalog()["apps"][0]["id"] == "store-example"


def test_wrong_pairing_key_is_rejected_without_overwriting_trust(connected):
    client, server = connected
    original = client.path.read_bytes()
    other = encode_public_key(Ed25519PrivateKey.generate().public_key())
    with pytest.raises(ValidationError, match="changed"):
        client.connect("http://127.0.0.1:8094", other, allow_http=True)
    assert client.path.read_bytes() == original


def test_tampered_catalog_signature_never_reaches_results(connected):
    client, server = connected
    def tamper(envelope):
        envelope["payload"]["apps"][0]["name"] = "Tampered app"
        return envelope
    server.transform = tamper
    with pytest.raises(ValidationError):
        client.catalog()
    assert client.status()["store"]["revision"] == 0


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(revision=-1),
    lambda value: value.update(store_id="another-store"),
    lambda value: value.update(generated_at=(NOW+timedelta(hours=1)).isoformat()),
    lambda value: value.update(expires_at=(NOW-timedelta(seconds=1)).isoformat()),
    lambda value: value.update(expires_at=(NOW+timedelta(days=100)).isoformat()),
])
def test_signed_but_invalid_catalog_headers_fail_closed(connected, mutation):
    client, server = connected
    mutation(server.header)
    with pytest.raises(ValidationError):
        client.catalog()


def test_catalog_cannot_roll_back_after_restart_or_reconnection(connected):
    client, server = connected
    server.header["revision"] = 8
    client.catalog()
    client.connect("http://127.0.0.1:8094", server.public_key, allow_http=True)
    server.header["revision"] = 7
    with pytest.raises(ValidationError, match="older"):
        StoreClient(client.path, http=server, clock=lambda: NOW).catalog()


def test_catalog_download_paths_cannot_escape_the_configured_store(connected):
    client, server = connected
    server.record["download_path"] = "https://attacker.invalid/package.zip"
    with pytest.raises(ValidationError, match="path"):
        client.catalog()
    assert all("attacker" not in call[1] for call in server.calls)


def test_tampered_download_is_not_accepted(connected):
    client, server = connected
    app = client.catalog()["apps"][0]
    server.blob += b"tampered"
    with pytest.raises(ValidationError, match="hash and size"):
        client.download(app)


def test_exact_version_or_identity_mismatch_fails_closed(connected):
    client, server = connected
    with pytest.raises(ValidationError, match="different app or version"):
        client.app("store-example", version="2.0.0")
    with pytest.raises(ValidationError, match="different app or version"):
        client.app("another-app")


def test_expired_memory_cache_is_not_used(connected):
    client, server = connected
    client.catalog()
    client.clock = lambda: NOW + timedelta(days=2)
    with pytest.raises(ValidationError, match="expired"):
        client.catalog()


@pytest.mark.parametrize("url,allow", [
    ("http://127.0.0.1:8094", False), ("file:///secret", True),
    ("https://name:password@example.com", False), ("https://example.com?token=secret", False),
    ("https://example.com/../other", False), ("https://example.com/%2e%2e/other", False),
    ("https://example.com\\@evil.test", False), ("https://example.com/#fragment", False),
])
def test_store_url_validation(url, allow):
    with pytest.raises(ValidationError):
        store_url(url, allow)


@pytest.mark.parametrize("url", ["http://169.254.169.254", "http://8.8.8.8", "http://0.0.0.0"])
def test_local_http_rejects_metadata_public_and_unspecified_hosts(tmp_path, url):
    server = StoreTransport()
    with pytest.raises(ValidationError):
        StoreClient(tmp_path / "store.json", http=server).probe(url, allow_http=True)
    assert server.calls == []


def test_corrupt_trust_never_silently_connects(tmp_path):
    path = tmp_path / "store.json"
    path.write_text('{"version": 1, "public_key": "bad"}')
    client = StoreClient(path)
    assert client.status()["error"]
    with pytest.raises(StoreNotConfigured):
        client.catalog()
    assert path.exists()


def test_cancelled_read_does_not_contact_store(connected):
    from concurrent.futures import CancelledError
    client, server = connected
    calls = len(server.calls)
    with pytest.raises(CancelledError):
        client.catalog(cancelled=lambda: True)
    assert len(server.calls) == calls
