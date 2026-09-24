#!/usr/bin/env python
"""Production entrypoint for one `voiceagent.followups.worker.FollowUpWorker`
process (Phase 2.9 brief section 11: "the smallest explicit polling
abstraction that safely executes due `FollowUpAction` rows").

Must run as its own process, separate from the call-runtime
(`scripts/run_call_runtime.py`) and from the API: `FollowUpWorker` owns its
own `DatabaseBoundary`, sized and scheduled independently, exactly so a slow
follow-up execution can never starve a live call's own database access
(`voiceagent/followups/worker.py`'s own module docstring).

**Tenant enumeration is this script's job, not the worker's** (documented,
pre-existing scope limit shared with `voiceagent.runtime.reconciliation`:
no SaaS-OS primitive enumerates tenants across the Row-Level Security
boundary). `VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS` is therefore a required,
operator-supplied, comma-separated list of tenant UUIDs -- an
operator-maintained tenant directory, exactly as `FollowUpWorker`'s own
docstring calls for. It is read fresh on every tick (not cached at process
start), so an operator can grow the list (e.g. by editing the deployment's
environment and restarting) without any code change.

Usage::

    python scripts/run_followup_worker.py

Required configuration (environment): every ordinary `voiceagent` process
variable (`DATABASE_URL`, `REDIS_URL` for import-time job registration,
`ENVIRONMENT`) plus:

* `VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS` -- comma-separated tenant UUIDs.
* `VOICEAGENT_FOLLOWUP_WORKER_SYSTEM_ACTOR_USER_ID` -- a `core.users.id`
  already provisioned with ordinary authority (see
  `scripts/bootstrap_rbac.py`); every claim/execution this worker performs
  is audited under this actor. Required -- `FollowUpWorker` takes no
  optional/`None` value here, unlike the call-runtime's own
  `system_actor_user_id` (`voiceagent.config.settings.RuntimeSettings`),
  so this script fails closed at startup rather than passing a guessed
  value.

Optional tuning (defaults match `FollowUpWorker`'s own, unchanged):
`VOICEAGENT_FOLLOWUP_WORKER_POLL_INTERVAL_SECONDS`,
`VOICEAGENT_FOLLOWUP_WORKER_MAX_CLAIMS_PER_TENANT_PER_TICK`,
`VOICEAGENT_FOLLOWUP_WORKER_MAX_CONCURRENT_TENANTS`.

Shutdown: SIGTERM or SIGINT calls `FollowUpWorker.shutdown()` (cancel the
polling task, await it) before the process exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from collections.abc import Sequence

from voiceagent.followups.worker import FollowUpWorker
from voiceagent.runtime.db import DatabaseBoundary

_logger = logging.getLogger("voiceagent.scripts.run_followup_worker")

#: Matches `voiceagent.config.settings.RuntimeSettings.to_thread_pool_size`'s
#: own default -- a conservative placeholder, not a benchmarked value, like
#: every other pool size in this product.
_DB_POOL_SIZE = 8


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required and was not set")
    return value


def _parse_tenant_ids(raw: str) -> tuple[uuid.UUID, ...]:
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    if not ids:
        raise SystemExit("VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS must name at least one tenant")
    try:
        return tuple(uuid.UUID(item) for item in ids)
    except ValueError as exc:
        raise SystemExit(f"VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS: invalid UUID -- {exc}") from exc


def _optional_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None else float(raw)


def _optional_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None else int(raw)


async def _run() -> None:
    logging.basicConfig(level=logging.INFO)

    tenant_ids = _parse_tenant_ids(_required_env("VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS"))
    system_actor_user_id = uuid.UUID(
        _required_env("VOICEAGENT_FOLLOWUP_WORKER_SYSTEM_ACTOR_USER_ID")
    )

    def _tenant_ids() -> Sequence[uuid.UUID]:
        return tenant_ids

    db = DatabaseBoundary(max_workers=_DB_POOL_SIZE)
    worker = FollowUpWorker(
        db=db,
        tenant_ids=_tenant_ids,
        system_actor_user_id=system_actor_user_id,
        poll_interval_seconds=_optional_float(
            "VOICEAGENT_FOLLOWUP_WORKER_POLL_INTERVAL_SECONDS", 30.0
        ),
        max_claims_per_tenant_per_tick=_optional_int(
            "VOICEAGENT_FOLLOWUP_WORKER_MAX_CLAIMS_PER_TENANT_PER_TICK", 10
        ),
        max_concurrent_tenants=_optional_int(
            "VOICEAGENT_FOLLOWUP_WORKER_MAX_CONCURRENT_TENANTS", 4
        ),
    )
    worker.start()
    _logger.info("followup_worker.started", extra={"tenant_count": len(tenant_ids)})

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_stop)

    await stop_event.wait()
    _logger.info("followup_worker.shutdown.begin")
    await worker.shutdown()
    db.close()
    _logger.info("followup_worker.shutdown.complete")


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
