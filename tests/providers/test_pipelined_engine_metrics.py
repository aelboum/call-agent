"""`PipelinedEngineSession`'s Phase 2.14 metrics recording: one
`voiceagent.metrics.record_provider_operation()` call per STT utterance, per
LLM turn, and per TTS synthesis -- `voiceagent.providers.engines.pipelined
.record_provider_operation` monkeypatched at its import site, matching
`tests/tools/test_gateway.py`'s own technique for a function this module
calls through a plain name."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from voiceagent.providers.engines.component_fakes import (
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    EngineSessionConfig,
    TurnEnded,
    VoiceRef,
)
from voiceagent.providers.engines.pipelined import PipelinedEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(instructions="x", voice=VoiceRef(provider="fake", voice_id="v"))


@pytest.fixture
def recorded_operations(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(
        "voiceagent.providers.engines.pipelined.record_provider_operation",
        lambda provider_family, operation, outcome, duration_seconds, error_category=None: (
            calls.append(  # noqa: E501
                (provider_family, operation, outcome, error_category)
            )
        ),
    )
    return calls


def test_a_full_turn_records_stt_llm_and_tts_operations(recorded_operations) -> None:
    stt = FakeSttProvider(["book an appointment"], frames_per_utterance=1, emit_partial=True)
    llm = FakeLlmProvider([["Sure, ", "one moment.", TurnEnded()]])
    tts = FakeTtsProvider()
    engine = PipelinedEngine(stt, llm, tts)

    async def scenario() -> None:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        await asyncio.sleep(0.05)
        await session.close()

    asyncio.run(scenario())

    kinds = [(family, operation, outcome) for family, operation, outcome, _ in recorded_operations]
    assert ("stt", "transcribe", "success") in kinds
    assert ("llm", "stream_turn", "success") in kinds
    assert ("tts", "synthesize", "success") in kinds


class _FailingSttProvider:
    async def stream(self, audio: AsyncIterator[bytes]):
        async for _ in audio:
            pass
        raise EngineException(EngineErrorCode.TRANSIENT, "connection dropped")
        yield  # pragma: no cover -- makes this an async generator.


def test_stt_failure_records_failure_with_error_category(recorded_operations) -> None:
    engine = PipelinedEngine(
        _FailingSttProvider(), FakeLlmProvider([[TurnEnded()]]), FakeTtsProvider()
    )

    async def scenario() -> None:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        await session.close()

    asyncio.run(scenario())

    stt_calls = [c for c in recorded_operations if c[0] == "stt"]
    assert stt_calls == [("stt", "transcribe", "failure", "transient")]


def test_stt_idle_timeout_records_timeout_outcome(recorded_operations) -> None:
    class _NeverSpeaksSttProvider:
        async def stream(self, audio: AsyncIterator[bytes]):
            async for _ in audio:
                pass
            return
            yield  # pragma: no cover

    engine = PipelinedEngine(
        _NeverSpeaksSttProvider(),
        FakeLlmProvider([[TurnEnded()]]),
        FakeTtsProvider(),
        stt_idle_timeout_seconds=0.01,
    )

    async def scenario() -> None:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        await asyncio.sleep(0.05)
        await session.close()

    asyncio.run(scenario())

    stt_calls = [c for c in recorded_operations if c[0] == "stt"]
    assert stt_calls == [("stt", "transcribe", "timeout", "transient")]
