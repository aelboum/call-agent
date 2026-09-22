"""The post-call intelligence provider registry (Phase 2.12). Reuses the
same `ProviderRegistry` mechanism as STT/LLM/TTS
(`voiceagent.providers.registry`) -- a name maps to a factory, and adding a
provider is a one-call `register()`, never a branch anywhere in
`voiceagent.call_intelligence`.
"""

from __future__ import annotations

from collections.abc import Mapping

from voiceagent.providers.call_intelligence.contracts import CallIntelligenceProvider
from voiceagent.providers.call_intelligence.fakes import create_fake_call_intelligence_provider
from voiceagent.providers.call_intelligence.groq import create_groq_call_intelligence_provider
from voiceagent.providers.registry import ProviderRegistry

__all__ = ["CALL_INTELLIGENCE_PROVIDERS", "create_call_intelligence_provider"]

CALL_INTELLIGENCE_PROVIDERS: ProviderRegistry[CallIntelligenceProvider] = ProviderRegistry(
    "call_intelligence"
)
CALL_INTELLIGENCE_PROVIDERS.register("fake", create_fake_call_intelligence_provider)
CALL_INTELLIGENCE_PROVIDERS.register("groq", create_groq_call_intelligence_provider)


def create_call_intelligence_provider(
    name: str, model: str, config: Mapping[str, object]
) -> CallIntelligenceProvider:
    merged = {"model": model, **config}
    return CALL_INTELLIGENCE_PROVIDERS.create(name, merged)
