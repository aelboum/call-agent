"""`FollowUpWorker`: the smallest explicit polling abstraction that safely
executes due `FollowUpAction` rows (Phase 2.9 brief §11).

**Why a bespoke poller, not `infra.jobs` (ARQ/Redis) or a cron schedule.**
`infra.jobs` is a generic enqueue/execute/retry/dead-letter job runner
(`infra/jobs/__init__.py`'s own docstring) -- a real fit for *dispatching
one job*, but the actual hard problem this phase solves (brief §6-§9: an
atomic claim, a database-held lease, deterministic backoff tracked in
`FollowUpAction` itself) has to live in PostgreSQL regardless of what
triggers a poll, because `voiceagent.tenancy` gives no cross-tenant,
RLS-bypassing read a Redis-queued job's handler could use to *find* what is
due (see `voiceagent.runtime.reconciliation`'s own module docstring, which
documents the identical structural limit: "no SaaS-OS primitive enumerates
tenants across the Row-Level Security boundary"). Reaching for ARQ here
would add a second execution surface (Redis-scheduled ticks) around
`voiceagent.followups.service.execute_due_follow_up()`, which does all the
real work already -- one bounded `asyncio` polling loop over an
operator-supplied tenant list, calling that one function, is the entire
mechanism this domain needs, and (brief §18) explicitly not a generic
job/event framework, workflow engine, or scheduler.

**Must not share the call audio execution path** (brief §11): this module
imports nothing from `voiceagent.runtime` and is never constructed by
`voiceagent.runtime.supervisor.CallRuntime`. It uses its own
`voiceagent.runtime.db.DatabaseBoundary` instance (the class is a generic,
reusable bounded-thread-pool wrapper -- nothing about it is call-runtime
-specific), sized and owned independently of the audio runtime's own pool,
so a slow follow-up execution can never starve a live call's database
access, and vice versa.

**Tenant enumeration is the caller's job**, exactly like
`voiceagent.runtime.reconciliation.reconcile_tenant()`'s own documented
scope limitation: `tenant_ids` is a zero-argument callable the operator
supplies (an operator-maintained tenant directory, a SaaS-OS primitive if
one is ever added) -- this module does not invent one.

**System attribution.** Every claim/execution audit entry
(`voiceagent.followups.service`) is written under a `TenantContext` this
worker builds itself, attributing to `system_actor_user_id` -- an
operator-provisioned `core.users.id`, the same "system actor" pattern
`voiceagent.config.settings.RuntimeSettings.system_actor_user_id` already
establishes for `voiceagent.runtime.privacy`, though this worker keeps its
own separate value rather than sharing that dataclass (a configuration
change to the call-runtime process must never silently reconfigure this
one). `membership_id` carries no foreign key and is read by nothing in this
domain (`voiceagent.followups.models` has no `membership_id` column
anywhere in its schema) -- a fresh `uuid.uuid4()` per tick is synthesized
for it, exactly as this product's own tests already do wherever a
`TenantContext` is built directly rather than from a `RequestContext`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from voiceagent.followups.service import execute_due_follow_up
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.tenancy import TenantContext

__all__ = ["FollowUpWorker", "WorkerTickReport"]

_logger = logging.getLogger(__name__)

#: Default per-tenant, per-tick claim ceiling -- bounded so one tenant with
#: many due follow-ups cannot starve every other tenant's own tick (brief
#: §11: "no unbounded task creation").
_DEFAULT_MAX_CLAIMS_PER_TENANT_PER_TICK = 10
_DEFAULT_MAX_CONCURRENT_TENANTS = 4
_DEFAULT_POLL_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class WorkerTickReport:
    """One `poll_once()` call's outcome, across every tenant -- returned,
    never only logged, so tests exercise it directly without scraping log
    output."""

    tenants_scanned: int
    executed: int = 0
    errored: int = 0
    per_tenant_errors: tuple[tuple[uuid.UUID, str], ...] = field(default_factory=tuple)


class FollowUpWorker:
    """Owns one bounded `asyncio` polling loop. `start()`/`shutdown()`
    mirror `voiceagent.runtime.supervisor.CallRuntime.start_heartbeat()`/
    `shutdown()` exactly: idempotent start, cancel-and-await shutdown,
    `asyncio.CancelledError` never suppressed past this class's own boundary
    check."""

    def __init__(
        self,
        *,
        db: DatabaseBoundary,
        tenant_ids: Callable[[], Sequence[uuid.UUID]],
        system_actor_user_id: uuid.UUID,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        max_claims_per_tenant_per_tick: int = _DEFAULT_MAX_CLAIMS_PER_TENANT_PER_TICK,
        max_concurrent_tenants: int = _DEFAULT_MAX_CONCURRENT_TENANTS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if max_claims_per_tenant_per_tick <= 0:
            raise ValueError("max_claims_per_tenant_per_tick must be positive")
        if max_concurrent_tenants <= 0:
            raise ValueError("max_concurrent_tenants must be positive")
        self._db = db
        self._tenant_ids = tenant_ids
        self._system_actor_user_id = system_actor_user_id
        self._poll_interval_seconds = poll_interval_seconds
        self._max_claims_per_tenant_per_tick = max_claims_per_tenant_per_tick
        self._max_concurrent_tenants = max_concurrent_tenants
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Idempotent -- a second call while already running is a no-op,
        matching `CallRuntime.start_heartbeat()`."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval_seconds)

    def _build_context(self, tenant_id: uuid.UUID) -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            actor_id=self._system_actor_user_id,
            membership_id=uuid.uuid4(),
        )

    async def _drain_one_tenant(self, tenant_id: uuid.UUID) -> tuple[int, str | None]:
        """Claim and execute due follow-ups for one tenant, up to
        `max_claims_per_tenant_per_tick`, stopping early once nothing more
        is due. Never raises: an unexpected error is caught, logged, and
        reported back in the returned error message -- one tenant's failure
        must never abort another tenant's own tick (the same isolation
        `voiceagent.runtime.supervisor._wrap_call_task()` gives each call)."""
        context = self._build_context(tenant_id)
        executed = 0
        try:
            for _ in range(self._max_claims_per_tenant_per_tick):
                outcome = await self._db.run(execute_due_follow_up, context)
                if outcome is None:
                    break
                executed += 1
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see docstring
            _logger.exception(
                "follow_up_worker_tenant_tick_failed",
                extra={"tenant_id": str(tenant_id), "executed_before_error": executed},
            )
            return executed, f"{type(exc).__name__}"
        return executed, None

    async def poll_once(self) -> WorkerTickReport:
        """One tick: drain every tenant `tenant_ids()` currently names, up
        to `max_concurrent_tenants` at once (brief §11: "bounded
        concurrency"). Safe to call directly in a test without `start()`."""
        tenant_ids = list(self._tenant_ids())
        semaphore = asyncio.Semaphore(self._max_concurrent_tenants)

        async def _bounded(tenant_id: uuid.UUID) -> tuple[uuid.UUID, int, str | None]:
            async with semaphore:
                executed, error = await self._drain_one_tenant(tenant_id)
                return tenant_id, executed, error

        results = await asyncio.gather(*(_bounded(tenant_id) for tenant_id in tenant_ids))

        total_executed = sum(executed for _, executed, _ in results)
        errors = tuple((tenant_id, error) for tenant_id, _, error in results if error is not None)
        return WorkerTickReport(
            tenants_scanned=len(tenant_ids),
            executed=total_executed,
            errored=len(errors),
            per_tenant_errors=errors,
        )

    async def shutdown(self) -> None:
        """Cancel the polling loop and wait for the current tick to unwind.
        Does not close `db` -- this worker does not own it (it is
        constructed by, and shared at the caller's discretion with, whatever
        process wires this worker up), mirroring
        `voiceagent.runtime.db.DatabaseBoundary`'s own "nothing about this
        class is specific to any one such function" design."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
