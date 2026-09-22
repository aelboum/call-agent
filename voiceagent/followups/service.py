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
from collections.abc import Sequence
from datetime import datetime

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
    FollowUpInvalidRelationshipError,
    InvalidFollowUpTransitionError,
    InvalidFollowUpTypeError,
    InvalidOutcomeValueError,
)
from voiceagent.followups.lifecycle import is_valid_follow_up_transition
from voiceagent.followups.models import (
    FOLLOW_UP_TYPES,
    OUTCOME_VALUES,
    CallOutcome,
    FollowUpAction,
)
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "cancel_follow_up",
    "complete_follow_up",
    "create_call_outcome",
    "create_follow_up",
    "get_call_outcome",
    "get_follow_up",
    "list_follow_ups",
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


def _transition_follow_up(
    context: TenantContext, follow_up_id: uuid.UUID, *, to_status: str
) -> FollowUpAction:
    with tenant_scope(context) as session:
        row = _get_follow_up_row(session, context.tenant_id, follow_up_id)
        if not is_valid_follow_up_transition(row.status, to_status):
            raise InvalidFollowUpTransitionError(follow_up_id, row.status, to_status)
        if row.status != to_status:
            row.status = to_status
            session.flush()
            session.refresh(row)
        session.expunge(row)
        return row


def complete_follow_up(context: TenantContext, follow_up_id: uuid.UUID) -> FollowUpAction:
    """`pending -> completed`. Idempotent: completing an already-completed
    follow-up is a no-op success (brief §7/§21)."""
    return _transition_follow_up(context, follow_up_id, to_status="completed")


def cancel_follow_up(context: TenantContext, follow_up_id: uuid.UUID) -> FollowUpAction:
    """`pending -> cancelled`. Idempotent, never a hard delete (brief §16:
    "do not expose DELETE for durable follow-up records")."""
    return _transition_follow_up(context, follow_up_id, to_status="cancelled")
