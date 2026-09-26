"""Tier 2: `voiceagent.providers.llm.openai`'s own wiring -- the shared wire
mechanics are covered once in `test_openai_compatible_adapter.py` (Phase
2.20: OpenAI reuses that class unchanged, exactly like Groq and Mistral).
"""

from __future__ import annotations

import pytest

from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider
from voiceagent.providers.llm.openai import OpenAiLlmConfig, create_openai_llm_provider


def test_create_openai_llm_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # pragma: allowlist secret -- fake, test-only
    provider = create_openai_llm_provider({"model": "gpt-4o-mini"})
    assert isinstance(provider, OpenAiCompatibleLlmProvider)


def test_create_openai_llm_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_openai_llm_provider({"model": "gpt-4o-mini"})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_config_defaults_to_the_real_openai_endpoint() -> None:
    config = OpenAiLlmConfig.model_validate({"model": "gpt-4o-mini"})
    assert config.endpoint == "https://api.openai.com/v1"


def test_config_rejects_unknown_fields() -> None:
    """`extra="forbid"` (matching every other vendor config model): a typo
    in an `AgentVersion`'s published `llm.config` must fail loudly at
    publish/build time, never be silently ignored."""
    with pytest.raises(ValueError):
        OpenAiLlmConfig.model_validate({"model": "gpt-4o-mini", "not_a_real_field": 1})


def test_config_rejects_a_non_positive_max_tokens() -> None:
    with pytest.raises(ValueError):
        OpenAiLlmConfig.model_validate({"model": "gpt-4o-mini", "max_tokens": 0})


def test_generation_settings_default_to_unset() -> None:
    """No agent-configured bound must not silently become an arbitrary
    default -- an unset `temperature`/`max_tokens` omits the wire field
    entirely (`OpenAiCompatibleLlmProvider`), asking OpenAI's own default."""
    config = OpenAiLlmConfig.model_validate({"model": "gpt-4o-mini"})
    assert config.temperature is None
    assert config.max_tokens is None
