"""`RealtimeEngine` (ADR-0006, ADR-0009 point 5): the product-owned adapter
boundary over a single, stateful, duplex vendor session -- transcription,
reasoning, tool-calling and synthesis happening inside one provider-held
session, which a three-box STT/LLM/TTS pipeline cannot express.

**Not a wrapper around `PipelinedEngine`.** It shares `PipelinedEngine`'s
outer contract (`ConversationEngine`/`EngineSession`, from
`voiceagent.providers.engines.contracts`) because that contract is what lets
the Call Runtime be written once against either engine type -- but it has no
STT/LLM/TTS composition, no turn buffer, no internal message list: a
`RealtimeProvider` session already speaks in `EngineEvent` terms once
adapted, because *translating* the vendor's own wire shape into that
vocabulary is the adapter's entire job (ADR-0006's own framing: "what
remains on our side is protocol adaptation, reconnection, and mapping the
provider's tool-call events onto the Tool Gateway").

`RealtimeProvider` is deliberately narrow and names no vendor -- ADR-0009
point 6: no vendor SDK, vendor type, or vendor-specific field may appear
here. `FakeRealtimeProvider` is the deterministic double every hermetic test
uses; a real OpenAI-Realtime-shaped adapter (ADR-0009 point 5's own "first
`RealtimeEngine` candidate, when it is built") is future, out-of-scope work
that would live in its own `voiceagent.providers.engines.<vendor>` module,
implementing exactly this same `RealtimeProvider` protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voiceagent.providers.engines.contracts import EngineEvent, EngineSessionConfig, ToolResult

__all__ = [
    "FakeRealtimeProvider",
    "FakeRealtimeProviderSession",
    "RealtimeEngine",
    "RealtimeProvider",
    "RealtimeProviderSession",
]


@runtime_checkable
class RealtimeProviderSession(Protocol):
    """One vendor duplex session, already adapted into contract terms. A
    real adapter's implementation of this is where vendor wire-protocol
    translation happens -- entirely below this module, never visible above
    it."""

    async def send_audio(self, frame: bytes) -> None: ...

    async def interrupt(self) -> None:
        """Most realtime providers own turn-taking and interruption
        natively server-side (ADR-0006: "the vendor already owns VAD, turn
        detection and interruption") -- a real adapter forwards this to
        whatever native cancellation the vendor exposes (e.g. a
        response-cancel message); it is still part of the contract because
        the Call Runtime calls `interrupt()` uniformly across every engine
        type and must never need to know which kind it is holding."""
        ...

    async def submit_tool_result(self, result: ToolResult) -> None: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[EngineEvent]: ...


@runtime_checkable
class RealtimeProvider(Protocol):
    """Opens one `RealtimeProviderSession` per call. The
    `ConversationEngine`-shaped counterpart to `SttProvider`/`LlmProvider`/
    `TtsProvider`, but for a single indivisible vendor session rather than
    three composable ones."""

    async def open(self, config: EngineSessionConfig) -> RealtimeProviderSession: ...


class RealtimeEngine:
    """A `ConversationEngine` over exactly one `RealtimeProvider`. Thin by
    design: everything that matters happens inside the provider's own
    `open()`/session, not here -- this class exists so the Call Runtime
    depends on `ConversationEngine` uniformly, never on `RealtimeProvider`
    directly."""

    def __init__(self, provider: RealtimeProvider) -> None:
        self._provider = provider

    async def start(self, config: EngineSessionConfig) -> RealtimeProviderSession:
        return await self._provider.open(config)


class FakeRealtimeProviderSession:
    """Replays a scripted event sequence, exactly like
    `voiceagent.providers.engines.fakes.FakeEngineSession` -- deliberately
    similar in shape, since a real vendor session, once adapted, presents
    the same shape to the runtime; the architectural distinction that
    matters is that this class has no STT/LLM/TTS composition inside it, not
    that its code must look different from a pipelined session's."""

    def __init__(self, config: EngineSessionConfig, script: list[EngineEvent]) -> None:
        self.config = config
        self.received_audio: list[bytes] = []
        self.tool_results: list[ToolResult] = []
        self.interrupts = 0
        self.closed = False
        self._queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        for event in script:
            self._queue.put_nowait(event)

    def emit(self, event: EngineEvent) -> None:
        self._queue.put_nowait(event)

    def end(self) -> None:
        self._queue.put_nowait(None)

    async def send_audio(self, frame: bytes) -> None:
        if self.closed:
            raise RuntimeError("session is closed")
        self.received_audio.append(frame)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def submit_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[EngineEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class FakeRealtimeProvider:
    """A `RealtimeProvider` handing out `FakeRealtimeProviderSession`s --
    the "non-Pipecat, non-vendor path" for the realtime side of the engine
    conformance suite, mirroring `FakeConversationEngine`'s role for the
    contract as a whole (ADR-0006 decision point 7)."""

    def __init__(self, script: list[EngineEvent] | None = None) -> None:
        self._script = list(script) if script is not None else []
        self.sessions: list[FakeRealtimeProviderSession] = []

    async def open(self, config: EngineSessionConfig) -> FakeRealtimeProviderSession:
        session = FakeRealtimeProviderSession(config, list(self._script))
        self.sessions.append(session)
        return session
