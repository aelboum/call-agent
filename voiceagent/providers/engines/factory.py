"""The provider-selection seam (Phase 2.3 brief):

```text
Configuration
     |
     +-- STT provider --> STT adapter
     |
     +-- LLM provider --> LLM adapter
     |
     +-- TTS provider --> TTS adapter
```

`build_conversation_engine()` is the only function that turns an
`AgentVersion`'s typed `engine: EngineSelection` (`voiceagent.agents.config`,
already approved in Phase 2.1 -- untouched by this phase) into a live
`ConversationEngine`. Every provider-specific branch lives inside the three
component registries it calls
(`voiceagent.providers.stt.registry.STT_PROVIDERS`,
`voiceagent.providers.llm.registry.LLM_PROVIDERS`,
`voiceagent.providers.tts.registry.TTS_PROVIDERS`) plus this module's own
tiny realtime-provider registry -- never an `if provider == "...":` here,
in `PipelinedEngine`, or in the Call Runtime. Adding a provider is a
`register()` call in the relevant registry module; this function's body
never changes for it.

No provider SDK type appears in this module's signature or body -- it
depends only on the four `Provider` protocols from
`voiceagent.providers.engines.contracts` and the registries above, which
depend on the same protocols. Vendor knowledge (Deepgram, ElevenLabs,
Gemini, Mistral, Groq, AssemblyAI) is confined entirely to each adapter
module beneath `voiceagent.providers.{stt,llm,tts}.<vendor>`.
"""

from __future__ import annotations

from voiceagent.agents.config import EngineSelection
from voiceagent.providers.engines.contracts import ConversationEngine
from voiceagent.providers.engines.pipelined import PipelinedEngine
from voiceagent.providers.engines.realtime import (
    FakeRealtimeProvider,
    RealtimeEngine,
    RealtimeProvider,
)
from voiceagent.providers.llm.registry import create_llm_provider
from voiceagent.providers.registry import ProviderRegistry
from voiceagent.providers.stt.registry import create_stt_provider
from voiceagent.providers.tts.registry import create_tts_provider

__all__ = ["REALTIME_PROVIDERS", "UnknownEngineKindError", "build_conversation_engine"]


class UnknownEngineKindError(Exception):
    """`EngineSelection.kind` is a closed `Literal["pipelined", "realtime"]`
    (`voiceagent.agents.config`) validated at `AgentConfig` publish time, so
    this should be unreachable in practice -- kept as an explicit,
    documented failure rather than an unguarded branch, the same defensive
    posture `voiceagent.providers.registry.UnknownProviderError` takes for a
    provider name a registry does not recognize."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"unknown engine kind: {kind!r}")
        self.kind = kind


#: `RealtimeEngine`'s own provider registry. Only `"fake"` is registered --
#: ADR-0009 point 5 names OpenAI Realtime as the eventual first candidate,
#: and the Phase 2.3 brief explicitly keeps it deferred; the registry exists
#: so that arrival is a `register()` call here, not a `RealtimeEngine`
#: change, exactly like the pipelined component registries.
REALTIME_PROVIDERS: ProviderRegistry[RealtimeProvider] = ProviderRegistry("realtime")
REALTIME_PROVIDERS.register("fake", lambda config: FakeRealtimeProvider())


def build_conversation_engine(engine: EngineSelection) -> ConversationEngine:
    if engine.kind == "pipelined":
        if engine.stt is None or engine.llm is None or engine.tts is None:
            raise ValueError("engine.kind == 'pipelined' requires stt, llm and tts to all be set")
        stt = create_stt_provider(engine.stt.provider, engine.stt.config)
        llm = create_llm_provider(engine.llm.provider, engine.llm.model, engine.llm.config)
        tts = create_tts_provider(engine.tts.provider, engine.tts.config)
        return PipelinedEngine(stt, llm, tts)
    if engine.kind == "realtime":
        if engine.realtime is None:
            raise ValueError("engine.kind == 'realtime' requires realtime to be set")
        provider = REALTIME_PROVIDERS.create(engine.realtime.provider, engine.realtime.config)
        return RealtimeEngine(provider)
    raise UnknownEngineKindError(engine.kind)
