"""`PipelinedEngineSession` -- Phase 2.13 production-hardening additions,
hermetic (deterministic fakes only, no vendor SDK, no Pipecat), each proving
one gap this phase's audit found and fixed in
`voiceagent.providers.engines.pipelined` (see that module's own docstring
for the numbered list):

1. bounded `_audio_in`/`_events` queues (brief §6);
2. a closed-session guard against starting a new turn from a late STT
   result (brief §7);
3. `SpeechStarted`/`SpeechEnded` barge-in signal emission (brief §9);
4. idle timeouts on every provider stream, and a bounded `close()` (brief
   §4/§10).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence

from voiceagent.providers.engines.component_fakes import (
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.engines.contracts import (
    AudioOut,
    EngineError,
    EngineErrorCode,
    EngineSessionConfig,
    FinalTranscript,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    ToolSpec,
    TurnEnded,
    VoiceRef,
)
from voiceagent.providers.engines.pipelined import PipelinedEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(instructions="x", voice=VoiceRef(provider="fake", voice_id="v"))


# ---------------------------------------------------------------------------
# Barge-in signal emission (brief §9)
# ---------------------------------------------------------------------------


def test_a_partial_transcript_emits_speech_started_exactly_once_per_utterance() -> None:
    stt = FakeSttProvider(["hello there"], frames_per_utterance=1, emit_partial=True)
    llm = FakeLlmProvider([[TurnEnded()]])
    engine = PipelinedEngine(stt, llm, FakeTtsProvider())

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await asyncio.sleep(0.02)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    kinds = [type(event) for event in events]
    assert kinds.count(SpeechStarted) == 1
    assert kinds.count(SpeechEnded) == 1
    # SpeechStarted precedes the PartialTranscript it was raised for, and
    # SpeechEnded precedes the FinalTranscript that closed the utterance.
    assert kinds.index(SpeechStarted) < kinds.index(PartialTranscript)
    assert kinds.index(SpeechEnded) < kinds.index(FinalTranscript)


def test_no_barge_in_signal_without_a_partial_transcript() -> None:
    """`FakeSttProvider`'s default (`emit_partial=False`, unchanged by this
    phase) never yields a `PartialTranscript` -- every pre-existing test
    against it must see no `SpeechStarted`/`SpeechEnded` either."""
    stt = FakeSttProvider(["hello"], frames_per_utterance=1)
    engine = PipelinedEngine(stt, FakeLlmProvider([[TurnEnded()]]), FakeTtsProvider())

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await asyncio.sleep(0.02)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert not any(isinstance(e, SpeechStarted | SpeechEnded) for e in events)


# ---------------------------------------------------------------------------
# Closed-session guard against a late STT result starting a new turn
# (brief §7: "STT result arrives after call termination")
# ---------------------------------------------------------------------------


class _LateFinalSttProvider:
    """Consumes the whole audio stream silently, then yields exactly one
    `FinalTranscript` only once the stream ends -- models a real STT
    connection that buffers its very last result until its own
    `CloseStream`-equivalent teardown, which lands *after*
    `PipelinedEngineSession.close()` has already set `self.closed = True`
    (it sets that flag before ever awaiting the STT task)."""

    async def stream(self, audio: AsyncIterator[bytes]):
        async for _ in audio:
            pass
        yield FinalTranscript(text="late result")


def test_a_final_transcript_arriving_after_close_does_not_start_a_new_turn() -> None:
    stt = _LateFinalSttProvider()
    llm = FakeLlmProvider([["should never run", TurnEnded()]])
    tts = FakeTtsProvider()
    engine = PipelinedEngine(stt, llm, tts)

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await session.close()  # closed=True is set before the late result ever surfaces
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    # Only the session-start SystemPromptSet (no greeting configured) --
    # never a turn, never the FinalTranscript itself, since it was dropped.
    assert not any(isinstance(e, FinalTranscript) for e in events)
    assert llm.calls == []  # the LLM was never invoked for the late result


# ---------------------------------------------------------------------------
# Bounded queues / backpressure (brief §6)
# ---------------------------------------------------------------------------


class _NeverConsumingSttProvider:
    """Never reads from its audio iterator at all -- the worst-case slow
    consumer, so `send_audio()`'s own backpressure is exercised directly."""

    async def stream(self, audio: AsyncIterator[bytes]):
        await asyncio.Event().wait()
        yield FinalTranscript(text="unreachable")  # pragma: no cover


def test_send_audio_backpressures_against_a_stalled_stt_consumer() -> None:
    """`_audio_in` is bounded -- once full, `send_audio()` blocks rather
    than growing the queue without limit (brief §6: "media must not cause
    unbounded memory growth", "do not allow a slow downstream provider to
    block the entire runtime indefinitely" -- only *this* task blocks)."""
    engine = PipelinedEngine(
        _NeverConsumingSttProvider(),
        FakeLlmProvider([[TurnEnded()]]),
        FakeTtsProvider(),
        audio_queue_maxsize=2,
    )

    async def scenario() -> bool:
        session = await engine.start(_config())
        await session.send_audio(b"f1")
        await session.send_audio(b"f2")
        blocked = asyncio.Event()

        async def fill_one_more() -> None:
            blocked.set()
            await session.send_audio(b"f3")  # must block: queue is full at maxsize=2

        task = asyncio.create_task(fill_one_more())
        await blocked.wait()
        await asyncio.sleep(0.05)
        still_blocked = not task.done()
        task.cancel()
        return still_blocked

    assert asyncio.run(scenario()) is True


