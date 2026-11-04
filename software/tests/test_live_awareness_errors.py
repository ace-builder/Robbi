import asyncio
import json
from types import SimpleNamespace

import pytest

from robbi.live_awareness.errors import LiveProviderError, failure_reason
from test_live_awareness import session, finish


@pytest.mark.parametrize('provider,detail,expected', [
    ('gemini', 'received 1011 Your prepayment credits are depleted.', 'https://ai.studio/projects'),
    ('openai', 'insufficient_quota', 'OpenAI credits'),
    ('gemini', 'API key not valid', 'authentication'),
    ('gemini', 'not supported for bidiGenerateContent', 'rejected this model'),
    ('gemini', 'RESOURCE_EXHAUSTED', 'rate or usage limit'),
    ('gemini', 'Unexpected provider error', 'network'),
])
def test_failure_messages_are_actionable_without_echoing_provider_content(provider, detail, expected):
    error = RuntimeError(detail + ' secret-api-key PRIVATE CAMERA CONTENT')
    message = failure_reason(error, provider)
    assert expected in message
    assert 'secret-api-key' not in message and 'PRIVATE CAMERA CONTENT' not in message


def test_socket_billing_close_reaches_session_status_safely():
    def connect(*args, **kwargs):
        raise RuntimeError('received 1011 Your prepayment credits are depleted. secret-api-key')
    live, _, reports, _ = session(connector=connect)
    try:
        live.start({'provider': 'gemini'})
        finish(live)
        status = live.status()
        assert status['state'] == 'failed' and not reports
        assert 'credits are depleted' in status['reason']
        assert 'secret-api-key' not in json.dumps(status)
    finally:
        live.shutdown()


def test_json_billing_error_is_not_replaced_by_generic_failure():
    live, _, _, _ = session()
    live._config = {'provider': 'gemini'}
    live._state['received_bytes'] = 0
    async def recv():
        return json.dumps({'error': {'message': 'Your prepayment credits are depleted. secret-api-key'}})
    try:
        with pytest.raises(LiveProviderError) as caught:
            asyncio.run(live._receive(SimpleNamespace(recv=recv)))
        assert 'credits are depleted' in failure_reason(caught.value, 'gemini')
        assert 'secret-api-key' not in str(caught.value)
    finally:
        live.shutdown()
