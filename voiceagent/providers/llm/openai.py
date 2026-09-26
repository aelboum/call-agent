"""OpenAI chat completions (`voiceagent.providers.llm._openai_compatible`
supplies the wire mechanics -- OpenAI's own Chat Completions API
(`POST https://api.openai.com/v1/chat/completions`) is the format that
shape is modeled on in the first place: Bearer auth, SSE streaming
(`data: {...}` chunks terminated by `data: [DONE]`), a `choices[0].delta`
with `content`/`tool_calls`, a closing `finish_reason` -- confirmed stable
across every OpenAI-compatible vendor this product already speaks to
(Groq, Mistral). Phase 2.20: the first *real* external AI provider this
product's own staging environment is configured against (ADR-0009 chose
OpenAI Realtime as the eventual `RealtimeEngine` candidate, still deferred;
this is `PipelinedEngine`'s own `LlmProvider` leg, unrelated to that).

No `openai` SDK dependency -- direct HTTP only, exactly like every other
adapter in this package (Phase 0 report's own "no vendor SDK" preference,
consistent with `groq.py`/`mistral.py`).
"""

from __future__ import annotations

from collections.abc import Mapping

from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict, Field

from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider

__all__ = ["OpenAiLlmConfig", "create_openai_llm_provider"]

_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
_SECRET_NAME = "OPENAI_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.  # pragma: allowlist secret


class OpenAiLlmConfig(BaseModel):
    """The typed shape of `voiceagent.providers.llm.registry`'s merged
    `{"model": ..., **LlmComponentConfig.config}` mapping for
    `provider: "openai"` -- see `voiceagent.providers.llm.mistral
    .MistralLlmConfig`'s docstring for why `model` is folded in here.

    `model` has no default and no vendor-specific validation: this product
    treats the model name as configuration an operator/agent-author
    chooses, never architecture this adapter assumes (Phase 2.20 brief
    section 15) -- exactly as `GroqLlmConfig`/`MistralLlmConfig` already
    require `model` with no default of their own.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 30.0
    #: Bounded generation settings (Phase 2.20 brief section 5/8: "bounded
    #: generation settings", "no unbounded response buffering"). Both
    #: optional -- `None` omits the wire field entirely, so an agent that
    #: sets neither gets OpenAI's own server-side default, unchanged from
    #: this adapter's behavior before either field existed.
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)


def create_openai_llm_provider(config: Mapping[str, object]) -> OpenAiCompatibleLlmProvider:
    parsed = OpenAiLlmConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"openai: {_SECRET_NAME} is not configured"
        ) from exc
    return OpenAiCompatibleLlmProvider(
        base_url=parsed.endpoint,
        model=parsed.model,
        api_key=api_key,
        provider_label="openai",
        timeout_seconds=parsed.timeout_seconds,
        temperature=parsed.temperature,
        max_tokens=parsed.max_tokens,
    )