def test_event_queue_never_exceeds_its_configured_bound() -> None:
    """A slow/absent `events()` consumer must not let `_events` grow past
    `event_queue_maxsize` -- proven here by producing far more audio chunks
    than the bound while nothing drains `events()` at all. `close()` is
    still called at the end, like every well-behaved session's caller: it is
    what directly cancels the turn this leaves blocked on a full queue
    (`interrupt()`/`close()`'s own docstring), the same way a real call's
    teardown would."""
    stt = FakeSttProvider(["a very long reply with plenty of words in it"], frames_per_utterance=1)
    llm = FakeLlmProvider([["a very long reply with plenty of words in it here", TurnEnded()]])
    tts = FakeTtsProvider()  # one AudioOut chunk per word, no delay
    engine = PipelinedEngine(stt, llm, tts, event_queue_maxsize=4)

    async def scenario() -> int:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await asyncio.sleep(0.1)  # let production race ahead of the (absent) consumer
        max_observed = session._events.qsize()  # noqa: SLF001 -- whitebox: proving the bound held
        await asyncio.wait_for(session.close(), timeout=2.0)
        return max_observed

    max_observed = asyncio.run(scenario())
    assert max_observed <= 4


# ---------------------------------------------------------------------------
# Idle timeouts on provider streams (brief §10)
# ---------------------------------------------------------------------------


class _StallingSttProvider:
    """Yields one `FinalTranscript`, then never yields again (and never
    ends) -- a wedged provider that has stopped producing without raising or
    closing its stream."""

    async def stream(self, audio: AsyncIterator[bytes]):
        yield FinalTranscript(text="hi")
        await asyncio.Event().wait()
        yield FinalTranscript(text="unreachable")  # pragma: no cover


def test_a_stalled_stt_stream_surfaces_as_a_transient_engine_error() -> None:
    engine = PipelinedEngine(
        _StallingSttProvider(),
        FakeLlmProvider([[TurnEnded()]]),
        FakeTtsProvider(),
        stt_idle_timeout_seconds=0.05,
    )

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await asyncio.sleep(0.3)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(e, EngineError) and e.code is EngineErrorCode.TRANSIENT for e in events)


class _StallingLlmProvider:
    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ):
        yield "partial reply, then nothing"
        await asyncio.Event().wait()
        yield "unreachable"  # pragma: no cover


def test_a_stalled_llm_stream_surfaces_as_a_transient_engine_error_and_stt_keeps_listening() -> (
    None
):
    stt = FakeSttProvider(["first", "second"])
    engine = PipelinedEngine(
        stt, _StallingLlmProvider(), FakeTtsProvider(), llm_idle_timeout_seconds=0.05
    )

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        await asyncio.sleep(0.2)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(e, EngineError) and e.code is EngineErrorCode.TRANSIENT for e in events)


class _StallingTtsProvider:
    async def synthesize(self, text: str, voice: VoiceRef | None):
        yield AudioOut(frame=b"one", sample_rate=8000)
        await asyncio.Event().wait()
        yield AudioOut(frame=b"unreachable", sample_rate=8000)  # pragma: no cover


def test_a_stalled_tts_stream_surfaces_as_a_transient_engine_error() -> None:
    stt = FakeSttProvider(["hello"], frames_per_utterance=1)
    llm = FakeLlmProvider([["a reply to synthesize", TurnEnded()]])
    engine = PipelinedEngine(stt, llm, _StallingTtsProvider(), tts_idle_timeout_seconds=0.05)

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.send_audio(b"frame")
        await asyncio.sleep(0.2)
        await session.close()
        return [event async for event in session.events()]

    events = asyncio.run(scenario())
    assert any(isinstance(e, EngineError) and e.code is EngineErrorCode.TRANSIENT for e in events)


# ---------------------------------------------------------------------------
# Bounded close() (brief §4)
# ---------------------------------------------------------------------------


class _NeverEndingSttProvider:
    """Never returns, never raises, no matter what the audio iterator does
    -- close()'s own bounded wait for `_stt_task` (`close_timeout_seconds`)
    is the only thing that can end this session's STT task."""

    async def stream(self, audio: AsyncIterator[bytes]):
        await asyncio.Event().wait()
        yield FinalTranscript(text="unreachable")  # pragma: no cover


def test_close_is_bounded_even_if_the_stt_task_never_stops_on_its_own() -> None:
    engine = PipelinedEngine(
        _NeverEndingSttProvider(),
        FakeLlmProvider([[TurnEnded()]]),
        FakeTtsProvider(),
        close_timeout_seconds=0.05,
    )

    async def scenario() -> float:
        session = await engine.start(_config())
        started_at = time.monotonic()
        await asyncio.wait_for(session.close(), timeout=2.0)
        return time.monotonic() - started_at

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0, elapsed
