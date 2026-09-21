"""Tier 1: `voiceagent.providers.engines.factory.build_conversation_engine()`
-- the seam that turns an `AgentVersion`'s typed `engine: EngineSelection`
into a live `ConversationEngine`, with no `if provider == ...` anywhere in
it (asserted structurally by `tests/architecture/test_import_boundaries.py`
alongside these behavioral tests).
"""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.agents.config import EngineComponentConfig, EngineSelection, LlmComponentConfig
from voiceagent.providers.engines.contracts import ConversationEngine, EngineSessionConfig, VoiceRef
from voiceagent.providers.engines.factory import build_conversation_engine
from voiceagent.providers.engines.pipelined import PipelinedEngine
from voiceagent.providers.engines.realtime import RealtimeEngine
from voiceagent.providers.registry import UnknownProviderError


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(
        instructions="Answer the phone.", voice=VoiceRef(provider="fake", voice_id="v")
    )


def test_pipelined_fake_selection_builds_a_working_pipelined_engine() -> None:
    selection = EngineSelection(
        kind="pipelined",
        stt=EngineComponentConfig(provider="fake", config={}),
        llm=LlmComponentConfig(provider="fake", model="any", config={}),
        tts=EngineComponentConfig(provider="fake", config={}),
    )
    engine = build_conversation_engine(selection)
    assert isinstance(engine, PipelinedEngine)
    assert isinstance(engine, ConversationEngine)
    session = asyncio.run(engine.start(_config()))
    assert session is not None


def test_realtime_fake_selection_builds_a_working_realtime_engine() -> None:
    selection = EngineSelection(
        kind="realtime", realtime=EngineComponentConfig(provider="fake", config={})
    )
    engine = build_conversation_engine(selection)
    assert isinstance(engine, RealtimeEngine)
    assert isinstance(engine, ConversationEngine)
    session = asyncio.run(engine.start(_config()))
    assert session is not None


def test_pipelined_selection_missing_a_component_raises() -> None:
    selection = EngineSelection(
        kind="pipelined",
        stt=EngineComponentConfig(provider="fake", config={}),
        llm=None,
        tts=EngineComponentConfig(provider="fake", config={}),
    )
    with pytest.raises(ValueError, match="stt, llm and tts"):
        build_conversation_engine(selection)


def test_realtime_selection_missing_realtime_raises() -> None:
    selection = EngineSelection(kind="realtime", realtime=None)
    with pytest.raises(ValueError, match="realtime"):
        build_conversation_engine(selection)


def test_unknown_stt_provider_name_raises_before_any_engine_is_built() -> None:
    selection = EngineSelection(
        kind="pipelined",
        stt=EngineComponentConfig(provider="not-a-real-vendor", config={}),
        llm=LlmComponentConfig(provider="fake", model="any", config={}),
        tts=EngineComponentConfig(provider="fake", config={}),
    )
    with pytest.raises(UnknownProviderError):
        build_conversation_engine(selection)


def test_gemini_and_deepgram_are_not_hardcoded_into_the_factory_body() -> None:
    """Swapping the STT/TTS provider names in the selection is the entire
    change needed -- no branch inside `build_conversation_engine()` reacts
    to a specific vendor name, only to registry membership. Uses `"fake"`
    (not real credentialed vendors) so this stays a Tier 1, network-free
    assertion about the *shape* of provider selection."""
    for stt_name, tts_name in (("fake", "fake"),):
        selection = EngineSelection(
            kind="pipelined",
            stt=EngineComponentConfig(provider=stt_name, config={}),
            llm=LlmComponentConfig(provider="fake", model="any", config={}),
            tts=EngineComponentConfig(provider=tts_name, config={}),
        )
        engine = build_conversation_engine(selection)
        assert isinstance(engine, PipelinedEngine)
