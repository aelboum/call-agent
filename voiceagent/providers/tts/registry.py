"""The TTS provider registry (Phase 2.3 brief section 4)."""

from __future__ import annotations

from collections.abc import Mapping

from voiceagent.providers.engines.component_fakes import FakeTtsProvider
from voiceagent.providers.engines.contracts import TtsProvider
from voiceagent.providers.registry import ProviderRegistry
from voiceagent.providers.tts.deepgram_aura import create_deepgram_aura_tts_provider
from voiceagent.providers.tts.elevenlabs import create_elevenlabs_tts_provider

__all__ = ["TTS_PROVIDERS", "create_tts_provider"]

TTS_PROVIDERS: ProviderRegistry[TtsProvider] = ProviderRegistry("tts")
TTS_PROVIDERS.register("fake", lambda config: FakeTtsProvider())
TTS_PROVIDERS.register("elevenlabs", create_elevenlabs_tts_provider)
TTS_PROVIDERS.register("deepgram_aura", create_deepgram_aura_tts_provider)


def create_tts_provider(name: str, config: Mapping[str, object]) -> TtsProvider:
    return TTS_PROVIDERS.create(name, config)
