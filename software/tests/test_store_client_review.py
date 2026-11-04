"""Store trust races and local HTTP rebinding checks, with no external I/O."""
import base64
from concurrent.futures import CancelledError
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from robbi.app_runtime.store_client import StoreClient, store_url
from robbi.errors import ValidationError
from robbi.services.app_management_commands import parse_app_management_command
from test_app_store_client import NOW, StoreTransport


@pytest.fixture
def connected(tmp_path):
    server = StoreTransport()
    client = StoreClient(tmp_path / "store.json", http=server, clock=lambda: NOW)
    client.connect("http://127.0.0.1:8094", server.public_key, allow_http=True)
    return client, server


def reconnect(client, server):
    client.disconnect()
    client.connect("http://127.0.0.1:8094", server.public_key, allow_http=True)


@pytest.mark.parametrize("replace_connection", [False, True])
def test_cached_catalog_rejects_disconnected_or_replaced_trust(connected, replace_connection):
    client, server = connected
    client.catalog()
    changed = False

    def clock():
        nonlocal changed
        if not changed:
            changed = True
            reconnect(client, server) if replace_connection else client.disconnect()
        return NOW

    client.clock = clock
    with pytest.raises(ValidationError, match="connection changed"):
        client.catalog()


def test_stop_after_connection_probe_does_not_persist_trust(connected):
    client, server = connected
    original = client.path.read_bytes()
    stopped = threading.Event()
    probe = client.probe

    def stopped_probe(*args, **kwargs):
        candidate = probe(*args, **kwargs)
        stopped.set()
        return candidate

    client.probe = stopped_probe
    with pytest.raises(CancelledError):
        client.connect("http://127.0.0.1:8094", server.public_key, allow_http=True, cancelled=stopped.is_set)
    assert client.path.read_bytes() == original


@pytest.mark.parametrize("operation", ["catalog", "app"])
def test_stop_during_verification_does_not_advance_trusted_revision(connected, operation):
    client, server = connected
    server.header["revision"] = 8
    stopped = threading.Event()

    def clock():
        stopped.set()
        return NOW

    client.clock = clock
    with pytest.raises(CancelledError):
        if operation == "catalog":
            client.catalog(cancelled=stopped.is_set)
        else:
            client.app("store-example", cancelled=stopped.is_set)
    assert client.status()["store"]["revision"] == 0


def image_transport(client, server, hook=lambda: None):
    output = io.BytesIO()
    Image.new("RGB", (280, 240), "blue").save(output, format="PNG")
    data = output.getvalue()
    server.record["image_paths"] = {"screen": f"/api/v1/images/{server.record['sha256']}/screen"}
    server.record["image_hashes"] = {"screen": hashlib.sha256(data).hexdigest()}
    request = server.request

    def with_image(method, url, **kwargs):
        if "/images/" in url:
            hook()
            return SimpleNamespace(body=data)
        return request(method, url, **kwargs)

    server.request = with_image


@pytest.mark.parametrize("replace_connection", [False, True])
def test_image_result_rejects_connection_change_during_download(connected, replace_connection):
    client, server = connected
    image_transport(client, server, lambda: reconnect(client, server) if replace_connection else client.disconnect())
    with pytest.raises(ValidationError, match="connection changed"):
        client.image("store-example", "screen")


@pytest.mark.parametrize("expired", [False, True])
def test_image_transform_honors_late_stop_or_deadline(connected, monkeypatch, expired):
    client, server = connected
    image_transport(client, server)
    stopped = threading.Event()
    now = [100.0]
    monkeypatch.setattr("robbi.app_services.http.time.monotonic", lambda: now[0])
    save = Image.Image.save

    def finish_then_stop(image, *args, **kwargs):
        result = save(image, *args, **kwargs)
        stopped.set()
        now[0] = 102.0
        return result

    monkeypatch.setattr(Image.Image, "save", finish_then_stop)
    with pytest.raises(TimeoutError if expired else CancelledError):
        client.image("store-example", "screen", cancelled=None if expired else stopped.is_set, deadline=101 if expired else None)


def test_package_rejects_disconnect_and_reconnect_with_same_identity(connected):
    client, server = connected
    app = client.catalog()["apps"][0]
    request = server.request

    def changed_during_download(method, url, **kwargs):
        result = request(method, url, **kwargs)
        if "/packages/" in url:
            reconnect(client, server)
        return result

    server.request = changed_during_download
    with pytest.raises(ValidationError, match="connection changed"):
        client.download(app)


def test_noncanonical_key_cannot_reset_rollback_protection(connected):
    client, server = connected
    server.header["revision"] = 8
    client.catalog()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    key = server.public_key
    alias = key[:-2] + alphabet[alphabet.index(key[-2]) + 1] + "="
    assert base64.b64decode(alias) == base64.b64decode(key)
    with pytest.raises(ValidationError, match="signing key"):
        client.connect("http://127.0.0.1:8094", alias, allow_http=True)
    assert client.status()["store"]["revision"] == 8


@pytest.mark.parametrize("url", ["https://@store.example", "https://:@store.example", "http://@127.0.0.1:8094"])
def test_empty_url_credentials_are_rejected(url):
    with pytest.raises(ValidationError):
        store_url(url, allow_http=True)


def test_default_http_transport_pins_validated_address_and_preserves_host(tmp_path, monkeypatch):
    transport = StoreTransport()
    hosts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hosts.append(self.headers["Host"])
            body = json.dumps(transport.request("GET", self.path).json()).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    resolve = socket.getaddrinfo
    lookups = []

    def rebinding(host, port, *args, **kwargs):
        if host == "store.example":
            lookups.append(host)
            host = "127.0.0.1" if len(lookups) == 1 else "8.8.8.8"
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    try:
        port = httpd.server_address[1]
        client = StoreClient(tmp_path / "store.json")
        result = client.probe(f"http://store.example:{port}", allow_http=True)
        assert result["public_key"] == transport.public_key
        assert hosts == [f"store.example:{port}"]
        assert lookups == ["store.example"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


@pytest.mark.parametrize("app_id", ["store-example", "my_app"])
def test_store_copy_request_is_accepted_by_actual_command_parser(app_id):
    result = parse_app_management_command(f"Install app ID {app_id} version 1.0.0")
    assert result is not None
    assert result["operation"] == "install"
    assert result["app_id"] == app_id
    assert result["version"] == "1.0.0"
