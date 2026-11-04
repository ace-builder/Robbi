"""Exercise the real dashboard boundary, which parses and caches JSON first."""
from types import SimpleNamespace

import pytest

from test_dashboard_access import dashboard


@pytest.fixture
def live_http(dashboard, monkeypatch):
    runtime, client = dashboard
    calls = []
    live = SimpleNamespace(start=lambda payload: calls.append(payload) or {"state": "connecting"})
    monkeypatch.setattr(runtime, 'controller', lambda: SimpleNamespace(live_awareness=live))
    return client, calls


def test_start_accepts_json_already_read_by_dashboard_boundary(live_http):
    client, calls = live_http
    payload = {'provider': 'openai', 'model': 'gpt-6.1-terra', 'duration_s': 15,
               'video': True, 'audio': False, 'react': False}
    result = client.post('/api/live-awareness/start', json=payload)
    assert result.status_code == 200, result.get_json()
    assert calls == [payload]


@pytest.mark.parametrize('body,content_type', [
    ('', 'application/json'), ('{broken', 'application/json'),
    ('[]', 'application/json'), ('{}', 'text/plain'),
    ('{"model":"' + 'x' * 2100 + '"}', 'application/json'),
])
def test_invalid_or_oversized_body_never_starts_session(live_http, body, content_type):
    client, calls = live_http
    result = client.post('/api/live-awareness/start', data=body, content_type=content_type)
    assert result.status_code == 400
    assert calls == []
