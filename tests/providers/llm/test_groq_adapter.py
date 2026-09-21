"""Tier 2: `voiceagent.providers.llm.groq`'s own wiring -- the shared wire
mechanics are covered once in `test_openai_compatible_adapter.py`.
"""

from __future__ import annotations

import pytest

from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider
from voiceagent.providers.llm.groq import GroqLlmConfig, create_groq_llm_provider


def test_create_groq_llm_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")
    provider = create_groq_llm_provider({"model": "llama-3.3-70b-versatile"})
    assert isinstance(provider, OpenAiCompatibleLlmProvider)


def test_create_groq_llm_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_groq_llm_provider({"model": "llama-3.3-70b-versatile"})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_config_defaults_to_the_openai_compatible_endpoint() -> None:
    config = GroqLlmConfig.model_validate({"model": "llama-3.3-70b-versatile"})
    assert config.endpoint == "https://api.groq.com/openai/v1"
