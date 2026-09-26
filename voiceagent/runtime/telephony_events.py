"""Demultiplexes `TelephonyProvider.events()` by `call_ref`, one queue per
subscribed call (Phase 2.21).

`voiceagent.telephony.contracts.TelephonyProvider`'s own docstring already
names the constraint this module exists to satisfy: "demultiplexing by
`call_ref` is the caller's job." One `CallRuntime` process runs many
concurrent calls (`voiceagent.runtime.supervisor`'s own module docstring:
"one process, one event loop, many calls") sharing exactly one
`TelephonyProvider` instance and therefore exactly one underlying event
stream -- if two calls each called `events()` independently, each async
generator would race the other for items off the same underlying queue,
splitting events between them non-deterministically instead of each call
seeing only its own. This module is the single, one-per-process consumer of
that one stream; a call task subscribes for its own `call_ref` and never
touches `TelephonyProvider.events()` directly.

An event for a `call_ref` with no current subscriber (a call that never
subscribed, or one that already unsubscribed at teardown) is silently
dropped -- this is deliberate, not a gap: `voiceagent.runtime.reconciliation`
already establishes that a call whose owning process is gone is never
resumed, and `voiceagent.calls.lifecycle`'s transition table already treats
a repeated/no-op transition as safe -- so a stale or unrecognized `call_ref`
mutating nothing is exactly the safe behavior Phase 2.21 brief section
5/6/13 asks for ("stale events must not mutate an unrelated/new call
session"), achieved here by construction rather than by an extra check.

**Phase 2.22 addition: `on_unrouted_offer`.** A brand-new inbound call's
`call_ref` is, by definition, never already subscribed -- someone has to see
its `CallEventType.OFFERED` event *as* an unrouted one to ever begin routing
it at all. Rather than let `voiceagent.runtime.orchestrator.CallOrchestrator`
open a second, independent consumer of `TelephonyProvider.events()` (which
would silently reintroduce the exact splitting bug this module exists to
prevent -- two consumers of one stream, each seeing a random subset), this
router calls one optional, synchronous callback for exactly that one case:
an `OFFERED` event with no current subscriber. Every other unrouted event
type is still dropped exactly as before -- this is deliberately narrow,
changes nothing about any other event's handling, and is `None` by default
(every Phase 2.21 caller/test), so existing behavior is byte-for-byte
unchanged unless a caller opts in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from voiceagent.telephony.contracts import CallEvent, CallEventType, CallRef, TelephonyProvider

__all__ = ["TelephonyEventRouter"]

_logger = logging.getLogger(__name__)

#: Bounded (brief section 12: "no unbounded queues"). Lifecycle events are
#: low-frequency and low-volume per call (a handful over a call's entire
#: life, never one per audio frame) -- this bound exists as a hard backstop,
#: not because real traffic is expected to approach it. Mirrors
#: `voiceagent.runtime.conversation_persistence`'s own established
#: bounded-queue discipline: drop the newest item and log on overflow,
#: never block the shared pump, never grow past the bound.
_SUBSCRIBER_QUEUE_MAXSIZE = 32


class TelephonyEventRouter:
    """One instance per `CallRuntime` process, wrapping the one
    `TelephonyProvider` every call task in that process shares.

    `subscribe()`/`unsubscribe()` are synchronous and cheap (a dict
    mutation) -- safe to call from a call task's own startup/teardown, never
    itself a source of blocking on the audio-adjacent path. `run()` is the
    one long-lived background task a caller starts once (alongside the
    runtime's own heartbeat loop) and lets run for the process's lifetime.
    """

    def __init__(
        self,
        telephony: TelephonyProvider,
        *,
        on_unrouted_offer: Callable[[CallEvent], None] | None = None,
    ) -> None:
        self._telephony = telephony
        self._subscribers: dict[CallRef, asyncio.Queue[CallEvent]] = {}
        #: Public and freely reassignable (not just constructor-injected):
        #: `voiceagent.runtime.orchestrator.CallOrchestrator` is naturally
        #: constructed *after* this router (it needs the router as one of
        #: its own dependencies), so the common real wiring is `router =
        #: TelephonyEventRouter(telephony); orchestrator =
        #: CallOrchestrator(telephony_events=router, ...); router
        #: .on_unrouted_offer = orchestrator.handle_unrouted_offer` -- never
        #: read by this class at construction time, only inside `run()`.
        self.on_unrouted_offer = on_unrouted_offer

    def subscribe(self, call_ref: CallRef) -> asyncio.Queue[CallEvent]:
        """Registers interest in `call_ref`'s own events, returning the
        bounded queue they will arrive on. Call once per call, before
        `run()` might see the first event for it (typically right after
        `TelephonyProvider.originate()`/`answer()` returns, or as soon as
        the call's own `call_ref` is otherwise known)."""
        queue: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._subscribers[call_ref] = queue
        return queue

    def unsubscribe(self, call_ref: CallRef) -> None:
        """Idempotent: safe to call from a call task's own `finally` block
        even if `subscribe()` was never called for it, and safe to call
        more than once."""
        self._subscribers.pop(call_ref, None)

    async def run(self) -> None:
        """Consumes `TelephonyProvider.events()` for as long as it yields
        events -- ends when the provider's own stream ends (e.g. an ESL
        control connection closing for good, `ManagedEslConnection.close()`).
        A caller that wants to keep routing across a *reconnect* re-invokes
        `run()` against the same router instance; `ManagedEslConnection`
        itself already presents one continuous `events()` stream across its
        own internal reconnects (see that class's own module docstring), so
        the common case needs nothing special here."""
        async for event in self._telephony.events():
            queue = self._subscribers.get(event.call_ref)
            if queue is None:
                if self.on_unrouted_offer is not None and event.type is CallEventType.OFFERED:
                    # Synchronous and expected to be fast/non-blocking
                    # (mirrors `ConversationPersist`'s own contract) -- the
                    # callback's real job (routing, a database call) must
                    # schedule its own task rather than run inline here,
                    # exactly like every other sync seam in this codebase.
                    self.on_unrouted_offer(event)
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _logger.warning(
                    "telephony_events.subscriber_queue_full",
                    extra={"event": "telephony_events.subscriber_queue_full"},
                )
