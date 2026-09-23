"""`/v1/ops` -- operator-only operational diagnostics (Phase 2.14 brief
section 12).

**A documented scope limitation, stated plainly.** `core.rbac.PrincipalType`
has no dedicated cross-tenant "platform operator" principal today (`USER`,
`SYSTEM`, `SERVICE_ACCOUNT` only) -- so this router is authorized the only
way any route in this product can be, `voiceagent.tenancy.require_tenant()`,
which is inescapably tenant-scoped. The data these two routes return is
*not* tenant-scoped, though (a Redis runtime heartbeat and a stuck-call scan
both span every tenant a runtime happens to be serving) -- `voiceagent.ops
.permissions.RESOURCE` is therefore deliberately never auto-granted (see
that module's own docstring); an operator must explicitly grant
`(voiceagent.ops, read)` only to a role reserved for genuine operator
memberships, never to an ordinary tenant-admin role, or a tenant admin would
see load/heartbeat information about runtimes serving other tenants too.
Phase 2.14's brief section 18 anticipates exactly this: "For Phase 2.14,
operator/internal diagnostics are preferred" over a real tenant-scoped
mechanism, which does not exist yet.

**Cross-process-safe by construction.** Neither route introspects a live
`CallRuntime` object -- that runs in a separate process (ADR-0008 point 1)
this API process has no channel to (see `voiceagent.runtime.diagnostics`'s
own module docstring). Both read data that is *already*, by design,
readable from any process: the Redis heartbeat set every `CallRuntime`
publishes for the Call Orchestrator's own benefit
(`voiceagent.runtime.heartbeat`), and `call_sessions` rows through the same
tenant-scoped database session every other route in this API already uses.

**Bounded** (brief section 10/16): the Redis read is wrapped in an explicit
timeout, exactly like every dependency check in `infra.health.readiness`
-- an unreachable Redis must fail this request quickly, not hang it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from infra.jobs.config import get_jobs_config
from pydantic import BaseModel

from voiceagent.config import get_settings
from voiceagent.ops.permissions import RESOURCE
from voiceagent.runtime.heartbeat import RedisHeartbeatStore
from voiceagent.runtime.stuck_calls import detect_stuck_calls_for_tenant
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/ops", tags=["ops"])

_read = require_tenant(RESOURCE, "read")

#: How long the Redis heartbeat read may take before this route reports it
#: unreachable rather than hanging the request (mirrors
#: `infra.health.readiness`'s own dependency-check discipline).
_HEARTBEAT_READ_TIMEOUT_SECONDS = 2.0


class RuntimeHeartbeatOut(BaseModel):
    instance_id: str
    capacity: int
    current_load: int
    has_capacity: bool
    seconds_since_heartbeat: float


class RuntimeHeartbeatsResponse(BaseModel):
    runtimes: list[RuntimeHeartbeatOut]
    redis_reachable: bool


@router.get("/runtime-heartbeats")
async def runtime_heartbeats_route(
    _context: TenantContext = Depends(_read),  # noqa: B008
) -> RuntimeHeartbeatsResponse:
    """Every currently-live `call-runtime` process (brief section 12:
    "runtime instance identity", "last successful heartbeat"). An empty list
    with `redis_reachable=true` means no runtime is currently registered --
    a real, reportable state, not an error. `redis_reachable=false` means
    the read itself failed or timed out; never a stack trace or a raw
    exception message in the response (brief section 19)."""
    store = RedisHeartbeatStore(get_jobs_config().redis_url)
    try:
        heartbeats = await asyncio.wait_for(
            store.read_all(), timeout=_HEARTBEAT_READ_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 -- an unreachable dependency is a
        # reportable state, not a 500; see this route's own docstring.
        return RuntimeHeartbeatsResponse(runtimes=[], redis_reachable=False)

    now = datetime.now(UTC).timestamp()
    return RuntimeHeartbeatsResponse(
        runtimes=[
            RuntimeHeartbeatOut(
                instance_id=heartbeat.instance_id,
                capacity=heartbeat.capacity,
                current_load=heartbeat.current_load,
                has_capacity=heartbeat.has_capacity,
                seconds_since_heartbeat=max(now - heartbeat.last_heartbeat_epoch_seconds, 0.0),
            )
            for heartbeat in heartbeats.values()
        ],
        redis_reachable=True,
    )


class StuckCallOut(BaseModel):
    call_session_id: str
    status: str
    phase: str
    seconds_since_progress: float


class StuckCallsResponse(BaseModel):
    stuck_calls: list[StuckCallOut]


@router.get("/stuck-calls")
async def stuck_calls_route(
    context: TenantContext = Depends(_read),  # noqa: B008
) -> StuckCallsResponse:
    """This tenant's own non-terminal calls whose owning runtime is still
    alive but whose status has not moved past its phase's threshold (brief
    section 13). Detection only -- this route never cancels a call; see
    `voiceagent.runtime.stuck_calls`'s own module docstring."""
    settings = get_settings()
    store = RedisHeartbeatStore(get_jobs_config().redis_url)
    try:
        heartbeats = await asyncio.wait_for(
            store.read_all(), timeout=_HEARTBEAT_READ_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 -- see runtime_heartbeats_route().
        # No live heartbeat data -- every runtime looks "gone" rather than
        # falsely "alive", so this scan conservatively reports nothing
        # stuck (a call whose runtime cannot be confirmed alive is
        # reconciliation's concern, not this route's, once its own
        # heartbeat genuinely expires).
        heartbeats = {}

    stuck = await run_in_threadpool(
        detect_stuck_calls_for_tenant,
        context,
        heartbeats,
        now=datetime.now(UTC),
        startup_threshold_seconds=settings.runtime.stuck_call_startup_threshold_seconds,
        active_threshold_seconds=settings.runtime.stuck_call_active_threshold_seconds,
    )
    return StuckCallsResponse(
        stuck_calls=[
            StuckCallOut(
                call_session_id=str(call.call_session_id),
                status=call.status,
                phase=call.phase,
                seconds_since_progress=call.seconds_since_progress,
            )
            for call in stuck
        ]
    )
