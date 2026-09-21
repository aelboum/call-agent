"""Tier 1: the two provider-neutral `PipelinedEngineSession` gaps this phase
found and fixed while wiring real adapters (`docs/PHASE-2.3-STATUS.md`
section 3 -- deviations):

1. A component provider raising `EngineException` used to crash the
   session's task instead of surfacing an `EngineError` event and staying
   alive (ADR-0006: "surfaced rather than raised").
2. `PipelinedEngineSession` never recorded an assistant `tool_calls`
   message before the `tool`-role result, which an OpenAI-compatible
   `LlmProvider` (Mistral, Groq -- and any future one) requires in its
   message history.

Neither test needs a real vendor: local fakes that raise/inspect exactly
what a real adapter would.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence

from voiceagent.providers.engines.component_fakes import (
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.engines.contracts import (
    EngineError,
    EngineErrorCode,
    EngineException,
    EngineSessionConfig,
    ToolCallRequested,
    ToolResult,
    ToolSpec,
    TurnEnded,
    VoiceRef,
)
from voiceagent.providers.engines.pipelined import PipelinedEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(instructions="x", voice=VoiceRef(provider="fake", voice_id="v"))


class _FailingSttProvider:
    async def stream(self, audio: AsyncIterator[bytes]):
        async for _ in audio:
            pass
        raise EngineException(EngineErrorCode.TRANSIENT, "connection dropped")
        yield  # pragma: no cover -- makes this an async generator.


class _FailingLlmProvider:
    def __init__(self, *, fail_first_n: int) -> None:
        self._fail_first_n = fail_first_n
        self.calls = 0

    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ):
        self.calls += 1
        if self.calls <= self._fail_first_n:
            raise EngineException(EngineErrorCode.PROVIDER_DOWN, "provider unavailable")
            yield  # pragma: no cover
        yield "ok"
        yield TurnEnded()


class _ToolCallingLlmProvider:
    """Yields one `ToolCallRequested` on the first turn, then records the
    exact message list it is handed on the *second* `stream_turn()` call --
    the one that must contain the assistant `tool_calls` message ahead of
    the `tool` result."""

    def __init__(self) -> None:
        self.second_call_messages: list[Mapping[str, object]] | None = None
        self._call_count = 0

    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ):
        self._call_count += 1
        if self._call_count == 1:
            yield ToolCallRequested(call_id="tc-1", name="lookup", arguments={"q": "x"})
        else:
            self.second_call_messages = list(messages)
            yield "done"
            yield TurnEnded()


async def _one_utterance_audio(frame_count: int = 1) -> AsyncIterator[bytes]:
    for _ in range(frame_count):
        yield b"frame"


def test_stt_failure_surfaces_as_engine_error_not_a_crash() -> None:
    engine = PipelinedEngine(
        _FailingSttProvider(), FakeLlmProvider(turns=[["ok"]]), FakeTtsProvider()
    )

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(e, EngineError) and e.code is EngineErrorCode.TRANSIENT for e in events)


def test_llm_failure_surfaces_as_engine_error_and_stt_keeps_listening() -> None:
    stt = FakeSttProvider(texts=["first", "second"])
    llm = _FailingLlmProvider(fail_first_n=1)
    tts = FakeTtsProvider()
    engine = PipelinedEngine(stt, llm, tts)

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")  # -> "first" -> LLM fails.
        await asyncio.sleep(0.01)
        await session.send_audio(b"frame-2")  # -> "second" -> LLM succeeds.
        await asyncio.sleep(0.01)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(
        isinstance(e, EngineError) and e.code is EngineErrorCode.PROVIDER_DOWN for e in events
    )
    # The session survived the first turn's failure and completed a second
    # turn -- STT consumption was never torn down by the LLM error.
    assert llm.calls == 2


def test_tool_call_records_an_assistant_tool_calls_message_before_the_result() -> None:
    stt = FakeSttProvider(texts=["book a table"])
    llm = _ToolCallingLlmProvider()
    tts = FakeTtsProvider()
    engine = PipelinedEngine(stt, llm, tts)

    async def scenario() -> None:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await asyncio.sleep(0.01)  # let the ToolCallRequested reach events()
        await session.submit_tool_result(ToolResult(call_id="tc-1", value={"ok": True}))
        await asyncio.sleep(0.01)
        await session.close()

    asyncio.run(scenario())
    assert llm.second_call_messages is not None
    roles_and_shapes = [
        (m.get("role"), "tool_calls" in m, m.get("tool_call_id")) for m in llm.second_call_messages
    ]
    tool_calls_index = next(
        i
        for i, m in enumerate(llm.second_call_messages)
        if m.get("role") == "assistant" and "tool_calls" in m
    )
    tool_result_index = next(
        i for i, m in enumerate(llm.second_call_messages) if m.get("role") == "tool"
    )
    assert tool_calls_index < tool_result_index, roles_and_shapes
    assistant_message = llm.second_call_messages[tool_calls_index]
    assert assistant_message["tool_calls"] == [
        {"id": "tc-1", "name": "lookup", "arguments": {"q": "x"}}
    ]
    tool_message = llm.second_call_messages[tool_result_index]
    assert tool_message["tool_call_id"] == "tc-1"
