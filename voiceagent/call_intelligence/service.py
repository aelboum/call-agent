"""Application service for `CallAiAnalysis` (Phase 2.12).

Every function takes a verified `voiceagent.tenancy.TenantContext` and opens
its own `tenant_scope()` -- no function accepts a bare `tenant_id`, matching
every other `voiceagent.*.service` module. The claim/complete/fail shape is
the identical "atomic claim, external work with no transaction held, then a
second short transaction to persist the outcome" discipline
`voiceagent.followups.service.claim_due_follow_up()`/
`complete_follow_up_execution()`/`fail_follow_up_execution()` already
establish for TRANSACTIONS/CONCURRENCY -- `voiceagent.call_intelligence
.analyzer.run_call_ai_analysis()` is the one caller of the claim/complete/
fail trio in production, and it never holds a database session open across
its own `await provider.analyze(...)` call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from core.audit_log import ActorType, AuditOutcome
from core.audit_log import record as record_audit_event

from voiceagent.call_intelligence.errors import (
    CallAiAnalysisExecutionConflictError,
    CallAiAnalysisInProgressError,
    CallAiAnalysisNotFoundError,
    CallNotEligibleForAnalysisError,
    InvalidCallAiAnalysisFailureReasonError,
)
from voiceagent.call_intelligence.models import CALL_AI_ANALYSIS_FAILURE_REASONS, CallAiAnalysis
from voiceagent.call_intelligence.retry_policy import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    next_attempt_delay_seconds,
)
from voiceagent.call_intelligence.schema import CallAiAnalysisResult
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.lifecycle import TERMINAL_STATUSES
from voiceagent.calls.models import CallSession
from voiceagent.db import select
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "SCHEMA_VERSION",
    "claim_pending_analysis",
    "complete_analysis",
    "fail_analysis",
    "get_latest_analysis",
    "list_analysis_versions",
    "request_analysis",
]

#: Bumped only if `voiceagent.call_intelligence.schema.CallAiAnalysisResult`'s
#: own shape changes incompatibly -- never silently; a stored row's
#: `schema_version` always names the shape its own `result` column actually
#: has (brief VERSIONING).
SCHEMA_VERSION = "1"


def _require_call_row(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    row = session.get(CallSession, call_session_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def _list_versions_desc(
    session, tenant_id: uuid.UUID, call_session_id: uuid.UUID
) -> list[CallAiAnalysis]:
    return list(
        session.execute(
            select(CallAiAnalysis)
            .where(CallAiAnalysis.tenant_id == tenant_id)
            .where(CallAiAnalysis.call_session_id == call_session_id)
            .order_by(CallAiAnalysis.version.desc())
        )
        .scalars()
        .all()
    )


def _get_row(session, tenant_id: uuid.UUID, analysis_id: uuid.UUID) -> CallAiAnalysis:
    row = session.get(CallAiAnalysis, analysis_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallAiAnalysisNotFoundError(analysis_id)
    return row


def request_analysis(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    provider: str,
    model: str,
    prompt_version: str,
    force_rebuild: bool = False,
) -> CallAiAnalysis:
    """Idempotent by default (`force_rebuild=False`): returns the existing
    latest version untouched if one already exists, never creating a
    duplicate (brief LIFECYCLE: "idempotent creation", "no duplicate
    completed analyses"). `force_rebuild=True` (the authorized, versioned
    rebuild path -- brief VERSIONING) always creates a fresh, higher-
    `version` row instead, unless a version is already `pending`/
    `processing`, in which case `CallAiAnalysisInProgressError` is raised
    rather than queuing a second concurrent analysis of the same call.

    Raises `CallNotEligibleForAnalysisError` unless the call has reached a
    terminal `voiceagent.calls.lifecycle.TERMINAL_STATUSES` status -- this
    is *post*-call intelligence."""
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        call = _require_call_row(session, context.tenant_id, call_session_id)
        if call.status not in TERMINAL_STATUSES:
            raise CallNotEligibleForAnalysisError(call_session_id)

        versions = _list_versions_desc(session, context.tenant_id, call_session_id)
        latest = versions[0] if versions else None

        if latest is not None and latest.status in ("pending", "processing"):
            if force_rebuild:
                raise CallAiAnalysisInProgressError(call_session_id)
            session.expunge(latest)
            return latest

        if latest is not None and not force_rebuild:
            session.expunge(latest)
            return latest

        next_version = (latest.version + 1) if latest is not None else 1
        row = CallAiAnalysis(
            tenant_id=context.tenant_id,
            call_session_id=call_session_id,
            version=next_version,
            status="pending",
            schema_version=SCHEMA_VERSION,
            prompt_version=prompt_version,
            provider=provider,
            model=model,
            attempt_count=0,
            requested_at=now,
            next_attempt_at=now,
        )
        session.add(row)
        session.flush()
        session.refresh(row)

        action = (
            "call_ai_analysis.rebuild_requested" if force_rebuild else "call_ai_analysis.requested"
        )
        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action=action,
            resource_type="call_ai_analysis",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(call_session_id),
            metadata={"version": next_version, "provider": provider},
        )

        session.expunge(row)
        return row


def claim_pending_analysis(
    context: TenantContext, *, now: datetime | None = None
) -> CallAiAnalysis | None:
    """Atomically claim the single most-overdue eligible `CallAiAnalysis`
    for `context`'s tenant (`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`) --
    the identical shape `voiceagent.followups.service.claim_due_follow_up()`
    already establishes. Eligible: `status` in `('pending', 'processing',
    'failed')` and `next_attempt_at` is set and has elapsed (a `'processing'`
    row only once its claim lease has expired; a `'failed'` row only once
    its backoff has elapsed)."""
    now = now or datetime.now(UTC)
    with tenant_scope(context) as session:
        row = (
            session.execute(
                select(CallAiAnalysis)
                .where(CallAiAnalysis.tenant_id == context.tenant_id)
                .where(CallAiAnalysis.status.in_(("pending", "processing", "failed")))
                .where(CallAiAnalysis.next_attempt_at.is_not(None))
                .where(CallAiAnalysis.next_attempt_at <= now)
                .order_by(CallAiAnalysis.next_attempt_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None

        row.status = "processing"
        # A reclaimed 'failed' row's own failure_reason must be cleared here
        # -- ck_call_ai_analyses_failed_iff_reason requires
        # (status = 'failed') = (failure_reason IS NOT NULL), and this row
        # is no longer 'failed' the moment it is claimed.
        row.failure_reason = None
        row.attempt_count += 1
        row.last_attempted_at = now
        row.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        if row.execution_id is None:
            row.execution_id = uuid.uuid4()
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="call_ai_analysis.started",
            resource_type="call_ai_analysis",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(row.execution_id),
            metadata={"attempt_count": row.attempt_count, "version": row.version},
        )

        session.expunge(row)
        return row


def complete_analysis(
    context: TenantContext,
    analysis_id: uuid.UUID,
    *,
    execution_id: uuid.UUID,
    result: CallAiAnalysisResult,
) -> CallAiAnalysis:
    """`processing -> completed`. Ownership-checked exactly like
    `voiceagent.followups.service.complete_follow_up_execution()`: a mismatch
    means this caller's lease already expired and a later claim reclaimed
    (or already finished) the row. Never mutated again afterward
    (`app.forbid_call_ai_analysis_completed_update`, migration `0010`)."""
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _get_row(session, context.tenant_id, analysis_id)
        if row.status != "processing" or row.execution_id != execution_id:
            raise CallAiAnalysisExecutionConflictError(analysis_id)

        row.status = "completed"
        row.completed_at = now
        row.failure_reason = None
        row.next_attempt_at = None
        row.result = result.model_dump(mode="json")
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="call_ai_analysis.completed",
            resource_type="call_ai_analysis",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(execution_id),
            metadata={
                "attempt_count": row.attempt_count,
                "version": row.version,
                "confidence": result.confidence,
                "escalation_required": result.escalation.required,
            },
        )

        session.expunge(row)
        return row


def fail_analysis(
    context: TenantContext,
    analysis_id: uuid.UUID,
    *,
    execution_id: uuid.UUID,
    reason: str,
) -> CallAiAnalysis:
    """`processing -> failed`. `reason` must be a member of
    `CALL_AI_ANALYSIS_FAILURE_REASONS` -- never a raw exception message or
    provider response (brief AUDIT/SECURITY). Sets `next_attempt_at` to the
    next backoff deadline while `attempt_count < MAX_ATTEMPTS`, or to `None`
    (never automatically claimed again) once exhausted -- the one place "no
    infinite retry loop" is enforced. Same ownership guard as
    `complete_analysis()`."""
    if reason not in CALL_AI_ANALYSIS_FAILURE_REASONS:
        raise InvalidCallAiAnalysisFailureReasonError()
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _get_row(session, context.tenant_id, analysis_id)
        if row.status != "processing" or row.execution_id != execution_id:
            raise CallAiAnalysisExecutionConflictError(analysis_id)

        row.status = "failed"
        row.failure_reason = reason
        exhausted = row.attempt_count >= MAX_ATTEMPTS
        row.next_attempt_at = (
            None
            if exhausted
            else now + timedelta(seconds=next_attempt_delay_seconds(row.attempt_count))
        )
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="call_ai_analysis.failed",
            resource_type="call_ai_analysis",
            resource_id=str(row.id),
            outcome=AuditOutcome.FAILURE,
            correlation_id=str(execution_id),
            metadata={
                "attempt_count": row.attempt_count,
                "failure_reason": reason,
                "exhausted": exhausted,
                "version": row.version,
            },
        )

        session.expunge(row)
        return row


def get_latest_analysis(context: TenantContext, call_session_id: uuid.UUID) -> CallAiAnalysis:
    """The highest-`version` row for this call, regardless of status -- a
    caller sees `pending`/`processing`/`failed` state too, not only a
    completed result (matching `voiceagent.followups.service.get_follow_up()`
    's own "expose real status" convention). Raises
    `CallAiAnalysisNotFoundError` if no analysis has ever been requested."""
    with tenant_scope(context) as session:
        versions = _list_versions_desc(session, context.tenant_id, call_session_id)
        if not versions:
            raise CallAiAnalysisNotFoundError(call_session_id)
        row = versions[0]
        session.expunge(row)
        return row


def list_analysis_versions(
    context: TenantContext, call_session_id: uuid.UUID
) -> list[CallAiAnalysis]:
    """Every version, ascending -- history visibility (brief VERSIONING:
    "must be able to coexist safely with an older result")."""
    with tenant_scope(context) as session:
        rows = _list_versions_desc(session, context.tenant_id, call_session_id)
        rows.reverse()
        for row in rows:
            session.expunge(row)
        return rows
