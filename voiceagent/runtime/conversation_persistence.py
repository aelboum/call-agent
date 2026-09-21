"""`ConversationPersistence` -- the bounded, off-audio-path mechanism that
turns a running call's `EngineEvent`s into durable `app.conversation_turns`
rows (Phase 2.5).

```text
pump_engine_events()  (voiceagent.runtime.call_task, audio-adjacent)
    |
    | persist_turn(PendingTurn(...))     synchronous, non-blocking put_nowait
    v
_CallWorker's own bounded asyncio.Queue  (one per call, maxsize=queue_maxsize)
    |
    | a single dedicated asyncio.Task, FIFO
    v
DatabaseBoundary.run(persist_conversation_turn, ...)   off the event loop
```

**Never on the audio path** (Phase 2.5 brief section 6). `enqueue()` is a
plain, synchronous, non-blocking method -- `asyncio.Queue.put_nowait()` never
awaits, so a caller on the audio-adjacent pump never blocks on this module,
on `DatabaseBoundary`, or on PostgreSQL. A full queue is handled by dropping
the new turn and logging the fact (never by blocking the caller and never by
growing the queue past `queue_maxsize` -- brief section 19: "never allow an
unbounded queue").

**Bounded retry, then an observable, final failure** (brief section 7): each
`_CallWorker` retries a failed write up to `max_retries` times, with a linear
backoff, before giving up and logging the permanent failure -- never an
infinite retry loop, never a second queue to spill into. The call itself is
never affected either way; only its own conversation history gains a
documented gap (`docs/PHASE-2.5-STATUS.md`, "Persistence failure policy").

**Bounded lifecycle, cancellation, deterministic shutdown** (brief section
6): every `_CallWorker` is created by exactly one `ConversationPersistence
.start()` call and torn down by exactly one matching `finish()` call
(`voiceagent.runtime.call_task.run_call_task()`'s own `finally` block, the
same place `voiceagent.tools.gateway.ToolGateway.forget_call()` is called).
`finish()` is bounded by `drain_timeout_seconds`: it lets the worker drain
whatever is already queued, up to that timeout, and force-cancels it if that
is exceeded -- a slow or wedged database can delay a call's teardown by at
most that configured amount, never indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from voiceagent.conversations.models import ConversationTurn
from voiceagent.conversations.service import persist_conversation_turn
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.tenancy import TenantContext

__all__ = ["ConversationPersistence", "PendingTurn", "WorkerStats"]

#: The synchronous function one `_CallWorker` runs, through `DatabaseBoundary
#: .run()`, for each turn it dequeues. Defaults to the real
#: `voiceagent.conversations.service.persist_conversation_turn` everywhere
#: except a hermetic test, which substitutes a fake with the same call
#: shape to exercise queue/retry/shutdown behavior with no database at all
#: -- exactly the same injectable-default shape
#: `voiceagent.tools.gateway.ToolGateway.__init__`'s own `registry`
#: parameter already establishes for this codebase. The return value is
#: never used by `_CallWorker` (only whether the call raised matters), so
#: the type only needs to accept the real function's `ConversationTurn`
#: return alongside a fake's `None`.
PersistFn = Callable[..., ConversationTurn | None]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingTurn:
    """One turn waiting to be durably persisted -- the runtime's own,
    provider-neutral shape, built once (by `voiceagent.runtime.call_task`)
    from whichever `EngineEvent`/`ToolResult` produced it."""

    event_id: str
    role: str
    content: str | None = None
    tool_payload: Mapping[str, object] | None = None


@dataclass(slots=True)
class WorkerStats:
    """Observable outcome counters for one call's persistence worker --
    never conversation content, so this is safe to log or expose without
    the privacy concerns raw turns carry (brief section 13)."""

    persisted: int = 0
    dropped: int = 0
    failed: int = 0


class _CallWorker:
    """Owns exactly one call's bounded queue and its one FIFO consumer
    task. FIFO processing of a single queue is itself what keeps a tool
    call's own turn ordered ahead of its tool result: `call_task.py` always
    enqueues the `tool_call` turn before it ever awaits the dispatch that
    produces the `tool_result` turn (brief section 5's "assistant tool-call
    message persisted before tool result")."""

    def __init__(
        self,
        *,
        db: DatabaseBoundary,
        context: TenantContext,
        call_session_id: uuid.UUID,
        queue_maxsize: int,
        max_retries: int,
        retry_backoff_seconds: float,
        persist_fn: PersistFn,
    ) -> None:
        self._db = db
        self._context = context
        self._call_session_id = call_session_id
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._persist_fn = persist_fn
        self._queue: asyncio.Queue[PendingTurn | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._task: asyncio.Task[None] | None = None
        self.stats = WorkerStats()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def enqueue(self, turn: PendingTurn) -> bool:
        """Synchronous and non-blocking. `False` means the turn was dropped
        (queue full) -- the caller (the audio-adjacent pump) must never
        treat that as fatal to the call itself."""
        try:
            self._queue.put_nowait(turn)
            return True
        except asyncio.QueueFull:
            self.stats.dropped += 1
            _logger.warning(
                "conversation persistence queue full for CallSession %s; turn dropped "
                "(event_id=%s, role=%s)",
                self._call_session_id,
                turn.event_id,
                turn.role,
            )
            return False

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            await self._persist_with_retry(item)

    async def _persist_with_retry(self, turn: PendingTurn) -> None:
        attempt = 0
        while True:
            try:
                await self._db.run(
                    self._persist_fn,
                    self._context,
                    self._call_session_id,
                    event_id=turn.event_id,
                    role=turn.role,
                    content=turn.content,
                    tool_payload=turn.tool_payload,
                )
                self.stats.persisted += 1
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a persistence failure must
                # never crash this call's own worker task (and, by
                # extension, never the call itself); bounded-retried, then
                # logged as a final, observable failure -- never a
                # transcript/tool-argument value (brief section 13: "be
                # careful with ... tool arguments").
                attempt += 1
                if attempt > self._max_retries:
                    self.stats.failed += 1
                    _logger.exception(
                        "conversation turn persistence permanently failed for CallSession "
                        "%s (event_id=%s, role=%s) after %d attempt(s)",
                        self._call_session_id,
                        turn.event_id,
                        turn.role,
                        attempt,
                    )
                    return
                await asyncio.sleep(self._retry_backoff_seconds * attempt)

    async def shutdown(self, drain_timeout_seconds: float) -> None:
        """Bounded and deterministic: returns within `drain_timeout_seconds`
        no matter what state the worker is in, cancelling it if it has not
        finished by then."""
        if self._task is None:
            return
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # The queue is already full of real work; the FIFO consumer
            # will still reach the end of it before drain_timeout_seconds
            # forces a cancellation below, so no explicit sentinel is
            # strictly required for boundedness here.
            pass
        try:
            await asyncio.wait_for(self._task, timeout=drain_timeout_seconds)
        except TimeoutError:
            _logger.warning(
                "conversation persistence drain timed out for CallSession %s "
                "(%.1fs); remaining queued turns discarded",
                self._call_session_id,
                drain_timeout_seconds,
            )
        except asyncio.CancelledError:
            pass


class ConversationPersistence:
    """Process-wide (one instance shared across every call a `CallRuntime`
    hosts, exactly like `voiceagent.tools.gateway.ToolGateway`), but its
    actual state is entirely per-call: `_workers` holds one `_CallWorker`
    per in-flight `call_session_id`, created by `start()` and removed by
    `finish()`."""

    def __init__(
        self,
        db: DatabaseBoundary,
        *,
        queue_maxsize: int = 256,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        drain_timeout_seconds: float = 5.0,
        persist_fn: PersistFn = persist_conversation_turn,
    ) -> None:
        self._db = db
        self._queue_maxsize = queue_maxsize
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._drain_timeout_seconds = drain_timeout_seconds
        self._persist_fn = persist_fn
        self._workers: dict[uuid.UUID, _CallWorker] = {}

    def start(self, context: TenantContext, call_session_id: uuid.UUID) -> None:
        """Idempotent for a call already started -- a second call is a
        no-op rather than replacing the existing worker (which would orphan
        its already-queued turns)."""
        if call_session_id in self._workers:
            return
        worker = _CallWorker(
            db=self._db,
            context=context,
            call_session_id=call_session_id,
            queue_maxsize=self._queue_maxsize,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
            persist_fn=self._persist_fn,
        )
        self._workers[call_session_id] = worker
        worker.start()

    def enqueue(self, call_session_id: uuid.UUID, turn: PendingTurn) -> bool:
        worker = self._workers.get(call_session_id)
        if worker is None:
            _logger.warning(
                "conversation persistence not started for CallSession %s; turn dropped "
                "(event_id=%s, role=%s)",
                call_session_id,
                turn.event_id,
                turn.role,
            )
            return False
        return worker.enqueue(turn)

    async def finish(self, call_session_id: uuid.UUID) -> None:
        """Bounded by `drain_timeout_seconds`; safe to call for a call that
        was never started (a no-op), and safe to call more than once (the
        second call is also a no-op, since the worker was already popped)."""
        worker = self._workers.pop(call_session_id, None)
        if worker is None:
            return
        await worker.shutdown(self._drain_timeout_seconds)

    def stats_for(self, call_session_id: uuid.UUID) -> WorkerStats | None:
        """Observability only -- never used for control flow. `None` for a
        call with no worker (never started, or already finished)."""
        worker = self._workers.get(call_session_id)
        return worker.stats if worker is not None else None
