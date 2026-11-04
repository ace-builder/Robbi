from types import SimpleNamespace

import pytest

from robbi.configuration.normalization import normalize_reasoning_effort
from robbi.configuration.resolution import resolve_settings
from robbi.configuration.settings import settings
from robbi.engine.llm import LLMClient


@pytest.mark.parametrize('effort', ['', 'none', 'low', 'medium', 'high', 'max'])
def test_reasoning_setting_roundtrips(effort):
    configured = resolve_settings({'providers': {'openrouter': {'reasoning_effort': effort}}}, {})
    assert configured.openrouter_reasoning_effort == effort


def test_invalid_reasoning_is_not_silently_sent():
    with pytest.raises(ValueError):
        normalize_reasoning_effort('arbitrary')


def test_openrouter_conversation_warmup_and_followup_send_requested_effort(monkeypatch):
    monkeypatch.setattr(settings, 'openrouter_reasoning_effort', 'medium')
    calls = []
    client = LLMClient(SimpleNamespace(), SimpleNamespace())
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"speak":"Ready"}'))])
    monkeypatch.setattr(client, '_ensure_chat_client', lambda provider: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(client, '_system_prompt', lambda **kwargs: 'Test system prompt')
    monkeypatch.setattr('robbi.services.spoken_followup.candidate_reason', lambda *args: None)
    try:
        model = 'deepseek/deepseek-v4.1-flash'
        assert client.ask('Hello', provider='openrouter', model=model)['speak'] == 'Ready'
        assert client.warm_chat('openrouter', model)['ok']
        client.classify_followup('Tell me more please', 'Would you like more details?', provider='openrouter', model=model)
        assert len(calls) == 3
        for call in calls:
            assert call['model'] == model
            assert call['extra_body'] == {'reasoning': {'effort': 'medium', 'exclude': True}, 'provider': {'require_parameters': True}}
            assert 'reasoning_effort' not in call
        assert calls[0]['max_tokens'] == calls[1]['max_tokens'] == 1800
        assert calls[2]['max_tokens'] == 128
    finally:
        client.shutdown()


def test_unconfigured_openrouter_and_other_providers_preserve_existing_behavior(monkeypatch):
    monkeypatch.setattr(settings, 'openrouter_reasoning_effort', '')
    client = LLMClient.__new__(LLMClient)
    assert client._chat_reasoning_options('openrouter', 'another-model') == {}
    assert client._chat_reasoning_options('local', 'another-model') == {}
    assert client._chat_reasoning_options('openai', 'gpt-6-luna') == {'reasoning_effort': 'none', 'max_completion_tokens': 1800}
