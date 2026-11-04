"""Regression tests for network admission, onboarding and app isolation."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robbi.dashboard_security import allowed_host
from robbi.setup.configuration import Configuration, new_config
from robbi.app_runtime.sandbox import Broker, command
from robbi.errors import UnavailableError, ValidationError


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "attacker.example:8082",
        "localhost.attacker.example",
        "127.0.0.1@attacker.example",
        "localhost/path",
        "[::1]:bad",
    ],
)
def test_rebinding_hosts_rejected_even_without_login(host):
    assert not allowed_host(host)


@pytest.mark.parametrize(
    "host",
    ["localhost:8082", "127.0.0.1", "[::1]:8082", "192.168.1.3:8082", "robbi.local"],
)
def test_lan_addresses_and_robot_names_remain_usable(host):
    assert allowed_host(host)


def test_proxy_hostname_requires_explicit_configuration():
    assert not allowed_host("robot.example")
    assert allowed_host("robot.example:443", ["robot.example"])


def test_setup_secure_defaults_and_preserves_existing_choice(tmp_path):
    config = Configuration(tmp_path)
    config.save({})
    config.ensure_dashboard_password()
    original = config.keys()["ROBBI_WEB_TOKEN"]
    assert len(original) >= 20 and config.read()["server"]["auth_enabled"]
    config.ensure_dashboard_password()
    assert config.keys()["ROBBI_WEB_TOKEN"] == original
    config.save({"server": {"auth_enabled": False}})
    config.ensure_dashboard_password()
    assert config.read()["server"]["auth_enabled"] is False
    assert not new_config()["telegram"]["allow_photos"]
    assert original not in config.path.read_text()


def test_setup_health_uses_private_password_without_redirects():
    from robbi.setup.checks import runtime_health

    client = Mock()
    client.get.return_value.status_code = 200
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=manager)
    assert runtime_health(8082, factory, token="test-only-token")
    assert client.get.call_args.kwargs['headers'] == {'Authorization': 'Bearer test-only-token'}
    assert client.get.call_args.args == ('http://127.0.0.1:8082/healthz',)
    assert factory.call_args.kwargs['follow_redirects'] is False
    assert factory.call_args.kwargs['trust_env'] is False


def test_telegram_empty_allowlist_denies_inbound_and_all_outbound(monkeypatch):
    from robot import telegram_service as telegram

    monkeypatch.setattr(
        telegram,
        "settings",
        SimpleNamespace(telegram_allowed_chat_ids="", telegram_allow_photos=True),
    )
    service = object.__new__(telegram.TelegramService)
    service._known_chats = {123, 456}
    service._api = Mock(side_effect=AssertionError("No outgoing message allowed"))
    service._api_multipart = Mock(
        side_effect=AssertionError("No outgoing media allowed")
    )
    assert not service._is_allowed(123)
    assert service._notification_chat_ids() == []
    assert not service.send_message(123, "private")
    assert not service.send_photo(123, Path("private.jpg"))
    assert not service.send_audio_bytes(123, b"private")
    telegram.settings.telegram_allowed_chat_ids = "123"
    assert service._is_allowed(123) and not service._is_allowed(456)
    assert service._notification_chat_ids() == [123]
    telegram.settings.telegram_allowed_chat_ids = "invalid"
    assert not service._is_allowed(123)


def test_telegram_setup_adds_only_selected_private_owner(tmp_path, monkeypatch):
    from robbi.setup import wizard

    w = wizard.Wizard(tmp_path)
    monkeypatch.setattr(wizard, "yes", lambda *a: True)
    monkeypatch.setattr(wizard, "ask", lambda *a: "12345")
    monkeypatch.setattr(wizard, "hidden", lambda *a: "123456:" + "a" * 30)
    w.telegram()
    value = w.config.read()["telegram"]
    assert value["allowed_chat_ids"] == "12345" and value["enabled"]
    assert not value["allow_photos"] and not value["allow_robot_actions"]
    assert "123456:" not in w.config.path.read_text()


def test_sandbox_never_falls_back_to_import(monkeypatch):
    from robbi.app_runtime import sandbox

    monkeypatch.setattr(
        sandbox, "availability", lambda: {"available": False, "message": "Unavailable"}
    )
    with pytest.raises(UnavailableError):
        command(SimpleNamespace(root=Path.cwd()))


@pytest.mark.parametrize(
    "method",
    [
        "context.__dict__",
        "robot._port",
        "services.google._invoke",
        "context.register_undo",
        "os.system",
    ],
)
def test_broker_rejects_reflection_and_arbitrary_code(method):
    broker = Broker(SimpleNamespace(checkpoint=lambda: None))
    with pytest.raises(ValidationError):
        broker.dispatch({"method": method, "args": [], "kwargs": {}})


def test_provider_cannot_redirect_owner_credentials():
    called = Mock(return_value={"ok": True})
    context = SimpleNamespace(
        checkpoint=lambda: None,
        config={"endpoint": "https://owner.example", "api_key_env": "TEST_KEY"},
        _provider_config={
            "endpoint": "https://owner.example",
            "api_key_env": "TEST_KEY",
        },
        services=SimpleNamespace(search=SimpleNamespace(search=called)),
    )
    Broker(context).dispatch(
        {
            "method": "services.search.search",
            "args": [{"endpoint": "https://attacker.example"}, "hello"],
            "kwargs": {},
        }
    )
    assert called.call_args.args[0] == context.config


def test_manifest_defaults_cannot_redirect_credentials():
    from robbi.app_runtime.sandbox import provider_config, worker_config

    context = SimpleNamespace(
        config={
            "endpoint": "https://attacker.example",
            "api_key_env": "ROBBI_WEB_TOKEN",
        },
        _provider_config={},
    )
    assert provider_config(context, "images") == {}
    context._provider_config = {
        "endpoint": "https://owner.example",
        "api_key_env": "TEST_KEY",
    }
    assert provider_config(context, "images") == context._provider_config
    assert worker_config(
        {"api_key": "private", "access_token": "private", "api_key_env": "TEST_KEY"}
    ) == {"api_key_env": "TEST_KEY"}


def test_imported_provider_overrides_require_owner_save(tmp_path, monkeypatch):
    import io
    import zipfile
    from robbi.app_runtime import sandbox
    from robbi.app_runtime.host import AppHost

    manifest = {
        "api_version": 2,
        "id": "probe",
        "entrypoint": "main:run",
        "permissions": [],
        "config_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "default": "https://attacker.example"}
            },
        },
        "tools": [
            {
                "name": "run",
                "description": "Test configuration isolation",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("app.yaml", json.dumps(manifest))
        archive.writestr("main.py", "def run(c,t,a): return {}\n")
    host = AppHost(
        tmp_path / "runtime",
        tmp_path / "state",
        SimpleNamespace(capture_token=lambda: None),
    )
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda project, ctx, tool, args: {
            "ok": True,
            "config": sandbox.provider_config(ctx, "images"),
        },
    )
    try:
        host.scan()
        host.install(payload.getvalue())
        host.set_enabled("probe", True)
        assert host.call("probe", "run", {})["config"] == {}
        host.configure("probe", {"endpoint": "https://owner.example"})
        assert host.call("probe", "run", {})["config"] == {
            "endpoint": "https://owner.example"
        }
    finally:
        host.stop()


def test_pcm_matches_previous_audioop_for_all_supported_widths():
    audioop = pytest.importorskip("audioop")
    import random
    from robbi.audio import pcm

    rng = random.Random(7)
    for width in (1, 2, 3, 4):
        data = bytes(rng.randrange(256) for _ in range(1000 * width))
        assert pcm.levels(data, width) == (
            audioop.rms(data, width),
            audioop.max(data, width),
        )
        for gain in (0, 0.1, 0.7, 1, 2, -1):
            assert pcm.mul(data, width, gain) == audioop.mul(data, width, gain)
    data = bytes(range(256))
    assert pcm.bias(data, 1, -128) == audioop.bias(data, 1, -128)


def test_pcm_edge_cases_without_audioop():
    from robbi.audio import pcm

    assert pcm.levels(b"", 2) == (0, 0)
    assert pcm.levels(b"\x00\x80\xff\x7f", 2) == (32767, 32768)
    assert pcm.mul(b"\xff\x7f\x00\x80", 2, 2) == b"\xff\x7f\x00\x80"
    with pytest.raises(ValueError):
        pcm.levels(b"\x00", 2)


def test_pcm_minimal_install_without_numpy(monkeypatch):
    import sys
    from robbi.audio import pcm

    monkeypatch.setitem(sys.modules, "numpy", None)
    assert pcm.levels(b"\x00\x80\xff\x7f") == (32767, 32768)
    assert pcm.mul(b"\xff\x7f\x00\x80", 2, 2) == b"\xff\x7f\x00\x80"
    assert pcm.bias(b"\x00\xff", 1, -128) == b"\x80\x7f"


def test_network_permission_alone_cannot_read_connected_accounts():
    def require(permission):
        raise ValidationError("Missing " + permission)

    c = SimpleNamespace(checkpoint=lambda: None, require=require)
    with pytest.raises(ValidationError, match="account.gmail"):
        Broker(c).dispatch(
            {
                "method": "services.google.gmail_search",
                "args": [{}, "private"],
                "kwargs": {},
            }
        )
