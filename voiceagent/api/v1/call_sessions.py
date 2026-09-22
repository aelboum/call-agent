"""`/v1/call-sessions` -- exactly the two read endpoints from Phase 2.0
report §23.9. No creation or transition route: those belong to the Call
Orchestrator (Phase 2.2+), not to a tenant-facing API in Phase 2.1
(`voiceagent.calls.service.create_call_session()`/`transition_call_session()`
exist and are tested directly, but are not exposed here). Phase 2.6 adds the
contact-association route; Phase 2.7 adds the outcome and call-scoped
follow-up routes (brief §16) -- both nested here, matching Phase 2.6's own
precedent of adding a call-scoped sub-resource route directly to this router
rather than a new file."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from voiceagent.api.errors import conflict, not_found
from voiceagent.calendars.errors import (
    CalendarEventConflictError,
    CalendarNotFoundError,
    InvalidIntervalError,
    NaiveDatetimeError,
)
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.lifecycle import VALID_STATUSES
from voiceagent.calls.models import CallSession
from voiceagent.calls.permissions import RESOURCE
from voiceagent.calls.service import associate_call, get_call_session, list_call_sessions
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.followups.errors import (
    CallOutcomeNotFoundError,
    FollowUpAppointmentRequiresCalendarEventError,
    FollowUpInvalidRelationshipError,
    InvalidFollowUpTypeError,
    InvalidOutcomeValueError,
)
from voiceagent.followups.models import CallOutcome, FollowUpAction
from voiceagent.followups.permissions import CALL_OUTCOMES_RESOURCE, FOLLOW_UP_ACTIONS_RESOURCE
from voiceagent.followups.service import (
    create_follow_up,
    get_call_outcome,
    list_follow_ups,
    set_call_outcome,
)
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/call-sessions", tags=["call-sessions"])

_read = require_tenant(RESOURCE, "read")
#: Phase 2.6 (brief §16) adds one new action to the existing
#: `voiceagent.call_sessions` resource, rather than reusing "read" for a
#: write -- a role that can only read call sessions must not thereby be able
#: to mutate one.
_associate = require_tenant(RESOURCE, "associate")
_outcome_read = require_tenant(CALL_OUTCOMES_RESOURCE, "read")
_outcome_update = require_tenant(CALL_OUTCOMES_RESOURCE, "update")
_follow_up_read = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "read")
_follow_up_create = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "create")


class CallSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: str
    status: str
    from_e164: str
    to_e164: str
    phone_number_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    contact_id: uuid.UUID | None
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    hangup_cause: str | None
    end_reason: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, call: CallSession) -> CallSessionOut:
        return cls.model_validate(call)


@router.get("/{call_session_id}")
def get_call_session_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> CallSessionOut:
    try:
        call = get_call_session(context, call_session_id)
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    return CallSessionOut.from_model(call)


@router.get("")
def list_call_sessions_route(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[CallSessionOut]:
    """Newest-first, paginated (`limit`/`offset`), optionally filtered to one
    lifecycle `status`. An unrecognized `status` value matches no row (the
    same `CHECK` constraint that governs `CallSession.status` itself) rather
    than being rejected -- there is no distinct-response reason to prefer a
    422 over an empty page here."""
    if status is not None and status not in VALID_STATUSES:
        return []
    return [
        CallSessionOut.from_model(call)
        for call in list_call_sessions(context, status=status, limit=limit, offset=offset)
    ]


class CallSessionAssociateContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: uuid.UUID


@router.post("/{call_session_id}/contact")
def associate_call_contact_route(
    call_session_id: uuid.UUID,
    payload: CallSessionAssociateContactRequest,
    context: TenantContext = Depends(_associate),  # noqa: B008
) -> CallSessionOut:
    """Phase 2.6 brief §16. `associate_call()` reads both rows inside one
    tenant-scoped session, so a `contact_id` belonging to a different tenant
    (or no tenant at all) is indistinguishable from "no such contact" --
    there is no cross-tenant lookup mechanism to expose."""
    try:
        call = associate_call(context, call_session_id, payload.contact_id)
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    except ContactNotFoundError:
        raise not_found("contact") from None
    return CallSessionOut.from_model(call)


# -- Phase 2.7: call outcome ---------------------------------------------------


class CallOutcomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_session_id: uuid.UUID
    contact_id: uuid.UUID | None
    outcome: str
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: CallOutcome) -> CallOutcomeOut:
        return cls.model_validate(row)


class CallOutcomeSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(min_length=1, max_length=30)
    notes: str | None = None


@router.get("/{call_session_id}/outcome")
def get_call_outcome_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_outcome_read),  # noqa: B008
) -> CallOutcomeOut:
    try:
        outcome = get_call_outcome(context, call_session_id)
    except CallOutcomeNotFoundError:
        raise not_found("call outcome") from None
    return CallOutcomeOut.from_model(outcome)


@router.put("/{call_session_id}/outcome")
def set_call_outcome_route(
    call_session_id: uuid.UUID,
    payload: CallOutcomeSetRequest,
    context: TenantContext = Depends(_outcome_update),  # noqa: B008
) -> CallOutcomeOut:
    """Idempotent create-or-update (`PUT`, brief §16): the one route for
    both first setting and later changing a call's outcome --
    `voiceagent.followups.service.set_call_outcome()`'s own docstring
    explains why one operation serves both."""
    try:
        outcome = set_call_outcome(
            context, call_session_id, outcome=payload.outcome, notes=payload.notes
        )
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    except InvalidOutcomeValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid outcome value."
        ) from None
    return CallOutcomeOut.from_model(outcome)


# -- Phase 2.7: call-scoped follow-ups -----------------------------------------


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_session_id: uuid.UUID
    contact_id: uuid.UUID | None
    type: str
    status: str
    due_at: datetime | None
    calendar_event_id: uuid.UUID | None
    description: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: FollowUpAction) -> FollowUpOut:
        return cls.model_validate(row)


class FollowUpCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=30)
    description: str | None = None
    due_at: AwareDatetime | None = None
    calendar_id: uuid.UUID | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None


@router.get("/{call_session_id}/follow-ups")
def list_follow_ups_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_follow_up_read),  # noqa: B008
) -> list[FollowUpOut]:
    try:
        rows = list_follow_ups(context, call_session_id)
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    return [FollowUpOut.from_model(row) for row in rows]


@router.post("/{call_session_id}/follow-ups", status_code=201)
def create_follow_up_route(
    call_session_id: uuid.UUID,
    payload: FollowUpCreateRequest,
    context: TenantContext = Depends(_follow_up_create),  # noqa: B008
) -> FollowUpOut:
    try:
        row = create_follow_up(
            context,
            call_session_id,
            type=payload.type,
            description=payload.description,
            due_at=payload.due_at,
            calendar_id=payload.calendar_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
        )
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    except InvalidFollowUpTypeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid follow-up type."
        ) from None
    except FollowUpAppointmentRequiresCalendarEventError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    except FollowUpInvalidRelationshipError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    except CalendarNotFoundError:
        raise not_found("calendar") from None
    except ContactNotFoundError:
        raise not_found("contact") from None
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    except CalendarEventConflictError:
        raise conflict("Requested interval conflicts with an existing appointment.") from None
    return FollowUpOut.from_model(row)
