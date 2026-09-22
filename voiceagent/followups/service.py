"""Application service for `CallOutcome`/`FollowUpAction` (Phase 2.7 brief
§10).

The one service in this package is responsible for both aggregates --
matching the brief's own framing ("Follow-up service") and the precedent
`voiceagent.calendars.service` already sets for one service module owning
two related tables. It coordinates with `voiceagent.calendars.service` for
an appointment follow-up (brief §9) and never duplicates that module's
timezone/overlap validation -- `create_follow_up()` calls
`calendar_service.create_event()` for the one case it needs to, rather than
reimplementing any of it.

**`contact_id`** (both `CallOutcome` and `FollowUpAction`): when a caller
does not supply one explicitly, it is auto-derived by reading the call's own
`CallSession.contact_id` -- this is *read*, never accepted as a tool
argument, from the one place a Tool Gateway handler could otherwise be
tempted to accept a model-supplied contact id (ADR-0003 point 4: identity
and scope never come from model output). `voiceagent.tools.handlers`'s two
Phase 2.7 tool input models therefore have no `contact_id` field at all; an
explicit override is honored only from a non-tool caller (e.g. the REST API)
and is validated exactly like `voiceagent.calendars.service.create_event()`'s
own optional `contact_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from core.audit_log import ActorType, AuditOutcome
from core.audit_log import record as record_audit_event

from voiceagent.calendars import service as calendar_service
from voiceagent.calendars.errors import CalendarEventNotFoundError
from voiceagent.calendars.models import CalendarEvent
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.models import CallSession
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.contacts.models import Contact
from voiceagent.db import select
from voiceagent.followups.errors import (
    CallOutcomeAlreadyExistsError,
    CallOutcomeNotFoundError,
    FollowUpActionNotFoundError,
    FollowUpAppointmentRequiresCalendarEventError,
    FollowUpExecutionConflictError,
    FollowUpInvalidRelationshipError,
    FollowUpNotRetryableError,
    InvalidFollowUpFailureReasonError,
    InvalidFollowUpStatusError,
    InvalidFollowUpTransitionError,
    InvalidFollowUpTypeError,
    InvalidOutcomeValueError,
)
from voiceagent.followups.lifecycle import (
    TERMINAL_STATUSES,
    VALID_STATUSES,
    is_valid_follow_up_transition,
)
from voiceagent.followups.models import (
    FOLLOW_UP_TYPES,
    OUTCOME_VALUES,
    CallOutcome,
    FollowUpAction,
)
from voiceagent.followups.retry_policy import (
    EXECUTABLE_TYPES,
    FAILURE_REASONS,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    next_attempt_delay_seconds,
)
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "cancel_follow_up",
    "claim_due_follow_up",
    "complete_follow_up",
    "complete_follow_up_execution",
    "create_call_outcome",
    "create_follow_up",
    "execute_due_follow_up",
    "fail_follow_up_execution",
    "get_call_outcome",
    "get_follow_up",
    "list_follow_ups",
    "list_follow_ups_by_status",
    "reprocess_follow_up",
    "set_call_outcome",
    "update_call_outcome",
]


def _require_call_row(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    row = session.get(CallSession, call_session_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def _require_contact_row(session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> None:
    row = session.get(Contact, contact_id)
    if row is None or row.tenant_id != tenant_id:
        raise ContactNotFoundError(contact_id)


def _require_calendar_event_row(
    session, tenant_id: uuid.UUID, calendar_event_id: uuid.UUID
) -> CalendarEvent:
    row = session.get(CalendarEvent, calendar_event_id)
    if row is None or row.tenant_id != tenant_id:
        raise CalendarEventNotFoundError(calendar_event_id)
    return row


def _resolve_contact_id(
    session, tenant_id: uuid.UUID, call: CallSession, contact_id: uuid.UUID | None
) -> uuid.UUID | None:
    if contact_id is not None:
        _require_contact_row(session, tenant_id, contact_id)
        return contact_id
    return call.contact_id


def _validate_outcome_value(outcome: str) -> None:
    if outcome not in OUTCOME_VALUES:
        raise InvalidOutcomeValueError()


def _find_outcome_row(
    session, tenant_id: uuid.UUID, call_session_id: uuid.UUID
) -> CallOutcome | None:
    return (
        session.execute(
            select(CallOutcome)
            .where(CallOutcome.tenant_id == tenant_id)
            .where(CallOutcome.call_session_id == call_session_id)
        )
        .scalars()
        .first()
    )


# -- CallOutcome --------------------------------------------------------------


def create_call_outcome(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    outcome: str,
    notes: str | None = None,
    contact_id: uuid.UUID | None = None,
) -> CallOutcome:
    """Fails with `CallOutcomeAlreadyExistsError` if this call already has a
    current outcome (brief §6: at most one) -- use `update_call_outcome()`
    or the idempotent `set_call_outcome()` to change it."""
    _validate_outcome_value(outcome)
    with tenant_scope(context) as session:
        call = _require_call_row(session, context.tenant_id, call_session_id)
        if _find_outcome_row(session, context.tenant_id, call_session_id) is not None:
            raise CallOutcomeAlreadyExistsError(call_session_id)
        resolved_contact_id = _resolve_contact_id(session, context.tenant_id, call, contact_id)
        row = CallOutcome(
            tenant_id=context.tenant_id,
            call_session_id=call_session_id,
            contact_id=resolved_contact_id,
            outcome=outcome,
            notes=notes,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def get_call_outcome(context: TenantContext, call_session_id: uuid.UUID) -> CallOutcome:
    with tenant_scope(context) as session:
        row = _find_outcome_row(session, context.tenant_id, call_session_id)
        if row is None:
            raise CallOutcomeNotFoundError(call_session_id)
        session.expunge(row)
        return row


def update_call_outcome(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    outcome: str | None = None,
    notes: str | None = None,
) -> CallOutcome:
    """Partial update of the existing current outcome -- fails with
    `CallOutcomeNotFoundError` if none exists yet (use `create_call_outcome()`
    or `set_call_outcome()` for that case)."""
    if outcome is not None:
        _validate_outcome_value(outcome)
    with tenant_scope(context) as session:
        row = _find_outcome_row(session, context.tenant_id, call_session_id)
        if row is None:
            raise CallOutcomeNotFoundError(call_session_id)
        if outcome is not None:
            row.outcome = outcome
        if notes is not None:
            row.notes = notes
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def set_call_outcome(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    outcome: str,
    notes: str | None = None,
    contact_id: uuid.UUID | None = None,
) -> CallOutcome:
    """Idempotent create-or-update (brief §12: "remains idempotent where
    practical") -- the one operation both the `PUT /v1/call-sessions/{id}
    /outcome` route and the `call.set_outcome` tool call, so setting the
    identical outcome twice converges to the same row rather than erroring
    on the second call. Never touches `CallSession.status` (brief §12)."""
    _validate_outcome_value(outcome)
    with tenant_scope(context) as session:
        call = _require_call_row(session, context.tenant_id, call_session_id)
        row = _find_outcome_row(session, context.tenant_id, call_session_id)
        if row is None:
            resolved_contact_id = _resolve_contact_id(session, context.tenant_id, call, contact_id)
            row = CallOutcome(
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                contact_id=resolved_contact_id,
                outcome=outcome,
                notes=notes,
            )
            session.add(row)
        else:
            row.outcome = outcome
            if notes is not None:
                row.notes = notes
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


# -- FollowUpAction -------------------------------------------------------------


def _get_follow_up_row(session, tenant_id: uuid.UUID, follow_up_id: uuid.UUID) -> FollowUpAction:
    row = session.get(FollowUpAction, follow_up_id)
    if row is None or row.tenant_id != tenant_id:
        raise FollowUpActionNotFoundError(follow_up_id)
    return row


def create_follow_up(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    type: str,
    description: str | None = None,
    due_at: datetime | None = None,
    contact_id: uuid.UUID | None = None,
    calendar_event_id: uuid.UUID | None = None,
    calendar_id: uuid.UUID | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> FollowUpAction:
    """Phase 2.7 brief §10's ordering: call ownership, contact ownership,
    type validation, relationship consistency (brief §8), then create.

    For `type == "appointment"`: an existing `calendar_event_id` is
    validated for tenant ownership; otherwise, if `calendar_id`/`start_at`/
    `end_at` are all given, `voiceagent.calendars.service.create_event()` is
    called to create the appointment (never duplicated here -- brief §9).
    Neither given is a deterministic rejection
    (`FollowUpAppointmentRequiresCalendarEventError`, brief §8), never a
    silent pending state. For any other `type`, a supplied
    `calendar_event_id` is rejected outright
    (`FollowUpInvalidRelationshipError`).
    """
    if type not in FOLLOW_UP_TYPES:
        raise InvalidFollowUpTypeError()
    if type != "appointment" and calendar_event_id is not None:
        raise FollowUpInvalidRelationshipError()

    with tenant_scope(context) as session:
        call = _require_call_row(session, context.tenant_id, call_session_id)
        resolved_contact_id = _resolve_contact_id(session, context.tenant_id, call, contact_id)

        resolved_calendar_event_id = calendar_event_id
        if type == "appointment" and resolved_calendar_event_id is None:
            if calendar_id is not None and start_at is not None and end_at is not None:
                event = calendar_service.create_event(
                    context,
                    calendar_id=calendar_id,
                    title=f"Follow-up for call {call_session_id}",
                    start_at=start_at,
                    end_at=end_at,
                    contact_id=resolved_contact_id,
                )
                resolved_calendar_event_id = event.id
            else:
                raise FollowUpAppointmentRequiresCalendarEventError()
        elif resolved_calendar_event_id is not None:
            _require_calendar_event_row(session, context.tenant_id, resolved_calendar_event_id)

        row = FollowUpAction(
            tenant_id=context.tenant_id,
            call_session_id=call_session_id,
            contact_id=resolved_contact_id,
            type=type,
            status="pending",
            due_at=due_at,
            calendar_event_id=resolved_calendar_event_id,
            description=description,
            # Phase 2.9 brief §4: seed the internal claim-eligibility column
            # from the caller's own `due_at`, only for a type
            # `claim_due_follow_up()` will ever pick up. A follow-up with no
            # `due_at`, or of a type with no concrete execution service
            # (brief §5), is never automatically claimed -- `next_attempt_at`
            # stays `NULL`.
            next_attempt_at=(due_at if type in EXECUTABLE_TYPES and due_at is not None else None),
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def get_follow_up(context: TenantContext, follow_up_id: uuid.UUID) -> FollowUpAction:
    with tenant_scope(context) as session:
        row = _get_follow_up_row(session, context.tenant_id, follow_up_id)
        session.expunge(row)
        return row


def list_follow_ups(context: TenantContext, call_session_id: uuid.UUID) -> Sequence[FollowUpAction]:
    """Newest-first. Raises `CallSessionNotFoundError` for a call this
    tenant cannot see -- never a silent empty list for that case, matching
    brief §21's "cross-tenant access" test expectation."""
    with tenant_scope(context) as session:
        _require_call_row(session, context.tenant_id, call_session_id)
        rows = (
            session.execute(
                select(FollowUpAction)
                .where(FollowUpAction.tenant_id == context.tenant_id)
                .where(FollowUpAction.call_session_id == call_session_id)
                .order_by(FollowUpAction.created_at.desc(), FollowUpAction.id.desc())
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


#: `list_follow_ups_by_status()`'s own hard cap (Phase 2.9 brief §12) --
#: matching `core.audit_log.list()`'s own "a caller cannot accidentally
#: request an unbounded result set" discipline; this route takes no caller
#: override, so it is a constant, not a validated parameter.
_LIST_BY_STATUS_LIMIT = 200


def list_follow_ups_by_status(
    context: TenantContext, *, status: str | None = None
) -> Sequence[FollowUpAction]:
    """Tenant-scoped, not call-scoped (unlike `list_follow_ups()`) -- the
    one query `GET /v1/follow-ups` needs for due/status visibility (brief
    §12). Most-overdue-first: a row with a `next_attempt_at` sorts by it
    ascending, ahead of every row with none (`created_at` descending among
    those)."""
    if status is not None and status not in VALID_STATUSES:
        raise InvalidFollowUpStatusError()
    with tenant_scope(context) as session:
        query = select(FollowUpAction).where(FollowUpAction.tenant_id == context.tenant_id)
        if status is not None:
            query = query.where(FollowUpAction.status == status)
        query = query.order_by(
            FollowUpAction.next_attempt_at.is_(None),
            FollowUpAction.next_attempt_at.asc(),
            FollowUpAction.created_at.desc(),
        ).limit(_LIST_BY_STATUS_LIMIT)
        rows = session.execute(query).scalars().all()
        for row in rows:
            session.expunge(row)
        return rows


def _transition_follow_up(
    context: TenantContext,
    follow_up_id: uuid.UUID,
    *,
    to_status: str,
    on_transition: Callable[[object, FollowUpAction], None] | None = None,
) -> FollowUpAction:
    with tenant_scope(context) as session:
        row = _get_follow_up_row(session, context.tenant_id, follow_up_id)
        if not is_valid_follow_up_transition(row.status, to_status):
            raise InvalidFollowUpTransitionError(follow_up_id, row.status, to_status)
        if row.status != to_status:
            row.status = to_status
            if to_status in TERMINAL_STATUSES:
                # Brief §4: "a cancelled/completed follow-up is never
                # executable again" -- belt-and-suspenders alongside
                # `claim_due_follow_up()`'s own `status`-based filter.
                row.next_attempt_at = None
            session.flush()
            session.refresh(row)
            if on_transition is not None:
                on_transition(session, row)
        session.expunge(row)
        return row


def complete_follow_up(context: TenantContext, follow_up_id: uuid.UUID) -> FollowUpAction:
    """`pending -> completed`. Idempotent: completing an already-completed
    follow-up is a no-op success (brief §7/§21). Manual completion, for a
    follow-up type with no concrete execution service (Phase 2.9 brief §5)
    -- unchanged from Phase 2.7, and never called by
    `execute_due_follow_up()`, which uses `complete_follow_up_execution()`
    instead."""
    return _transition_follow_up(context, follow_up_id, to_status="completed")


def _audit_follow_up_cancelled(
    session: object, row: FollowUpAction, context: TenantContext
) -> None:
    """Phase 2.9 brief §15: cancelling a follow-up
    `voiceagent.followups.retry_policy.EXECUTABLE_TYPES` covers is
    externally meaningful -- it stops a scheduled execution -- and is
    audited. Cancelling any other type stays exactly as Phase 2.7 left it:
    no audit entry."""
    if row.type not in EXECUTABLE_TYPES:
        return
    record_audit_event(
        tenant_id=context.tenant_id,
        actor_type=ActorType.USER,
        actor_user_id=context.actor_id,
        action="follow_up.cancelled",
        resource_type="follow_up_action",
        resource_id=str(row.id),
        outcome=AuditOutcome.SUCCESS,
        metadata={"attempt_count": row.attempt_count},
    )


def cancel_follow_up(context: TenantContext, follow_up_id: uuid.UUID) -> FollowUpAction:
    """`pending -> cancelled` or `failed -> cancelled` (Phase 2.9 widens the
    second edge -- brief §4: a follow-up mid-retry can be given up on
    without waiting out `retry_policy.MAX_ATTEMPTS`). Idempotent, never a
    hard delete (brief §16: "do not expose DELETE for durable follow-up
    records")."""
    return _transition_follow_up(
        context,
        follow_up_id,
        to_status="cancelled",
        on_transition=lambda session, row: _audit_follow_up_cancelled(session, row, context),
    )


# -- FollowUpAction: scheduled execution (Phase 2.9) ---------------------------


def claim_due_follow_up(
    context: TenantContext, *, now: datetime | None = None
) -> FollowUpAction | None:
    """Atomically claim the single most-overdue eligible follow-up for
    `context`'s tenant (brief §6): `SELECT ... FOR UPDATE SKIP LOCKED LIMIT
    1`, so two concurrent callers (two worker processes, or two calls to
    this function racing) never claim the same row -- the loser's query
    simply skips the locked row and, if nothing else is eligible, returns
    `None`. The claim itself -- reading the row and writing
    `status='processing'` plus its execution metadata -- is one, short
    transaction; `execute_due_follow_up()` performs the external action
    *after* this function returns, with no database transaction held open
    (brief §6).

    Eligible: `type` in `retry_policy.EXECUTABLE_TYPES`, `status` in
    `('pending', 'processing', 'failed')`, and `next_attempt_at` is set and
    has elapsed -- a `'processing'` row only matches once its claim lease
    has expired (brief §9's stale-processing recovery: an *active* lease's
    `next_attempt_at` is still in the future, so it is never stolen), and a
    `'failed'` row only once `retry_policy.next_attempt_delay_seconds()`'s
    backoff has elapsed, or `reprocess_follow_up()` cleared it early.

    `execution_id` is generated once, on a follow-up's first-ever claim, and
    reused by every later attempt or reclaim (brief §7: "a stable
    execution/idempotency identity").
    """
    now = now or datetime.now(UTC)
    with tenant_scope(context) as session:
        row = (
            session.execute(
                select(FollowUpAction)
                .where(FollowUpAction.tenant_id == context.tenant_id)
                .where(FollowUpAction.type.in_(tuple(EXECUTABLE_TYPES)))
                .where(FollowUpAction.status.in_(("pending", "processing", "failed")))
                .where(FollowUpAction.next_attempt_at.is_not(None))
                .where(FollowUpAction.next_attempt_at <= now)
                .order_by(FollowUpAction.next_attempt_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None

        if not is_valid_follow_up_transition(row.status, "processing"):
            # Unreachable given the query above -- every matched status
            # already has a legal -> "processing" edge (lifecycle.py) --
            # kept as an explicit guard rather than a silent skip, so a
            # future lifecycle/retry_policy drift fails loudly instead of
            # claiming a row it should not.
            raise InvalidFollowUpTransitionError(row.id, row.status, "processing")

        row.status = "processing"
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
            action="follow_up.claimed",
            resource_type="follow_up_action",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(row.execution_id),
            metadata={"attempt_count": row.attempt_count, "type": row.type},
        )

        session.expunge(row)
        return row


def complete_follow_up_execution(
    context: TenantContext, follow_up_id: uuid.UUID, *, execution_id: uuid.UUID
) -> FollowUpAction:
    """`processing -> completed` (brief §6). `execution_id` must match the
    row's own current one, and the row must still be `status='processing'`
    -- a mismatch means this caller's claim lease already expired and a
    later claim reclaimed (or already finished) the row; raises
    `FollowUpExecutionConflictError` rather than overwriting that later
    attempt's outcome."""
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _get_follow_up_row(session, context.tenant_id, follow_up_id)
        if row.status != "processing" or row.execution_id != execution_id:
            raise FollowUpExecutionConflictError(follow_up_id)

        row.status = "completed"
        row.completed_at = now
        row.failure_reason = None
        row.next_attempt_at = None
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="follow_up.completed",
            resource_type="follow_up_action",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(execution_id),
            metadata={"attempt_count": row.attempt_count, "type": row.type},
        )

        session.expunge(row)
        return row


def fail_follow_up_execution(
    context: TenantContext,
    follow_up_id: uuid.UUID,
    *,
    execution_id: uuid.UUID,
    reason: str,
) -> FollowUpAction:
    """`processing -> failed` (brief §8). `reason` must be one of
    `retry_policy.FAILURE_REASONS` -- never a raw exception message or
    provider response. Sets `next_attempt_at` to the next backoff deadline
    (`retry_policy.next_attempt_delay_seconds()`) while `attempt_count` has
    not yet reached `retry_policy.MAX_ATTEMPTS`, or to `None` (never
    automatically claimed again) once it has -- the one place "no infinite
    retry loop" (brief §8) is enforced. Same ownership guard as
    `complete_follow_up_execution()`."""
    if reason not in FAILURE_REASONS:
        raise InvalidFollowUpFailureReasonError()
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _get_follow_up_row(session, context.tenant_id, follow_up_id)
        if row.status != "processing" or row.execution_id != execution_id:
            raise FollowUpExecutionConflictError(follow_up_id)

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
            action="follow_up.failed",
            resource_type="follow_up_action",
            resource_id=str(row.id),
            outcome=AuditOutcome.FAILURE,
            correlation_id=str(execution_id),
            metadata={
                "attempt_count": row.attempt_count,
                "failure_reason": reason,
                "exhausted": exhausted,
            },
        )

        session.expunge(row)
        return row


def _execute_appointment_follow_up(
    context: TenantContext, claimed: FollowUpAction
) -> FollowUpAction:
    """The one concrete execution type this phase implements (brief §5):
    confirm the linked `CalendarEvent` still stands, via
    `voiceagent.calendars.service` -- never creating or duplicating one. The
    appointment's `calendar_event_id` is already fixed at `create_follow_up()`
    time (brief §8's own relationship rule, `ck_follow_up_actions
    _appointment_requires_calendar_event`); this function performs no
    calendar *write* at all, which is what makes retrying it freely
    idempotent (brief §7) -- re-reading the same event twice has no side
    effect, so there is no duplicate-appointment risk to guard against with
    a second idempotency mechanism.

    Any unexpected exception from `calendar_service.get_event()` is
    translated into a bounded, backoff-retried failure
    (`reason="unexpected_error"`) rather than left to propagate -- brief
    §8's "retries must be deterministic and testable" would not hold if a
    transient error instead bypassed `fail_follow_up_execution()`'s own
    bookkeeping and relied solely on lease-expiry recovery (brief §9),
    which is far coarser-grained. `asyncio.CancelledError` (a
    `BaseException`, not an `Exception`) still propagates unmodified,
    matching `voiceagent.runtime.supervisor`'s own error-isolation rule.
    """
    execution_id = claimed.execution_id
    if execution_id is None or claimed.calendar_event_id is None:
        # Guaranteed by claim_due_follow_up() and
        # ck_follow_up_actions_appointment_requires_calendar_event
        # respectively -- narrowed explicitly (never `assert`, which a
        # production `-O` run would strip) rather than left implicit.
        raise RuntimeError(
            f"FollowUpAction {claimed.id} claimed with no execution_id/calendar_event_id"
        )

    try:
        event = calendar_service.get_event(context, claimed.calendar_event_id)
    except CalendarEventNotFoundError:
        return fail_follow_up_execution(
            context, claimed.id, execution_id=execution_id, reason="calendar_event_not_found"
        )
    except Exception:
        return fail_follow_up_execution(
            context, claimed.id, execution_id=execution_id, reason="unexpected_error"
        )

    if event.status == "cancelled":
        return fail_follow_up_execution(
            context, claimed.id, execution_id=execution_id, reason="calendar_event_cancelled"
        )

    return complete_follow_up_execution(context, claimed.id, execution_id=execution_id)


def execute_due_follow_up(
    context: TenantContext, *, now: datetime | None = None
) -> FollowUpAction | None:
    """Claim the next due follow-up for `context`'s tenant and execute it
    (brief §10). Returns `None` when nothing is currently eligible -- not an
    error, the ordinary "nothing to do this tick" result
    `voiceagent.followups.worker.FollowUpWorker` polls for. The sole caller
    of `claim_due_follow_up()`/`complete_follow_up_execution()`/
    `fail_follow_up_execution()` in production; everything between the claim
    and the final transition runs with no database transaction open (brief
    §6)."""
    claimed = claim_due_follow_up(context, now=now)
    if claimed is None:
        return None

    if claimed.type == "appointment":
        return _execute_appointment_follow_up(context, claimed)

    execution_id = claimed.execution_id
    if execution_id is None:
        raise RuntimeError(f"FollowUpAction {claimed.id} claimed with no execution_id")

    # Unreachable today: claim_due_follow_up()'s own query only ever matches
    # retry_policy.EXECUTABLE_TYPES (currently just "appointment"). Handled
    # explicitly, rather than left to fall through, so a future
    # EXECUTABLE_TYPES addition with no matching dispatch branch here fails
    # this one attempt loudly (bounded-retried, then terminal) instead of
    # leaving a follow-up stranded in "processing" until its lease expires.
    return fail_follow_up_execution(
        context, claimed.id, execution_id=execution_id, reason="unexpected_error"
    )


def reprocess_follow_up(context: TenantContext, follow_up_id: uuid.UUID) -> FollowUpAction:
    """Administrative retry (brief §12/§13, dedicated `retry` permission):
    for a `status='failed'` follow-up that has not yet exhausted
    `retry_policy.MAX_ATTEMPTS`, clears its backoff wait
    (`next_attempt_at = now`) so the next worker poll claims it immediately.
    Does not reset `attempt_count` and does not raise the bounded retry
    ceiling -- a follow-up that has already exhausted its attempts stays
    refused (`FollowUpNotRetryableError`); there is no path in this service
    that grants more than `retry_policy.MAX_ATTEMPTS` attempts."""
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _get_follow_up_row(session, context.tenant_id, follow_up_id)
        if row.status != "failed" or row.attempt_count >= MAX_ATTEMPTS:
            raise FollowUpNotRetryableError(follow_up_id)

        row.next_attempt_at = now
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="follow_up.reprocess_requested",
            resource_type="follow_up_action",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            metadata={"attempt_count": row.attempt_count},
        )

        session.expunge(row)
        return row
