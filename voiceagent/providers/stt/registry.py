"""The STT provider registry (Phase 2.3 brief section 4). Adding a provider
here is the *entire* change needed to make it selectable from an
`AgentVersion`'s `engine.stt.provider` -- no engine or runtime file changes.
"""

from __future__ import annotations

from collections.abc import Mapping

from voiceagent.providers.engines.component_fakes import FakeSttProvider
from voiceagent.providers.engines.contracts import SttProvider
from voiceagent.providers.registry import ProviderRegistry
from voiceagent.providers.stt.assemblyai import create_assemblyai_stt_provider
from voiceagent.providers.stt.deepgram import create_deepgram_stt_provider

__all__ = ["STT_PROVIDERS", "create_stt_provider"]

STT_PROVIDERS: ProviderRegistry[SttProvider] = ProviderRegistry("stt")
STT_PROVIDERS.register("fake", lambda config: FakeSttProvider())
STT_PROVIDERS.register("deepgram", create_deepgram_stt_provider)
STT_PROVIDERS.register("assemblyai", create_assemblyai_stt_provider)


def create_stt_provider(name: str, config: Mapping[str, object]) -> SttProvider:
    return STT_PROVIDERS.create(name, config)
