"""`voiceagent.runtime.call_task`'s Phase 2.4 tool-dispatch wiring, exercised
directly against `_run_pumps()` -- hermetic (no database, no SaaS-OS, no
real `ToolGateway`; a bare async callable stands in for
`deps.tool_gateway.execute(...)`, exactly the seam `run_call_task()`'s own
`_dispatch_tool_call` closure crosses).

The real, end-to-end path (a genuine `ToolGateway` behind
`CallTaskDependencies`, driven through `run_call_task()` against a real
`CallSession`) is `tests/integration/test_tool_gateway_integration.py`'s job;
this file's job is proving `_run_pumps()`'s own dispatch/cleanup contract,
independent of what actually executes a tool.

'The engine never executes a tool itself' is proven structurally, not by a
test here: `tests/architecture/test_tool_gateway_isolation.py
::test_the_conversation_engine_never_imports_the_tool_gateway` asserts no
`voiceagent.providers.engines.*` module can even import
`voiceagent.tools` -- there is no code path by which an engine could execute
one.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from voiceagent.providers.engines.contracts import (
    EngineSessionConfig,
    ToolCallRequested,
    ToolResult,
)
from voiceagent.providers.engines.fakes import FakeEngineSession
from voiceagent.runtime.call_task import _run_pumps
from voiceagent.telephony.contracts import AudioFormat
from voiceagent.telephony.fakes import FakeMediaStream


def _session(script) -> FakeEngineSession:
    return FakeEngineSession(EngineSessionConfig(instructions="hi"), script)


def test_tool_call_requested_reaches_dispatch_and_result_is_submitted_back() -> None:
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hangup", arguments={})
    session = _session([request])
    media = FakeMediaStream(AudioFormat())
    dispatched: list[ToolCallRequested] = []

    async def dispatch(req: ToolCallRequested) -> ToolResult:
        dispatched.append(req)
        return ToolResult(call_id=req.call_id, value={"hung_up": True})

    async def scenario() -> None:
        task = asyncio.create_task(_run_pumps(call_session_id, media, session, dispatch))
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

    assert dispatched == [request]
    assert session.tool_results == [ToolResult(call_id="c1", value={"hung_up": True})]


def test_a_dispatch_failure_is_normalized_rather_than_crashing_the_pump() -> None:
    """A `ToolGateway`/config bug (Tool Gateway misconfiguration, an
    unhandled exception) must not hang the model waiting for a result
    forever and must not crash this call's own event pump -- the model gets
    one normalized failure instead (call_task.py's own
    `_execute_and_submit_tool_call`)."""
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hangup", arguments={})
    session = _session([request])
    media = FakeMediaStream(AudioFormat())

    async def broken_dispatch(req: ToolCallRequested) -> ToolResult:
        raise RuntimeError("gateway misconfigured")

    async def scenario() -> None:
        task = asyncio.create_task(_run_pumps(call_session_id, media, session, broken_dispatch))
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

    assert len(session.tool_results) == 1
    result = session.tool_results[0]
    assert result.call_id == "c1"
    assert result.error_code == "internal_error"
    assert result.retryable is False


def test_multiple_tool_calls_in_one_session_are_each_dispatched_independently() -> None:
    call_session_id = uuid.uuid4()
    requests = [
        ToolCallRequested(call_id="c1", name="call.hold", arguments={}),
        ToolCallRequested(call_id="c2", name="call.resume", arguments={}),
    ]
    session = _session(requests)
    media = FakeMediaStream(AudioFormat())
    dispatched: list[str] = []

    async def dispatch(req: ToolCallRequested) -> ToolResult:
        dispatched.append(req.call_id)
        return ToolResult(call_id=req.call_id, value={})

    async def scenario() -> None:
        task = asyncio.create_task(_run_pumps(call_session_id, media, session, dispatch))
        for _ in range(200):
            if len(session.tool_results) >= 2:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("not every tool call was answered")
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())

    assert sorted(dispatched) == ["c1", "c2"]
    assert {r.call_id for r in session.tool_results} == {"c1", "c2"}


def test_cancelling_run_pumps_cancels_an_in_flight_tool_dispatch_task() -> None:
    """No orphaned background task may continue processing a completed call
    (Phase 2.2 brief section 22, unchanged by Phase 2.4's own dispatch
    tasks)."""
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.transfer", arguments={})
    session = _session([request])
    media = FakeMediaStream(AudioFormat())
    started = asyncio.Event()
    cancelled = False

    async def slow_dispatch(req: ToolCallRequested) -> ToolResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return ToolResult(call_id=req.call_id, value={})  # pragma: no cover

    async def scenario() -> None:
        task = asyncio.create_task(_run_pumps(call_session_id, media, session, slow_dispatch))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        media.end_inbound()
        session.end()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert cancelled is True
    assert session.tool_results == []  # never got a result -- the dispatch never finished
