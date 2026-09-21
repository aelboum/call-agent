"""`RealtimeEngine` (Phase 2.2 brief section 7): the product-owned boundary
over a single duplex vendor session, proven with a deterministic fake and no
Pipecat -- and proven distinct from `PipelinedEngine`, not a wrapper around
it (ADR-0006).
"""

from __future__ import annotations

import ast
import asyncio
import inspect

from voiceagent.providers.engines import realtime as realtime_module
from voiceagent.providers.engines.contracts import (
    AudioOut,
    ConversationEngine,
    EngineSession,
    EngineSessionConfig,
    ToolResult,
    TurnEnded,
    VoiceRef,
)
from voiceagent.providers.engines.realtime import FakeRealtimeProvider, RealtimeEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(
        instructions="Answer the phone.", voice=VoiceRef(provider="fake", voice_id="v1")
    )


def test_realtime_engine_satisfies_the_conversation_engine_contract() -> None:
    engine = RealtimeEngine(FakeRealtimeProvider())
    assert isinstance(engine, ConversationEngine)

    async def scenario() -> None:
        session = await engine.start(_config())
        assert isinstance(session, EngineSession)

    asyncio.run(scenario())


def test_realtime_engine_does_not_depend_on_pipelined_engine() -> None:
    """A separate execution model (ADR-0006), not a wrapper around
    `PipelinedEngine` -- checked on the parsed source, the same technique
    `tests/providers/test_engine_contract.py` already uses for the
    Pipecat-free assertion."""
    tree = ast.parse(inspect.getsource(realtime_module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    assert not any("pipelined" in name.lower() for name in names)


def test_events_are_replayed_in_order() -> None:
    script = [AudioOut(frame=b"a", sample_rate=8000), TurnEnded()]
    engine = RealtimeEngine(FakeRealtimeProvider(script))

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        session.end()  # type: ignore[attr-defined]  -- FakeRealtimeProviderSession's own test hook.
        return [event async for event in session.events()]

    assert asyncio.run(scenario()) == script


def test_send_audio_and_tool_result_round_trip() -> None:
    engine = RealtimeEngine(FakeRealtimeProvider())

    async def scenario() -> tuple[list[bytes], list[ToolResult]]:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        result = ToolResult(call_id="tc-1", value={"ok": True})
        await session.submit_tool_result(result)
        return session.received_audio, session.tool_results  # type: ignore[attr-defined]

    audio, results = asyncio.run(scenario())
    assert audio == [b"frame-1"]
    assert results == [ToolResult(call_id="tc-1", value={"ok": True})]


def test_interrupt_and_close_are_idempotent() -> None:
    engine = RealtimeEngine(FakeRealtimeProvider())

    async def scenario() -> int:
        session = await engine.start(_config())
        await session.interrupt()
        await session.interrupt()
        await session.close()
        await session.close()
        return session.interrupts  # type: ignore[attr-defined]

    assert asyncio.run(scenario()) == 2


def test_a_second_realtime_provider_satisfies_the_contract_with_no_runtime_change() -> None:
    """A distinct fake, structurally unrelated to `FakeRealtimeProvider`,
    still satisfies `RealtimeProvider` -- proving the seam is real."""

    class MinimalRealtimeProvider:
        async def open(self, config: EngineSessionConfig):
            class _Session:
                def __init__(self) -> None:
                    self.closed = False

                async def send_audio(self, frame: bytes) -> None:
                    return None

                async def interrupt(self) -> None:
                    return None

                async def submit_tool_result(self, result: ToolResult) -> None:
                    return None

                async def close(self) -> None:
                    self.closed = True

                async def events(self):
                    return
                    yield  # pragma: no cover -- makes this an async generator.

            return _Session()

    engine = RealtimeEngine(MinimalRealtimeProvider())

    async def scenario() -> None:
        session = await engine.start(_config())
        await session.close()

    asyncio.run(scenario())
