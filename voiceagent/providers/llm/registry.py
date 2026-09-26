"""The LLM provider registry (Phase 2.3 brief section 4/7/23). Reuses the
same `ProviderRegistry` mechanism as STT/TTS
(`voiceagent.providers.registry`); the only wrinkle is that
`LlmComponentConfig` (`voiceagent.agents.config`) carries `model` as its own
typed field, separate from `config`, so `create_llm_provider()` merges the
two into the single mapping every registered factory expects -- each
factory's own typed config model (`MistralLlmConfig`, `GroqLlmConfig`,
`OpenAiLlmConfig`, `GeminiLlmConfig`) declares `model` as a required field
for exactly this reason.
"""

from __future__ import annotations

from collections.abc import Mapping

from voiceagent.providers.engines.component_fakes import FakeLlmProvider
from voiceagent.providers.engines.contracts import LlmProvider
from voiceagent.providers.llm.gemini import create_gemini_llm_provider
from voiceagent.providers.llm.groq import create_groq_llm_provider
from voiceagent.providers.llm.mistral import create_mistral_llm_provider
from voiceagent.providers.llm.openai import create_openai_llm_provider
from voiceagent.providers.registry import ProviderRegistry

__all__ = ["LLM_PROVIDERS", "create_llm_provider"]


def _fake_llm_provider(config: Mapping[str, object]) -> FakeLlmProvider:
    return FakeLlmProvider(turns=[["OK."]])


LLM_PROVIDERS: ProviderRegistry[LlmProvider] = ProviderRegistry("llm")
LLM_PROVIDERS.register("fake", _fake_llm_provider)
LLM_PROVIDERS.register("gemini", create_gemini_llm_provider)
LLM_PROVIDERS.register("mistral", create_mistral_llm_provider)
LLM_PROVIDERS.register("groq", create_groq_llm_provider)
LLM_PROVIDERS.register("openai", create_openai_llm_provider)


def create_llm_provider(name: str, model: str, config: Mapping[str, object]) -> LlmProvider:
    merged = {"model": model, **config}
    return LLM_PROVIDERS.create(name, merged)
