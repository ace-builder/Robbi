"""Replaceable text-model boundary with conservative, persisted spend accounting."""
import json
import math
import os
import time

import httpx


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, limit, path=None):
        self.limit, self.path = limit, path
        self.used = 0.0
        if path and path.exists():
            self.used = float(json.loads(path.read_text())['reserved_or_charged_usd'])
            if not math.isfinite(self.used) or self.used < 0:
                raise ValueError('Invalid budget ledger')

    def reserve(self, dollars):
        if not math.isfinite(dollars) or dollars < 0 or self.used + dollars > self.limit:
            raise BudgetExceeded('Experiment cloud budget reached; no further paid call dispatched')
        self.used += dollars
        self.save()

    def settle(self, reserved, usage):
        # Missing cost (including cancellation/timeouts) keeps the full reservation.
        cost = usage.get('cost') if isinstance(usage, dict) else None
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            self.used = max(0, self.used - reserved + cost)
            self.save()

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps({'limit_usd': self.limit, 'reserved_or_charged_usd': self.used}), encoding='utf8')
            temp.replace(self.path)


class ChatModels:
    def __init__(self, options, budget, key='', event=lambda *_: None):
        self.options, self.budget, self.event = options, budget, event
        self.cloud = options.model_url.rstrip('/') == 'https://openrouter.ai/api/v1'
        # Never forward an OpenRouter secret to a user-configured local endpoint.
        token = key if self.cloud else os.environ.get('ROBBI_CONTINUOUS_LOCAL_KEY', '')
        self.http = httpx.AsyncClient(base_url=options.model_url.rstrip('/') + '/', trust_env=False,
            timeout=20, headers={'Authorization': 'Bearer ' + token} if token else {})
        self.prices = {}

    async def start(self):
        if self.cloud and 'Authorization' not in self.http.headers:
            raise ValueError('OpenRouter key missing in Robbi server environment; configure OPENROUTER_API_KEY')
        response = await self.http.get('models', timeout=10)
        response.raise_for_status()
        catalog = {x['id']: x for x in response.json().get('data', [])}
        for model in {self.options.foreground_model, self.options.worker_model}:
            if self.cloud:
                if model not in catalog:
                    raise ValueError('Configured model is unavailable: ' + model)
                pricing = catalog[model].get('pricing', {})
                variants = [pricing, *pricing.get('overrides', [])]
                # Use highest advertised price rather than a temporary discount.
                self.prices[model] = tuple(max(float(v.get(k, pricing.get(k, 0))) for v in variants)
                                          for k in ('prompt', 'completion', 'request'))
                if any(not math.isfinite(x) or x < 0 for x in self.prices[model]):
                    raise ValueError('Cannot bound model pricing')
                if 'prompt' not in pricing or 'completion' not in pricing:
                    raise ValueError('Model pricing unavailable')

    async def close(self):
        await self.http.aclose()

    async def complete(self, messages, *, worker=False, tools=None, json_mode=False):
        model = self.options.worker_model if worker else self.options.foreground_model
        payload = {'model': model, 'messages': messages, 'max_tokens': 600 if worker else 240,
                   'temperature': .4, 'stream': False}
        if tools:
            payload['tools'] = tools
            payload['parallel_tool_calls'] = False
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        if self.cloud:
            payload['reasoning'] = {'effort': 'none'}
            payload['provider'] = {'require_parameters': True}
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        if len(encoded) > 60000:
            raise ValueError('Conversation context exceeds experiment bound')
        reservation = 0.0
        if self.cloud:
            prompt, output, request = self.prices[model]
            # UTF-8 bytes conservatively overcount ordinary text tokens, plus template overhead.
            reservation = 1.2 * ((len(encoded) + 4096) * prompt + payload['max_tokens'] * output + request)
            self.budget.reserve(reservation)
        start = time.monotonic()
        # Do not automatically retry paid calls or replay effects on network failure.
        response = await self.http.post('chat/completions', json=payload)
        response.raise_for_status()
        body = response.json()
        if self.cloud:
            self.budget.settle(reservation, body.get('usage', {}))
        if not isinstance(body.get('choices'), list) or not body['choices']:
            raise ValueError('Model returned no completion; not retried automatically')
        self.event('latency', json.dumps({'stage': 'worker_model' if worker else 'foreground_model',
            'ms': round((time.monotonic() - start) * 1000), 'model': model,
            'reserved_or_charged_usd': round(self.budget.used, 6)}))
        return body['choices'][0]['message']

