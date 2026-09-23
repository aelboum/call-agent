"""`CallAiAnalysisWorker` -- the smallest explicit polling abstraction that
safely executes due `CallAiAnalysis` rows (Phase 2.12).

Deliberately mirrors `voiceagent.followups.worker.FollowUpWorker` almost
exactly (bounded per-tenant claim ceiling, bounded concurrent-tenant
semaphore, per-tenant error isolation, idempotent `start()`, cancel-and-
await `shutdown()`) -- the identical worker shape already proven for
Phase 2.9's own bounded background execution, reused rather than a second
polling framework invented for this domain (brief WORKER: "Do not create a
general-purpose task framework"). The one structural difference:
`voiceagent.call_intelligence.analyzer.run_call_ai_analysis()` is itself a
coroutine (it awaits an external AI provider), so this worker calls it
directly on the event loop rather than wrapping the whole call in
`DatabaseBoundary.run()` the way `FollowUpWorker` wraps its fully-
synchronous `execute_due_follow_up()` -- `run_call_ai_analysis()` still
crosses `DatabaseBoundary.run()` internally for every one of its own
database-touching steps.

**Must not share the call audio execution path** -- identical to
`FollowUpWorker`'s own documented rule: this module imports nothing from
`voiceagent.providers.engines`/`voiceagent.telephony`/`voiceagent.tools`,
and is never constructed by `voiceagent.runtime.supervisor.CallRuntime`. It
owns its own `DatabaseBoundary`, sized and scheduled independently of the
audio runtime's own pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from voiceagent.call_intelligence.analyzer import run_call_ai_analysis
from voiceagent.config.settings import AiProviderSettings
from voiceagent.metrics import record_worker_tick
from voiceagent.providers.call_intelligence.contracts import CallIntelligenceProvider
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.tenancy import TenantContext

__all__ = ["CallAiAnalysisWorker", "CallAiAnalysisWorkerTickReport"]

_logger = logging.getLogger(__name__)

_DEFAULT_MAX_CLAIMS_PER_TENANT_PER_TICK = 5
_DEFAULT_MAX_CONCURRENT_TENANTS = 4
_DEFAULT_POLL_INTERVAL_SECONDS = 30.0
_DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CallAiAnalysisWorkerTickReport:
    """One `poll_once()` call's outcome, across every tenant -- returned,
    never only logged, matching `voiceagent.followups.worker
    .WorkerTickReport`'s own reasoning."""

    tenants_scanned: int
    executed: int = 0
    errored: int = 0
    per_tenant_errors: tuple[tuple[uuid.UUID, str], ...] = field(default_factory=tuple)


class CallAiAnalysisWorker:
    """Owns one bounded `asyncio` polling loop."""

    def __init__(
        self,
        *,
        db: DatabaseBoundary,
        tenant_ids: Callable[[], Sequence[uuid.UUID]],
        provider: CallIntelligenceProvider,
        provider_name: str,
        ai_provider_settings: AiProviderSettings,
        system_actor_user_id: uuid.UUID | None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
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
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._db = db
        self._tenant_ids = tenant_ids
        self._provider = provider
        self._provider_name = provider_name
        self._ai_provider_settings = ai_provider_settings
        self._system_actor_user_id = system_actor_user_id
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._max_claims_per_tenant_per_tick = max_claims_per_tenant_per_tick
        self._max_concurrent_tenants = max_concurrent_tenants
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Idempotent -- a second call while already running is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval_seconds)

    def _build_context(self, tenant_id: uuid.UUID) -> TenantContext:
        """`actor_id`/`membership_id` carry no foreign key anywhere this
        worker's own codepath reaches -- `claim_pending_analysis()`/
        `complete_analysis()`/`fail_analysis()` all audit as
        `ActorType.SYSTEM` (no `actor_user_id`), matching
        `voiceagent.followups.worker.FollowUpWorker`'s own reasoning for why
        a synthesized value is safe here. The *privacy* check's own acting
        principal is `self._system_actor_user_id`, passed to
        `run_call_ai_analysis()` explicitly, never derived from this
        context."""
        return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), membership_id=uuid.uuid4())

    async def _drain_one_tenant(self, tenant_id: uuid.UUID) -> tuple[int, str | None]:
        """Claim and run due analyses for one tenant, up to
        `max_claims_per_tenant_per_tick`, stopping early once nothing more
        is due. Never raises: one tenant's failure must never abort
        another tenant's own tick (identical isolation
        `voiceagent.followups.worker.FollowUpWorker._drain_one_tenant()`
        already establishes)."""
        context = self._build_context(tenant_id)
        executed = 0
        try:
            for _ in range(self._max_claims_per_tenant_per_tick):
                outcome = await run_call_ai_analysis(
                    db=self._db,
                    context=context,
                    provider=self._provider,
                    provider_name=self._provider_name,
                    ai_provider_settings=self._ai_provider_settings,
                    system_actor_user_id=self._system_actor_user_id,
                    timeout_seconds=self._timeout_seconds,
                )
                if outcome is None:
                    break
                executed += 1
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see docstring
            _logger.exception(
                "call_ai_analysis_worker_tenant_tick_failed",
                extra={"tenant_id": str(tenant_id), "executed_before_error": executed},
            )
            return executed, f"{type(exc).__name__}"
        return executed, None

    async def poll_once(self) -> CallAiAnalysisWorkerTickReport:
        """One tick: drain every tenant `tenant_ids()` currently names, up
        to `max_concurrent_tenants` at once. Safe to call directly in a
        test without `start()`."""
        tick_started = time.monotonic()
        tenant_ids = list(self._tenant_ids())
        semaphore = asyncio.Semaphore(self._max_concurrent_tenants)

        async def _bounded(tenant_id: uuid.UUID) -> tuple[uuid.UUID, int, str | None]:
            async with semaphore:
                executed, error = await self._drain_one_tenant(tenant_id)
                return tenant_id, executed, error

        results = await asyncio.gather(*(_bounded(tenant_id) for tenant_id in tenant_ids))

        total_executed = sum(executed for _, executed, _ in results)
        errors = tuple((tenant_id, error) for tenant_id, _, error in results if error is not None)
        report = CallAiAnalysisWorkerTickReport(
            tenants_scanned=len(tenant_ids),
            executed=total_executed,
            errored=len(errors),
            per_tenant_errors=errors,
        )
        record_worker_tick(
            "call_intelligence",
            claimed=report.executed,
            failed=0,
            tenants_errored=report.errored,
            duration_seconds=time.monotonic() - tick_started,
        )
        _logger.info(
            "call_ai_analysis_worker.tick",
            extra={
                "tenants_scanned": report.tenants_scanned,
                "executed": report.executed,
                "errored": report.errored,
            },
        )
        return report

    async def shutdown(self) -> None:
        """Cancel the polling loop and wait for the current tick to unwind.
        Does not close `db` -- this worker does not own it, matching
        `voiceagent.followups.worker.FollowUpWorker.shutdown()`."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
