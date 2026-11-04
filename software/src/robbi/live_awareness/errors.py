"""Actionable provider failures without exposing remote bodies or credentials."""


class LiveProviderError(Exception):
    """Contains only a message selected by failure_reason, never a raw response."""


def failure_reason(error, provider):
    if isinstance(error, LiveProviderError):
        return str(error)
    name = "Gemini" if provider == "gemini" else "OpenAI"
    # Match known causes, but never return remote text: exceptions may contain
    # headers, API keys, camera content, or arbitrary provider response bodies.
    detail = str(error)[:4096].lower()
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if ("prepayment credits are depleted" in detail or "insufficient_quota" in detail
            or "billing_hard_limit_reached" in detail or "insufficient credits" in detail):
        return ("Gemini project credits are depleted or its billing quota is exhausted. "
                "Add credits/check billing at https://ai.studio/projects, then start again. "
                "OpenAI camera awareness remains a separate option." if provider == "gemini" else
                "OpenAI credits or billing quota are exhausted. Check your OpenAI project billing, then start again.")
    if status in (401, 403) or any(text in detail for text in
            ("api key not valid", "api_key_invalid", "invalid api key", "unauthenticated")):
        return f"{name} rejected authentication or access. Check the key and project restrictions in Settings → Connections."
    if any(text in detail for text in ("model not found", "model is not found", "not supported for bidigeneratecontent")):
        return f"{name} rejected this model. Choose a model available to your project that supports this awareness connection."
    if status == 429 or "resource_exhausted" in detail or "rate limit" in detail:
        return f"{name} rate or usage limit reached. Check your project limits and retry later."
    return f"{name} live connection failed. Check model access and network, then start again."
