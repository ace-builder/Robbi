"""Independent settings for the opt-in conversation experiment; no V2 mutation."""
from dataclasses import asdict, dataclass
import json
import math
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ContinuousSettings:
    speech_url: str = 'http://127.0.0.1:8093'
    model_url: str = 'https://openrouter.ai/api/v1'
    foreground_model: str = 'deepseek/deepseek-v4.1-flash'
    worker_model: str = 'deepseek/deepseek-v4.1-flash'
    voice: str = 'am_adam'
    language: str = 'en'
    budget_usd: float = .25
    silence_ms: int = 600
    job_seconds: int = 90
    max_calls: int = 12

    @classmethod
    def load(cls, root):
        path = root / 'continuous.local.json'
        return cls.parse(json.loads(path.read_text(encoding='utf8')) if path.exists() else {})

    @classmethod
    def parse(cls, values):
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError('Unknown conversation setting')
        obj = cls(**values)
        for name in ('speech_url', 'model_url'):
            value = getattr(obj, name)
            if not isinstance(value, str) or len(value) > 300:
                raise ValueError('Invalid endpoint')
            try:
                url = urlsplit(value)
                valid = (url.scheme in ('http', 'https') and url.hostname and url.port != 0
                         and not url.username and not url.password and not url.query and not url.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Use an HTTP(S) endpoint without credentials or query parameters')
            if name == 'model_url' and url.hostname == 'openrouter.ai' and value.rstrip('/') != 'https://openrouter.ai/api/v1':
                raise ValueError('Use https://openrouter.ai/api/v1 for OpenRouter')
        for name in ('foreground_model', 'worker_model', 'voice', 'language'):
            value = getattr(obj, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 150:
                raise ValueError('Invalid ' + name)
        if type(obj.budget_usd) not in (int, float) or not math.isfinite(obj.budget_usd) or not .01 <= obj.budget_usd <= 10:
            raise ValueError('Budget must be between $0.01 and $10')
        for name, low, high in [('silence_ms', 300, 1500), ('job_seconds', 10, 180), ('max_calls', 1, 20)]:
            value = getattr(obj, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Invalid ' + name)
        return obj

    def save(self, root):
        path = root / 'continuous.local.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(asdict(self), indent=2), encoding='utf8')
        temp.replace(path)

