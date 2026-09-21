"""Phase 2.2 brief section 24: provider-independence architecture tests.

Complements `tests/architecture/test_import_boundaries.py`'s static import
fences with the dynamic checks the brief specifically asks for: Pipecat and
every commercial AI SDK researched in ADR-0009 are verifiably *not
installed* in this environment (not merely "not imported by our code" --
that they cannot be imported by anything), and `FakeEngine`/`RealtimeEngine`
both work in that environment.
"""

from __future__ import annotations

import asyncio
import importlib.util

from voiceagent.agents.config import EngineComponentConfig, EngineSelection, LlmComponentConfig
from voiceagent.providers.engines.contracts import (
    ConversationEngine,
    EngineSessionConfig,
    VoiceRef,
)
from voiceagent.providers.engines.factory import build_conversation_engine
from voiceagent.providers.engines.fakes import FakeConversationEngine
from voiceagent.providers.engines.realtime import FakeRealtimeProvider, RealtimeEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(instructions="x", voice=VoiceRef(provider="fake", voice_id="v"))


def test_pipecat_is_not_installed() -> None:
    assert importlib.util.find_spec("pipecat") is None


def test_no_commercial_ai_sdk_is_installed() -> None:
    """The three vendors ADR-0009 researched for the first vertical slice
    (OpenAI, ElevenLabs, Deepgram) -- none may be an installed dependency in
    Phase 2.2, hard or soft."""
    for module_name in ("openai", "elevenlabs", "deepgram"):
        assert importlib.util.find_spec(module_name) is None, module_name


def test_fake_engine_starts_a_session_with_pipecat_absent() -> None:
    engine = FakeConversationEngine()
    assert isinstance(engine, ConversationEngine)
    session = asyncio.run(engine.start(_config()))
    assert session is not None


def test_realtime_engine_starts_a_session_with_pipecat_absent() -> None:
    engine = RealtimeEngine(FakeRealtimeProvider())
    assert isinstance(engine, ConversationEngine)
    session = asyncio.run(engine.start(_config()))
    assert session is not None


def _find_spec_or_none(module_name: str) -> object | None:
    """`importlib.util.find_spec("a.b")` raises `ModuleNotFoundError`
    (rather than returning `None`) when even the parent package `a` does
    not exist -- expected here (this environment has no `google` package
    at all), so that case counts as "not installed" exactly like a
    top-level miss does."""
    try:
        return importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        return None


def test_no_llm_vendor_sdk_is_installed() -> None:
    """Phase 2.3: none of Gemini, Mistral or Groq's official SDK packages
    (`google-generativeai`/`google-genai`, `mistralai`, `groq`) is an
    installed dependency, hard or soft -- every adapter is built directly
    against `httpx`."""
    for module_name in ("google.generativeai", "google.genai", "mistralai", "groq"):
        assert _find_spec_or_none(module_name) is None, module_name


def test_no_second_stt_or_tts_vendor_sdk_is_installed() -> None:
    """The second STT/TTS vendors this phase adds (AssemblyAI, and Deepgram
    Aura -- already covered by `test_no_commercial_ai_sdk_is_installed`'s
    `deepgram` check) likewise have no SDK dependency."""
    assert importlib.util.find_spec("assemblyai") is None


def test_factory_builds_a_working_pipelined_engine_with_every_vendor_sdk_absent() -> None:
    """`voiceagent.providers.engines.factory.build_conversation_engine()`
    resolving `"fake"` STT/LLM/TTS providers with no vendor SDK installed
    anywhere -- the same "always works with nothing installed" property the
    Pipecat/realtime tests above prove, now for the provider-selection seam
    itself."""
    selection = EngineSelection(
        kind="pipelined",
        stt=EngineComponentConfig(provider="fake", config={}),
        llm=LlmComponentConfig(provider="fake", model="any", config={}),
        tts=EngineComponentConfig(provider="fake", config={}),
    )
    engine = build_conversation_engine(selection)
    assert isinstance(engine, ConversationEngine)
    session = asyncio.run(engine.start(_config()))
    assert session is not None
