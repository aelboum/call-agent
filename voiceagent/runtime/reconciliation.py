"""Reconciliation: detect stale runtime ownership and repair the bookkeeping
(ADR-0008 point 11; Phase 2.0 report §5.2, §17 "runtime process crash").

**Deliberately does not implement session takeover.** A `CallSession` found
pointing at a heartbeat-expired runtime is marked `interrupted` -- the call
itself is already gone (its media socket died with the process that crashed;
FreeSWITCH's own drop-detection ends it independently, per ADR-0008 point 9)
-- never reassigned to a different runtime. ADR-0008 records session
replication/hot-takeover as an explicit, deferred Phase 3+ candidate (Phase
2.0 report §21 OQ-4), not a Phase 2.2 gap.

**A genuine, documented scope limitation**: this module's `reconcile_tenant()`
scans *one* tenant's `call_sessions`, because no SaaS-OS primitive this
product is allowed to depend on enumerates tenants across the Row-Level
Security boundary (`core.tenancy` exports no `list_all_tenants()`-shaped
function, verified against the pinned SHA) -- RLS is deliberately
tenant-scoped by construction, and a background loop that needs to visit
every tenant is therefore, structurally, a loop over a tenant list from
*somewhere else* (an operator-maintained tenant directory, or a future
SaaS-OS primitive), calling `reconcile_tenant()` once per tenant. Phase 2.0
report §5.2's own sequence diagram already scopes the reconciler's query as
`SELECT call_sessions WHERE runtime_instance_id = <expired>` without
specifying cross-tenant enumeration, so this is a documented implementation
detail, not a deviation from that diagram -- see
`docs/PHASE-2.2-STATUS.md`'s deferred-work section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from voiceagent.calls.errors import InvalidCallSessionTransitionError
from voiceagent.calls.models import CallSession
from voiceagent.calls.service import list_non_terminal_call_sessions, transition_call_session
from voiceagent.metrics import record_reconciliation
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.tenancy import TenantContext

__all__ = ["ReconciliationReport", "find_stale_call_sessions", "reconcile_tenant"]

_logger = logging.getLogger(__name__)


def find_stale_call_sessions(
    non_terminal: list[CallSession], heartbeats: dict[str, RuntimeHeartbeat]
) -> list[CallSession]:
    """Every non-terminal call whose `runtime_instance_id` is set but is not
    a key in `heartbeats` -- i.e. that runtime's heartbeat has expired, or it
    never registered one at all (ADR-0008 point 9). A call with no
    `runtime_instance_id` yet (still being assigned) is never stale -- it has
    no runtime to have crashed."""
    return [
        call
        for call in non_terminal
        if call.runtime_instance_id is not None and call.runtime_instance_id not in heartbeats
    ]


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scanned: int
    stale: tuple[CallSession, ...] = field(default_factory=tuple)
    repaired: tuple[CallSession, ...] = field(default_factory=tuple)


def reconcile_tenant(
    context: TenantContext, heartbeats: dict[str, RuntimeHeartbeat]
) -> ReconciliationReport:
    """Scan `context`'s tenant for stale runtime ownership and mark each
    affected call `interrupted` with `end_reason="runtime_crashed"` (Phase
    2.0 report §17's own row for this exact failure). Idempotent: a call
    already reconciled is already terminal and therefore excluded from the
    next run's `non_terminal` scan; running this twice in a row against the
    same state repairs nothing the second time, rather than erroring."""
    non_terminal = list(list_non_terminal_call_sessions(context))
    stale = find_stale_call_sessions(non_terminal, heartbeats)

    repaired: list[CallSession] = []
    for call in stale:
        try:
            repaired.append(
                transition_call_session(
                    context,
                    call.id,
                    to_status="interrupted",
                    end_reason="runtime_crashed",
                )
            )
        except InvalidCallSessionTransitionError:
            # Already resolved by something else between the scan and this
            # write (e.g. a legitimate, later hangup event) -- the delayed-
            # event handling Phase 2.0 report §17 already specifies: applied
            # if it does not contradict an already-finalized state, ignored
            # otherwise. Not a reconciliation failure.
            continue

    if stale:
        for _ in stale:
            record_reconciliation(result="stale")
        for _ in repaired:
            record_reconciliation(result="repaired")
        _logger.info(
            "runtime.reconciliation.tenant_scan",
            extra={
                "tenant_id": str(context.tenant_id),
                "scanned": len(non_terminal),
                "stale": len(stale),
                "repaired": len(repaired),
            },
        )

    return ReconciliationReport(
        scanned=len(non_terminal), stale=tuple(stale), repaired=tuple(repaired)
    )
