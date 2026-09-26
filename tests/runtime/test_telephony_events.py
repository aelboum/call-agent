"""`TelephonyEventRouter` (Phase 2.21): demultiplexing `TelephonyProvider
.events()` by `call_ref`, one bounded queue per subscribed call.
"""

from __future__ import annotations

import asyncio

from voiceagent.runtime.telephony_events import TelephonyEventRouter
from voiceagent.telephony.contracts import CallDirection, CallEvent, CallEventType
from voiceagent.telephony.fakes import FakeTelephonyProvider


def test_subscriber_receives_only_its_own_call_refs_events() -> None:
    async def scenario() -> tuple[list[CallEvent], list[CallEvent]]:
        telephony = FakeTelephonyProvider()
        router = TelephonyEventRouter(telephony)
        queue_a = router.subscribe("call-a")
        queue_b = router.subscribe("call-b")
        run_task = asyncio.create_task(router.run())

        telephony.emit(
            CallEvent(
                type=CallEventType.ANSWERED, call_ref="call-a", direction=CallDirection.OUTBOUND
            )
        )
        telephony.emit(
            CallEvent(
                type=CallEventType.ANSWERED, call_ref="call-b", direction=CallDirection.OUTBOUND
            )
        )
        telephony.emit(
            CallEvent(
                type=CallEventType.HUNGUP, call_ref="call-a", direction=CallDirection.OUTBOUND
            )
        )

        event_a1 = await asyncio.wait_for(queue_a.get(), timeout=2.0)
        event_a2 = await asyncio.wait_for(queue_a.get(), timeout=2.0)
        event_b1 = await asyncio.wait_for(queue_b.get(), timeout=2.0)

        run_task.cancel()
        return [event_a1, event_a2], [event_b1]

    events_a, events_b = asyncio.run(scenario())
    assert [e.type for e in events_a] == [CallEventType.ANSWERED, CallEventType.HUNGUP]
    assert [e.type for e in events_b] == [CallEventType.ANSWERED]


def test_an_event_for_an_unknown_call_ref_is_dropped_not_delivered_anywhere() -> None:
    """Phase 2.21 brief section 5/13: a stale/unrecognized `call_ref` must
    not mutate an unrelated or new call session -- here, must not be
    delivered to any subscriber at all."""

    async def scenario() -> asyncio.Queue[CallEvent]:
        telephony = FakeTelephonyProvider()
        router = TelephonyEventRouter(telephony)
        queue = router.subscribe("call-a")
        run_task = asyncio.create_task(router.run())

        telephony.emit(
            CallEvent(
                type=CallEventType.HUNGUP, call_ref="unknown-call", direction=CallDirection.OUTBOUND
            )
        )
        telephony.emit(
            CallEvent(
                type=CallEventType.ANSWERED, call_ref="call-a", direction=CallDirection.OUTBOUND
            )
        )
        # The only event that should ever reach `queue` is the second one.
        event = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert event.call_ref == "call-a"
        run_task.cancel()
        return queue

    queue = asyncio.run(scenario())
    assert queue.empty()


def test_events_after_unsubscribe_are_dropped() -> None:
    async def scenario() -> asyncio.Queue[CallEvent]:
        telephony = FakeTelephonyProvider()
        router = TelephonyEventRouter(telephony)
        queue = router.subscribe("call-a")
        run_task = asyncio.create_task(router.run())

        telephony.emit(
            CallEvent(
                type=CallEventType.ANSWERED, call_ref="call-a", direction=CallDirection.OUTBOUND
            )
        )
        await asyncio.wait_for(queue.get(), timeout=2.0)
        router.unsubscribe("call-a")

        telephony.emit(
            CallEvent(
                type=CallEventType.HUNGUP, call_ref="call-a", direction=CallDirection.OUTBOUND
            )
        )
        await asyncio.sleep(0.05)  # let run() actually process the emitted event.
        run_task.cancel()
        return queue

    queue = asyncio.run(scenario())
    assert queue.empty()


def test_unsubscribe_is_idempotent_and_safe_with_no_prior_subscribe() -> None:
    telephony = FakeTelephonyProvider()
    router = TelephonyEventRouter(telephony)
    router.unsubscribe("never-subscribed")
    router.unsubscribe("never-subscribed")


def test_a_full_subscriber_queue_drops_the_new_event_and_does_not_raise() -> None:
    async def scenario() -> int:
        telephony = FakeTelephonyProvider()
        router = TelephonyEventRouter(telephony)
        queue = router.subscribe("call-a")
        run_task = asyncio.create_task(router.run())

        # Fill the bounded queue past its limit without draining it.
        for _ in range(64):
            telephony.emit(
                CallEvent(
                    type=CallEventType.RINGING, call_ref="call-a", direction=CallDirection.OUTBOUND
                )
            )
        await asyncio.sleep(0.1)
        run_task.cancel()
        return queue.qsize()

    size = asyncio.run(scenario())
    assert size == 32  # bounded, never grew past the router's own maxsize.


def test_concurrent_calls_never_cross_talk() -> None:
    """At least one test demonstrating that multiple simulated calls can
    operate concurrently without cross-talk (Phase 2.21 brief section 15)."""

    async def scenario() -> dict[str, list[CallEventType]]:
        telephony = FakeTelephonyProvider()
        router = TelephonyEventRouter(telephony)
        call_refs = [f"call-{i}" for i in range(10)]
        queues = {ref: router.subscribe(ref) for ref in call_refs}
        run_task = asyncio.create_task(router.run())

        for ref in call_refs:
            telephony.emit(
                CallEvent(
                    type=CallEventType.ANSWERED, call_ref=ref, direction=CallDirection.OUTBOUND
                )
            )
        for ref in reversed(call_refs):
            telephony.emit(
                CallEvent(type=CallEventType.HUNGUP, call_ref=ref, direction=CallDirection.OUTBOUND)
            )

        results: dict[str, list[CallEventType]] = {}
        for ref in call_refs:
            first = await asyncio.wait_for(queues[ref].get(), timeout=2.0)
            second = await asyncio.wait_for(queues[ref].get(), timeout=2.0)
            results[ref] = [first.type, second.type]
            assert first.call_ref == ref
            assert second.call_ref == ref

        run_task.cancel()
        return results

    results = asyncio.run(scenario())
    for events in results.values():
        assert events == [CallEventType.ANSWERED, CallEventType.HUNGUP]
