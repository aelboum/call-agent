"""Groq chat completions for post-call intelligence
(`voiceagent.providers.call_intelligence.openai_compatible` supplies the
wire mechanics -- Groq's API is OpenAI-compatible, the same fact
`voiceagent.providers.llm.groq` already established for the live engine).

No `groq` SDK dependency -- direct HTTP only.
"""

from __future__ import annotations

from collections.abc import Mapping

from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict

from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceErrorCode,
    CallIntelligenceProviderError,
)
from voiceagent.providers.call_intelligence.openai_compatible import (
    OpenAiCompatibleCallIntelligenceProvider,
)

__all__ = ["GroqCallIntelligenceConfig", "create_groq_call_intelligence_provider"]

_DEFAULT_ENDPOINT = "https://api.groq.com/openai/v1"
_SECRET_NAME = "GROQ_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class GroqCallIntelligenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 30.0


def create_groq_call_intelligence_provider(
    config: Mapping[str, object],
) -> OpenAiCompatibleCallIntelligenceProvider:
    parsed = GroqCallIntelligenceConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise CallIntelligenceProviderError(
            CallIntelligenceErrorCode.AUTH, f"groq: {_SECRET_NAME} is not configured"
        ) from exc
    return OpenAiCompatibleCallIntelligenceProvider(
        base_url=parsed.endpoint,
        model=parsed.model,
        api_key=api_key,
        provider_label="groq",
        timeout_seconds=parsed.timeout_seconds,
    )
