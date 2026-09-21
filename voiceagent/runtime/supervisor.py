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
    ) -> None:
        self.instance_id = instance_id
        self.address = address
        self.capacity = capacity
        self._heartbeat_store = heartbeat_store
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
        """Cancel one call and wait for its teardown to finish. A no-op if
        the call is not (or no longer) running here -- cancelling an
        already-finished or unknown call is not an error."""
        task = self._tasks.get(call_session_id)
        if task is None or task.done():
            return
        cancellation = self._cancellations.get(call_session_id)
        if cancellation is not None:
            cancellation.reason = reason
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

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
        is what keeps a clean stop from ever being mistaken for a crash."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        for call_session_id in list(self._tasks.keys()):
            await self.cancel_call(call_session_id, reason="runtime_shutdown")

        await self._heartbeat_store.remove(self.instance_id)
