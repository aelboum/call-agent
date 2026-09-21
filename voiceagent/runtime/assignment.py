"""Runtime assignment: least-loaded selection and the atomic ownership claim
(ADR-0008 points 5, 6, 10, 13, 14; Phase 2.0 report §5.3, §8).

This is Call Orchestrator code -- synchronous, ordinary `control-api`-style
database access through `voiceagent.calls.service` (Phase 2.0 report §6.1:
"synchronous, direct... this is the process where synchronous DB access is
native and unproblematic"), *not* call-runtime-process code, and therefore
does not go through `voiceagent.runtime.db.DatabaseBoundary` -- that boundary
exists for the call-hosting event loop's own audio-adjacent database touches
(`voiceagent.runtime.call_task`), which this module is not.
"""

from __future__ import annotations

import uuid

from voiceagent.calls.models import CallSession
from voiceagent.calls.service import claim_runtime_ownership
from voiceagent.runtime.errors import NoRuntimeCapacityError
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.tenancy import TenantContext

__all__ = ["assign_call_to_runtime", "select_runtime_for_assignment"]


def select_runtime_for_assignment(
    heartbeats: dict[str, RuntimeHeartbeat],
) -> RuntimeHeartbeat | None:
    """The least-loaded live runtime with capacity below its own configured
    ceiling (ADR-0008 point 5), or `None` if every known runtime is at or
    above its ceiling (point 14). No session affinity is needed: a call
    lives entirely on one runtime for its duration once assigned (point 1),
    so there is nothing to be "sticky" about here."""
    candidates = [heartbeat for heartbeat in heartbeats.values() if heartbeat.has_capacity]
    if not candidates:
        return None
    return min(candidates, key=lambda heartbeat: heartbeat.current_load)


def assign_call_to_runtime(
    context: TenantContext,
    call_session_id: uuid.UUID,
    heartbeats: dict[str, RuntimeHeartbeat],
) -> tuple[CallSession, str]:
    """Select the least-loaded runtime with capacity and atomically claim
    `call_session_id` for it. Returns `(call_session, runtime_instance_id)`.

    Raises `NoRuntimeCapacityError` if no runtime has capacity (ADR-0008
    point 14 -- a defined outcome, never a silent wait). Raises
    `CallSessionAlreadyOwnedError` if the call was already claimed by a
    *different* runtime between the caller reading `heartbeats` and this call
    (an inherent, narrow race in any least-loaded selection scheme; the
    caller's own retry policy, not this function, decides whether to re-select
    and try again -- this module does not retry silently, per ADR-0008's "no
    distributed locking is introduced anywhere in this model", Phase 2.0
    report §16).
    """
    selected = select_runtime_for_assignment(heartbeats)
    if selected is None:
        raise NoRuntimeCapacityError(f"no runtime has capacity for CallSession {call_session_id}")

    call = claim_runtime_ownership(
        context, call_session_id, runtime_instance_id=selected.instance_id
    )
    return call, selected.instance_id
