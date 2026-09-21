"""`voiceagent.runtime.call_task._run_pumps()`'s Phase 2.5 conversation-
persistence wiring -- hermetic (no database, no SaaS-OS; a bare synchronous
callable stands in for `deps.conversation_persistence.enqueue(...)`, exactly
the seam `run_call_task()`'s own `_persist_turn` closure crosses), mirroring
`tests/runtime/test_call_task_tools.py`'s own structure for the Tool Gateway
dispatch seam.
"""

from __future__ import annotations

import asyncio
import uuid

from voiceagent.providers.engines.contracts import (
    AssistantResponse,
    EngineSessionConfig,
    FinalTranscript,
    SystemPromptSet,
    ToolCallRequested,
    ToolResult,
)
from voiceagent.providers.engines.fakes import FakeEngineSession
from voiceagent.runtime.call_task import _run_pumps
from voiceagent.runtime.conversation_persistence import PendingTurn
from voiceagent.telephony.contracts import AudioFormat
from voiceagent.telephony.fakes import FakeMediaStream


def _session(script) -> FakeEngineSession:
    return FakeEngineSession(EngineSessionConfig(instructions="hi"), script)


def test_persist_turn_is_optional_and_defaults_to_a_no_op() -> None:
    """Every hermetic test predating Phase 2.5 (`test_call_task_tools.py`)
    calls `_run_pumps()` with no fifth argument -- it must keep working
    unchanged."""
    call_session_id = uuid.uuid4()
    session = _session([FinalTranscript(text="hi")])
    media = FakeMediaStream(AudioFormat())

    async def dispatch(req: ToolCallRequested) -> ToolResult:
        return ToolResult(call_id=req.call_id, value={})

    async def scenario() -> None:
        task = asyncio.create_task(_run_pumps(call_session_id, media, session, dispatch))
        await asyncio.sleep(0.02)
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())  # must not raise


def test_system_user_and_assistant_events_are_persisted_with_matching_roles() -> None:
    call_session_id = uuid.uuid4()
    script = [
        SystemPromptSet(instructions="be nice", event_id="sys-1"),
        FinalTranscript(text="hello there", event_id="ft-1"),
        AssistantResponse(text="hi!", event_id="ar-1"),
    ]
    session = _session(script)
    media = FakeMediaStream(AudioFormat())
    persisted: list[PendingTurn] = []

    async def dispatch(req: ToolCallRequested) -> ToolResult:
        raise AssertionError("no tool call in this script")

    async def scenario() -> None:
        task = asyncio.create_task(
            _run_pumps(call_session_id, media, session, dispatch, persisted.append)
        )
        await asyncio.sleep(0.02)
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())

    by_event_id = {turn.event_id: turn for turn in persisted}
    assert by_event_id["sys-1"] == PendingTurn(event_id="sys-1", role="system", content="be nice")
    assert by_event_id["ft-1"] == PendingTurn(event_id="ft-1", role="user", content="hello there")
    assert by_event_id["ar-1"] == PendingTurn(event_id="ar-1", role="assistant", content="hi!")


def test_tool_call_turn_is_persisted_before_the_tool_result_turn() -> None:
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hangup", arguments={"reason": "done"})
    session = _session([request])
    media = FakeMediaStream(AudioFormat())
    persisted: list[PendingTurn] = []

    async def dispatch(req: ToolCallRequested) -> ToolResult:
        return ToolResult(call_id=req.call_id, value={"hung_up": True})

    async def scenario() -> None:
        task = asyncio.create_task(
            _run_pumps(call_session_id, media, session, dispatch, persisted.append)
        )
        for _ in range(200):
            if session.tool_results:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("tool result was never submitted back to the engine session")
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())

    tool_turns = [turn for turn in persisted if turn.event_id == "c1"]
    assert [turn.role for turn in tool_turns] == ["tool_call", "tool_result"]
    assert tool_turns[0].tool_payload == {"name": "call.hangup", "arguments": {"reason": "done"}}
    assert tool_turns[1].tool_payload == {"value": {"hung_up": True}, "error_code": None}


def test_a_normalized_dispatch_failure_still_persists_its_tool_result_turn() -> None:
    """The dispatch failure gets normalized into a `ToolResult` before this
    function ever sees it (`_execute_and_submit_tool_call`'s own `except`
    clause) -- the durable "tool_result" turn records that normalized
    outcome, not the raw exception."""
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hangup", arguments={})
    session = _session([request])
    media = FakeMediaStream(AudioFormat())
    persisted: list[PendingTurn] = []

    async def broken_dispatch(req: ToolCallRequested) -> ToolResult:
        raise RuntimeError("gateway misconfigured")

    async def scenario() -> None:
        task = asyncio.create_task(
            _run_pumps(call_session_id, media, session, broken_dispatch, persisted.append)
        )
        for _ in range(200):
            if session.tool_results:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("no normalized result was ever submitted")
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())

    tool_result_turns = [turn for turn in persisted if turn.role == "tool_result"]
    assert len(tool_result_turns) == 1
    assert tool_result_turns[0].tool_payload == {"value": None, "error_code": "internal_error"}


def test_partial_transcripts_and_turn_ended_are_never_persisted() -> None:
    from voiceagent.providers.engines.contracts import PartialTranscript, TurnEnded

    call_session_id = uuid.uuid4()
    script = [PartialTranscript(text="hel"), PartialTranscript(text="hell"), TurnEnded()]
    session = _session(script)
    media = FakeMediaStream(AudioFormat())
    persisted: list[PendingTurn] = []

    async def dispatch(req: ToolCallRequested) -> ToolResult:
        raise AssertionError("no tool call in this script")

    async def scenario() -> None:
        task = asyncio.create_task(
            _run_pumps(call_session_id, media, session, dispatch, persisted.append)
        )
        await asyncio.sleep(0.02)
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())

    assert persisted == []
