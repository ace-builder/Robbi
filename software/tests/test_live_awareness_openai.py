import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from robbi.errors import ValidationError
from robbi.live_awareness.session import options
from test_live_awareness import session, finish


class Client:
    def __init__(self, *, malformed=False, hang=False):
        self.requests = []
        self.closed = False
        self.malformed, self.hang = malformed, hang
        self.responses = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def create(self, **request):
        self.requests.append(request)
        if self.hang:
            await asyncio.Event().wait()
        data = {"summary": "A ball is on the table.", "changed": len(self.requests) == 1,
                "salience": "medium"}
        if self.malformed:
            data["drive"] = "forward"
        return SimpleNamespace(output_text=json.dumps(data), usage=SimpleNamespace(total_tokens=123))


def make(client, **changes):
    credentials = []
    def factory(**kwargs):
        credentials.append(kwargs)
        return client
    live, ws, reports, taps = session(openai_key=lambda: 'private-openai-key', openai_factory=factory, **changes)
    return live, ws, reports, taps, credentials


def test_default_routes_to_terra_with_private_key_and_bounded_images():
    client = Client()
    live, ws, reports, taps, credentials = make(client)
    try:
        live.start({})
        finish(live)
        assert not ws.messages and client.closed and len(client.requests) == 2
        assert credentials == [{"api_key": "private-openai-key", "timeout": 12, "max_retries": 0}]
        first, second = client.requests
        assert first['model'] == 'gpt-6.1-terra'
        assert first['reasoning'] == {'effort': 'none'}
        assert first['max_output_tokens'] == 256 and first['store'] is False
        assert 'tools' not in first and first['text']['format']['strict']
        assert first['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,')
        assert 'A ball is on the table.' in second['input'][0]['content'][0]['text']
        assert len(reports) == 2 and all(not react for _, react in reports)
        assert all(tap is None for tap in taps)
        status = live.status()
        assert status['turns'] == 2 and status['audio_bytes'] == 0
        assert status['providers']['openai']['key_configured']
        assert 'private-openai-key' not in json.dumps(status)
    finally:
        live.shutdown()


def test_openai_does_not_silently_drop_requested_audio():
    with pytest.raises(ValidationError, match='Gemini'):
        options({'audio': True})
    assert options({'model': 'gemini-3.8-live', 'audio': True})['provider'] == 'gemini'
    assert options({'provider': 'gemini'})['model'] == 'gemini-3.8-live'


def test_openai_requires_its_own_key_not_gemini_key():
    live, _, _, _ = session(openai_factory=lambda **_: pytest.fail('No call without key'))
    with pytest.raises(ValidationError, match='openai'):
        live.start({})
    assert live._thread is None


def test_invalid_provider_output_never_reaches_robot():
    live, _, reports, _, _ = make(Client(malformed=True))
    try:
        live.start({'react': True})
        finish(live)
        assert not reports and live.status()['state'] == 'failed'
    finally:
        live.shutdown()


def test_stop_cancels_inflight_openai_request_without_late_reaction():
    client = Client(hang=True)
    live, _, reports, _, _ = make(client)
    try:
        live.start({'react': True})
        deadline = time.monotonic() + 2
        while not client.requests and time.monotonic() < deadline:
            time.sleep(.01)
        assert client.requests
        live.stop()
        finish(live, 1)
        assert client.closed and not reports
        assert len(client.requests) == 1 and live.status()['state'] == 'off'
    finally:
        live.shutdown()


def test_missing_camera_never_spends_api_call():
    client = Client()
    live, _, reports, _, _ = make(client, frame=lambda: {'ok': False})
    try:
        live.start({})
        finish(live, 4)
        assert not client.requests and not reports
        assert 'No fresh camera' in live.status()['reason']
    finally:
        live.shutdown()


def test_autonomy_gate_after_response_prevents_late_delivery():
    client = Client()
    live, _, reports, _, _ = make(client, gate=lambda: 'Paused' if client.requests else '')
    try:
        live.start({'react': True})
        finish(live)
        assert len(client.requests) == 1 and not reports
    finally:
        live.shutdown()
