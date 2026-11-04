"""Bounded setup probes. No movement, media capture or personal chat history."""
from pathlib import Path
import socket
import subprocess
from urllib.parse import urlsplit

import httpx

PROVIDERS = {
    'openai': ('https://api.openai.com/v1', 'OPENAI_API_KEY'),
    'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY'),
    'local': ('http://127.0.0.1:1234/v1', 'LOCAL_API_KEY'),
}


def endpoint(value, provider):
    p = urlsplit(value)
    if (not p.hostname or p.username or p.password or p.query or p.fragment
            or p.scheme not in ('https', 'http') or (provider != 'local' and p.scheme != 'https')
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Use an HTTPS API base URL without credentials, query or fragment. Local servers also accept HTTP.')
    return value.rstrip('/')


def llm_test(config, keys, client_factory=httpx.Client):
    provider = config.get('llm', {}).get('chat_provider', 'openai')
    if provider not in PROVIDERS:
        return False, 'Choose OpenAI, OpenRouter or an OpenAI-compatible local server.'
    default, key_name = PROVIDERS[provider]
    key = keys.get(key_name, 'local' if provider == 'local' else '')
    if not key:
        return False, f'{key_name} is not set. You can finish setup offline.'
    base = config.get('providers', {}).get(provider, {}).get('llm_base_url' if provider == 'local' else 'base_url', default)
    try:
        url = endpoint(base, provider) + '/chat/completions'
        with client_factory(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = client.post(url, headers={'Authorization': 'Bearer ' + key}, json={
                'model': config.get('llm', {}).get('chat_model', 'gpt-6-luna'),
                'messages': [{'role': 'user', 'content': 'Reply with JSON: {"ok":true}'}],
                'response_format': {'type': 'json_object'}, 'max_completion_tokens': 64})
        if response.status_code != 200:
            help_text = {401: 'Check the API key.', 403: 'Check model/project access.',
                         404: 'Check the model name and API base URL.', 429: 'Check quota or rate limits.'}.get(response.status_code, 'Check the provider settings.')
            return False, f'LLM test returned HTTP {response.status_code}. {help_text}'
        content = response.json()['choices'][0]['message']['content']
        if not isinstance(content, str) or not content.strip():
            return False, 'The API responded without usable text. Check model compatibility.'
        return True, 'LLM connection works. This test used a small billable request.'
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return False, 'LLM test failed or timed out. Check internet access, endpoint and model; no credentials were logged.'


def network_test(client_factory=httpx.Client):
    try:
        with client_factory(timeout=6, follow_redirects=False, trust_env=False) as client:
            response = client.get('https://api.github.com')
        return response.status_code in (200, 403, 429), 'Current network: HTTPS connection reached GitHub.'
    except httpx.HTTPError:
        return False, 'Current network: HTTPS connection failed. Check Wi-Fi, DNS and system time.'


def inventory():
    """List interfaces/devices without competing for the runtime's camera or I2C."""
    rows = []
    for label, pattern in [('GPIO', 'gpiochip*'), ('SPI display interface', 'spidev0.0'),
                           ('I2C sensor bus', 'i2c-1'), ('ALSA audio interface', 'snd')]:
        rows.append((any(Path('/dev').glob(pattern)), label))
    for label, command in [('Microphone', ['arecord', '-l']), ('Speaker', ['aplay', '-l'])]:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            rows.append((result.returncode == 0 and 'card ' in result.stdout.lower(), label))
        except (OSError, subprocess.SubprocessError):
            rows.append((False, label))
    return rows


def runtime_health(port=8082, client_factory=httpx.Client, *, token=''):
    try:
        with client_factory(timeout=3, trust_env=False, follow_redirects=False) as client:
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            response = client.get(f'http://127.0.0.1:{int(port)}/healthz', headers=headers)
        return response.status_code == 200
    except (httpx.HTTPError, ValueError):
        return False


def dashboard_url(port):
    return f'http://{socket.gethostname()}.local:{port}'
