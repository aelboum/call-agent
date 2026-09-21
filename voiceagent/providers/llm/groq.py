"""Groq chat completions (`voiceagent.providers.llm._openai_compatible`
supplies the wire mechanics -- Groq's API is explicitly OpenAI-compatible,
confirmed via `WebFetch` against `console.groq.com/docs/api-reference`
during this phase: Bearer auth, `POST /openai/v1/chat/completions`, SSE
streaming, a `tools` array. See `docs/PHASE-2.3-STATUS.md` section 3 for the
exact result and date).

No `groq` SDK dependency -- direct HTTP only.
"""

from __future__ import annotations

from collections.abc import Mapping

from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict

from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider

__all__ = ["GroqLlmConfig", "create_groq_llm_provider"]

_DEFAULT_ENDPOINT = "https://api.groq.com/openai/v1"
_SECRET_NAME = "GROQ_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class GroqLlmConfig(BaseModel):
    """The typed shape of `voiceagent.providers.llm.registry`'s merged
    `{"model": ..., **LlmComponentConfig.config}` mapping for
    `provider: "groq"` -- see `voiceagent.providers.llm.mistral
    .MistralLlmConfig`'s docstring for why `model` is folded in here."""

    model_config = ConfigDict(extra="forbid")

    model: str
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 30.0


def create_groq_llm_provider(config: Mapping[str, object]) -> OpenAiCompatibleLlmProvider:
    parsed = GroqLlmConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"groq: {_SECRET_NAME} is not configured"
        ) from exc
    return OpenAiCompatibleLlmProvider(
        base_url=parsed.endpoint,
        model=parsed.model,
        api_key=api_key,
        provider_label="groq",
        timeout_seconds=parsed.timeout_seconds,
    )
