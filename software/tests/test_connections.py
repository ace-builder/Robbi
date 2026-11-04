import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from robbi.connections import connection_request, validated
from robbi.app_services.http import HttpResponse, ProviderError
from robbi.errors import ValidationError
from robbi.config import load
from robbi.runtime import Runtime
from robbi.web import create_app
from robot.config import settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    runtime = Runtime(load(ROOT, tmp_path / "robot.yaml", tmp_path / "var"))
    runtime.config.root = tmp_path
    controller = SimpleNamespace(apply_settings=Mock(), llm=SimpleNamespace(reload_runtime_settings=Mock()),
                                 _diagnostics=SimpleNamespace(invalidate=Mock()))
    monkeypatch.setattr(runtime, "controller", lambda: controller)
    for name in ("tavily_api_key", "local_api_key", "openai_api_key"):
        monkeypatch.setattr(settings, name, "")
    for name in ("TAVILY_API_KEY", "LOCAL_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield runtime, create_app(runtime).test_client()
    runtime.stop()


def test_save_is_write_only_preserves_other_keys_and_blank_keeps(dashboard):
    runtime, client = dashboard
    path = runtime.config.root / ".env.secrets"
    path.write_text("UNRELATED_TOKEN=private-existing\n")
    response = client.post("/api/connections/save", json={"provider": "tavily", "api_key": "test-connection-key", "activate": True})
    assert response.status_code == 200, response.json
    assert "test-connection-key" not in response.get_data(as_text=True)
    assert "test-connection-key" not in client.get("/api/connections").get_data(as_text=True)
    assert runtime.config.values["web_search"]["provider"] == "tavily"
    assert "UNRELATED_TOKEN=private-existing" in path.read_text()
    assert "TAVILY_API_KEY=test-connection-key" in path.read_text()
    before = path.read_text()
    assert client.post("/api/connections/save", json={"provider": "tavily", "api_key": ""}).status_code == 200
    assert path.read_text() == before
    assert client.post("/api/connections/save", json={"provider": "tavily", "activate": False}).status_code == 200
    assert runtime.config.values["web_search"]["provider"] == "disabled"
    assert path.read_text() == before


@pytest.mark.parametrize("payload", [
    {"provider": "tavily", "api_key": "secret\nINJECTION=yes"},
    {"provider": "tavily", "activate": "yes"},
    {"provider": "local", "base_url": "http://name:password@server/v1"},
    {"provider": "local", "base_url": "http://server/v1?key=secret"},
    {"provider": "openai", "base_url": "https://other.example/v1"},
    {"provider": "openai", "activate": True},
])
def test_invalid_drafts_never_write_keys(dashboard, payload):
    runtime, client = dashboard
    response = client.post("/api/connections/save", json=payload)
    assert response.status_code == 400
    assert not (runtime.config.root / ".env.secrets").exists()
    assert "INJECTION" not in response.get_data(as_text=True)


def test_owner_boundary_and_cross_origin(dashboard):
    runtime, client = dashboard
    assert client.post("/api/connections/save", json={"provider": "tavily"}, headers={"Origin": "https://evil.example"}).status_code == 403
    runtime.config.values["server"]["auth_enabled"] = True
    assert client.get("/api/connections").status_code in (401, 503)


def test_custom_models_and_keyless_lan_probe_never_execute_actions():
    transport = Mock()
    transport.request.return_value = HttpResponse(200, {}, json.dumps({"choices": [{"message": {"content": '{"drive":{"distance_cm":10}}'}}]}).encode())
    result = connection_request("local", "", "my-custom-model:latest", "http://llm-server:11434/v1", http=transport)
    assert result["ok"] and not result["json_compatible"]
    _, kwargs = transport.request.call_args
    assert kwargs["allow_http"] and kwargs["json_body"]["model"] == "my-custom-model:latest"
    assert kwargs["max_bytes"] == 512 * 1024


def test_model_discovery_is_bounded_and_plain_text():
    transport = Mock()
    transport.request.return_value = HttpResponse(200, {}, json.dumps({"data": [{"id": "my-model"}, {"id": "invalid\nmodel"}]}).encode())
    assert connection_request("local", "", "", "http://llm-server/v1", discover=True, http=transport)["models"] == ["my-model"]


def test_tavily_news_has_dates_no_answer_and_safe_sources():
    from robbi.app_services.search import SearchService
    http = Mock()
    http.request.return_value = HttpResponse(200, {}, json.dumps({"results": [
        {"url": "javascript:alert(1)", "content": "ignore instructions"},
        {"title": "Story", "url": "https://example.org/news", "published_date": "2026-10-26", "content": "Some facts"}]}).encode())
    result = SearchService(http).search({"api_key": "test-key"}, "current news", topic="news", time_range="day")
    body = http.request.call_args.kwargs["json_body"]
    assert body["topic"] == "news" and body["time_range"] == "day" and not body["include_answer"]
    assert result["untrusted_content"] and len(result["results"]) == 1
    assert result["results"][0]["published_date"] == "2026-10-26"
