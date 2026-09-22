"""Tier 2: `voiceagent.providers.call_intelligence.groq`'s own wiring -- the
shared wire mechanics are covered once in
`test_openai_compatible_adapter.py`.
"""

from __future__ import annotations

import pytest

from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceErrorCode,
    CallIntelligenceProviderError,
)
from voiceagent.providers.call_intelligence.groq import (
    GroqCallIntelligenceConfig,
    create_groq_call_intelligence_provider,
)
from voiceagent.providers.call_intelligence.openai_compatible import (
    OpenAiCompatibleCallIntelligenceProvider,
)


def test_create_groq_provider_reads_the_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")
    provider = create_groq_call_intelligence_provider({"model": "llama-3.3-70b-versatile"})
    assert isinstance(provider, OpenAiCompatibleCallIntelligenceProvider)


def test_create_groq_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(CallIntelligenceProviderError) as exc_info:
        create_groq_call_intelligence_provider({"model": "llama-3.3-70b-versatile"})
    assert exc_info.value.code is CallIntelligenceErrorCode.AUTH


def test_config_defaults_to_the_openai_compatible_endpoint() -> None:
    config = GroqCallIntelligenceConfig.model_validate({"model": "llama-3.3-70b-versatile"})
    assert config.endpoint == "https://api.groq.com/openai/v1"
