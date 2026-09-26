"""`voiceagent.runtime.call_task._run_pumps_with_remote_hangup_detection`
(Phase 2.21) -- hermetic, at the same `_run_pumps()` layer
`tests/runtime/test_call_task_tools.py` already tests at (no database, no
SaaS-OS, no real `TelephonyProvider`).
"""

from __future__ import annotations

import asyncio
import uuid

from voiceagent.providers.engines.contracts import EngineSessionConfig
from voiceagent.providers.engines.fakes import FakeEngineSession
from voiceagent.runtime.call_task import (
    CancellationSignal,
    _run_pumps_with_remote_hangup_detection,
)
from voiceagent.runtime.telephony_events import TelephonyEventRouter
from voiceagent.telephony.contracts import AudioFormat, CallDirection, CallEvent, CallEventType
from voiceagent.telephony.fakes import FakeMediaStream, FakeTelephonyProvider


def _session() -> FakeEngineSession:
    return FakeEngineSession(EngineSessionConfig(instructions="hi"), [])


async def _noop_dispatch(request):  # pragma: no cover -- no tool calls scripted in these tests.
    raise AssertionError("no tool call expected")


def test_none_router_behaves_exactly_like_plain_run_pumps() -> None:
    """Regression safety: every pre-Phase-2.21 caller (every existing test)
    passes no `telephony_events` at all -- this must be indistinguishable
    from calling `_run_pumps()` directly."""
    media = FakeMediaStream(AudioFormat())
    session = _session()
    cancellation = CancellationSignal()

    async def scenario() -> None:
        task = asyncio.create_task(
            _run_pumps_with_remote_hangup_detection(
                uuid.uuid4(),
                media,
                session,
                _noop_dispatch,
                None,
                telephony_events=None,
                call_ref="call-1",
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.01)
        media.end_inbound()
        session.end()
        await task

    asyncio.run(scenario())
    assert cancellation.reason == "hangup"  # untouched default -- never set by this path.


def test_a_remote_hangup_event_stops_the_pump_and_sets_cancellation_reason() -> None:
    media = FakeMediaStream(AudioFormat())
    session = _session()
    telephony = FakeTelephonyProvider()
    router = TelephonyEventRouter(telephony)
    cancellation = CancellationSignal(reason="unset")

    async def scenario() -> None:
        router_task = asyncio.create_task(router.run())
        call_task = asyncio.create_task(
            _run_pumps_with_remote_hangup_detection(
                uuid.uuid4(),
                media,
                session,
                _noop_dispatch,
                None,
                telephony_events=router,
                call_ref="call-1",
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.02)  # let the subscription actually register.
        telephony.emit(
            CallEvent(
                type=CallEventType.HUNGUP, call_ref="call-1", direction=CallDirection.OUTBOUND
            )
        )
        await asyncio.wait_for(call_task, timeout=2.0)
        router_task.cancel()

    asyncio.run(scenario())
    assert cancellation.reason == "hangup"
    assert media.closed is False  # this function only stops the pump; teardown closes media.


def test_events_for_a_different_call_ref_do_not_stop_this_pump() -> None:
    media = FakeMediaStream(AudioFormat())
    session = _session()
    telephony = FakeTelephonyProvider()
    router = TelephonyEventRouter(telephony)
    cancellation = CancellationSignal(reason="unset")

    async def scenario() -> None:
        router_task = asyncio.create_task(router.run())
        call_task = asyncio.create_task(
            _run_pumps_with_remote_hangup_detection(
                uuid.uuid4(),
                media,
                session,
                _noop_dispatch,
                None,
                telephony_events=router,
                call_ref="call-1",
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.02)
        telephony.emit(
            CallEvent(
                type=CallEventType.HUNGUP,
                call_ref="a-different-call",
                direction=CallDirection.OUTBOUND,
            )
        )
        await asyncio.sleep(0.05)
        assert not call_task.done()

        media.end_inbound()
        session.end()
        await asyncio.wait_for(call_task, timeout=2.0)
        router_task.cancel()

    asyncio.run(scenario())
    assert cancellation.reason == "unset"  # never touched -- the event wasn't for this call.


def test_outer_cancellation_still_propagates_and_is_not_swallowed() -> None:
    """The supervisor's own cancellation path (`CallRuntime.cancel_call()`
    sets `CancellationSignal.reason` *before* calling `task.cancel()`) must
    keep working unchanged -- this function must never mistake an outer
    cancellation for its own remote-hangup stop."""
    media = FakeMediaStream(AudioFormat())
    session = _session()
    telephony = FakeTelephonyProvider()
    router = TelephonyEventRouter(telephony)
    cancellation = CancellationSignal(reason="runtime_shutdown")

    async def scenario() -> bool:
        router_task = asyncio.create_task(router.run())
        call_task = asyncio.create_task(
            _run_pumps_with_remote_hangup_detection(
                uuid.uuid4(),
                media,
                session,
                _noop_dispatch,
                None,
                telephony_events=router,
                call_ref="call-1",
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.02)
        call_task.cancel()
        raised = False
        try:
            await call_task
        except asyncio.CancelledError:
            raised = True
        router_task.cancel()
        return raised

    assert asyncio.run(scenario()) is True
    assert (
        cancellation.reason == "runtime_shutdown"
    )  # exactly what the "supervisor" set, untouched.


def test_unsubscribes_on_normal_completion() -> None:
    media = FakeMediaStream(AudioFormat())
    session = _session()
    telephony = FakeTelephonyProvider()
    router = TelephonyEventRouter(telephony)
    cancellation = CancellationSignal()

    async def scenario() -> bool:
        router_task = asyncio.create_task(router.run())
        call_task = asyncio.create_task(
            _run_pumps_with_remote_hangup_detection(
                uuid.uuid4(),
                media,
                session,
                _noop_dispatch,
                None,
                telephony_events=router,
                call_ref="call-1",
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.02)
        media.end_inbound()
        session.end()
        await asyncio.wait_for(call_task, timeout=2.0)
        router_task.cancel()
        return "call-1" in router._subscribers  # noqa: SLF001 -- test assertion only.

    assert asyncio.run(scenario()) is False
