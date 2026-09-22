"""`CallRuntime`: one `call-runtime` process, one `asyncio` event loop, many
concurrent calls (ADR-0008 point 1). Horizontal scaling is adding more
`CallRuntime` processes, never more calls per process beyond `capacity`
(ADR-0008 points 6, 15).

**Error isolation** (this phase's brief section 27): every call runs inside
its own task, wrapped by `_wrap_call_task()`, which is the one place an
exception from `voiceagent.runtime.call_task.run_call_task()` is caught.
A failure there is recorded (`error_for()`) and logged; it never propagates
to another call's task, and never crashes the event loop this `CallRuntime`
owns. Only `asyncio.CancelledError` is allowed to pass through unmodified --
catching it here would silently defeat `cancel_call()`/`shutdown()`.

**Ownership is exclusive by construction** (ADR-0008 point 10): this class
does not itself decide *which* runtime a call belongs to (that is
`voiceagent.runtime.assignment`'s job, run once by the Call Orchestrator
before `start_call()` is ever called); it only supervises calls it has
already been told to run, keyed by `call_session_id`, and `start_call()` is
idempotent for a call already running here so a redelivered assignment
cannot start a second, duplicate task for the same call.

**Bounded, per-call cancellation (Phase 2.13 hardening, brief §16):**
`cancel_call()` waits for one call's own teardown for at most
`cancel_timeout_seconds` before giving up on *waiting* -- it never forcibly
kills the call's task a second way; `run_call_task()`'s own `finally` block
is the one teardown path (`voiceagent.runtime.call_task`), and it keeps
running to completion in the background even once this method has stopped
waiting on it. This exists so that one call whose own teardown is wedged
(a provider cleanup call that itself ignores cancellation) cannot block
`shutdown()` -- or a caller cancelling one specific call -- forever.
`shutdown()` cancels every owned call **concurrently**, not one at a time:
sequential cancellation would let a single stuck call's bounded wait still
serialize behind every other call's, defeating the point of bounding each
one individually. "One broken call must not block shutdown forever" (brief
§16) is therefore a property of `shutdown()` as a whole, not just of one
call's own `cancel_call()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from voiceagent.runtime.call_task import CallTaskDependencies, CancellationSignal, run_call_task
from voiceagent.runtime.heartbeat import HeartbeatStore, RuntimeHeartbeat, wall_clock_now
from voiceagent.telephony.contracts import CallRef
from voiceagent.tenancy import TenantContext

__all__ = ["CallRuntime"]

_logger = logging.getLogger(__name__)


class CallRuntime:
    """Owns one event loop's worth of concurrent calls."""

    def __init__(
        self,
        *,
        instance_id: str,
        address: str,
        capacity: int,
        heartbeat_store: HeartbeatStore,
        cancel_timeout_seconds: float = 10.0,
    ) -> None:
        self.instance_id = instance_id
        self.address = address
        self.capacity = capacity
        self._heartbeat_store = heartbeat_store
        self._cancel_timeout_seconds = cancel_timeout_seconds
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._cancellations: dict[uuid.UUID, CancellationSignal] = {}
        self._errors: dict[uuid.UUID, BaseException] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def current_load(self) -> int:
        return len(self._tasks)

    def is_running(self, call_session_id: uuid.UUID) -> bool:
        task = self._tasks.get(call_session_id)
        return task is not None and not task.done()

    def error_for(self, call_session_id: uuid.UUID) -> BaseException | None:
        return self._errors.get(call_session_id)

    async def start_call(
        self,
        context: TenantContext,
        call_session_id: uuid.UUID,
        call_ref: CallRef,
        deps: CallTaskDependencies,
    ) -> None:
        """Start supervising one call. Idempotent: starting a call already
        running here is a no-op, never a second concurrent task for the same
        `call_session_id`."""
        if self.is_running(call_session_id):
            return
        cancellation = CancellationSignal()
        self._cancellations[call_session_id] = cancellation
        self._errors.pop(call_session_id, None)
        task = asyncio.create_task(
            self._wrap_call_task(context, call_session_id, call_ref, deps, cancellation)
        )
        self._tasks[call_session_id] = task

    async def _wrap_call_task(
        self,
        context: TenantContext,
        call_session_id: uuid.UUID,
        call_ref: CallRef,
        deps: CallTaskDependencies,
        cancellation: CancellationSignal,
    ) -> None:
        try:
            await run_call_task(
                context=context,
                call_session_id=call_session_id,
                call_ref=call_ref,
                deps=deps,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the call-task error boundary; see module docstring.
            self._errors[call_session_id] = exc
            _logger.exception("call %s failed", call_session_id)
        finally:
            self._tasks.pop(call_session_id, None)
            self._cancellations.pop(call_session_id, None)

    async def cancel_call(self, call_session_id: uuid.UUID, *, reason: str) -> None:
        """Cancel one call and wait -- bounded by `cancel_timeout_seconds`
        -- for its teardown to finish. A no-op if the call is not (or no
        longer) running here -- cancelling an already-finished or unknown
        call is not an error.

        If teardown does not finish within the bound, this method simply
        stops waiting and returns; `task.cancel()` was already delivered, so
        `run_call_task()`'s own teardown keeps running in the background
        (`_wrap_call_task()` still pops it from `self._tasks` whenever it
        eventually does finish) -- this method just refuses to let one
        wedged call hold up its own caller, in particular `shutdown()`
        cancelling every other call concurrently below."""
        task = self._tasks.get(call_session_id)
        if task is None or task.done():
            return
        cancellation = self._cancellations.get(call_session_id)
        if cancellation is not None:
            cancellation.reason = reason
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._cancel_timeout_seconds)
        except TimeoutError:
            _logger.warning(
                "call %s did not finish tearing down within %.1fs of cancellation (reason=%r); "
                "no longer waiting on it, teardown continues in the background",
                call_session_id,
                self._cancel_timeout_seconds,
                reason,
            )
        except asyncio.CancelledError:
            pass

    def start_heartbeat(self, *, interval_seconds: float, ttl_seconds: float) -> None:
        """Idempotent. Runs until `shutdown()` cancels it -- a live
        heartbeat is itself the observable proof (brief section 23) that
        this runtime is up and serving its current load, independent of
        whether any individual call succeeds or fails."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(
            self._run_heartbeat_loop(interval_seconds=interval_seconds, ttl_seconds=ttl_seconds)
        )

    async def _run_heartbeat_loop(self, *, interval_seconds: float, ttl_seconds: float) -> None:
        while True:
            await self._heartbeat_store.write(
                RuntimeHeartbeat(
                    instance_id=self.instance_id,
                    address=self.address,
                    capacity=self.capacity,
                    current_load=self.current_load,
                    last_heartbeat_epoch_seconds=wall_clock_now(),
                ),
                ttl_seconds=ttl_seconds,
            )
            await asyncio.sleep(interval_seconds)

    async def shutdown(self) -> None:
        """Cancel every owned call cleanly, then deregister this runtime's
        heartbeat. A graceful shutdown is not the failure mode
        `voiceagent.runtime.reconciliation` reacts to -- deregistering here
        is what keeps a clean stop from ever being mistaken for a crash.

        **Concurrent, not sequential** (Phase 2.13 hardening, brief §16):
        every owned call is cancelled at once, each bounded independently by
        `cancel_call()`'s own `cancel_timeout_seconds` -- cancelling one at a
        time would let a single wedged call's bounded wait still serialize
        in front of every call behind it, making the effective shutdown
        bound `len(self._tasks) * cancel_timeout_seconds` instead of just
        `cancel_timeout_seconds`. `return_exceptions=True` is required only
        as a defensive backstop: `cancel_call()` itself already contains
        every exception it can raise, but shutdown must never fail to
        deregister this runtime's heartbeat because of a bug in one call's
        own cancellation path."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        results = await asyncio.gather(
            *(
                self.cancel_call(call_session_id, reason="runtime_shutdown")
                for call_session_id in list(self._tasks.keys())
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                _logger.exception(
                    "cancel_call() raised during shutdown() -- continuing to deregister "
                    "this runtime's heartbeat regardless",
                    exc_info=result,
                )

        await self._heartbeat_store.remove(self.instance_id)
