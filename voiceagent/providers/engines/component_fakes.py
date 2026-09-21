"""Deterministic `SttProvider`/`LlmProvider`/`TtsProvider` fakes, composed by
`voiceagent.providers.engines.pipelined.PipelinedEngine` (ADR-0009 point 1:
the first vertical slice targets `PipelinedEngine`, proven here with no
vendor SDK -- Deepgram/ElevenLabs-shaped behavior in spirit, never in name or
dependency).

Two implementations of `LlmProvider` exist on purpose
(`FakeLlmProvider`/`EchoLlmProvider`): the brief's provider-independence
requirement is that a *second* fake can satisfy a component protocol without
the runtime or `PipelinedEngine` changing at all -- proven by
`tests/providers/test_pipelined_engine.py` running the same conformance
assertions against both.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence

from voiceagent.providers.engines.contracts import (
    AudioOut,
    FinalTranscript,
    PartialTranscript,
    ToolCallRequested,
    ToolSpec,
    TurnEnded,
    VoiceRef,
)

__all__ = ["EchoLlmProvider", "FakeLlmProvider", "FakeSttProvider", "FakeTtsProvider"]


class FakeSttProvider:
    """Yields one `FinalTranscript` after every `frames_per_utterance` audio
    frames received, cycling through `texts` (repeating the last entry once
    exhausted, rather than raising, so a test need not predict the exact
    number of utterances in advance)."""

    def __init__(self, texts: Sequence[str] = ("hello",), *, frames_per_utterance: int = 1) -> None:
        self._texts = list(texts)
        self._frames_per_utterance = frames_per_utterance
        self.frames_received = 0

    async def stream(
        self, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[PartialTranscript | FinalTranscript]:
        count = 0
        utterance_index = 0
        async for _frame in audio:
            self.frames_received += 1
            count += 1
            if count >= self._frames_per_utterance:
                count = 0
                text = self._texts[min(utterance_index, len(self._texts) - 1)]
                utterance_index += 1
                yield FinalTranscript(text=text)


class FakeLlmProvider:
    """Replays one pre-scripted turn (a sequence of text deltas, tool
    requests and a closing `TurnEnded`) per call to `stream_turn()`. The last
    scripted turn repeats once the script is exhausted."""

    def __init__(self, turns: Sequence[Sequence[str | ToolCallRequested | TurnEnded]]) -> None:
        self._turns = [list(turn) for turn in turns]
        self.calls: list[list[Mapping[str, object]]] = []
        self._turn_index = 0

    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ) -> AsyncIterator[str | ToolCallRequested | TurnEnded]:
        self.calls.append(list(messages))
        turn = self._turns[min(self._turn_index, len(self._turns) - 1)]
        self._turn_index += 1
        for item in turn:
            yield item


class EchoLlmProvider:
    """A structurally different `LlmProvider`: no script, no turn index --
    it deterministically echoes the most recent `user` message back as the
    assistant's reply, then ends the turn. Proves a second, independently
    written component can satisfy the protocol with no change to
    `PipelinedEngine` or the runtime."""

    def __init__(self, *, prefix: str = "you said: ") -> None:
        self._prefix = prefix
        self.calls: list[list[Mapping[str, object]]] = []

    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ) -> AsyncIterator[str | ToolCallRequested | TurnEnded]:
        self.calls.append(list(messages))
        last_user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user_text = str(message.get("content", ""))
                break
        yield self._prefix
        yield last_user_text
        yield TurnEnded()


class FakeTtsProvider:
    """Splits `text` into one `AudioOut` chunk per word, awaiting
    `chunk_delay` between chunks so a test can start consuming and then
    cancel mid-stream (the interruption/barge-in conformance case) rather
    than always racing a single instantaneous frame."""

    def __init__(self, *, chunk_delay: float = 0.0, sample_rate: int = 8000) -> None:
        self._chunk_delay = chunk_delay
        self._sample_rate = sample_rate
        self.synthesized: list[str] = []

    async def synthesize(self, text: str, voice: VoiceRef | None) -> AsyncIterator[AudioOut]:
        self.synthesized.append(text)
        words = text.split() or [text]
        for index, word in enumerate(words):
            if index > 0 and self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
            yield AudioOut(frame=word.encode("utf-8"), sample_rate=self._sample_rate)
