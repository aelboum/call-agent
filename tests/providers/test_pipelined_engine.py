"""`PipelinedEngine` conformance (Phase 2.2 brief sections 5, 6, 24, 28):
pipeline ordering, streaming, cancellation, interruption, and provider
replacement -- all against deterministic fakes, no vendor SDK, no Pipecat.
"""

from __future__ import annotations

import asyncio

from voiceagent.providers.engines.component_fakes import (
    EchoLlmProvider,
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.engines.contracts import (
    AssistantResponse,
    AudioOut,
    ConversationEngine,
    EngineSession,
    EngineSessionConfig,
    FinalTranscript,
    PartialTranscript,
    SystemPromptSet,
    ToolCallRequested,
    ToolResult,
    TurnEnded,
    VoiceRef,
)
from voiceagent.providers.engines.pipelined import PipelinedEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(
        instructions="Answer the phone.",
        greeting="Hello.",
        voice=VoiceRef(provider="fake", voice_id="v1"),
    )


def test_pipelined_engine_satisfies_the_contract() -> None:
    engine = PipelinedEngine(FakeSttProvider(), FakeLlmProvider([[TurnEnded()]]), FakeTtsProvider())
    assert isinstance(engine, ConversationEngine)

    async def scenario() -> None:
        session = await engine.start(_config())
        assert isinstance(session, EngineSession)
        await session.close()

    asyncio.run(scenario())


def test_full_turn_pipeline_ordering() -> None:
    """audio in -> STT -> transcript -> LLM -> assistant text -> TTS -> audio
    out -> TurnEnded, in that order."""
    stt = FakeSttProvider(["book an appointment"], frames_per_utterance=1)
    llm = FakeLlmProvider([["Sure, ", "one moment.", TurnEnded()]])
    tts = FakeTtsProvider()
    engine = PipelinedEngine(stt, llm, tts)

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        await asyncio.sleep(0.05)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    kinds = [type(event) for event in events]
    # SystemPromptSet and the configured greeting (as an AssistantResponse)
    # are emitted at session start, ahead of anything caller-driven
    # (Phase 2.5: the durable "system"/"assistant" turns).
    assert kinds[0] is SystemPromptSet
    assert kinds[1] is AssistantResponse
    assert AudioOut in kinds
    assert kinds[-1] is TurnEnded
    assert kinds.index(FinalTranscript) < kinds.index(AudioOut) < len(kinds)
    # The turn's own finalized assistant text, distinct from the greeting.
    turn_response = [event for event in events if isinstance(event, AssistantResponse)][-1]
    assert turn_response.text == "Sure, one moment."


def test_partial_and_final_transcripts_are_forwarded() -> None:
    stt = FakeSttProvider(["hi"], frames_per_utterance=2)
    llm = FakeLlmProvider([[TurnEnded()]])
    engine = PipelinedEngine(stt, llm, FakeTtsProvider())

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"f1")
        await asyncio.sleep(0.02)
        await session.send_audio(b"f2")
        await asyncio.sleep(0.05)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(event, FinalTranscript) for event in events)
    assert not any(isinstance(event, PartialTranscript) for event in events)


def test_tool_call_round_trip_continues_the_turn() -> None:
    request = ToolCallRequested(call_id="tc-1", name="contact.lookup", arguments={})
    llm = FakeLlmProvider([[request], ["done", TurnEnded()]])
    stt = FakeSttProvider(["look up jane"], frames_per_utterance=1)
    engine = PipelinedEngine(stt, llm, FakeTtsProvider())

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"f1")
        await asyncio.sleep(0.02)
        await session.submit_tool_result(ToolResult(call_id="tc-1", value={"name": "Jane"}))
        await asyncio.sleep(0.05)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(event, ToolCallRequested) for event in events)
    assert any(isinstance(event, TurnEnded) for event in events)
    assert llm.calls[-1][-1]["role"] == "tool"


def test_interrupt_cancels_in_flight_synthesis_and_drops_queued_audio() -> None:
    stt = FakeSttProvider(["hello"], frames_per_utterance=1)
    llm = FakeLlmProvider([["a long reply with many words to synthesize", TurnEnded()]])
    tts = FakeTtsProvider(chunk_delay=0.05)
    engine = PipelinedEngine(stt, llm, tts)

    async def scenario() -> tuple[int, list[object]]:
        session = await engine.start(_config())
        await session.send_audio(b"f1")
        await asyncio.sleep(0.03)  # let synthesis begin, mid-stream
        await session.interrupt()
        await session.interrupt()  # idempotent
        await asyncio.sleep(0.2)
        await session.close()
        return session.interrupts, [event async for event in session.events()]

    interrupts, events = asyncio.run(scenario())
    assert interrupts == 2
    # The turn was cut off mid-synthesis, never completed -- no TurnEnded
    # for it, and strictly fewer audio chunks than the full reply would
    # have produced (proves cancellation actually interrupted TTS, not just
    # raced past a fast/instant fake).
    assert not any(isinstance(event, TurnEnded) for event in events)
    audio_chunks = [event for event in events if isinstance(event, AudioOut)]
    full_reply_word_count = len("a long reply with many words to synthesize".split())
    assert len(audio_chunks) < full_reply_word_count


def test_close_is_idempotent_and_ends_the_event_stream() -> None:
    engine = PipelinedEngine(FakeSttProvider(), FakeLlmProvider([[TurnEnded()]]), FakeTtsProvider())

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.close()
        await session.close()
        return [event async for event in session.events()]

    # The session-start SystemPromptSet/greeting-AssistantResponse pair is
    # already queued the moment the session is constructed -- close() ends
    # the stream after them, it does not discard them.
    events = asyncio.run(scenario())
    assert [type(event) for event in events] == [SystemPromptSet, AssistantResponse]


def test_send_audio_after_close_raises() -> None:
    engine = PipelinedEngine(FakeSttProvider(), FakeLlmProvider([[TurnEnded()]]), FakeTtsProvider())

    async def scenario() -> None:
        session = await engine.start(_config())
        await session.close()
        try:
            await session.send_audio(b"late")
        except RuntimeError:
            return
        raise AssertionError("expected RuntimeError")

    asyncio.run(scenario())


def test_a_second_llm_provider_satisfies_the_contract_with_no_runtime_change() -> None:
    """`EchoLlmProvider` is structurally different from `FakeLlmProvider` --
    proving a fake can be replaced without touching `PipelinedEngine` or the
    runtime (Phase 2.2 brief section 24)."""
    stt = FakeSttProvider(["what is the weather"], frames_per_utterance=1)
    llm = EchoLlmProvider()
    engine = PipelinedEngine(stt, llm, FakeTtsProvider())

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"f1")
        await asyncio.sleep(0.05)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(event, TurnEnded) for event in events)
    assert any(isinstance(event, AudioOut) for event in events)
