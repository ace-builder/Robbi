"""Existing provider and wake-engine aliases, shared by startup and edits."""


def normalize_provider(raw: str) -> str:
    value = (raw or "openai").strip().lower().replace("_", "-")
    aliases = {
        "openai": "openai",
        "alibaba": "alibaba",
        "openrouter": "openrouter",
        "local": "local",
        "lan": "local",
        "lm-studio": "local",
        "lmstudio": "local",
        "gemini": "gemini",
        "google": "gemini",
        "google-ai": "gemini",
        "googleai": "gemini",
        "elevenlabs": "elevenlabs",
        "bailian": "alibaba",
        "dashscope": "alibaba",
        "coding-plan": "alibaba",
        "alibaba-coding-plan": "alibaba",
    }
    return aliases.get(value, "openai")


def normalize_wake_engine(raw: str) -> str:
    value = (raw or "auto").strip().lower().replace("-", "_")
    aliases = {
        "auto": "auto",
        "nanowakeword": "nanowakeword",
        "nano_wakeword": "nanowakeword",
        "nanowake": "nanowakeword",
        "local_wake": "local_wake",
        "localwake": "local_wake",
        "stt": "stt",
        "cloud_stt": "stt",
    }
    return aliases.get(value, "auto")


def normalize_reasoning_effort(raw):
    value = str(raw or "").strip().lower()
    if value not in {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unknown reasoning effort")
    return value
