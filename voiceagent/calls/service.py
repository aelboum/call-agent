"""Application service for `CallSession`.

There is deliberately no public API route that creates or transitions a
`CallSession` (Phase 2.0 report §23.9 lists only the two read endpoints) --
these functions exist for the future Call Orchestrator (Phase 2.2+) to call,
and for this phase's own tests to exercise the lifecycle and ownership model
directly. `create_call_session()` does not itself resolve tenant/agent/
phone-number ownership consistency by application logic; the composite
tenant-aware foreign keys (`voiceagent.calls.models.CallSession.__table_args__`)
reject an inconsistent combination at the database layer, which is the exact
property Phase 2.1's composite-FK isolation tests exercise.

Timestamp semantics adopted here (Phase 2.0 report §23.5 names the columns
without fixing their exact set-points; documented as an implementation
clarification, not a Phase 2.0 contradiction -- see
`docs/PHASE-2.1-STATUS.md`):

* `started_at` -- set at creation (`create_call_session()`).
* `answered_at` -- set on the first transition into `answered` or
  `in_progress`.
* `ended_at` -- set on transition into any terminal status
  (`completed`/`failed`/`interrupted`).
* `duration_ms` -- `(ended_at - started_at)` in whole milliseconds, computed
  once, at the terminal transition.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from voiceagent.calls.errors import (
    CallSessionAlreadyOwnedError,
    CallSessionNotFoundError,
    InvalidCallSessionTransitionError,
)
from voiceagent.calls.lifecycle import TERMINAL_STATUSES, VALID_STATUSES, is_valid_transition
from voiceagent.calls.models import CallSession
from voiceagent.db import select
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "claim_runtime_ownership",
    "create_call_session",
    "get_call_session",
    "list_call_sessions",
    "list_non_terminal_call_sessions",
    "transition_call_session",
]


def _get_row(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    row = session.get(CallSession, call_session_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def _get_row_for_update(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    """Like `_get_row`, but takes a `SELECT ... FOR UPDATE` row lock for the
    life of the transaction -- the one place this module needs one, because
    `claim_runtime_ownership()` must read-then-conditionally-write
    `runtime_instance_id` atomically against a concurrent claim attempt for
    the same call (ADR-0008 point 10: ownership is exclusive by
    construction, not by a runtime-side lock; this is the database-level
    mechanism that makes that true rather than merely likely)."""
    row = session.get(CallSession, call_session_id, with_for_update=True)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def create_call_session(
    context: TenantContext,
    *,
    direction: str,
    from_e164: str,
    to_e164: str,
    phone_number_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
) -> CallSession:
    """`agent_version_id` is resolved by the caller (e.g. via
    `voiceagent.agents.service.select_agent_version_id()`) *before* this
    call, and is written once, here -- this function provides no way to
    change it afterward (ADR-0004; Phase 2.1 brief §15)."""
    with tenant_scope(context) as session:
        call = CallSession(
            tenant_id=context.tenant_id,
            direction=direction,
            status="initiated",
            from_e164=from_e164,
            to_e164=to_e164,
            phone_number_id=phone_number_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            started_at=datetime.now(UTC),
        )
        session.add(call)
        session.flush()
        session.refresh(call)
        session.expunge(call)
        return call


def get_call_session(context: TenantContext, call_session_id: uuid.UUID) -> CallSession:
    with tenant_scope(context) as session:
        row = _get_row(session, context.tenant_id, call_session_id)
        session.expunge(row)
        return row


def list_call_sessions(
    context: TenantContext,
    *,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> Sequence[CallSession]:
    """`status`/`limit`/`offset` are all optional (Phase 2.5 brief section
    11: call-history "filtering/pagination") -- omitting every one of them
    preserves this function's original, unfiltered, unlimited Phase 2.1
    behavior for its one other caller
    (`voiceagent.runtime.reconciliation`-adjacent callers use
    `list_non_terminal_call_sessions()` instead, unaffected by this
    signature). Ordered newest-first (`created_at` descending, `id`
    descending as a stable tie-break) so pagination is deterministic."""
    with tenant_scope(context) as session:
        query = select(CallSession).where(CallSession.tenant_id == context.tenant_id)
        if status is not None:
            query = query.where(CallSession.status == status)
        query = query.order_by(CallSession.created_at.desc(), CallSession.id.desc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        rows = session.execute(query).scalars().all()
        for row in rows:
            session.expunge(row)
        return rows


def list_non_terminal_call_sessions(context: TenantContext) -> Sequence[CallSession]:
    """Every call in `context`'s tenant that has not reached a terminal
    status -- the reconciliation loop's own query
    (`voiceagent.runtime.reconciliation`), scoped to one tenant because no
    SaaS-OS primitive enumerates tenants across the RLS boundary (see that
    module's docstring). Not exposed through the API (Phase 2.0 report
    §23.9 lists no status-filtered route)."""
    with tenant_scope(context) as session:
        rows = (
            session.execute(
                select(CallSession)
                .where(CallSession.tenant_id == context.tenant_id)
                .where(CallSession.status.not_in(tuple(TERMINAL_STATUSES)))
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def transition_call_session(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    to_status: str,
    hangup_cause: str | None = None,
    end_reason: str | None = None,
    fs_channel_uuid: str | None = None,
    runtime_instance_id: str | None = None,
) -> CallSession:
    """Apply one lifecycle transition. A redelivered event that names the
    call's *current* status (including an already-terminal one) is a no-op
    success, never an error (Phase 2.0 report §16/§17: duplicate/delayed
    lifecycle events must not corrupt state)."""
    if to_status not in VALID_STATUSES:
        raise InvalidCallSessionTransitionError(call_session_id, "?", to_status)

    with tenant_scope(context) as session:
        call = _get_row(session, context.tenant_id, call_session_id)

        if not is_valid_transition(call.status, to_status):
            raise InvalidCallSessionTransitionError(call_session_id, call.status, to_status)

        if to_status == call.status:
            # Idempotent redelivery -- optional metadata (e.g. a channel
            # UUID observed again) may still be recorded, but no lifecycle
            # timestamp is touched a second time.
            if fs_channel_uuid is not None:
                call.fs_channel_uuid = fs_channel_uuid
            if runtime_instance_id is not None:
                call.runtime_instance_id = runtime_instance_id
                call.runtime_assigned_at = datetime.now(UTC)
            session.flush()
            session.refresh(call)
            session.expunge(call)
            return call

        now = datetime.now(UTC)
        call.status = to_status
        if to_status in ("answered", "in_progress") and call.answered_at is None:
            call.answered_at = now
        if to_status in TERMINAL_STATUSES:
            call.ended_at = now
            if call.started_at is not None:
                call.duration_ms = int((now - call.started_at).total_seconds() * 1000)
            if hangup_cause is not None:
                call.hangup_cause = hangup_cause
            if end_reason is not None:
                call.end_reason = end_reason
        if fs_channel_uuid is not None:
            call.fs_channel_uuid = fs_channel_uuid
        if runtime_instance_id is not None:
            call.runtime_instance_id = runtime_instance_id
            call.runtime_assigned_at = now

        session.flush()
        session.refresh(call)
        session.expunge(call)
        return call


def claim_runtime_ownership(
    context: TenantContext, call_session_id: uuid.UUID, *, runtime_instance_id: str
) -> CallSession:
    """Assign `call_session_id` to `runtime_instance_id` (Phase 2.0 report
    §8/§5.3; ADR-0008 points 4, 5, 10) -- the Call Orchestrator's one write
    at runtime-assignment time, called before media is attached.

    **Idempotent**: claiming a call already owned by `runtime_instance_id`
    itself is a no-op success (a redelivered assignment, or a retry of the
    same assignment, must not fail or re-stamp `runtime_assigned_at`).
    **Exclusive**: claiming a call already owned by a *different* runtime
    raises `CallSessionAlreadyOwnedError` -- a second runtime must never
    silently take over (ADR-0008 point 10; this phase does not implement
    crash takeover, Phase 2.0 report §21 OQ-4). The read-then-write is a
    single `SELECT ... FOR UPDATE` transaction (`_get_row_for_update()`), so
    two concurrent claim attempts for the same call cannot both observe "no
    owner yet" and both succeed.
    """
    with tenant_scope(context) as session:
        call = _get_row_for_update(session, context.tenant_id, call_session_id)

        if call.runtime_instance_id == runtime_instance_id:
            session.expunge(call)
            return call
        if call.runtime_instance_id is not None:
            raise CallSessionAlreadyOwnedError(call_session_id, call.runtime_instance_id)

        call.runtime_instance_id = runtime_instance_id
        call.runtime_assigned_at = datetime.now(UTC)
        session.flush()
        session.refresh(call)
        session.expunge(call)
        return call
