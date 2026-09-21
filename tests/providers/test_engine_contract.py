"""The ConversationEngine contract and its fake (Phase 1 brief sections 9, 12).

The first two tests are the architecturally load-bearing ones: the contract is
framework-free, and a working non-vendor engine exists. Together they are what
makes "Pipecat is removable, not load-bearing" a fact rather than an intention
(ADR-0006 decision points 1 and 7).

The rest are the seed of the engine conformance suite every real engine will
have to pass in Phase 2.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Sequence

from voiceagent.providers.engines import contracts as contracts_module
from voiceagent.providers.engines.contracts import (
    AudioOut,
    ConversationEngine,
    EngineError,
    EngineErrorCode,
    EngineEvent,
    EngineSession,
    EngineSessionConfig,
    FinalTranscript,
    SpeechStarted,
    ToolCallRequested,
    ToolResult,
    ToolSpec,
    TurnEnded,
    VoiceRef,
)
from voiceagent.providers.engines.fakes import FakeConversationEngine


def _config() -> EngineSessionConfig:
    return EngineSessionConfig(
        instructions="Answer the phone.",
        greeting="Hello.",
        voice=VoiceRef(provider="fake", voice_id="v1"),
        tools=[ToolSpec(name="contact.lookup", description="Find a contact", input_schema={})],
    )


def test_contract_module_is_framework_free() -> None:
    """ADR-0006 decision point 1: no Pipecat type, frame, processor or
    exception appears in the contract -- which is what lets the Call Runtime
    be written once, against this module, and never against a framework.

    Checked on the parsed syntax tree rather than the file text, so the
    module's own docstring may name the rule it enforces: every import,
    identifier and attribute is inspected, and no comment or docstring is.
    """
    tree = ast.parse(inspect.getsource(contracts_module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not any("pipecat" in name.lower() for name in names)


def test_a_non_vendor_engine_exists_and_satisfies_the_contract() -> None:
    """ADR-0006 decision point 7: a non-Pipecat, non-vendor path always
    exists and always runs in CI, so no engine implementation is ever
    structurally required."""
    engine = FakeConversationEngine()
    assert isinstance(engine, ConversationEngine)
    session = asyncio.run(engine.start(_config()))
    assert isinstance(session, EngineSession)


def test_session_receives_the_resolved_configuration() -> None:
    """An engine gets a resolved, immutable session config and has no tenant
    concept: tenant isolation lives above it, never inside it."""
    engine = FakeConversationEngine()
    config = _config()
    session = asyncio.run(engine.start(config))
    assert session.config is config
    assert not hasattr(config, "tenant_id")


def test_events_are_replayed_in_order() -> None:
    script = [
        SpeechStarted(),
        FinalTranscript(text="I'd like to book an appointment"),
        AudioOut(frame=b"audio", sample_rate=8000),
        TurnEnded(),
    ]
    engine = FakeConversationEngine(script)

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        session.end()
        return [event async for event in session.events()]

    assert asyncio.run(scenario()) == script


def test_audio_is_forwarded_to_the_session() -> None:
    engine = FakeConversationEngine()

    async def scenario() -> list[bytes]:
        session = await engine.start(_config())
        await session.send_audio(b"frame-1")
        await session.send_audio(b"frame-2")
        return session.received_audio

    assert asyncio.run(scenario()) == [b"frame-1", b"frame-2"]


def test_interrupt_drops_pending_agent_audio_and_is_idempotent() -> None:
    """Barge-in is defined by the contract, not by an implementation: calling
    `interrupt()` must stop agent speech and be safe to call repeatedly at any
    point in a turn. This is the conformance assertion every real engine will
    inherit."""
    engine = FakeConversationEngine([AudioOut(frame=b"a", sample_rate=8000), TurnEnded()])

    async def scenario() -> Sequence[EngineEvent]:
        session = await engine.start(_config())
        await session.interrupt()
        await session.interrupt()
        session.end()
        events = [event async for event in session.events()]
        assert session.interrupts == 2
        return events

    events = asyncio.run(scenario())
    assert not any(isinstance(event, AudioOut) for event in events)
    assert any(isinstance(event, TurnEnded) for event in events)


def test_tool_call_round_trip() -> None:
    """The engine *requests* a tool and *receives* a result. It never
    executes one: authorization, validation, idempotency and audit belong to
    the Tool Gateway (ADR-0003)."""
    request = ToolCallRequested(call_id="tc-1", name="contact.lookup", arguments={"q": "+1555"})
    engine = FakeConversationEngine([request])

    async def scenario() -> tuple[list[object], list[ToolResult]]:
        session = await engine.start(_config())
        await session.submit_tool_result(ToolResult(call_id="tc-1", value={"found": True}))
        session.end()
        return [event async for event in session.events()], session.tool_results

    events, results = asyncio.run(scenario())
    assert events[0] == request
    assert results == [ToolResult(call_id="tc-1", value={"found": True})]


def test_tool_failure_is_a_value_not_an_exception() -> None:
    """So the agent can say "I couldn't book that, may I take a message?"
    instead of going silent -- and so internal detail never crosses to the
    model."""
    result = ToolResult(call_id="tc-2", error_code="unavailable", retryable=True)
    engine = FakeConversationEngine()

    async def scenario() -> list[ToolResult]:
        session = await engine.start(_config())
        await session.submit_tool_result(result)
        return session.tool_results

    assert asyncio.run(scenario()) == [result]


def test_close_is_idempotent_and_ends_the_event_stream() -> None:
    engine = FakeConversationEngine()

    async def scenario() -> list[object]:
        session = await engine.start(_config())
        await session.close()
        await session.close()
        return [event async for event in session.events()]

    assert asyncio.run(scenario()) == []


def test_error_taxonomy_is_provider_agnostic() -> None:
    """Five codes, so the runtime's fallback policy never changes when a
    provider does."""
    assert {code.value for code in EngineErrorCode} == {
        "auth",
        "rate_limit",
        "transient",
        "invalid_request",
        "provider_down",
    }
    assert EngineError(code=EngineErrorCode.TRANSIENT, message="retry").code is (
        EngineErrorCode.TRANSIENT
    )
