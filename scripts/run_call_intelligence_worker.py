#!/usr/bin/env python
"""Production entrypoint for one
`voiceagent.call_intelligence.worker.CallAiAnalysisWorker` process (Phase
2.12): the post-call AI analysis poller, deliberately its own process --
sharing neither the call-runtime's own `DatabaseBoundary` nor its
`system_actor_user_id` (`voiceagent/call_intelligence/worker.py`'s own
module docstring).

Provider/model/timeout/poll-interval/claim-batching are all already
configurable via `voiceagent.config.settings.CallIntelligenceSettings`
(read from the ordinary `VOICEAGENT_CALL_INTELLIGENCE_*` environment
variables `settings_from_env()` already parses) -- this script adds no new
configuration surface for any of those. `create_call_intelligence_provider()`
resolves the named provider (`"fake"` by default; `"groq"` reads its own API
key through `infra.secrets`, never from this script or from `Settings`).

**Tenant enumeration is this script's job**, the identical, documented scope
limit `scripts/run_followup_worker.py` and `voiceagent.runtime.reconciliation`
both already carry: no SaaS-OS primitive enumerates tenants across the
Row-Level Security boundary, so `VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS`
is a required, operator-supplied, comma-separated tenant UUID list, read
fresh on every tick.

Usage::

    python scripts/run_call_intelligence_worker.py

Required configuration (environment): every ordinary `voiceagent` process
variable, plus `VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS`
(comma-separated tenant UUIDs). `VOICEAGENT_CALL_INTELLIGENCE_SYSTEM_ACTOR_USER_ID`
should also be set (`voiceagent.config.settings.CallIntelligenceSettings
.system_actor_user_id`) -- it is `None` by default, and this worker's own
`_authorize()` step fails closed without one (the same discipline
`RuntimeSettings.system_actor_user_id` already establishes); this script
does not weaken that by inventing a value.

Shutdown: SIGTERM or SIGINT calls `CallAiAnalysisWorker.shutdown()` (cancel
the polling task, await the in-flight tick to unwind), then closes this
script's own `DatabaseBoundary` (the worker does not own it).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from collections.abc import Sequence

from voiceagent.call_intelligence.worker import CallAiAnalysisWorker
from voiceagent.config import settings_from_env
from voiceagent.providers.call_intelligence.registry import create_call_intelligence_provider
from voiceagent.runtime.db import DatabaseBoundary

_logger = logging.getLogger("voiceagent.scripts.run_call_intelligence_worker")

#: Matches `voiceagent.config.settings.RuntimeSettings.to_thread_pool_size`'s
#: own default, for the identical reason `run_followup_worker.py` uses it:
#: a conservative, unbenchmarked placeholder, not shared with any other
#: process's own pool.
_DB_POOL_SIZE = 8


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required and was not set")
    return value


def _parse_tenant_ids(raw: str) -> tuple[uuid.UUID, ...]:
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    if not ids:
        raise SystemExit(
            "VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS must name at least one tenant"
        )
    try:
        return tuple(uuid.UUID(item) for item in ids)
    except ValueError as exc:
        raise SystemExit(
            f"VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS: invalid UUID -- {exc}"
        ) from exc


async def _run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = settings_from_env()

    tenant_ids = _parse_tenant_ids(_required_env("VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS"))

    def _tenant_ids() -> Sequence[uuid.UUID]:
        return tenant_ids

    provider = create_call_intelligence_provider(
        settings.call_intelligence.provider, settings.call_intelligence.model, {}
    )

    db = DatabaseBoundary(max_workers=_DB_POOL_SIZE)
    worker = CallAiAnalysisWorker(
        db=db,
        tenant_ids=_tenant_ids,
        provider=provider,
        provider_name=settings.call_intelligence.provider,
        ai_provider_settings=settings.ai_providers,
        system_actor_user_id=settings.call_intelligence.system_actor_user_id,
        timeout_seconds=settings.call_intelligence.timeout_seconds,
        poll_interval_seconds=settings.call_intelligence.poll_interval_seconds,
        max_claims_per_tenant_per_tick=settings.call_intelligence.max_claims_per_tenant_per_tick,
        max_concurrent_tenants=settings.call_intelligence.max_concurrent_tenants,
    )
    worker.start()
    _logger.info(
        "call_intelligence_worker.started",
        extra={"tenant_count": len(tenant_ids), "provider": settings.call_intelligence.provider},
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_stop)

    await stop_event.wait()
    _logger.info("call_intelligence_worker.shutdown.begin")
    await worker.shutdown()
    db.close()
    _logger.info("call_intelligence_worker.shutdown.complete")


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
