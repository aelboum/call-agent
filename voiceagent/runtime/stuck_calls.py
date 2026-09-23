"""Stuck-call detection (Phase 2.14 brief section 13).

Detects, never takes over. Complements `voiceagent.runtime.reconciliation`,
which reacts to a runtime whose *heartbeat has expired* (a crash) -- this
module reacts to the opposite condition: a call whose owning runtime's
heartbeat is still alive, but whose `CallSession` row has not moved in
longer than expected. Two cases, both real operational anomalies that are
not crashes:

* **Stuck in startup** -- assigned to a runtime (`runtime_instance_id` set)
  but still `initiated`/`ringing` long past a realistic answer window.
* **Stuck active** -- `answered`/`in_progress` far longer than any realistic
  call, e.g. a provider stream that neither completes nor raises (§10's own
  idle timeouts should catch that first; this is the backstop for whatever
  they do not).

A call "stuck in teardown" (brief's third example) is a distinct signal this
module does not produce: `voiceagent.runtime.supervisor.CallRuntime
.cancel_call()` already detects and counts that case directly
(`voiceagent.metrics.record_teardown_timeout()`) at the one place teardown
actually happens, in-process -- a `CallSession` row alone cannot tell "still
tearing down" apart from "wedged active" (both are the same DB status until
the row is finalized), so re-deriving it here from a DB scan would be a
strictly worse copy of a signal the runtime already has firsthand.

**No automatic takeover, no cancellation issued by this module** (brief:
"detect and report ... without silently changing ownership"), mirroring
`voiceagent.runtime.reconciliation`'s own documented restraint. An operator
(or a future, explicitly-authorized process) decides whether and how to act
on a `StuckCallSession` this module reports -- `CallRuntime.cancel_call()` is
that action, if one is ever taken, and is never called from here.

`updated_at` (`voiceagent.db.TimestampMixin`, present on every `CallSession`
row) stands in for "time since this call's status last moved": every
`transition_call_session()` write touches it, so no new "last progress"
column is added for a fact an existing one already carries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from voiceagent.calls.models import CallSession
from voiceagent.calls.service import list_non_terminal_call_sessions
from voiceagent.metrics import record_stuck_call_detected
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.tenancy import TenantContext

__all__ = ["StuckCallSession", "detect_stuck_calls_for_tenant", "find_stuck_call_sessions"]

_logger = logging.getLogger(__name__)

_STARTUP_STATUSES = frozenset({"initiated", "ringing"})
_ACTIVE_STATUSES = frozenset({"answered", "in_progress"})


@dataclass(frozen=True, slots=True)
class StuckCallSession:
    """One call this scan found stuck. Identifiers and timing only -- never
    call content -- safe to log or return from an operator-only diagnostics
    endpoint (brief section 12)."""

    call_session_id: object
    status: str
    phase: str
    seconds_since_progress: float
    runtime_instance_id: str | None


def find_stuck_call_sessions(
    non_terminal: list[CallSession],
    heartbeats: dict[str, RuntimeHeartbeat],
    *,
    now: datetime,
    startup_threshold_seconds: float,
    active_threshold_seconds: float,
) -> list[StuckCallSession]:
    """Every non-terminal call whose owning runtime's heartbeat is still
    live (the complement of `voiceagent.runtime.reconciliation
    .find_stale_call_sessions()`'s own condition -- a crashed runtime's
    calls are that function's business, never this one's) and whose
    `updated_at` is older than its phase's threshold. A call with no
    `runtime_instance_id` yet is never stuck by this definition -- it has no
    runtime to have wedged, exactly `find_stale_call_sessions()`'s own
    reasoning for the identical case."""
    stuck: list[StuckCallSession] = []
    for call in non_terminal:
        if call.runtime_instance_id is None or call.runtime_instance_id not in heartbeats:
            continue
        if call.status in _STARTUP_STATUSES:
            phase = "startup"
            threshold = startup_threshold_seconds
        elif call.status in _ACTIVE_STATUSES:
            phase = "active"
            threshold = active_threshold_seconds
        else:
            continue
        age_seconds = (now - call.updated_at).total_seconds()
        if age_seconds > threshold:
            stuck.append(
                StuckCallSession(
                    call_session_id=call.id,
                    status=call.status,
                    phase=phase,
                    seconds_since_progress=age_seconds,
                    runtime_instance_id=call.runtime_instance_id,
                )
            )
    return stuck


def detect_stuck_calls_for_tenant(
    context: TenantContext,
    heartbeats: dict[str, RuntimeHeartbeat],
    *,
    now: datetime,
    startup_threshold_seconds: float,
    active_threshold_seconds: float,
) -> tuple[StuckCallSession, ...]:
    """Scan one tenant, record a bounded metric per stuck call found, and
    log one structured summary line. Mirrors `voiceagent.runtime
    .reconciliation.reconcile_tenant()`'s own shape (per-tenant scan, no
    cross-tenant enumeration -- see that function's module docstring for why
    this product's RLS boundary makes that the caller's job, not this
    one's)."""
    non_terminal = list(list_non_terminal_call_sessions(context))
    stuck = find_stuck_call_sessions(
        non_terminal,
        heartbeats,
        now=now,
        startup_threshold_seconds=startup_threshold_seconds,
        active_threshold_seconds=active_threshold_seconds,
    )
    if stuck:
        for call in stuck:
            record_stuck_call_detected(phase=call.phase)
        _logger.warning(
            "runtime.stuck_calls.detected",
            extra={
                "tenant_id": str(context.tenant_id),
                "scanned": len(non_terminal),
                "stuck": len(stuck),
            },
        )
    return tuple(stuck)
