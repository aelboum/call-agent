"""A deterministic `ConversationEngine` (ADR-0006 decision point 7).

This fake is load-bearing architecture, not test scaffolding. It is the
guarantee that a non-Pipecat, non-vendor path always exists and always runs in
CI: the product can be built, tested and reasoned about with no AI provider
present, so no engine implementation is ever structurally required.

It is deterministic by construction -- a script of events in, the same events
out -- so runtime tests can assert exact sequences rather than sampling a
probabilistic system.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from voiceagent.providers.engines.contracts import (
    AudioOut,
    EngineEvent,
    EngineSessionConfig,
    ToolResult,
    TurnEnded,
)

__all__ = ["FakeConversationEngine", "FakeEngineSession"]


class FakeEngineSession:
    """Replays a scripted event sequence and records what it was given."""

    def __init__(self, config: EngineSessionConfig, script: Sequence[EngineEvent]) -> None:
        self.config = config
        self.received_audio: list[bytes] = []
        self.tool_results: list[ToolResult] = []
        self.interrupts = 0
        self.closed = False
        self._queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        for event in script:
            self._queue.put_nowait(event)

    def emit(self, event: EngineEvent) -> None:
        """Queue an additional event (a test's way of driving the session)."""
        self._queue.put_nowait(event)

    def end(self) -> None:
        """Signal that no further events will arrive."""
        self._queue.put_nowait(None)

    async def send_audio(self, frame: bytes) -> None:
        if self.closed:
            raise RuntimeError("session is closed")
        self.received_audio.append(frame)

    async def interrupt(self) -> None:
        """Barge-in. Idempotent, and drops any queued agent audio -- the
        observable behavior the engine conformance suite asserts."""
        self.interrupts += 1
        remaining: list[EngineEvent | None] = []
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if isinstance(event, AudioOut):
                continue
            remaining.append(event)
        for event in remaining:
            self._queue.put_nowait(event)

    async def submit_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)
        self._queue.put_nowait(TurnEnded())

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


class FakeConversationEngine:
    """A `ConversationEngine` handing out `FakeEngineSession`s."""

    def __init__(self, script: Sequence[EngineEvent] = ()) -> None:
        self._script = tuple(script)
        self.sessions: list[FakeEngineSession] = []

    async def start(self, config: EngineSessionConfig) -> FakeEngineSession:
        session = FakeEngineSession(config, self._script)
        self.sessions.append(session)
        return session
