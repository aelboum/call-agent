"""Mistral chat completions (`voiceagent.providers.llm._openai_compatible`
supplies the wire mechanics -- Mistral's API is OpenAI-compatible-shaped,
confirmed via `WebFetch` against `docs.mistral.ai/api/` during this phase:
Bearer auth, `POST /v1/chat/completions`, SSE streaming, a `tools` array
with `tool_choice`. See `docs/PHASE-2.3-STATUS.md` section 3 for the exact
result and date).

No `mistralai` SDK dependency -- direct HTTP only.
"""

from __future__ import annotations

from collections.abc import Mapping

from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict

from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider

__all__ = ["MistralLlmConfig", "create_mistral_llm_provider"]

_DEFAULT_ENDPOINT = "https://api.mistral.ai/v1"
_SECRET_NAME = "MISTRAL_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class MistralLlmConfig(BaseModel):
    """The typed shape of `voiceagent.providers.llm.registry`'s merged
    `{"model": ..., **LlmComponentConfig.config}` mapping for
    `provider: "mistral"`. `model` is `LlmComponentConfig.model` itself
    (e.g. `mistral-large-latest`) -- already a first-class, typed field on
    `voiceagent.agents.config.LlmComponentConfig`; folded in here only so
    every LLM adapter factory shares one `Callable[[Mapping], LlmProvider]`
    shape with the STT/TTS registries (`voiceagent.providers.registry
    .ProviderRegistry`)."""

    model_config = ConfigDict(extra="forbid")

    model: str
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 30.0


def create_mistral_llm_provider(config: Mapping[str, object]) -> OpenAiCompatibleLlmProvider:
    parsed = MistralLlmConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"mistral: {_SECRET_NAME} is not configured"
        ) from exc
    return OpenAiCompatibleLlmProvider(
        base_url=parsed.endpoint,
        model=parsed.model,
        api_key=api_key,
        provider_label="mistral",
        timeout_seconds=parsed.timeout_seconds,
    )
