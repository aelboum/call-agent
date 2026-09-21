"""Tier 2: `voiceagent.providers.llm.mistral`'s own wiring (secret loading,
config validation, default endpoint) -- the shared wire mechanics are
covered once in `test_openai_compatible_adapter.py`.
"""

from __future__ import annotations

import pytest

from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider
from voiceagent.providers.llm.mistral import MistralLlmConfig, create_mistral_llm_provider


def test_create_mistral_llm_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "mk-test")
    provider = create_mistral_llm_provider({"model": "mistral-large-latest"})
    assert isinstance(provider, OpenAiCompatibleLlmProvider)


def test_create_mistral_llm_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_mistral_llm_provider({"model": "mistral-large-latest"})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_config_requires_a_model() -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017 -- pydantic ValidationError.
        MistralLlmConfig.model_validate({})


def test_config_defaults_to_the_documented_endpoint() -> None:
    config = MistralLlmConfig.model_validate({"model": "mistral-small-latest"})
    assert config.endpoint == "https://api.mistral.ai/v1"
